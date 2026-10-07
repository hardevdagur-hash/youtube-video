"""Unit and integration tests for the Transcript Service."""

from unittest.mock import MagicMock, patch

import pytest

from models.transcript import (
    TranscriptResult,
    TranscriptSegment,
    TranscriptSource,
    TranscriptProviderName,
    PipelineStep,
)
from exceptions.transcript_errors import InvalidVideoIdError
from interfaces.transcript_provider import TranscriptProvider
from interfaces.speech_to_text import TranscriptionResult, TranscriptionSegment
from clients.youtube_transcript_client import NoTranscriptFoundError


# ─── Mock Providers ───────────────────────────────────────────────


class MockManualProvider(TranscriptProvider):
    def __init__(self, succeed=True, segments=None):
        self._succeed = succeed
        self._segments = segments or [
            TranscriptSegment(start=0.0, end=2.0, duration=2.0, text="Hello from manual."),
        ]

    def name(self):
        return "Manual"

    def get_transcript(self, video_id, language=None):
        if not self._succeed:
            raise NoTranscriptFoundError("No manual transcript")
        text = " ".join(s.text for s in self._segments)
        return TranscriptResult(
            success=True,
            video_id=video_id,
            source=TranscriptSource.MANUAL,
            provider=TranscriptProviderName.YOUTUBE_MANUAL,
            language="en",
            segments=self._segments,
            plain_text=text,
            word_count=len(text.split()),
            character_count=len(text),
            estimated_read_time="< 1 min",
        )


class MockAutoProvider(TranscriptProvider):
    def __init__(self, succeed=True, segments=None):
        self._succeed = succeed
        self._segments = segments or [
            TranscriptSegment(start=0.0, end=2.0, duration=2.0, text="Hello from auto."),
        ]

    def name(self):
        return "Auto"

    def get_transcript(self, video_id, language=None):
        if not self._succeed:
            raise NoTranscriptFoundError("No auto transcript")
        text = " ".join(s.text for s in self._segments)
        return TranscriptResult(
            success=True,
            video_id=video_id,
            source=TranscriptSource.AUTO,
            provider=TranscriptProviderName.YOUTUBE_AUTO,
            language="en",
            segments=self._segments,
            plain_text=text,
            word_count=len(text.split()),
            character_count=len(text),
            estimated_read_time="< 1 min",
        )


class MockWhisperProvider(TranscriptProvider):
    def __init__(self, succeed=True):
        self._succeed = succeed

    def name(self):
        return "Whisper"

    def get_transcript(self, video_id, language=None):
        if not self._succeed:
            raise RuntimeError("Whisper failed")
        segments = [
            TranscriptSegment(start=0.0, end=2.0, duration=2.0, text="Hello from whisper."),
        ]
        text = "Hello from whisper."
        return TranscriptResult(
            success=True,
            video_id=video_id,
            source=TranscriptSource.WHISPER,
            provider=TranscriptProviderName.FASTER_WHISPER,
            language="en",
            segments=segments,
            plain_text=text,
            word_count=3,
            character_count=19,
            estimated_read_time="< 1 min",
        )


