from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TranscriptSource(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"
    WHISPER = "whisper"


class TranscriptProviderName(str, Enum):
    YOUTUBE_MANUAL = "youtube_manual"
    YOUTUBE_AUTO = "youtube_auto"
    FASTER_WHISPER = "faster_whisper"
    YOUTUBE_CAPTIONS = "youtube_captions"
    GROQ_WHISPER_LARGE_V3 = "groq_whisper_large_v3"
    CACHE = "cache"


class TranscriptSegment(BaseModel):
    start: float = Field(..., description="Start time in seconds")
    end: float = Field(..., description="End time in seconds")
    duration: float = Field(..., description="Duration in seconds")
    text: str = Field(..., description="Segment text")


class WhisperProcessingInfo(BaseModel):
    model_name: str = Field(default="base", description="Whisper model used")
    transcription_duration_seconds: float | None = Field(default=None)
    audio_duration_seconds: float | None = Field(default=None)
    processing_time_seconds: float | None = Field(default=None)
    language_detected: str | None = Field(default=None)
    language_confidence: float | None = Field(default=None)
    word_timestamps: bool = Field(default=False)
    audio_download_time_seconds: float | None = Field(default=None)
    # "transcribe" = verbatim spoken language. None = produced before this field existed,
    # when Whisper ran with task="translate" (English output), so it is not the original.
    task: str | None = Field(default=None, description="Whisper task used: transcribe | translate")


class PipelineStep(BaseModel):
    name: str = Field(..., description="Step name")
    status: str = Field(..., description="pending | running | ok | error | skipped")
    detail: str = Field(default="", description="Optional detail message")
    duration_seconds: float | None = Field(default=None)


class TranscriptResult(BaseModel):
    success: bool = Field(default=True)
    video_id: str
    source: TranscriptSource = Field(default=TranscriptSource.MANUAL)
    provider: str = Field(default="youtube_captions")
    language: str = Field(default="en")
    language_confidence: float | None = Field(default=None)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    plain_text: str = Field(default="")
    raw_transcript: str = Field(default="", description="Canonical immutable source transcript verbatim from YouTube/STT")
    source_language: str | None = Field(default=None, description="Actual spoken source language")
    source_language_code: str | None = Field(default=None, description="Source language ISO code")
    output_format: str = Field(default="original_spoken", description="Output representation mode")
    simple_english_transcript: str = Field(default="", description="Derived Simple English representation")
    simple_hindi_transcript: str = Field(default="", description="Derived Simple Hindi representation")
    paragraph_text: str = Field(default="")
    word_count: int = Field(default=0)
    character_count: int = Field(default=0)
    estimated_read_time: str = Field(default="")
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    duration_seconds: float | None = Field(default=None)
    whisper_info: WhisperProcessingInfo | None = Field(default=None)
    pipeline_steps: list[PipelineStep] = Field(default_factory=list)
    available_languages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of available transcript language metadata",
    )
    translation_source: str | None = Field(
        default=None,
        description="Original language code if this result was translated",
    )
    error: str | None = Field(default=None)
    error_code: str | None = Field(default=None, description="Machine-readable error code")
    method: str = Field(default="youtube_transcript", description="Extraction method used")
