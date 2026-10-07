"""Base class for all transcript processing pipeline stages."""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from exceptions.processing_errors import ProcessorError
from models.processing_result import ProcessingStatus, ProcessingStep, ProcessingStepName

logger = logging.getLogger(__name__)


class BaseProcessor(ABC):
    """Abstract base for a single processing pipeline stage.

    Each processor is independently testable, stateless (or configurable
    via constructor), and operates on a ``ProcessingContext`` dict that
    flows through the pipeline.
    """

    step_name: ClassVar[ProcessingStepName]

    @abstractmethod
    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute this processing stage.

        Args:
            context: Mutable dict shared across pipeline stages.
                     Expected keys include ``text``, ``segments``, ``timestamps``,
                     ``language``, ``statistics``, etc.

        Returns:
            Updated context dict.

        Raises:
            ProcessorError: On unrecoverable processing failure.
        """

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        """Wrap ``process()`` with timing, logging, and error handling.

        Args:
            context: Pipeline context dict.

        Returns:
            Updated context with step result appended.
        """
        step = ProcessingStep(name=self.step_name, status=ProcessingStatus.RUNNING)
        request_id = uuid.uuid4().hex[:8]
        start = time.time()

        logger.info("[%s] Stage %s started", request_id, self.step_name.value)

        try:
            context = self.process(context)
            elapsed = (time.time() - start) * 1000
            step.status = ProcessingStatus.OK
            step.duration_ms = round(elapsed, 1)
            step.detail = _summarize(context)
            logger.info(
                "[%s] Stage %s OK (%.1fms)",
                request_id, self.step_name.value, elapsed,
            )
        except ProcessorError as exc:
            elapsed = (time.time() - start) * 1000
            step.status = ProcessingStatus.ERROR
            step.duration_ms = round(elapsed, 1)
            step.detail = str(exc)
            logger.error(
                "[%s] Stage %s FAILED (%.1fms): %s",
                request_id, self.step_name.value, elapsed, exc,
            )
        except Exception as exc:
            elapsed = (time.time() - start) * 1000
            step.status = ProcessingStatus.ERROR
            step.duration_ms = round(elapsed, 1)
            step.detail = f"Unexpected error: {exc}"
            logger.exception(
                "[%s] Stage %s UNEXPECTED ERROR (%.1fms)",
                request_id, self.step_name.value, elapsed,
            )

        context.setdefault("steps", []).append(step)
        return context


def _summarize(context: dict[str, Any]) -> str:
    text = context.get("text", "")
    word_count = len(text.split()) if text else 0
    return f"{word_count} words"
