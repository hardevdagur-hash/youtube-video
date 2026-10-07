# ruff: noqa: ARG001, ARG002  (stub signatures mirror the real interfaces)
"""Regression: "Original Spoken" must never silently become an English translation.

Reproduces the defect where Whisper ran with task="translate" (English output) and
that English text was stored as the canonical raw transcript, then shown and exported
as "Original Spoken". External edges (captions, audio, STT engine, translator,
pacing) are stubbed; the real WhisperProvider, TranscriptService, job loop, CSV
builder and routes are exercised.
"""

from __future__ import annotations

import asyncio
import csv
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from models.transcript import TranscriptResult, TranscriptSource, WhisperProcessingInfo
from services.transcript_service import TranscriptService

# isort: split
# Must follow transcript_service (import order as in the app; avoids a circular import).
from providers.whisper_provider import WhisperProvider

HINDI_SPEECH = "नमस्ते दोस्तों आज हम भौतिकी का एक कठिन प्रश्न हल करेंगे"
WHISPER_ENGLISH = "Hello friends today we will solve a difficult physics question"
SIMPLE_ENGLISH = "SIMPLE-EN: Hello friends, today we solve a hard physics question."
SIMPLE_HINDI = "SIMPLE-HI: नमस्ते दोस्तों"
VIDEO_ID = "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# Stubs for external edges
# ---------------------------------------------------------------------------


class StubSTT:
    """Follows Whisper's contract: task='translate' outputs English, otherwise the spoken language."""

    def __init__(self):
        self.tasks: list[str] = []

    def transcribe(self, audio_path, language=None, task="transcribe", initial_prompt=None):
        self.tasks.append(task)
        text = WHISPER_ENGLISH if task == "translate" else HINDI_SPEECH
        return SimpleNamespace(
            segments=[SimpleNamespace(start=0.0, end=5.0, text=text)],
            language="en" if task == "translate" else "hi",
            language_confidence=0.97, duration_seconds=5.0, processing_time_seconds=0.1,
        )

    def model_name(self):
        return "stub-whisper"


class NoCaptions:
    def name(self):
        return "no-captions"

    def get_transcript(self, video_id, *args, **kwargs):
        return TranscriptResult(success=False, video_id=video_id, source=TranscriptSource.MANUAL,
                                error="No captions", error_code="NO_CAPTIONS")


class HindiCaptions:
    def name(self):
        return "hindi-captions"

    def get_transcript(self, video_id, *args, **kwargs):
        from models.transcript import TranscriptSegment

        return TranscriptResult(success=True, video_id=video_id, source=TranscriptSource.AUTO,
                                language="hi", plain_text=HINDI_SPEECH, paragraph_text=HINDI_SPEECH,
                                segments=[TranscriptSegment(start=0.0, end=5.0, duration=5.0, text=HINDI_SPEECH)])


class MemRepo:
    def __init__(self, cached=None):
        self.cached = cached
        self.saved: list[TranscriptResult] = []

    def get(self, video_id):
        return self.cached

    def save(self, result):
        self.saved.append(result)


class StubTranslator:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def translate(self, video_id, original_text, target_language, source_language=None, **kwargs):
        self.calls.append({"original_text": original_text, "target_language": target_language,
                           "source_language": source_language})
        if self.fail:
            raise RuntimeError("translation provider unavailable")
        return {"transcript": SIMPLE_HINDI if target_language == "hi" else SIMPLE_ENGLISH}


def _whisper(stt: StubSTT) -> WhisperProvider:
    provider = WhisperProvider(stt_client=stt, temp_dir=tempfile.mkdtemp())
    provider._audio_service = SimpleNamespace(
        download_audio=lambda vid: Path(tempfile.gettempdir()) / f"{vid}.m4a", cleanup=lambda p: None,
    )
    return provider


def _service(stt: StubSTT, captions=None, repo: MemRepo | None = None) -> TranscriptService:
    captions = captions or NoCaptions()
    return TranscriptService(manual_provider=captions, auto_provider=NoCaptions(),
                             whisper_provider=_whisper(stt), repository=repo or MemRepo())


