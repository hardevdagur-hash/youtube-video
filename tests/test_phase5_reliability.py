"""Reliability of the channel job pipeline under provider failures (no network)."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from config.settings import settings
from models.transcript_job import JobStatus
from services.jobs.transcript_job_manager import TranscriptJobManager

VIDEOS = ["vid00000001", "vid00000002", "vid00000003"]


def _ok(text: str):
    return SimpleNamespace(
        success=True, plain_text=text, paragraph_text=text, raw_transcript=text, language="en",
        source_language="English", source_language_code="en", error_code=None, error=None,
    )


def _fail(code: str):
    return SimpleNamespace(
        success=False, plain_text="", paragraph_text="", raw_transcript="", language="en",
        source_language=None, source_language_code=None, error_code=code, error="internal detail must not leak",
    )


class FakeYouTube:
    """Channel + video services: three eligible 5-minute videos."""

    fail_resolve = False

    def resolve_handle(self, handle):
        if FakeYouTube.fail_resolve:
            raise RuntimeError("YouTube API HTTP 400: API key not valid (key=AIzaSECRET)")
        return {"id": "UC1", "snippet": {"title": "Chan"}}

    def get_uploads_playlist_id(self, channel_id):
        return "UU1"

    def get_playlist_items(self, playlist_id, page_token=None):
        return {"video_ids": VIDEOS, "next_page_token": None}

    def get_videos_batch(self, ids):
        return [
            {"id": vid, "snippet": {"title": f"T {vid}", "publishedAt": "2026-01-01T00:00:00Z",
                                    "liveBroadcastContent": "none"},
             "contentDetails": {"duration": "PT5M"}}
            for vid in ids
        ]


@pytest.fixture
def env(monkeypatch, tmp_path):
    import api.channel_service as channel_module
    import api.video_service as video_module
    import services.transcript_service as ts_module
    from services.transcript_limiter import transcript_limiter

    FakeYouTube.fail_resolve = False
    behaviour: dict[str, object] = {}

    class FakeTranscripts:
        def get_transcript(self, video_id, **kwargs):
            outcome = behaviour.get(video_id, "ok")
            if callable(outcome):
                return outcome(video_id, **kwargs)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "ok":
                return _ok(f"transcript of {video_id}")
            return _fail(outcome)

    async def no_wait(video_id):
        return None

    monkeypatch.setattr(channel_module, "ChannelService", FakeYouTube)
    monkeypatch.setattr(video_module, "VideoService", FakeYouTube)
    monkeypatch.setattr(ts_module, "TranscriptService", FakeTranscripts)
    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    monkeypatch.setattr(transcript_limiter, "record_rate_limit", lambda video_id: 0.01)
    monkeypatch.setattr(settings, "whisper_enabled", False)
    return SimpleNamespace(manager=TranscriptJobManager(jobs_dir=tmp_path), behaviour=behaviour, dir=tmp_path)


async def _finish(manager, job_id, timeout=5.0):
    task = manager._tasks.get(job_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    await asyncio.sleep(0)
    return manager.get_job(job_id)


async def test_youtube_failure_during_discovery_fails_job_safely(env):
    FakeYouTube.fail_resolve = True
    job = await env.manager.start_channel_job("chan", owner="u")
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.FAILED
    persisted = (env.dir / f"{job.job_id}.json").read_text(encoding="utf-8")
    assert "AIzaSECRET" not in persisted and "API key not valid" not in persisted
    assert "Channel discovery failed" in job.error


async def test_partial_channel_failure_completes_job(env):
    env.behaviour.update({VIDEOS[1]: "NO_CAPTIONS", VIDEOS[2]: TimeoutError("read timed out talking to 10.0.0.5")})
    job = await env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED
    assert (job.successful, job.no_captions, job.failed) == (1, 1, 1)
    statuses = {v.video_id: v.status for v in job.videos}
    assert statuses == {VIDEOS[0]: "success", VIDEOS[1]: "no_captions", VIDEOS[2]: "failed"}
    failed = job.videos[2]
    assert failed.error_code == "UNEXPECTED_ERROR"
    assert "10.0.0.5" not in (failed.error_message or "")
    csv_text = env.manager.generate_csv(job.job_id)
    assert csv_text.count("\n") == 4  # header + every video, failures included


async def test_persistent_rate_limiting_pauses_then_resume_completes(env):
    for vid in VIDEOS:
        env.behaviour[vid] = "RATE_LIMITED"
    job = await env.manager.start_channel_job("chan", owner="u")
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.PAUSED
    assert "rate limit" in job.error.lower()
    assert json.loads((env.dir / f"{job.job_id}.json").read_text(encoding="utf-8"))["status"] == "paused"

    env.behaviour.clear()  # YouTube stops rate limiting
    resumed = await env.manager.resume_job(job.job_id)
    assert resumed.status == JobStatus.RUNNING
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.successful == 3


async def test_cancel_during_processing_stops_the_job(env):
    started, release = threading.Event(), threading.Event()

    def slow(video_id, **kwargs):
        started.set()
        release.wait(5)
        return _ok("late")

    env.behaviour[VIDEOS[1]] = slow
    job = await env.manager.start_channel_job("chan", owner="u")
    while not started.is_set():
        await asyncio.sleep(0.01)
    assert await env.manager.cancel_job(job.job_id) is True
    release.set()
    await asyncio.sleep(0.05)
    job = env.manager.get_job(job.job_id)
    assert job.status == JobStatus.CANCELLED
    assert job.videos[2].status == "pending"  # never started
    persisted = json.loads((env.dir / f"{job.job_id}.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "cancelled"


async def test_translation_outage_keeps_original_transcripts(env, monkeypatch):
    import services.translation.service as translation_module

    class Down:
        def translate(self, **kwargs):
            raise translation_module.TranslationError("Groq 503 upstream connect error")

    monkeypatch.setattr(translation_module, "TranslationService", lambda *a, **k: Down())
    job = await env.manager.start_channel_job("chan", owner="u", output_language="en")
    job = await _finish(env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED
    for item in job.videos:
        assert item.status == "success"
        assert item.transcript == item.raw_transcript  # honest fallback, never empty
        assert item.simple_english_transcript == ""
