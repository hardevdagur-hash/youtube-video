"""Punctuation restoration processor."""
import re
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor


class PunctuationProcessor(BaseProcessor):
    step_name = ProcessingStepName.FIX_PUNCTUATION

    _SPACE_BEFORE = re.compile(r"\s+([.,!?;:])")
    _SPACE_AFTER_OPEN = re.compile("([\\[{\"'(-])\\s+")
    _SPACE_BEFORE_CLOSE = re.compile("\\s+([\\]}\"')-])")
    _MISSING_PERIOD = re.compile(r"([a-zA-Z0-9])\n([A-Z])")
    _MISSING_SPACE = re.compile(r"([.!?])([A-Za-z])")
    _ELLIPSIS = re.compile(r"\.{3,}")
    _MULTI_PUNCT = re.compile(r"([!?]){2,}")
    _RUN_ON_COMMA = re.compile(r",\s*,+")

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            return context
        text = self._SPACE_BEFORE.sub(r"\1", text)
        text = self._SPACE_AFTER_OPEN.sub(r"\1 ", text)
        text = self._SPACE_BEFORE_CLOSE.sub(r"\1", text)
        text = self._MISSING_PERIOD.sub(r"\1.\n\2", text)
        text = self._MISSING_SPACE.sub(r"\1 \2", text)
        text = self._ELLIPSIS.sub("...", text)
        text = self._MULTI_PUNCT.sub(r"\1\1", text)
        text = self._RUN_ON_COMMA.sub(", ", text)
        context["text"] = text.strip()
        return context