def _is_hindi(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


# ---------------------------------------------------------------------------
# 1. Speech-to-text keeps the spoken language
# ---------------------------------------------------------------------------


def test_whisper_provider_transcribes_verbatim_never_translates():
    stt = StubSTT()
    result = _whisper(stt).get_transcript(VIDEO_ID, title="t", channel_title="c")
    assert stt.tasks == ["transcribe"]
    assert result.raw_transcript == HINDI_SPEECH  # verbatim spoken-language source
    assert result.whisper_info.task == "transcribe"
    assert result.language == "Hinglish"  # truthful label, never "English (India)"
    # plain_text is a transliteration of the same speech, not Whisper's English translation
    assert result.plain_text
    assert WHISPER_ENGLISH.split()[0] not in result.plain_text


@pytest.mark.parametrize("mode", ["original", "original_spoken"])
def test_no_caption_video_original_mode_returns_spoken_hindi(mode):
    stt = StubSTT()
    result = _service(stt).get_transcript(VIDEO_ID, allow_whisper=True, output_format=mode)
    assert stt.tasks == ["transcribe"]
    assert result.success
    assert result.raw_transcript == HINDI_SPEECH
    assert result.plain_text == HINDI_SPEECH
    assert result.source_language == "Hindi"
    assert WHISPER_ENGLISH.split()[0] not in result.plain_text


def test_non_english_captions_are_not_replaced_by_whisper_in_english_mode():
    stt = StubSTT()
    result = _service(stt, captions=HindiCaptions()).get_transcript(VIDEO_ID, allow_whisper=True, output_format="en")
    assert stt.tasks == []  # no audio download / STT just to get "English"
    assert result.raw_transcript == HINDI_SPEECH


# ---------------------------------------------------------------------------
# 2. Legacy translated cache entries are ignored, not served as "original"
# ---------------------------------------------------------------------------


def _cached_whisper(task: str | None) -> TranscriptResult:
    return TranscriptResult(
        success=True, video_id=VIDEO_ID, source=TranscriptSource.WHISPER, language="English (India)",
        plain_text=WHISPER_ENGLISH, raw_transcript=WHISPER_ENGLISH,
        whisper_info=WhisperProcessingInfo(model_name="old", task=task),
    )


@pytest.mark.parametrize("task", [None, "translate"])
def test_cached_translated_whisper_result_is_recomputed(task):
    stt = StubSTT()
    repo = MemRepo(cached=_cached_whisper(task))
    result = _service(stt, repo=repo).get_transcript(VIDEO_ID, allow_whisper=True, output_format="original")
    assert stt.tasks == ["transcribe"]
    assert result.raw_transcript == HINDI_SPEECH
    assert repo.saved
    assert repo.saved[-1].whisper_info.task == "transcribe"


def test_cached_verbatim_whisper_result_is_reused():
    stt = StubSTT()
    cached = _cached_whisper("transcribe")
    cached.plain_text = cached.raw_transcript = HINDI_SPEECH
    result = _service(stt, repo=MemRepo(cached=cached)).get_transcript(VIDEO_ID, output_format="original")
    assert stt.tasks == []
    assert result.raw_transcript == HINDI_SPEECH


# ---------------------------------------------------------------------------
# 3. Caption track selection prefers the spoken language
# ---------------------------------------------------------------------------


class FakeTrack:
    def __init__(self, code, generated, text):
        self.language_code = code
        self.language = code
        self.is_generated = generated
        self.is_translatable = True
        self._text = text

    def fetch(self):
        return [SimpleNamespace(text=self._text, start=0.0, duration=1.0)]

    def translate(self, code):
        return FakeTrack(code, self.is_generated, f"machine-translated to {code}")


def _client():
    from clients.youtube_transcript_client import YouTubeTranscriptClient
    return YouTubeTranscriptClient.__new__(YouTubeTranscriptClient)


def test_any_mode_prefers_spoken_language_over_english_subtitles():
    tracks = [FakeTrack("en", False, "creator English subtitles"), FakeTrack("hi", True, HINDI_SPEECH)]
    segments, lang, is_manual, translated_from = _client()._find_best_any(tracks, ["en", "hi"])
    assert lang == "hi"
    assert segments[0]["text"] == HINDI_SPEECH
    assert translated_from is None


def test_any_mode_prefers_manual_track_in_spoken_language():
    tracks = [FakeTrack("hi", True, "asr"), FakeTrack("hi", False, "manual hindi"), FakeTrack("en", False, "en subs")]
    segments, lang, is_manual, _ = _client()._find_best_any(tracks, ["en", "hi"])
    assert (lang, is_manual, segments[0]["text"]) == ("hi", True, "manual hindi")


def test_english_video_behaviour_unchanged():
    tracks = [FakeTrack("en", True, "asr en"), FakeTrack("en", False, "manual en")]
    segments, lang, is_manual, _ = _client()._find_best_any(tracks, ["en", "hi"])
    assert (lang, is_manual, segments[0]["text"]) == ("en", True, "manual en")


def test_manual_stage_rejects_subtitles_that_are_translations():
    from clients.youtube_transcript_client import NoTranscriptFoundError

    tracks = [FakeTrack("en", False, "creator English subtitles"), FakeTrack("hi", True, HINDI_SPEECH)]
    with pytest.raises(NoTranscriptFoundError):
        _client()._find_best_manual(tracks, ["en", "hi"])


def test_without_asr_track_existing_priority_is_kept():
    tracks = [FakeTrack("en", False, "only manual en")]
    segments, lang, _, _ = _client()._find_best_any(tracks, ["en", "hi"])
    assert (lang, segments[0]["text"]) == ("en", "only manual en")


# ---------------------------------------------------------------------------
# 4. Channel background job: real _run_job loop + CSV export
# ---------------------------------------------------------------------------


@pytest.fixture
def job_env(monkeypatch, tmp_path):
    import services.transcript_service as ts_module
    import services.translation.service as translation_module
    from config.settings import settings
    from services.jobs.transcript_job_manager import TranscriptJobManager
    from transcript_reliability.transcript_limiter import transcript_limiter

    stt = StubSTT()
    translator = StubTranslator()
    monkeypatch.setattr(ts_module, "TranscriptService", lambda *a, **k: _service(stt))
    monkeypatch.setattr(translation_module, "TranslationService", lambda *a, **k: translator)
    monkeypatch.setattr(settings, "whisper_enabled", True)

    async def no_wait(video_id):
        return None

    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    return SimpleNamespace(stt=stt, translator=translator, manager=TranscriptJobManager(jobs_dir=tmp_path))


def _run_channel_job(env, output_language: str):
    from models.transcript_job import JobStatus, TranscriptJobProgress, TranscriptVideoItem

    job = TranscriptJobProgress(
        job_id="abcabcabcabc", channel_handle="physicschannel", channel_id="UC1", channel_title="Physics",
        status=JobStatus.QUEUED, total_discovered=1, eligible_videos=1, skipped_videos=0, remaining=1,
        output_language=output_language,
        videos=[TranscriptVideoItem(
            video_id=VIDEO_ID, video_url=f"https://www.youtube.com/watch?v={VIDEO_ID}", channel_id="UC1",
            channel_title="Physics", title="Hard question", published_at="2026-01-01T00:00:00Z",
            duration_seconds=300, duration="5:00", language="", status="pending", transcript="",
            source=None, method=None,
        )],
    )
    env.manager._jobs[job.job_id] = job
    asyncio.run(env.manager._run_job(job))
    rows = list(csv.DictReader(io.StringIO(env.manager.generate_csv(job.job_id).lstrip("﻿"))))
    return job, job.videos[0], rows


def test_channel_job_original_spoken_is_never_english(job_env):
    job, item, rows = _run_channel_job(job_env, "original")
    assert job_env.stt.tasks == ["transcribe"]
    assert item.status == "success"
    assert item.method == "speech_to_text"
    assert item.raw_transcript == HINDI_SPEECH
    assert item.transcript == HINDI_SPEECH
    assert job_env.translator.calls == []  # original mode never translates
    assert rows[0]["transcript"] == HINDI_SPEECH
    assert WHISPER_ENGLISH.split()[0] not in rows[0]["transcript"]


def test_channel_job_simple_english_translates_from_spoken_source(job_env):
    job, item, rows = _run_channel_job(job_env, "en")
    assert job_env.stt.tasks == ["transcribe"]
    assert job_env.translator.calls[0]["original_text"] == HINDI_SPEECH
    assert job_env.translator.calls[0]["target_language"] == "en"
    assert item.transcript == SIMPLE_ENGLISH
    assert item.raw_transcript == HINDI_SPEECH  # original preserved alongside translation
    assert rows[0]["transcript"] == SIMPLE_ENGLISH


def test_channel_job_hindi_output(job_env):
    _, item, _ = _run_channel_job(job_env, "hi")
    assert job_env.translator.calls[0]["target_language"] == "hi"
    assert item.transcript == SIMPLE_HINDI
    assert item.raw_transcript == HINDI_SPEECH


# ---------------------------------------------------------------------------
# 5. Legacy HTTP routes: CSV export and synchronous channel transcripts
# ---------------------------------------------------------------------------


@pytest.fixture
def route_env(authed_client, monkeypatch):
    import webapp.main as web

    stt = StubSTT()
    translator = StubTranslator()
    service = _service(stt)
    video_svc = SimpleNamespace(
        get_videos_batch=lambda ids: [{"id": i, "snippet": {"title": "Hard question", "channelId": "UC1",
                                                           "channelTitle": "Physics", "publishedAt": "2026-01-01"},
                                       "contentDetails": {"duration": "PT5M"}} for i in ids],
        get_uploads_playlist_id=lambda cid: "UU1",
        get_playlist_items=lambda pid, token=None: {"video_ids": [VIDEO_ID], "next_page_token": None},
    )
    monkeypatch.setattr(web, "_get_transcript_service", lambda: service)
    monkeypatch.setattr(web, "_get_video_service", lambda: video_svc)
    monkeypatch.setattr(web, "_get_channel_service",
                        lambda: SimpleNamespace(resolve_handle=lambda h: {"id": "UC1", "snippet": {"title": "Physics"}}))
    monkeypatch.setattr(web, "_get_unified_services", lambda: (None, translator))
    monkeypatch.setattr(web.settings, "whisper_enabled", True)
    return SimpleNamespace(client=authed_client, stt=stt, translator=translator)


def _export_rows(env, **body):
    resp = env.client.post("/api/transcript/export", json=body)
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))


