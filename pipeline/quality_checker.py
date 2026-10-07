"""Quality checker — validates processed transcript quality."""
import re
from typing import Any

from models.processing_result import ProcessingFlags, ProcessingStepName
from pipeline.base_processor import BaseProcessor


class QualityChecker(BaseProcessor):
    step_name = ProcessingStepName.CALCULATE_METRICS

    _REPEATED_WORDS = re.compile(r"\b(\w{3,})\s+\1\b", re.IGNORECASE)
    _BROKEN_SENTENCE = re.compile(r"^[a-z]")
    _MALFORMED_PUNCT = re.compile(r"[.!?]{4,}")
    _EXCESSIVE_WS = re.compile(r" {3,}")
    _CORRUPTED = re.compile(r"[^\x20-\x7E\u00A0-\uFFFF]")

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        flags = context.get("flags") or ProcessingFlags()

        issues: list[str] = []

        if self._CORRUPTED.search(text):
            issues.append("corrupted_chars")

        dupes = self._REPEATED_WORDS.findall(text)
        if dupes:
            context["text"] = self._REPEATED_WORDS.sub(r"\1", text)
            text = context["text"]

        broken = self._BROKEN_SENTENCE.findall(text)
        malformed = self._MALFORMED_PUNCT.findall(text)
        excessive_ws = self._EXCESSIVE_WS.findall(text)

        quality_pass = len(issues) == 0
        flags.quality_passed = quality_pass

        if not quality_pass:
            for issue in issues:
                context.setdefault("quality_issues", []).append(issue)

        context["flags"] = flags
        return context
