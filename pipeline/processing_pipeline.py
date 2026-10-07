"""Processing pipeline orchestrator — sequences and runs all processors."""

import logging
import uuid
from typing import Any

from models.processing_result import ProcessingStatus, ProcessingStep, ProcessingStepName
from pipeline.base_processor import BaseProcessor
from pipeline.capitalization_processor import CapitalizationProcessor
from pipeline.caption_merger import CaptionMerger
from pipeline.filler_processor import FillerProcessor
from pipeline.language_processor import LanguageProcessor
from pipeline.paragraph_processor import ParagraphProcessor
from pipeline.punctuation_processor import PunctuationProcessor
from pipeline.quality_checker import QualityChecker
from pipeline.timestamp_processor import TimestampProcessor
from utils.metrics import compute_statistics
from utils.text_utils import (
    normalize_whitespace,
    remove_empty_lines,
    remove_repeated_lines,
)
from utils.unicode_utils import normalize_unicode

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """Orchestrates the transcript processing pipeline.

    Runs each processor in sequence with logging, timing, error isolation,
    and automatic step recording.
    """

    def __init__(self, remove_fillers: bool = False) -> None:
        self._processors: list[BaseProcessor] = [
            TimestampProcessor(),
            CaptionMerger(),
            PunctuationProcessor(),
            CapitalizationProcessor(),
            ParagraphProcessor(),
            FillerProcessor(remove_fillers=remove_fillers),
            LanguageProcessor(),
            QualityChecker(),
        ]

    def run(
        self,
        segments: list[dict[str, Any]],
        video_id: str = "",
        remove_fillers: bool = False,
    ) -> dict[str, Any]:
        """Execute the full processing pipeline.

        Args:
            segments: List of transcript segment dicts with ``text``, ``start``, ``duration``.
            video_id: YouTube video ID.
            remove_fillers: Whether to remove filler words.

        Returns:
            Context dict with all processing results.
        """
        pid = uuid.uuid4().hex[:8]
        logger.info("[%s] Pipeline started: %d segments", pid, len(segments))

        # Build initial context
        text = self._build_initial_text(segments)
        context: dict[str, Any] = {
            "segments": segments,
            "text": text,
            "video_id": video_id,
            "steps": [],
            "flags": None,
            "language": None,
            "paragraphs": [],
            "filler_word_count": 0,
        }

        # --- Pre-processing steps ---
        context = self._preprocess(context)

        # --- Run registered processors ---
        for processor in self._processors:
            if isinstance(processor, FillerProcessor):
                processor = FillerProcessor(remove_fillers=remove_fillers)
            context = processor.execute(context)

        # --- Post-processing steps ---
        context = self._postprocess(context)

        logger.info(
            "[%s] Pipeline complete: %d words, %d steps",
            pid,
            len(context.get("text", "").split()),
            len(context.get("steps", [])),
        )
        return context

    @staticmethod
    def _build_initial_text(segments: list[dict[str, Any]]) -> str:
        return "\n".join(
            seg.get("text", "").strip()
            for seg in segments
            if seg.get("text", "").strip()
        )

    @staticmethod
    def _preprocess(context: dict[str, Any]) -> dict[str, Any]:
        steps: list[ProcessingStep] = []

        # Normalize Unicode
        step = ProcessingStep(
            name=ProcessingStepName.NORMALIZE_UNICODE,
            status=ProcessingStatus.RUNNING,
        )
        try:
            context["text"] = normalize_unicode(context.get("text", ""))
            step.status = ProcessingStatus.OK
            step.detail = "NFC normalization"
        except Exception as exc:
            step.status = ProcessingStatus.ERROR
            step.detail = str(exc)
        steps.append(step)

        # Remove empty lines
        step = ProcessingStep(
            name=ProcessingStepName.REMOVE_EMPTY,
            status=ProcessingStatus.RUNNING,
        )
        try:
            context["text"] = remove_empty_lines(context.get("text", ""))
            step.status = ProcessingStatus.OK
        except Exception as exc:
            step.status = ProcessingStatus.ERROR
            step.detail = str(exc)
        steps.append(step)

        # Deduplicate
        step = ProcessingStep(
            name=ProcessingStepName.DEDUPLICATE,
            status=ProcessingStatus.RUNNING,
        )
        try:
            context["text"] = remove_repeated_lines(context.get("text", ""))
            step.status = ProcessingStatus.OK
        except Exception as exc:
            step.status = ProcessingStatus.ERROR
            step.detail = str(exc)
        steps.append(step)

        # Normalize whitespace
        step = ProcessingStep(
            name=ProcessingStepName.NORMALIZE_WHITESPACE,
            status=ProcessingStatus.RUNNING,
        )
        try:
            context["text"] = normalize_whitespace(context.get("text", ""))
            step.status = ProcessingStatus.OK
        except Exception as exc:
            step.status = ProcessingStatus.ERROR
            step.detail = str(exc)
        steps.append(step)

        context.setdefault("steps", []).extend(steps)
        return context

    @staticmethod
    def _postprocess(context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        paragraphs = context.get("paragraphs", []) or [text]
        filler_count = context.get("filler_word_count", 0)
        stats = compute_statistics(text, paragraphs, filler_count)

        step = ProcessingStep(
            name=ProcessingStepName.CALCULATE_METRICS,
            status=ProcessingStatus.OK,
            detail=f"{stats['word_count']} words, {stats['sentence_count']} sentences",
        )
        context.setdefault("steps", []).append(step)
        context["statistics"] = stats
        return context