@pytest.mark.parametrize("source", [{"video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
                                    {"channel_handle": "physicschannel"}])
def test_csv_export_original_spoken_is_spoken_text(route_env, source):
    rows = _export_rows(route_env, output_language="original", max_videos=5, **source)
    assert rows[0]["transcript"] == HINDI_SPEECH
    assert rows[0]["language"] == "Hindi"
    assert route_env.translator.calls == []
    assert route_env.stt.tasks == ["transcribe"]


@pytest.mark.parametrize("source", [{"video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}"},
                                    {"channel_handle": "physicschannel"}])
def test_csv_export_simple_english_uses_translation_step(route_env, source):
    rows = _export_rows(route_env, output_language="en", max_videos=5, **source)
    assert rows[0]["transcript"] == SIMPLE_ENGLISH
    assert rows[0]["language"] == "Simple English"
    assert route_env.translator.calls[0]["original_text"] == HINDI_SPEECH


def test_csv_export_translation_failure_is_labelled_honestly(route_env):
    route_env.translator.fail = True
    rows = _export_rows(route_env, output_language="en", max_videos=5,
                        video_url=f"https://www.youtube.com/watch?v={VIDEO_ID}")
    assert rows[0]["transcript"] == HINDI_SPEECH
    assert rows[0]["language"] != "Simple English"  # never claims English it does not contain


def test_csv_export_rejects_unknown_output_language(route_env):
    resp = route_env.client.post("/api/transcript/export",
                                 json={"video_url": "https://youtu.be/dQw4w9WgXcQ", "output_language": "fr"})
    assert resp.status_code == 422


@pytest.mark.parametrize(("mode", "expected"), [("original", HINDI_SPEECH), ("en", SIMPLE_ENGLISH)])
def test_sync_channel_transcripts_honour_output_language(route_env, monkeypatch, mode, expected):
    from transcript_reliability.transcript_limiter import transcript_limiter

    async def no_wait(video_id):
        return None

    monkeypatch.setattr(transcript_limiter, "acquire", no_wait)
    resp = route_env.client.get("/api/channel/physicschannel/transcripts",
                                params={"limit": 5, "output_language": mode})
    assert resp.status_code == 200, resp.text
    video = resp.json()["data"]["videos"][0]
    assert video["transcript"] == expected
    assert video["raw_transcript"] == HINDI_SPEECH
