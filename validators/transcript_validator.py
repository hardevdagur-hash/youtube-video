"""Input validation for raw transcripts before processing."""

import logging
from collections.abc import Sequence
from typing import Any

from exceptions.processing_errors import (
    EmptyTranscriptError,
    ProcessingLimitError,
    ValidationError,
)
from models.transcript import TranscriptSegment

logger = logging.getLogger(__name__)

_MAX_TRANSCRIPT_WORDS = 200_000
_MAX_SEGMENTS = 50_000
_MIN_SEGMENTS = 1


def _seg_text(seg: TranscriptSegment | dict[str, Any]) -> str:
    return seg.text if isinstance(seg, TranscriptSegment) else seg.get("text", "")


def _seg_start(seg: TranscriptSegment | dict[str, Any]) -> float:
    return seg.start if isinstance(seg, TranscriptSegment) else seg.get("start", 0)


def _seg_duration(seg: TranscriptSegment | dict[str, Any]) -> float:
    return seg.duration if isinstance(seg, TranscriptSegment) else seg.get("duration", 0)


def validate_segments(segments: Sequence[TranscriptSegment] | Sequence[dict[str, Any]]) -> None:
    if not segments:
        raise EmptyTranscriptError("Transcript has no segments.")
    if len(segments) > _MAX_SEGMENTS:
        raise ProcessingLimitError(
            f"Transcript exceeds maximum segment count "
            f"({len(segments)} > {_MAX_SEGMENTS})."
        )
    for i, seg in enumerate(segments):
        text = _seg_text(seg)
        if not text or not text.strip():
            continue
        start = _seg_start(seg)
        if start < 0:
            raise ValidationError(f"Segment {i} has negative start time: {start}.")
        duration = _seg_duration(seg)
        if duration < 0:
            raise ValidationError(f"Segment {i} has negative duration: {duration}.")


def validate_text(text: str) -> None:
    if not text or not text.strip():
        raise EmptyTranscriptError("Transcript text is empty.")
    word_count = len(text.split())
    if word_count > _MAX_TRANSCRIPT_WORDS:
        raise ProcessingLimitError(
            f"Transcript exceeds maximum word count "
            f"({word_count} > {_MAX_TRANSCRIPT_WORDS})."
        )


def validate_video_id(video_id: str) -> None:
    if not video_id or not isinstance(video_id, str):
        raise ValidationError("Video ID must be a non-empty string.")
    if len(video_id) != 11:
        raise ValidationError(
            f"Invalid video ID '{video_id}'. Must be exactly 11 characters."
        )