class TestTranscriptService:
    """Tests for TranscriptService orchestration."""

    def test_manual_transcript_used_first(self):
        """Stage 1 (manual) should be preferred when available."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=True),
            auto_provider=MockAutoProvider(succeed=True),
            whisper_provider=MockWhisperProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is True
        assert result.source == TranscriptSource.MANUAL
        assert result.provider == TranscriptProviderName.YOUTUBE_MANUAL

    def test_fallback_to_auto_when_manual_fails(self):
        """Stage 2 should be used when Stage 1 fails."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=True),
            whisper_provider=MockWhisperProvider(succeed=False),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is True
        assert result.source == TranscriptSource.AUTO

    def test_fallback_to_whisper_when_all_others_fail(self):
        """Stage 3 should be used when Stages 1 and 2 fail."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=False),
            whisper_provider=MockWhisperProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is True
        assert result.source == TranscriptSource.WHISPER

    def test_all_stages_fail(self):
        """Should return error result when all stages fail."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=False),
            whisper_provider=MockWhisperProvider(succeed=False),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is False
        assert result.error is not None

    def test_whisper_can_be_disabled(self):
        """Whisper fallback should be skippable."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=False),
            whisper_provider=MockWhisperProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ", allow_whisper=False)
        assert result.success is False

    def test_invalid_video_id_raises(self):
        """Invalid video IDs should raise immediately."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(use_cache=False)

        with pytest.raises(InvalidVideoIdError):
            service.get_transcript("too-short")

        with pytest.raises(InvalidVideoIdError):
            service.get_transcript("")

    def test_pipeline_steps_are_recorded(self):
        """Pipeline steps should be populated in the result."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert len(result.pipeline_steps) > 0

    def test_manual_failure_is_skipped_not_error(self):
        """Manual transcript failure should be 'skipped', not 'error'."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is True
        assert result.source == TranscriptSource.AUTO

        manual_step = next(
            (s for s in result.pipeline_steps if "Manual" in s.name),
            None,
        )
        assert manual_step is not None, "Manual step should exist in pipeline"
        assert manual_step.status == "skipped", (
            f"Manual step should be 'skipped', got '{manual_step.status}'"
        )
        assert "error" not in manual_step.detail.lower()

    def test_manual_failure_pipeline_does_not_show_error_status(self):
        """When manual fails but auto succeeds, no pipeline step should be 'error'."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=True),
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        error_steps = [s for s in result.pipeline_steps if s.status == "error"]
        assert len(error_steps) == 0, (
            f"Pipeline should have no error steps when auto succeeds, "
            f"got: {[(s.name, s.status) for s in result.pipeline_steps]}"
        )

    def test_all_stages_skipped_properly(self):
        """All pipeline stages should be 'skipped' when transcripts are unavailable, then 'error'."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=False),
            auto_provider=MockAutoProvider(succeed=False),
            whisper_provider=None,
            use_cache=False,
        )

        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is False

        steps = result.pipeline_steps
        skipped_steps = [s for s in steps if s.status == "skipped"]
        error_steps = [s for s in steps if s.status == "error"]
        assert len(skipped_steps) >= 1
        assert len(error_steps) >= 1

    def test_cache_hit_returns_fast(self):
        """Cached results should be returned without calling providers."""
        from services.transcript_service import TranscriptService

        manual = MockManualProvider(succeed=True)
        service = TranscriptService(
            manual_provider=manual,
            auto_provider=MockAutoProvider(succeed=False),
            use_cache=True,
        )

        result1 = service.get_transcript("dQw4w9WgXcQ", force_refresh=True)
        assert result1.success is True

        result2 = service.get_transcript("dQw4w9WgXcQ")
        assert result2.success is True

    def test_force_refresh_bypasses_cache(self):
        """force_refresh=True should bypass cache."""
        from services.transcript_service import TranscriptService

        manual = MockManualProvider(succeed=True)
        service = TranscriptService(
            manual_provider=manual,
            use_cache=True,
        )

        result1 = service.get_transcript("vtest123456", force_refresh=True)
        result2 = service.get_transcript("vtest123456", force_refresh=True)
        assert result1.success
        assert result2.success

    def test_clear_cache(self):
        """Cache should be clearable."""
        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=MockManualProvider(succeed=True),
            use_cache=True,
        )

        service.get_transcript("dQw4w9WgXcQ")
        service.clear_cache()

class TestTranscriptServiceIntegration:
    """Integration tests with real dependencies (mocked API)."""

    @patch("services.transcript_service.ManualTranscriptProvider")
    def test_real_manual_provider_mocked(self, mock_manual_cls):
        """Service should work with real provider classes."""
        mock_provider = MagicMock()
        mock_provider.name.return_value = "Manual"
        mock_provider.get_transcript.return_value = TranscriptResult(
            success=True,
            video_id="dQw4w9WgXcQ",
            source=TranscriptSource.MANUAL,
            provider=TranscriptProviderName.YOUTUBE_MANUAL,
            language="en",
            segments=[TranscriptSegment(start=0.0, end=1.0, duration=1.0, text="Test")],
            plain_text="Test",
            word_count=1,
            character_count=4,
            estimated_read_time="< 1 min",
        )
        mock_manual_cls.return_value = mock_provider

        from services.transcript_service import TranscriptService

        service = TranscriptService(
            manual_provider=mock_provider,
            use_cache=False,
        )
        result = service.get_transcript("dQw4w9WgXcQ")
        assert result.success is True
        assert result.plain_text == "Test"


class TestSpeechToTextInterface:
    """Tests for the STT interface and dummy client."""

    def test_transcription_result_dataclass(self):
        segments = [
            TranscriptionSegment(start=0.0, end=2.0, text="Hello"),
            TranscriptionSegment(start=2.0, end=4.0, text="world"),
        ]
        result = TranscriptionResult(
            segments=segments,
            language="en",
            language_confidence=0.95,
            duration_seconds=4.0,
            processing_time_seconds=0.5,
        )
        assert len(result.segments) == 2
        assert result.language == "en"
        assert result.processing_time_seconds == 0.5

    def test_dummy_whisper_client(self):
        from clients.whisper_client import DummyWhisperClient

        client = DummyWhisperClient()
        assert client.model_name() == "dummy"
        result = client.transcribe("/fake/path.wav")
        assert len(result.segments) == 2
        assert result.language == "en"

    def test_dummy_whisper_custom_segments(self):
        from clients.whisper_client import DummyWhisperClient
        from interfaces.speech_to_text import TranscriptionSegment

        custom = [TranscriptionSegment(start=0.0, end=5.0, text="Custom segment", confidence=0.99)]
        client = DummyWhisperClient(canned_segments=custom)
        result = client.transcribe("test.wav", language="es")
        assert len(result.segments) == 1
        assert result.segments[0].text == "Custom segment"
        assert result.language == "es"
        assert result.segments[0].confidence == 0.99
