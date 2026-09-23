"""Transcript Processing Service — main entry point for Phase 5.

Transforms raw transcript segments into clean, AI-ready content
through a multi-stage processing pipeline.

This is the canonical preprocessing layer for transcripts:
punctuation restoration, capitalization correction, deduplication, and formatting.
"""

import logging
import time
import uuid
from typing import Any

from models.transcript import TranscriptSegment, TranscriptResult
from models.processing_result import (
    ProcessingResult,
    ProcessingStatistics,
    ProcessingFlags,
    ProcessingStep,
    ProcessingStatus,
    LanguageDistribution,
    ProcessedTimestamp,
)
from pipeline.processing_pipeline import ProcessingPipeline
from validators.transcript_validator import validate_segments, validate_video_id
from exceptions.processing_errors import ValidationError, ProcessingError
from utils.text_utils import split_sentences

logger = logging.getLogger(__name__)


class TranscriptProcessor:
    """Processes raw transcripts into AI-ready content.

    Usage::

        processor = TranscriptProcessor()
        result = processor.process(segments, video_id="dQw4w9WgXcQ")
        print(result.clean_transcript[:200])
    """

    def __init__(self, pipeline: ProcessingPipeline | None = None) -> None:
        self._pipeline = pipeline or ProcessingPipeline()

    def process(
        self,
        segments: list[dict[str, Any]] | list[TranscriptSegment],
        video_id: str = "",
        remove_fillers: bool = False,
    ) -> ProcessingResult:
        """Process raw transcript segments into clean, structured AI-ready content.

        Args:
            segments: Raw transcript segments (dicts or TranscriptSegment objects).
            video_id: YouTube video ID.
            remove_fillers: Whether to remove filler words.

        Returns:
            ``ProcessingResult`` with clean transcript, paragraphs, statistics, etc.
        """
        overall_start = time.time()
        rid = uuid.uuid4().hex[:8]
        logger.info("[%s] TranscriptProcessor started: video_id=%s", rid, video_id)

        try:
            validate_video_id(video_id)
            raw_segments = self._normalize_segments(segments)
            validate_segments(raw_segments)
        except (ValidationError, ProcessingError) as exc:
            logger.error("[%s] Validation failed: %s", rid, exc)
            return ProcessingResult(
                success=False,
                video_id=video_id,
                error=str(exc),
                processing_time_ms=round((time.time() - overall_start) * 1000, 1),
            )

        try:
            pipeline_context = self._pipeline.run(
                segments=raw_segments,
                video_id=video_id,
                remove_fillers=remove_fillers,
            )
        except ProcessingError as exc:
            logger.error("[%s] Pipeline failed: %s", rid, exc)
            return ProcessingResult(
                success=False,
                video_id=video_id,
                error=str(exc),
                processing_time_ms=round((time.time() - overall_start) * 1000, 1),
            )

        elapsed = round((time.time() - overall_start) * 1000, 1)
        result = self._build_result(pipeline_context, video_id, elapsed)
        logger.info(
            "[%s] Processing complete: %d words, %.1fms",
            rid, result.statistics.word_count, elapsed,
        )
        return result

    @staticmethod
    def _normalize_segments(
        segments: list[dict[str, Any]] | list[TranscriptSegment],
    ) -> list[dict[str, Any]]:
        if not segments:
            return []
        if isinstance(segments[0], TranscriptSegment):
            return [
                {
                    "text": seg.text,
                    "start": seg.start,
                    "duration": seg.duration,
                    "end": seg.end,
                }
                for seg in segments
            ]
        return [
            {
                "text": seg.get("text", ""),
                "start": float(seg.get("start", 0)),
                "duration": float(seg.get("duration", 0)),
                "end": float(seg.get("start", 0)) + float(seg.get("duration", 0)),
            }
            for seg in segments
        ]

    @staticmethod
    def _build_result(
        context: dict[str, Any],
        video_id: str,
        elapsed_ms: float,
    ) -> ProcessingResult:
        text = context.get("text", "")
        paragraphs = context.get("paragraphs", []) or ([text] if text else [])
        raw_stats = context.get("statistics", {})
        lang_data = context.get("language")
        raw_steps = context.get("steps", [])
        flags = context.get("flags") or ProcessingFlags()

        sentences = split_sentences(text)
        stats = ProcessingStatistics(**raw_stats)

        language = None
        if lang_data and isinstance(lang_data, dict):
            language = LanguageDistribution(**lang_data)
        elif lang_data and isinstance(lang_data, LanguageDistribution):
            language = lang_data

        steps = []
        for s in raw_steps:
            if isinstance(s, ProcessingStep):
                steps.append(s)
            elif isinstance(s, dict):
                try:
                    steps.append(ProcessingStep(**s))
                except Exception as exc:
                    logger.warning("Failed to construct ProcessingStep: %s", exc)

        timestamps = [
            ProcessedTimestamp(
                original_start=seg.get("start", 0),
                original_end=seg.get("end", 0),
                original_text=seg.get("text", ""),
                cleaned_text=seg.get("text", ""),
            )
            for seg in context.get("segments", [])
        ]

        return ProcessingResult(
            success=True,
            video_id=video_id,
            language=language,
            statistics=stats,
            clean_transcript=text,
            paragraphs=paragraphs,
            sentences=sentences,
            processing_steps=steps,
            timestamps=timestamps,
            flags=flags,
            processing_time_ms=elapsed_ms,
        )
