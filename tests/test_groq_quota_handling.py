"""Groq quota exhaustion (429) and the job states around it.

Regression for: a channel job hit Groq's tokens-per-day limit, kept sending a translation
request every few seconds, and finished "completed" with 131 of 145 videos holding the
untranslated original text and no way to retry them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.translation.service as translation_module
from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem
from services.transcription import groq as groq_module
from services.transcription.groq import (
    GroqRateLimitError,
    GroqWhisperProvider,
    groq_cooldown,
    rate_limit_wait_seconds,
)
from services.translation.service import TranslationError, TranslationService

TPD_MESSAGE = (
    "Error code: 429 - Rate limit reached for model `openai/gpt-oss-120b` on tokens per day (TPD): "
    "Limit 200000, Used 199850, Requested 3000. Please try again in 11m31.2s."
)


class _RateLimited(Exception):
    status_code = 429

    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})


# ---------------------------------------------------------------------------
# Wait parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "expected"), [
    (_RateLimited("x", retry_after="7"), 7.0),
    (_RateLimited(TPD_MESSAGE), 11 * 60 + 31.2),
    (_RateLimited("Please try again in 1h2m3s."), 3723.0),
    (_RateLimited("Please try again in 450ms."), 0.45),
    (_RateLimited("Please try again in 2.5s."), 2.5),
    (_RateLimited("rate_limit_exceeded"), 60.0),
])
def test_rate_limit_wait_seconds(exc, expected):
    assert rate_limit_wait_seconds(exc) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Translation service
# ---------------------------------------------------------------------------


class _Completions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome), finish_reason="stop")])


def _translator(tmp_path, outcomes):
    from repositories.transcript_repository import TranscriptRepository

    svc = TranslationService(repository=TranscriptRepository(persist_dir=str(tmp_path)), api_key="gsk_test")
    completions = _Completions(outcomes)
    svc._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return svc, completions


def test_daily_quota_fails_fast_and_stops_further_requests(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(translation_module.time, "sleep", sleeps.append)
    svc, completions = _translator(tmp_path, [_RateLimited(TPD_MESSAGE)])

    with pytest.raises(TranslationError) as exc:
        svc.translate(video_id="aaaaaaaaaaa", original_text="hello there.", target_language="en")
    assert exc.value.error_code == "TRANSLATION_RATE_LIMITED"
    assert exc.value.retry_after == pytest.approx(691.2)
    assert sleeps == []  # an 11-minute wait is never slept off in a worker thread
    assert completions.calls == 1

    with pytest.raises(TranslationError) as again:
        svc.translate(video_id="bbbbbbbbbbb", original_text="another one.", target_language="en")
    assert again.value.error_code == "TRANSLATION_RATE_LIMITED"
    assert completions.calls == 1  # the cooldown answered without contacting Groq


def test_short_rate_limit_is_waited_out_and_retried(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(translation_module.time, "sleep", sleeps.append)
    svc, completions = _translator(tmp_path, [_RateLimited("x", retry_after="2"), "Hello."])

    result = svc.translate(video_id="aaaaaaaaaaa", original_text="hello there.", target_language="en")
    assert result["transcript"] == "Hello."
    assert sleeps == [2.0] and completions.calls == 2
    assert groq_cooldown.remaining(svc.model) == 0


def test_repeated_short_rate_limits_give_up_after_the_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(translation_module.time, "sleep", lambda s: None)
    svc, completions = _translator(tmp_path, [_RateLimited("x", retry_after="1")])

    with pytest.raises(TranslationError) as exc:
        svc.translate(video_id="aaaaaaaaaaa", original_text="hello there.", target_language="en")
    assert exc.value.error_code == "TRANSLATION_RATE_LIMITED"
    assert completions.calls == translation_module._RATE_LIMIT_RETRIES + 1


# ---------------------------------------------------------------------------
# Speech-to-text provider
# ---------------------------------------------------------------------------


def test_stt_daily_quota_trips_the_cooldown(tmp_path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(groq_module.time, "sleep", sleeps.append)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _RateLimited("Rate limit reached for model whisper-large-v3 on audio seconds per day. "
                           "Please try again in 20m0s.")

    provider = GroqWhisperProvider(api_key="gsk_test", model="whisper-large-v3", max_retries=3)
    provider._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 128)

    with pytest.raises(GroqRateLimitError) as exc:
        provider.transcribe(audio)
    assert exc.value.retry_after == pytest.approx(1200)
    assert len(calls) == 1 and sleeps == []
    with pytest.raises(GroqRateLimitError):
        provider.transcribe(audio)
    assert len(calls) == 1  # no upload while the cooldown runs


def test_groq_clients_disable_sdk_retries(monkeypatch):
    seen = []

    class FakeGroq:
        def __init__(self, **kwargs):
            seen.append(kwargs)

    import groq

    monkeypatch.setattr(groq, "Groq", FakeGroq)
    GroqWhisperProvider(api_key="gsk_test")
    TranslationService(api_key="gsk_test")._get_client()
    assert [kwargs.get("max_retries") for kwargs in seen] == [0, 0]


# ---------------------------------------------------------------------------
# Channel jobs
# ---------------------------------------------------------------------------

VIDEOS = ["vid00000001", "vid00000002", "vid00000003"]


class _Translator:
    def __init__(self):
        self.quota_left = 0
        self.calls: list[str] = []

    def translate(self, video_id, **kwargs):
        self.calls.append(video_id)
        if self.quota_left <= 0:
            raise TranslationError("quota", error_code="TRANSLATION_RATE_LIMITED", retry_after=600)
        self.quota_left -= 1
        return {"transcript": f"simple {video_id}"}


@pytest.fixture
def quota_env(monkeypatch, tmp_path):
    import api.channel_service as channel_module
    import api.video_service as video_module
    import services.transcript_service as ts_module
    from config.settings import settings
    from services.jobs.transcript_job_manager import TranscriptJobManager
    from services.transcript_limiter import transcript_limiter
    from tests.test_phase5_reliability import FakeYouTube

    fetches: list[tuple[str, bool]] = []
    stt_code: dict[str, str] = {}

    class FakeTranscripts:
        def get_transcript(self, video_id, allow_whisper=True, **kwargs):
            fetches.append((video_id, allow_whisper))
            if video_id in stt_code:
                code = stt_code[video_id] if allow_whisper else "NO_CAPTIONS"
                return SimpleNamespace(success=False, plain_text="", paragraph_text="", raw_transcript="",
                                       language="en", error_code=code, error="x")
            return SimpleNamespace(success=True, plain_text=f"raw {video_id}", paragraph_text="",
                                   raw_transcript=f"raw {video_id}", language="Hindi", source_language="Hindi",
                                   source_language_code="hi", error_code=None, error=None)

    async def no_wait(video_id):
        return None

    translator = _Translator()
    monkeypatch.setattr(channel_module, "ChannelService", FakeYouTube)
    monkeypatch.setattr(video_module, "VideoService", FakeYouTube)
    monkeypatch.setattr(ts_module, "TranscriptService", FakeTranscripts)
    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    monkeypatch.setattr(settings, "whisper_enabled", True)
    monkeypatch.setattr(translation_module, "get_translation_service", lambda: translator)
    return SimpleNamespace(
        manager=TranscriptJobManager(jobs_dir=tmp_path), translator=translator, fetches=fetches, stt_code=stt_code,
    )


async def test_translation_quota_pauses_the_job_and_resume_finishes_it(quota_env):
    from tests.test_phase5_reliability import _finish

    quota_env.translator.quota_left = 1
    job = await quota_env.manager.start_channel_job("chan", owner="u", output_language="en")
    job = await _finish(quota_env.manager, job.job_id)

    assert job.status == JobStatus.PAUSED
    assert "usage limit" in job.error and "10 minutes" in job.error
    items = {v.video_id: v for v in job.videos}
    assert items[VIDEOS[0]].transcript == f"simple {VIDEOS[0]}"
    assert items[VIDEOS[1]].status == "success" and items[VIDEOS[1]].translation_error  # original text kept
    assert items[VIDEOS[2]].status == "pending"  # never fetched while the quota is gone
    assert quota_env.translator.calls == VIDEOS[:2]
    assert job.translation_failed == 1

    quota_env.translator.quota_left = 10
    quota_env.fetches.clear()
    await quota_env.manager.resume_job(job.job_id)
    job = await _finish(quota_env.manager, job.job_id)

    assert job.status == JobStatus.COMPLETED and job.error is None
    assert job.translation_failed == 0 and job.successful == 3
    assert all(v.transcript == f"simple {v.video_id}" and not v.translation_error for v in job.videos)
    assert [vid for vid, _ in quota_env.fetches] == [VIDEOS[2]]  # VIDEOS[1] was only re-translated


async def test_resume_retries_translations_of_an_already_completed_job(quota_env):
    """Jobs finished before this fix: 'completed' with success items holding untranslated text."""
    from tests.test_phase5_reliability import _finish

    items = [
        TranscriptVideoItem(
            video_id=vid, video_url=f"https://www.youtube.com/watch?v={vid}", status="success",
            raw_transcript=f"raw {vid}", transcript=f"raw {vid}", source_language="Hindi",
            translation_error="The transcript could not be translated.",
        )
        for vid in VIDEOS
    ]
    job = TranscriptJobProgress(
        job_id="0123456789ab", channel_handle="@c", channel_id="UC1", channel_title="C",
        output_language="en", status=JobStatus.COMPLETED, eligible_videos=3, videos=items,
    )
    quota_env.manager._save_checkpoint(job)
    quota_env.translator.quota_left = 10

    await quota_env.manager.resume_job(job.job_id)
    job = await _finish(quota_env.manager, job.job_id)

    assert job.status == JobStatus.COMPLETED
    assert [v.simple_english_transcript for v in job.videos] == [f"simple {vid}" for vid in VIDEOS]
    assert quota_env.fetches == []  # captions were not fetched again


async def test_completed_job_with_untranslated_items_says_so(quota_env, monkeypatch):
    from tests.test_phase5_reliability import _finish

    class Broken:
        def translate(self, **kwargs):
            raise TranslationError("Groq 503 upstream")

    monkeypatch.setattr(translation_module, "get_translation_service", lambda: Broken())
    job = await quota_env.manager.start_channel_job("chan", owner="u", output_language="en")
    job = await _finish(quota_env.manager, job.job_id)

    assert job.status == JobStatus.COMPLETED
    assert job.translation_failed == 3
    assert "3 video(s) could not be translated" in job.error


async def test_stt_quota_pauses_the_job(quota_env):
    from tests.test_phase5_reliability import _finish

    quota_env.stt_code[VIDEOS[0]] = "STT_RATE_LIMITED"
    job = await quota_env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(quota_env.manager, job.job_id)

    assert job.status == JobStatus.PAUSED and "speech-to-text" in job.error
    statuses = [v.status for v in job.videos]
    assert statuses == ["temporary_error", "pending", "pending"]


async def test_original_mode_job_never_needs_translation_retry(quota_env):
    from tests.test_phase5_reliability import _finish

    job = await quota_env.manager.start_channel_job("chan", owner="u", output_language="original")
    job = await _finish(quota_env.manager, job.job_id)
    assert job.status == JobStatus.COMPLETED and job.translation_failed == 0
    assert quota_env.translator.calls == []


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED])
async def test_cancel_does_not_rewrite_a_finished_job(tmp_path, status):
    from services.jobs.transcript_job_manager import TranscriptJobManager

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job = TranscriptJobProgress(job_id="0123456789ab", channel_handle="@c", channel_id="UC1",
                                channel_title="C", status=status)
    manager._save_checkpoint(job)
    manager._recent.clear()

    assert await manager.cancel_job(job.job_id) is False
    assert manager._load_checkpoint(job.job_id).status == status


async def test_cancel_still_stops_a_paused_job(tmp_path):
    from services.jobs.transcript_job_manager import TranscriptJobManager

    manager = TranscriptJobManager(jobs_dir=tmp_path)
    job = TranscriptJobProgress(job_id="0123456789ab", channel_handle="@c", channel_id="UC1",
                                channel_title="C", status=JobStatus.PAUSED)
    manager._save_checkpoint(job)
    assert await manager.cancel_job(job.job_id) is True
    assert manager._load_checkpoint(job.job_id).status == JobStatus.CANCELLED
