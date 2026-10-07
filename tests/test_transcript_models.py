"""Unit tests for transcript Pydantic models."""

from models.transcript import (
    PipelineStep,
    TranscriptProviderName,
    TranscriptResult,
    TranscriptSegment,
    TranscriptSource,
    WhisperProcessingInfo,
)


class TestTranscriptSegment:
    def test_create_segment(self):
        seg = TranscriptSegment(start=0.0, end=4.2, duration=4.2, text="Hello world")
        assert seg.start == 0.0
        assert seg.end == 4.2
        assert seg.duration == 4.2
        assert seg.text == "Hello world"

    def test_segment_defaults(self):
        seg = TranscriptSegment(start=1.0, end=2.0, duration=1.0, text="test")
        assert seg.start == 1.0


class TestTranscriptResult:
    def test_create_transcript_result(self):
        segments = [
            TranscriptSegment(start=0.0, end=2.0, duration=2.0, text="Hello"),
            TranscriptSegment(start=2.0, end=4.0, duration=2.0, text="world"),
        ]
        result = TranscriptResult(
            video_id="dQw4w9WgXcQ",
            source=TranscriptSource.MANUAL,
            provider=TranscriptProviderName.YOUTUBE_MANUAL,
            language="en",
            segments=segments,
            plain_text="Hello world",
            word_count=2,
            character_count=11,
            estimated_read_time="< 1 min",
        )
        assert result.success is True
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.source == TranscriptSource.MANUAL
        assert result.segments == segments
        assert result.word_count == 2

    def test_transcript_result_defaults(self):
        result = TranscriptResult(video_id="test1234567")
        assert result.success is True
        assert result.source == TranscriptSource.MANUAL
        assert result.segments == []
        assert result.word_count == 0
        assert result.character_count == 0

    def test_with_whisper_info(self):
        wi = WhisperProcessingInfo(
            model_name="faster-whisper/base",
            processing_time_seconds=12.5,
            audio_duration_seconds=120.0,
            language_detected="en",
            language_confidence=0.95,
        )
        result = TranscriptResult(
            video_id="test1234567",
            source=TranscriptSource.WHISPER,
            whisper_info=wi,
        )
        assert result.whisper_info is not None
        assert result.whisper_info.model_name == "faster-whisper/base"
        assert result.whisper_info.processing_time_seconds == 12.5

    def test_with_pipeline_steps(self):
        steps = [
            PipelineStep(name="Manual", status="ok", detail="Found"),
            PipelineStep(name="Auto", status="skipped", detail="Skipped"),
        ]
        result = TranscriptResult(
            video_id="test1234567",
            pipeline_steps=steps,
        )
        assert len(result.pipeline_steps) == 2
        assert result.pipeline_steps[0].status == "ok"

    def test_error_result(self):
        result = TranscriptResult(
            success=False,
            video_id="bad12345678",
            error="No transcript available.",
        )
        assert result.success is False
        assert result.error == "No transcript available."


class TestTranscriptSource:
    def test_source_values(self):
        assert TranscriptSource.MANUAL.value == "manual"
        assert TranscriptSource.AUTO.value == "auto"
        assert TranscriptSource.WHISPER.value == "whisper"

    def test_source_str(self):
        assert str(TranscriptSource.MANUAL) == "TranscriptSource.MANUAL"


class TestTranscriptProviderName:
    def test_provider_values(self):
        assert TranscriptProviderName.YOUTUBE_MANUAL.value == "youtube_manual"
        assert TranscriptProviderName.YOUTUBE_AUTO.value == "youtube_auto"
        assert TranscriptProviderName.FASTER_WHISPER.value == "faster_whisper"


class TestPipelineStep:
    def test_create_step(self):
        step = PipelineStep(name="Test", status="ok", detail="All good")
        assert step.name == "Test"
        assert step.status == "ok"
        assert step.detail == "All good"
        assert step.duration_seconds is None

    def test_step_with_duration(self):
        step = PipelineStep(name="Test", status="running", duration_seconds=3.5)
        assert step.duration_seconds == 3.5
