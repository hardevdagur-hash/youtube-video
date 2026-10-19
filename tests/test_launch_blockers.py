# ruff: noqa: ARG001  (fixture-activation args)
"""Regression tests for the production-readiness launch blockers.

1. Single-video CSV export survives a transcript fetch that raises.
2. Atomic writes use a unique, fsynced temp file (no corruption under concurrency).
3. Job checkpoint/CSV I/O runs off the event loop; cooldown checkpoints are throttled.
4. Job polling has a lightweight progress view.
5. Per-user in-flight transcript cap; force_refresh is admin-only.
6. Container hardening (root-owned code, read-only rootfs, nginx capabilities).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import services.jobs.transcript_job_manager as jm
import webapp.main as web
from config.settings import settings
from models.transcript import TranscriptResult, TranscriptSource
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from repositories.transcript_repository import TranscriptRepository
from services.jobs.transcript_job_manager import PROGRESS_PREVIEW_CHARS, TranscriptJobManager
from tests.conftest import TEST_ADMIN_KEY, TEST_OTHER_USER_KEY, TEST_USER_KEY
from utils.atomic_file import atomic_write_text

ROOT = Path(__file__).resolve().parent.parent
USER_SUBJECT = "key:user-key"


def _job(job_id: str, owner: str | None = USER_SUBJECT, videos=None, status=JobStatus.COMPLETED):
    videos = videos or []
    return TranscriptJobProgress(
        job_id=job_id, channel_handle="@chan", channel_id="UC1", channel_title="Chan", status=status,
        eligible_videos=len(videos), owner=owner, output_language="original", videos=videos,
    )


def _item(video_id: str, text: str) -> TranscriptVideoItem:
    return TranscriptVideoItem(
        video_id=video_id, video_url=f"https://www.youtube.com/watch?v={video_id}", status="success",
        transcript=text, raw_transcript=text, simple_english_transcript=text, method="caption",
    )


@pytest.fixture
def stored_manager(tmp_path, monkeypatch):
    manager = TranscriptJobManager(jobs_dir=tmp_path / "jobs")
    monkeypatch.setattr(jm, "transcript_job_manager", manager)
    return manager


# ---------------------------------------------------------------------------
# 1. Single-video export when the transcript fetch raises
# ---------------------------------------------------------------------------


def test_single_video_export_survives_transcript_fetch_exception(authed_client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(web, "_get_video_service", lambda: SimpleNamespace(get_videos_batch=lambda ids: []))
    monkeypatch.setattr(web, "_get_transcript_service", lambda: SimpleNamespace(get_transcript=None))
    monkeypatch.setattr(web, "_transcript_work", boom)

    resp = authed_client.post(
        "/api/transcript/export", json={"video_url": "https://youtu.be/dQw4w9WgXcQ", "include_audit_columns": True},
    )

    assert resp.status_code == 200, resp.text  # previously UnboundLocalError -> 500
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    row = rows[0]
    assert row["video_id"] == "dQw4w9WgXcQ"
    assert row["status"] == "failed"
    assert row["error_code"] == "EXTRACTION_ERROR"
    assert row["transcript"] == ""
    assert row["source_language_code"] == "en"
    assert "provider exploded" not in resp.text


# ---------------------------------------------------------------------------
# 2. Atomic writes
# ---------------------------------------------------------------------------


def test_atomic_write_is_safe_under_concurrent_writers(tmp_path):
    target = tmp_path / "VIDEO000001.json"
    payloads = [json.dumps({"writer": n, "body": "x" * 20_000}) for n in range(8)]
    errors: list[BaseException] = []

    def writer(payload: str) -> None:
        try:
            for _ in range(25):
                atomic_write_text(target, payload)
        except BaseException as exc:  # pragma: no cover - the failure being tested for
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if os.name == "nt":
        # Windows may refuse a rename while another thread has the target open; that is
        # reported as an error, never as a corrupt file, and never leaves temp files.
        errors = [e for e in errors if not isinstance(e, PermissionError)]
    assert errors == []
    assert target.read_text(encoding="utf-8") in payloads  # one complete payload, never a mix
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_fsyncs_and_cleans_up_on_failure(tmp_path, monkeypatch):
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    target = tmp_path / "a.json"
    atomic_write_text(target, "one")
    assert target.read_text(encoding="utf-8") == "one" and synced

    def failing_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "one"
    assert list(tmp_path.glob("*.tmp")) == []


def test_repository_concurrent_saves_never_fail_or_leave_temp_files(tmp_path, caplog):
    repo = TranscriptRepository(persist_dir=str(tmp_path))

    def save(n: int) -> None:
        for _ in range(10):
            repo.save(TranscriptResult(
                success=True, video_id="dQw4w9WgXcQ", source=TranscriptSource.MANUAL, plain_text=f"text {n}",
            ))

    threads = [threading.Thread(target=save, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((tmp_path / "dQw4w9WgXcQ.json").read_text(encoding="utf-8"))
    assert data["video_id"] == "dQw4w9WgXcQ"
    assert list(tmp_path.glob("*.tmp")) == []
    if os.name != "nt":  # see test_atomic_write_is_safe_under_concurrent_writers
        assert "Failed to persist transcript" not in caplog.text


def test_job_checkpoint_is_fsynced(tmp_path, monkeypatch):
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_job("0123456789ab"))
    assert synced
    assert json.loads((tmp_path / "0123456789ab.json").read_text(encoding="utf-8"))["job_id"] == "0123456789ab"
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 3. Blocking I/O off the event loop; throttled cooldown checkpoints
# ---------------------------------------------------------------------------


async def test_job_disk_io_runs_off_the_event_loop(tmp_path, monkeypatch):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_job("0123456789ab", videos=[_item("dQw4w9WgXcQ", "hello")]))
    loop_thread = threading.current_thread()
    threads: dict[str, threading.Thread] = {}

    def spy(name, func):
        def wrapper(*args, **kwargs):
            threads[name] = threading.current_thread()
            return func(*args, **kwargs)
        monkeypatch.setattr(manager, name, wrapper)

    spy("_load_checkpoint", manager._load_checkpoint)
    spy("_write_checkpoint", manager._write_checkpoint)
    spy("_build_csv", manager._build_csv)

    job = await manager.aget_job("0123456789ab")
    assert job is not None and job.videos[0].transcript == "hello"
    await manager._save_checkpoint_async(job)
    csv_text = await manager.agenerate_csv("0123456789ab")
    assert "hello" in csv_text

    assert set(threads) == {"_load_checkpoint", "_write_checkpoint", "_build_csv"}
    assert all(t is not loop_thread for t in threads.values())


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


async def test_cooldown_checkpoints_are_throttled(tmp_path, monkeypatch):
    import api.channel_service as channel_module
    import api.video_service as video_module
    import services.transcript_service as ts_module
    from services.transcript_limiter import transcript_limiter

    video_id = "vid00000001"

    class FakeYouTube:
        def resolve_handle(self, handle):
            return {"id": "UC1", "snippet": {"title": "Chan"}}

        def get_uploads_playlist_id(self, channel_id):
            return "UU1"

        def get_playlist_items(self, playlist_id, page_token=None):
            return {"video_ids": [video_id], "next_page_token": None}

        def get_videos_batch(self, ids):
            return [{"id": v, "snippet": {"title": "T", "publishedAt": "2026-01-01T00:00:00Z",
                                          "liveBroadcastContent": "none"},
                     "contentDetails": {"duration": "PT5M"}} for v in ids]

    calls = {"n": 0}

    class FakeTranscripts:
        def get_transcript(self, video_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return SimpleNamespace(success=False, plain_text="", paragraph_text="", raw_transcript="",
                                       error_code="RATE_LIMITED", error="slow down")
            return SimpleNamespace(success=True, plain_text="ok", paragraph_text="ok", raw_transcript="ok",
                                   language="en", source_language="English", source_language_code="en")

    async def no_wait(video_id):
        return None

    clock = _FakeClock()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr(channel_module, "ChannelService", FakeYouTube)
    monkeypatch.setattr(video_module, "VideoService", FakeYouTube)
    monkeypatch.setattr(ts_module, "TranscriptService", FakeTranscripts)
    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    monkeypatch.setattr(transcript_limiter, "is_in_cooldown", lambda: False)
    monkeypatch.setattr(transcript_limiter, "record_rate_limit", lambda vid: 60.0)
    monkeypatch.setattr(settings, "whisper_enabled", False)
    monkeypatch.setattr(jm, "time", SimpleNamespace(time=clock.time, monotonic=clock.monotonic))
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    cooldown_saves: list[float] = []
    real_write = manager._write_checkpoint

    def counting_write(job, target):
        if job.status == JobStatus.COOLDOWN:
            cooldown_saves.append(job.cooldown_seconds_remaining)
        return real_write(job, target)

    monkeypatch.setattr(manager, "_write_checkpoint", counting_write)

    job = await manager.start_channel_job("chan", owner="u", output_language="original")
    await asyncio.wait_for(asyncio.shield(manager._tasks[job.job_id]), 10)

    assert manager.get_job(job.job_id).status == JobStatus.COMPLETED
    # A 60s cooldown ticks every 2s (30 ticks); checkpoints are capped at one per 15s.
    assert 1 <= len(cooldown_saves) <= 5, cooldown_saves


# ---------------------------------------------------------------------------
# 4. Lightweight job progress view
# ---------------------------------------------------------------------------


def test_job_progress_view_omits_transcript_bodies(authed_client, stored_manager):
    long_text = "word " * 2000
    stored_manager._save_checkpoint(_job("abcdef012345", videos=[_item("dQw4w9WgXcQ", long_text)]))

    progress = authed_client.get("/api/transcript/jobs/abcdef012345?view=progress")
    assert progress.status_code == 200
    data = progress.json()["data"]
    video = data["videos"][0]
    assert data["status"] == "completed"
    for field in ("raw_transcript", "simple_english_transcript", "simple_hindi_transcript", "transcript_timestamped"):
        assert field not in video
    assert len(video["transcript"]) == PROGRESS_PREVIEW_CHARS
    assert video["transcript_truncated"] is True
    assert video["status"] == "success" and video["method"] == "caption"
    assert len(progress.content) < len(long_text)

    full = authed_client.get("/api/transcript/jobs/abcdef012345").json()["data"]["videos"][0]
    assert full["transcript"] == long_text and full["raw_transcript"] == long_text

    download = authed_client.get("/api/transcript/jobs/abcdef012345/download")
    assert download.status_code == 200
    assert long_text.strip() in download.content.decode("utf-8-sig")


def test_job_progress_view_keeps_ownership_and_validation(auth_config, stored_manager):
    stored_manager._save_checkpoint(_job("abcdef012345"))
    other = TestClient(web.app, headers={"X-API-Key": TEST_OTHER_USER_KEY})
    assert other.get("/api/transcript/jobs/abcdef012345?view=progress").status_code == 404
    owner = TestClient(web.app, headers={"X-API-Key": TEST_USER_KEY})
    assert owner.get("/api/transcript/jobs/abcdef012345?view=everything").status_code == 422


# ---------------------------------------------------------------------------
# 5. Per-user in-flight cap and admin-only force_refresh
# ---------------------------------------------------------------------------


def _no_transcript_services(monkeypatch):
    def unavailable():
        raise RuntimeError("no providers in tests")

    monkeypatch.setattr(web, "_get_unified_services", unavailable)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("post", "/api/transcript", {"video_url": "https://youtu.be/dQw4w9WgXcQ"}),
    ("get", "/api/channel/somechannel/transcripts", None),
    ("post", "/api/transcript/export", {"video_url": "https://youtu.be/dQw4w9WgXcQ"}),
    ("post", "/api/transcript/export", {"channel_handle": "chan"}),
])
def test_user_inflight_cap_rejects_with_429(authed_client, monkeypatch, method, path, body):
    monkeypatch.setattr(web, "_inflight_by_user", {USER_SUBJECT: settings.max_inflight_transcripts_per_user})
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(authed_client, method)(path, **kwargs)
    assert resp.status_code == 429, resp.text
    assert resp.json()["error_code"] == "USER_BUSY"
    assert web._inflight_by_user == {USER_SUBJECT: settings.max_inflight_transcripts_per_user}


def test_user_inflight_cap_is_per_user_and_released(auth_config, monkeypatch):
    _no_transcript_services(monkeypatch)
    inflight = {USER_SUBJECT: settings.max_inflight_transcripts_per_user}
    monkeypatch.setattr(web, "_inflight_by_user", inflight)
    other = TestClient(web.app, raise_server_exceptions=False, headers={"X-API-Key": TEST_OTHER_USER_KEY})

    resp = other.post("/api/transcript", json={"video_url": "https://youtu.be/dQw4w9WgXcQ"})
    assert resp.json().get("error_code") != "USER_BUSY"  # another user is not affected
    assert "key:other-key" not in inflight  # its slot was released after the request


async def test_user_inflight_cap_counts_concurrent_requests(monkeypatch):
    monkeypatch.setattr(web, "_inflight_by_user", {})
    monkeypatch.setattr(settings, "max_inflight_transcripts_per_user", 2)
    release = asyncio.Event()

    @web._limit_user_inflight("http_request")
    async def slow(http_request):
        await release.wait()
        return "done"

    req = SimpleNamespace(state=SimpleNamespace(principal=SimpleNamespace(subject="alice")))
    first = asyncio.create_task(slow(http_request=req))
    second = asyncio.create_task(slow(http_request=req))
    await asyncio.sleep(0)
    third = await slow(http_request=req)
    assert third.status_code == 429  # third concurrent request of the same user
    release.set()
    assert await first == "done" and await second == "done"
    assert web._inflight_by_user == {}


def test_force_refresh_is_admin_only(auth_config, stored_manager, monkeypatch):
    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(stored_manager, "_discover_and_run", never_finishes)
    user = TestClient(web.app, headers={"X-API-Key": TEST_USER_KEY})
    resp = user.post("/api/channel/somechannel/transcript-job", json={"force_refresh": True})
    assert resp.status_code == 403
    resp = user.post("/api/channel/somechannel/transcript-job?force_refresh=true")
    assert resp.status_code == 403
    with TestClient(web.app, headers={"X-API-Key": TEST_ADMIN_KEY}) as admin:
        resp = admin.post("/api/channel/somechannel/transcript-job", json={"force_refresh": True})
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 6. Container hardening
# ---------------------------------------------------------------------------


def test_dockerfile_copies_code_owned_by_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert copies and not any("--chown" in line for line in copies)
    assert "chown app:app /app/data" in dockerfile
    assert "USER app" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile and "XDG_CACHE_HOME=/tmp/" in dockerfile


def test_compose_read_only_app_and_nginx_capabilities():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    app = compose["services"]["app"]
    assert app["read_only"] is True
    assert any(entry.startswith("/tmp:") for entry in app["tmpfs"])  # noqa: S108  (container path, not a temp file)
    assert "transcript_data:/app/data" in app["volumes"]
    assert app["cap_drop"] == ["ALL"]
    # Everything the app writes is under DATA_DIR (the volume) or /tmp (tmpfs).
    assert app["environment"]["DATA_DIR"] == "/app/data"
    assert app["environment"]["LOG_TO_FILE"] == "false"

    nginx = compose["services"]["nginx"]
    assert nginx["cap_drop"] == ["ALL"]
    assert set(nginx["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID", "NET_BIND_SERVICE"}
