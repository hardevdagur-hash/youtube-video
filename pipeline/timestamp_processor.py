"""Timestamp removal processor."""
import re
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor

_TIMESTAMP_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b"
)
_TIMESTAMP_MS_RE = re.compile(
    r"\b\d{1,2}:\d{2}:\d{2}\.\d{1,3}\b"
)
_CHAPTER_RE = re.compile(
    r"^\d{1,2}:\d{2}\s+-+\s+.*$", re.MULTILINE
)


class TimestampProcessor(BaseProcessor):
    step_name = ProcessingStepName.REMOVE_INVALID_CHARS

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            return context
        text = _CHAPTER_RE.sub("", text)
        text = _TIMESTAMP_MS_RE.sub("", text)
        text = _TIMESTAMP_RE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        context["text"] = text
        return context
