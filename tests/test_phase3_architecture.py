"""Single-instance architecture: job lifecycle, retention, memory bounds, STT backend, translation."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from config.settings import ConfigurationError, Settings, settings
from models.transcript import TranscriptResult, TranscriptSource
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.jobs.transcript_job_manager import TranscriptJobManager
from utils.cache import TTLCache


def _job(job_id: str, status: JobStatus, videos: list[TranscriptVideoItem] | None = None, **kw) -> TranscriptJobProgress:
    return TranscriptJobProgress(
        job_id=job_id, channel_handle="@chan", channel_id="UC1", channel_title="Chan", status=status,
        total_discovered=len(videos or []), eligible_videos=len(videos or []), videos=videos or [], **kw,
    )


def _item(video_id: str, status: str) -> TranscriptVideoItem:
    return TranscriptVideoItem(video_id=video_id, video_url=f"https://www.youtube.com/watch?v={video_id}", status=status)


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def test_constructor_does_not_load_jobs_into_memory(tmp_path):
    first = TranscriptJobManager(jobs_dir=tmp_path)
    first._save_checkpoint(_job("aaaaaaaaaaaa", JobStatus.COMPLETED))
    second = TranscriptJobManager(jobs_dir=tmp_path)
    assert second._jobs == {} and len(second._recent) == 0
    assert second.get_job("aaaaaaaaaaaa").status == JobStatus.COMPLETED  # read on demand


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COOLDOWN])
def test_crash_interrupted_jobs_recovered_as_paused(tmp_path, status):
    crashed = TranscriptJobManager(jobs_dir=tmp_path)
    crashed._save_checkpoint(_job("bbbbbbbbbbbb", status, [_item("v0000000001", "success"), _item("v0000000002", "processing")]))

    restarted = TranscriptJobManager(jobs_dir=tmp_path)
    assert restarted.recover_interrupted_jobs() == 1
    job = restarted.get_job("bbbbbbbbbbbb")
    assert job.status == JobStatus.PAUSED
    assert "Resume" in job.error
    assert [v.status for v in job.videos] == ["success", "pending"]


def test_recovery_leaves_finished_and_paused_jobs_alone(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    for job_id, status in (("cccccccccc01", JobStatus.COMPLETED), ("cccccccccc02", JobStatus.PAUSED),
                           ("cccccccccc03", JobStatus.CANCELLED), ("cccccccccc04", JobStatus.FAILED)):
        manager._save_checkpoint(_job(job_id, status))
    assert TranscriptJobManager(jobs_dir=tmp_path).recover_interrupted_jobs() == 0


async def test_resume_after_crash_retries_interrupted_item(tmp_path, monkeypatch):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_job("dddddddddddd", JobStatus.RUNNING, [_item("v0000000001", "success"), _item("v0000000002", "processing")]))
    manager.recover_interrupted_jobs()

    ran = asyncio.Event()

    async def fake_run(job, **kwargs):
        assert [v.status for v in job.videos] == ["success", "pending"]
        ran.set()

    monkeypatch.setattr(manager, "_run_job", fake_run)
    job = await manager.resume_job("dddddddddddd")
    assert job.status == JobStatus.RUNNING
    await asyncio.wait_for(ran.wait(), 2)


async def test_resume_reruns_interrupted_discovery(tmp_path, monkeypatch):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_job("eeeeeeeeeeee", JobStatus.PAUSED, max_videos=7, output_language="original"))
    calls = []

    async def fake_discover(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(manager, "_discover_and_run", fake_discover)
    job = await manager.resume_job("eeeeeeeeeeee")
    await asyncio.sleep(0)
    assert job.status == JobStatus.QUEUED  # not wrongly marked completed
    assert calls and calls[0]["max_videos"] == 7 and calls[0]["output_language"] == "original"


# ---------------------------------------------------------------------------
# Memory release
# ---------------------------------------------------------------------------


async def test_finished_jobs_leave_the_active_set(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "job_memory_cache_size", 2)
    manager = TranscriptJobManager(jobs_dir=tmp_path)

    async def instant(progress, **kwargs):
        progress.status = JobStatus.COMPLETED
        manager._save_checkpoint(progress)

    monkeypatch.setattr(manager, "_discover_and_run", instant)
    ids = []
    for _ in range(4):
        job = await manager.start_channel_job("chan", owner=f"user{_}")
        ids.append(job.job_id)
        await asyncio.sleep(0.01)
    assert manager._jobs == {} and manager._tasks == {}
    assert list(manager._recent) == ids[-2:]  # bounded LRU
    assert manager.get_job(ids[0]).status == JobStatus.COMPLETED  # still served from disk


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _age(path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_retention_deletes_only_old_non_running_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "job_retention_days", 30)
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    old_ts = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    for job_id, status in (("f00000000001", JobStatus.COMPLETED), ("f00000000002", JobStatus.RUNNING),
                           ("f00000000003", JobStatus.PAUSED), ("f00000000004", JobStatus.COMPLETED)):
        manager._save_checkpoint(_job(job_id, status))
    for job_id in ("f00000000001", "f00000000002", "f00000000003"):
        path = tmp_path / f"{job_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated_at"] = old_ts
        path.write_text(json.dumps(data), encoding="utf-8")
        _age(path, 40)

    removed = manager.cleanup_expired_jobs()
    assert sorted(removed) == ["f00000000001", "f00000000003"]
    assert (tmp_path / "f00000000002.json").exists()  # never delete a running job
    assert (tmp_path / "f00000000004.json").exists()  # recent


def test_retention_skips_jobs_running_in_this_process(tmp_path):
    manager = TranscriptJobManager(jobs_dir=tmp_path)
    manager._save_checkpoint(_job("f00000000009", JobStatus.COMPLETED, updated_at="2000-01-01T00:00:00+00:00"))
    path = tmp_path / "f00000000009.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    _age(path, 9000)
    manager._jobs["f00000000009"] = _job("f00000000009", JobStatus.COMPLETED)  # pinned / active
    assert manager.cleanup_expired_jobs() == []


# ---------------------------------------------------------------------------
# Bounded caches
# ---------------------------------------------------------------------------


def test_ttl_cache_is_bounded_lru():
    cache = TTLCache[int](ttl_seconds=60, max_entries=3)
    for i in range(10):
        cache.set(f"k{i}", i)
    assert cache.size == 3
    assert cache.get("k0") is None and cache.get("k9") == 9


def test_ttl_cache_expiry():
    cache = TTLCache[int](ttl_seconds=0.01, max_entries=10)
    cache.set("a", 1)
    time.sleep(0.03)
    assert cache.get("a") is None


# ---------------------------------------------------------------------------
# Speech-to-text backend
# ---------------------------------------------------------------------------


def _service():
    from services.transcript_service import TranscriptService

    return TranscriptService(use_cache=False)


def test_groq_backend_used_by_default(monkeypatch):
    from clients.groq_stt_client import GroqSpeechToTextClient

    monkeypatch.setattr(settings, "stt_backend", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test_not_real")
    monkeypatch.setattr(settings, "whisper_enabled", True)
    provider = _service()._get_whisper_provider()
    assert isinstance(provider._stt, GroqSpeechToTextClient)


def test_stt_unavailable_without_key_is_reported_not_attempted(monkeypatch):
    from services.transcript_service import SpeechToTextUnavailableError

    monkeypatch.setattr(settings, "stt_backend", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "")
    with pytest.raises(SpeechToTextUnavailableError):
        _service()._get_whisper_provider()


def test_stt_disabled(monkeypatch):
    from services.transcript_service import SpeechToTextUnavailableError

    monkeypatch.setattr(settings, "whisper_enabled", False)
    with pytest.raises(SpeechToTextUnavailableError):
        _service()._get_whisper_provider()


def test_groq_adapter_maps_segments_and_text_only_responses(tmp_path):
    from clients.groq_stt_client import GroqSpeechToTextClient
    from services.transcription.provider import TranscriptionResult, TranscriptionSegment

    class FakeGroq:
        model = "whisper-large-v3"

        def __init__(self, result):
            self.result = result

        def transcribe(self, path, language=None):
            return self.result

    audio = tmp_path / "a.m4a"
    with_segments = GroqSpeechToTextClient(FakeGroq(TranscriptionResult(
        text="नमस्ते दोस्तों", language="hindi", duration=4.0,
        segments=[TranscriptionSegment(0.0, 2.0, "नमस्ते"), TranscriptionSegment(2.0, 4.0, "दोस्तों")],
    ))).transcribe(str(audio), language=None, task="transcribe", initial_prompt="ignored")
    assert [s.text for s in with_segments.segments] == ["नमस्ते", "दोस्तों"]
    assert with_segments.language == "hindi"

    text_only = GroqSpeechToTextClient(FakeGroq(TranscriptionResult(text="hello world", duration=3.0))).transcribe(str(audio))
    assert [(s.start, s.end, s.text) for s in text_only.segments] == [(0.0, 3.0, "hello world")]


# ---------------------------------------------------------------------------
# Translation of long transcripts
# ---------------------------------------------------------------------------


class _FakeCompletions:
    def __init__(self, finish_reason="stop"):
        self.calls = []
        self.finish_reason = finish_reason

    def create(self, messages, model, temperature, max_tokens):
        self.calls.append({"content": messages[1]["content"], "max_tokens": max_tokens})
        text = f"PART{len(self.calls)}"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=self.finish_reason)])


def _translator(tmp_path, completions):
    from repositories.transcript_repository import TranscriptRepository
    from services.translation.service import TranslationService

    svc = TranslationService(repository=TranscriptRepository(persist_dir=str(tmp_path)), api_key="gsk_test")
    svc._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return svc


def test_long_transcript_translated_in_chunks(tmp_path):
    completions = _FakeCompletions()
    text = " ".join(f"Sentence number {i} is here." for i in range(1000))  # 5,000 words
    result = _translator(tmp_path, completions).translate("dQw4w9WgXcQ", text, "en")
    assert len(completions.calls) == 5
    assert all(call["max_tokens"] <= 8000 for call in completions.calls)
    assert result["transcript"] == "\n\n".join(f"PART{i}" for i in range(1, 6))


def test_truncated_translation_is_rejected_and_not_cached(tmp_path):
    from services.translation.service import TranslationError

    svc = _translator(tmp_path, _FakeCompletions(finish_reason="length"))
    with pytest.raises(TranslationError):
        svc.translate("dQw4w9WgXcQ", "Some text to translate.", "hi")
    assert svc.repository.get_translation("dQw4w9WgXcQ", "hi:simple") is None


# ---------------------------------------------------------------------------
# Cache namespaces: the single-video route never serves channel-pipeline output
# ---------------------------------------------------------------------------


def test_single_video_cache_is_separate_from_channel_cache(tmp_path, monkeypatch):
    from repositories.transcript_repository import TranscriptRepository
    from services.transcription.service import TranscriptService as CanonicalService

    monkeypatch.setattr(settings, "transcript_cache_dir", tmp_path)
    # Channel pipeline cached a Simple-English-mode (transliterated) rendering of a Hindi video
    TranscriptRepository(persist_dir=str(tmp_path)).save(TranscriptResult(
        success=True, video_id="dQw4w9WgXcQ", source=TranscriptSource.MANUAL,
        plain_text="namaste doston", raw_transcript="नमस्ते दोस्तों", language="English (India)",
    ))
    canonical = CanonicalService()
    assert canonical.repository.get("dQw4w9WgXcQ") is None


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "value"), [
    ("MAX_VIDEOS_PER_JOB", "lots"),
    ("MAX_VIDEOS_PER_JOB", "0"),
    ("TRANSCRIPT_REQUEST_INTERVAL", "-1"),
    ("WHISPER_ENABLED", "maybe"),
    ("STT_BACKEND", "openai"),
    ("LOG_LEVEL", "verbose"),
])
def test_invalid_settings_fail_fast(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError, match=key):
        Settings()


def test_per_user_job_limit_cannot_exceed_server_limit(monkeypatch):
    monkeypatch.setenv("MAX_ACTIVE_JOBS", "2")
    monkeypatch.setenv("MAX_ACTIVE_JOBS_PER_USER", "3")
    with pytest.raises(ConfigurationError):
        Settings()


def test_data_dir_relocates_all_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "volume"))
    s = Settings()
    assert s.transcript_jobs_dir == tmp_path / "volume" / "transcript_jobs"
    assert s.transcript_cache_dir == tmp_path / "volume" / "transcripts"
    assert s.audio_temp_dir == tmp_path / "volume" / "tmp" / "audio"


def test_real_environment_wins_over_dotenv():
    # Deployment env (Docker env_file / environment) must never be overridden by a stray .env.
    import importlib
    import inspect

    assert "override=False" in inspect.getsource(importlib.import_module("config.settings"))
