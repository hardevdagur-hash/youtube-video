"""Filler word detection and optional removal processor."""
import re
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor

_FILLER_WORDS: set[str] = {
    "um", "uh", "ah", "er", "hmm",
    "like", "you know", "i mean", "sort of", "kind of",
    "actually", "basically", "literally", "honestly",
    "well", "so", "anyway", "right", "okay",
    "you see", "you know what i mean", "i guess",
    "i think", "the thing is", "as i was saying",
    "in other words", "more or less",
}

_FILLER_PATTERNS = [
    re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE)
    for w in _FILLER_WORDS
]

_MULTI_WORD_FILLERS = sorted(
    [w for w in _FILLER_WORDS if " " in w],
    key=len, reverse=True,
)


class FillerProcessor(BaseProcessor):
    step_name = ProcessingStepName.REMOVE_INVALID_CHARS

    def __init__(self, remove_fillers: bool = False) -> None:
        self._remove_fillers = remove_fillers

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            context["filler_word_count"] = 0
            return context

        count = self._count_fillers(text)
        context["filler_word_count"] = count

        if self._remove_fillers:
            text = self._remove(text)
            text = re.sub(r" {2,}", " ", text).strip()
            context["text"] = text

        return context

    @staticmethod
    def _count_fillers(text: str) -> int:
        count = 0
        for pattern in _FILLER_PATTERNS:
            count += len(pattern.findall(text))
        return count

    @staticmethod
    def _remove(text: str) -> str:
        for filler in _MULTI_WORD_FILLERS:
            pattern = re.compile(rf"\b{re.escape(filler)}\b", re.IGNORECASE)
            text = pattern.sub("", text)
        text = re.sub(r"\b(um|uh|ah|er|hmm|like|you know|i mean|sort of|kind of|"
                       r"actually|basically|literally|honestly|well|so|anyway|"
                       r"right|okay|you see|i guess|i think|the thing is)\b",
                       "", text, flags=re.IGNORECASE)
        return re.sub(r" {2,}", " ", text).strip()
