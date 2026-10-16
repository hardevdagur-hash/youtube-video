"""Job checkpoints stay small (bodies in per-video files), and waiting for speech-to-text
never parks transcript worker threads."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from config.settings import settings
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager
from services.transcription.stt_gate import STTBusyError, STTGate
from tests import test_phase5_reliability as phase5
from tests.test_phase5_reliability import VIDEOS, _finish, _ok

env = phase5.env  # the channel-job fixture (fake YouTube and transcript services)

BIG = "word " * 20_000  # ~100 KB of transcript per video


def _checkpoint(env_) -> dict:
    (path,) = env_.dir.glob("*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _bodies_dir(env_, job_id):
    return env_.dir / f"{job_id}.bodies"


# ---------------------------------------------------------------------------
# Per-video transcript storage
# ---------------------------------------------------------------------------


async def test_checkpoint_holds_metadata_only_and_bodies_reload(env):
    for vid in VIDEOS:
        env.behaviour[vid] = lambda video_id, **kw: _ok(f"{BIG}{video_id}")
    job = await env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED and job.successful == 3

    checkpoint_path = env.dir / f"{job.job_id}.json"
    assert checkpoint_path.stat().st_size < 10_000  # 300 KB of transcripts live elsewhere
    for video in _checkpoint(env)["videos"]:
        assert "transcript" not in video and "raw_transcript" not in video
    assert sorted(p.name for p in _bodies_dir(env, job.job_id).iterdir()) == sorted(f"{v}.json" for v in VIDEOS)

    reloaded = TranscriptJobManager(jobs_dir=env.dir).get_job(job.job_id)
    for item in reloaded.videos:
        assert item.transcript == item.raw_transcript == f"{BIG}{item.video_id}"
    csv_text = TranscriptJobManager(jobs_dir=env.dir).generate_csv(job.job_id)
    assert all(f"{BIG}{vid}".strip() in csv_text for vid in VIDEOS)


async def test_each_body_is_written_once_however_many_checkpoints(env, monkeypatch):
    import services.jobs.transcript_job_manager as jm

    real_write = jm.atomic_write_text
    body_writes: list[str] = []

    def counting_write(path, text, *args, **kwargs):
        if path.parent.name.endswith(".bodies"):
            body_writes.append(path.name)
        return real_write(path, text, *args, **kwargs)

    monkeypatch.setattr(jm, "atomic_write_text", counting_write)
    job = await env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(env.manager, job.job_id)
    assert job.successful == 3
    assert sorted(body_writes) == sorted(f"{v}.json" for v in VIDEOS)  # not once per checkpoint

    env.manager._save_checkpoint(job)  # nothing changed: no body is rewritten
    assert len(body_writes) == 3
    job.videos[0].transcript = "edited"
    env.manager._save_checkpoint(job)
    assert body_writes[-1] == f"{VIDEOS[0]}.json" and len(body_writes) == 4


def _legacy_job(job_id: str, status=JobStatus.COMPLETED) -> TranscriptJobProgress:
    return TranscriptJobProgress(
        job_id=job_id, channel_handle="chan", channel_id="UC1", channel_title="Chan", status=status,
        eligible_videos=1,
        videos=[TranscriptVideoItem(video_id=VIDEOS[0], video_url="u", status="success",
                                    transcript="inline text", raw_transcript="inline raw")],
    )


def test_legacy_inline_checkpoint_is_read_and_migrated(tmp_path):
    job = _legacy_job("0123456789ab")
    (tmp_path / "0123456789ab.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    loaded = manager.get_job("0123456789ab")
    assert loaded.videos[0].transcript == "inline text"

    manager._save_checkpoint(loaded)
    on_disk = json.loads((tmp_path / "0123456789ab.json").read_text(encoding="utf-8"))
    assert "transcript" not in on_disk["videos"][0]
    body = json.loads((tmp_path / "0123456789ab.bodies" / f"{VIDEOS[0]}.json").read_text(encoding="utf-8"))
    assert body == {"transcript": "inline text", "raw_transcript": "inline raw"}
    again = TranscriptJobManager(jobs_dir=tmp_path).get_job("0123456789ab")
    assert (again.videos[0].transcript, again.videos[0].raw_transcript) == ("inline text", "inline raw")


def test_startup_recovery_keeps_bodies(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_legacy_job("0123456789ab", status=JobStatus.RUNNING))

    fresh = TranscriptJobManager(jobs_dir=tmp_path)
    assert fresh.recover_interrupted_jobs() == 1  # reads and rewrites the checkpoint without bodies
    job = TranscriptJobManager(jobs_dir=tmp_path).get_job("0123456789ab")
    assert job.status == JobStatus.PAUSED
    assert job.videos[0].transcript == "inline text"


def test_legacy_running_job_is_migrated_by_recovery(tmp_path):
    (tmp_path / "0123456789ab.json").write_text(
        _legacy_job("0123456789ab", status=JobStatus.RUNNING).model_dump_json(), encoding="utf-8",
    )
    TranscriptJobManager(jobs_dir=tmp_path).recover_interrupted_jobs()
    on_disk = json.loads((tmp_path / "0123456789ab.json").read_text(encoding="utf-8"))
    assert "transcript" not in on_disk["videos"][0]
    assert TranscriptJobManager(jobs_dir=tmp_path).get_job("0123456789ab").videos[0].transcript == "inline text"


def test_retention_removes_bodies_with_the_job_and_orphans(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(settings, "job_retention_days", 30)
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job = _legacy_job("f00000000001")
    manager._save_checkpoint(job)
    path = tmp_path / "f00000000001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    old = time.time() - 40 * 86400
    os.utime(path, (old, old))

    orphan = tmp_path / "f00000000002.bodies"
    orphan.mkdir()
    (orphan / f"{VIDEOS[0]}.json").write_text("{}", encoding="utf-8")
    os.utime(orphan, (old, old))
    kept = tmp_path / "f00000000003.bodies"  # recent: its checkpoint may be about to land
    kept.mkdir()

    assert manager.cleanup_expired_jobs() == ["f00000000001"]
    assert not (tmp_path / "f00000000001.bodies").exists()
    assert not orphan.exists()
    assert kept.exists()


def test_unusual_video_ids_stay_inside_the_bodies_dir(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job = _legacy_job("0123456789ab")
    job.videos[0].video_id = "../../escaped"
    manager._save_checkpoint(job)
    files = list((tmp_path / "0123456789ab.bodies").iterdir())
    assert len(files) == 1 and files[0].name.startswith("x")
    assert not (tmp_path.parent / "escaped.json").exists()
    assert TranscriptJobManager(jobs_dir=tmp_path).get_job("0123456789ab").videos[0].transcript == "inline text"


# ---------------------------------------------------------------------------
# Waiting for a speech-to-text slot
# ---------------------------------------------------------------------------


async def test_reserve_waits_on_the_loop_not_in_worker_threads(monkeypatch):
    import infrastructure.work_pool as work_pool

    gate = STTGate(max_concurrency=1, queue_timeout_seconds=10)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcript-work")
    monkeypatch.setattr(work_pool, "_executor", pool)
    release = threading.Event()
    held = threading.Event()
    ran: list[str] = []

    def hold_slot():
        with gate.slot("holder"):
            held.set()
            release.wait(10)

    def stt_work(video_id):
        with gate.slot(video_id):  # reuses the reservation: never a second slot
            ran.append(video_id)
            return gate.status()["active"]

    async def reserved(video_id):
        async with gate.reserve(video_id):
            return await work_pool.run_transcript_work(stt_work, video_id)

    try:
        holder = asyncio.ensure_future(work_pool.run_transcript_work(hold_slot))
        await asyncio.to_thread(held.wait, 5)
        waiters = [asyncio.ensure_future(reserved(f"v{i}")) for i in range(6)]
        await asyncio.sleep(0.1)
        assert gate.status()["waiting"] == 6
        # Six requests are queued for speech-to-text, yet the second pool thread is free.
        assert await asyncio.wait_for(work_pool.run_transcript_work(lambda: "captions"), 2) == "captions"
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*waiters), 10) == [1] * 6
        await holder
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert sorted(ran) == [f"v{i}" for i in range(6)]
    assert gate.status() == {"limit": 1, "active": 0, "waiting": 0}


async def test_reserve_times_out_with_stt_busy():
    gate = STTGate(max_concurrency=1, queue_timeout_seconds=0.1)
    with gate.slot("a"), pytest.raises(STTBusyError):
        async with gate.reserve("b"):
            pass
    assert gate.status() == {"limit": 1, "active": 0, "waiting": 0}
    async with gate.reserve("c"):  # nothing leaked
        assert gate.status()["active"] == 1


def test_thread_waiters_are_capped_and_extra_requests_fail_fast():
    gate = STTGate(max_concurrency=1, queue_timeout_seconds=5, max_thread_waiters=1)
    release = threading.Event()
    waiting = threading.Event()

    def wait_for_slot():
        waiting.set()
        with gate.slot("queued"):
            pass

    with gate.slot("holder"):
        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        waiting.wait(2)
        deadline = time.monotonic() + 2
        while gate.status()["waiting"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        start = time.monotonic()
        with pytest.raises(STTBusyError), gate.slot("overflow"):
            pass
        assert time.monotonic() - start < 1  # did not wait out the 5 s queue timeout
        release.set()
    waiter.join(5)
    assert gate.status() == {"limit": 1, "active": 0, "waiting": 0}


async def test_job_waits_for_stt_without_calling_the_pipeline(env, monkeypatch):
    """A busy STT slot leaves the item resumable, and no worker thread was spent waiting."""
    import services.transcription.stt_gate as gate_module

    gate = STTGate(max_concurrency=1, queue_timeout_seconds=0.1)
    monkeypatch.setattr(gate_module, "stt_gate", gate)
    monkeypatch.setattr(settings, "whisper_enabled", True)
    stt_calls: list[str] = []

    def captions_missing(video_id, allow_whisper=True, **kwargs):
        if allow_whisper:
            stt_calls.append(video_id)
            return _ok("spoken")
        return SimpleNamespace(success=False, plain_text="", paragraph_text="", raw_transcript="",
                               language="en", source_language=None, source_language_code=None,
                               error_code="NO_CAPTIONS", error="x")

    env.behaviour[VIDEOS[0]] = captions_missing
    with gate.slot("someone-else"):  # another request is transcribing
        job = await env.manager.start_channel_job("chan", owner="u", output_language="original")
        job = await _finish(env.manager, job.job_id)
    item = job.videos[0]
    assert (item.status, item.error_code, item.retryable) == ("temporary_error", "STT_BUSY", True)
    assert stt_calls == []  # never reached a worker thread

    await env.manager.resume_job(job.job_id)  # slot free now: the resumed item succeeds
    job = await _finish(env.manager, job.job_id)
    assert job.videos[0].status == "success" and stt_calls == [VIDEOS[0]]
