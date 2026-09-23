"""Comprehensive automated test suite for Production Transcript & Translation Architecture."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from exceptions import YouTubeURLError
from services.youtube.resolver import YouTubeResolver
from services.youtube.captions import YouTubeCaptionsService, CaptionsUnavailableError
from services.youtube.audio import YouTubeAudioExtractor, AudioExtractionError
from services.transcription.provider import TranscriptionResult, TranscriptionSegment
from services.transcription.groq import (
    GroqWhisperProvider,
    GroqAuthError,
    GroqRateLimitError,
    GroqTimeoutError,
    GroqTranscriptionError,
)
from services.transcription.cleaner import TranscriptCleaner
from services.transcription.validator import (
    TranscriptValidator,
    TranscriptEmptyError,
    TranscriptValidationError,
)
from services.transcription.service import TranscriptService
from services.translation.service import TranslationService
from repositories.transcript_repository import TranscriptRepository
from observability.transcript_metrics import TranscriptMetricsTracker


# ---------------------------------------------------------------------------
# 1. YouTube Resolver Tests
# ---------------------------------------------------------------------------

def test_resolver_valid_urls():
    resolver = YouTubeResolver()
    assert resolver.resolve_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert resolver.resolve_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert resolver.resolve_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert resolver.resolve_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert resolver.resolve_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_resolver_invalid_urls():
    resolver = YouTubeResolver()
    with pytest.raises(YouTubeURLError):
        resolver.resolve_video_id("")
    with pytest.raises(YouTubeURLError):
        resolver.resolve_video_id("https://notyoutube.com/watch?v=12345")


# ---------------------------------------------------------------------------
# 2. YouTube Captions Service Tests
# ---------------------------------------------------------------------------

def test_captions_success():
    mock_client = MagicMock()
    mock_client.find_best_transcript.return_value = (
        [{"text": "Hello world", "start": 0.0, "duration": 2.0}],
        "en",
        True,
        None,
    )
    service = YouTubeCaptionsService(client=mock_client)
    segments, lang, is_manual = service.fetch_captions("abc12345678")
    assert len(segments) == 1
    assert lang == "en"
    assert is_manual is True


def test_captions_disabled_fallback():
    mock_client = MagicMock()
    from clients.youtube_transcript_client import TranscriptsDisabledError
    mock_client.find_best_transcript.side_effect = TranscriptsDisabledError("Captions disabled")
    service = YouTubeCaptionsService(client=mock_client)

    with pytest.raises(CaptionsUnavailableError) as exc_info:
        service.fetch_captions("abc12345678")
    assert exc_info.value.error_code == "CAPTIONS_DISABLED"


# ---------------------------------------------------------------------------
# 3. Groq Whisper Provider Tests
# ---------------------------------------------------------------------------

def test_groq_auth_missing_key():
    provider = GroqWhisperProvider(api_key="")
    with pytest.raises(GroqAuthError):
        provider.transcribe(Path("nonexistent.m4a"))


def test_groq_success_mock(tmp_path):
    audio_file = tmp_path / "test.m4a"
    audio_file.write_bytes(b"dummy audio data")

    provider = GroqWhisperProvider(api_key="gsk_dummy_test_key")

    mock_resp = MagicMock()
    mock_resp.text = "This is a transcribed sentence."
    mock_seg = MagicMock()
    mock_seg.start = 0.0
    mock_seg.end = 2.5
    mock_seg.text = "This is a transcribed sentence."
    mock_resp.segments = [mock_seg]
    mock_resp.language = "english"
    mock_resp.duration = 2.5

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create.return_value = mock_resp
    provider._client = mock_client

    result = provider.transcribe(audio_file)
    assert result.text == "This is a transcribed sentence."
    assert result.language == "en"
    assert len(result.segments) == 1
    assert result.segments[0].duration == 2.5


# ---------------------------------------------------------------------------
# 4. Transcript Cleaner & Validator Tests
# ---------------------------------------------------------------------------

def test_transcript_cleaner():
    cleaner = TranscriptCleaner()
    raw = [
        {"start": 0.0, "duration": 2.0, "text": "  hello world  "},
        {"start": 2.0, "duration": 3.0, "text": "this is a test sentence."},
    ]
    cleaned = cleaner.clean(raw_segments=raw, video_id="test1234567")
    assert cleaned["word_count"] > 0
    assert len(cleaned["segments"]) == 2
    assert cleaned["segments"][0]["text"] == "hello world"


def test_transcript_validator_empty():
    validator = TranscriptValidator()
    with pytest.raises(TranscriptEmptyError):
        validator.validate(text="   ", segments=[])


def test_transcript_validator_hallucination():
    validator = TranscriptValidator()
    # Degenerate loop: same phrase repeated 10 times
    repeated = "Thank you for watching. " * 10
    with pytest.raises(TranscriptValidationError):
        validator.validate(text=repeated)


def test_transcript_validator_valid():
    validator = TranscriptValidator()
    report = validator.validate(
        text="In this tutorial we explore database indexing and query performance.",
        duration_seconds=120.0,
    )
    assert report.is_valid is True
    assert report.word_count == 10
    assert report.confidence >= 0.9


# ---------------------------------------------------------------------------
# 5. Translation Service Tests (Simple English & Simple Hindi)
# ---------------------------------------------------------------------------

def test_translation_service_mock():
    repo = TranscriptRepository()
    service = TranslationService(repository=repo, api_key="gsk_test")

    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "We will learn SQL."
    mock_resp = MagicMock(choices=[mock_choice])
    mock_client.chat.completions.create.return_value = mock_resp
    service._client = mock_client

    # 1. First translation (miss)
    res1 = service.translate(
        video_id="vid12345678",
        original_text="So basically, you know, we're gonna look at SQL...",
        target_language="en",
    )
    assert res1["transcript"] == "We will learn SQL."
    assert res1["from_cache"] is False

    # 2. Second translation (hit from cache)
    res2 = service.translate(
        video_id="vid12345678",
        original_text="So basically, you know, we're gonna look at SQL...",
        target_language="en",
    )
    assert res2["transcript"] == "We will learn SQL."
    assert res2["from_cache"] is True


# ---------------------------------------------------------------------------
# 6. Observability Metrics Tracker Tests
# ---------------------------------------------------------------------------

def test_metrics_tracker():
    tracker = TranscriptMetricsTracker()
    tracker.record_request_start()
    tracker.record_caption_result(success=True)
    tracker.record_final_result(success=True)

    snapshot = tracker.get_snapshot()
    assert snapshot["total_transcript_requests"] == 1
    assert snapshot["transcript_success_rate_percent"] == 100.0
    assert snapshot["caption_success_rate_percent"] == 100.0
