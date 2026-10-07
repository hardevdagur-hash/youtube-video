"""Paragraph detection processor — splits transcript into logical paragraphs."""
import re
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor


class ParagraphProcessor(BaseProcessor):
    step_name = ProcessingStepName.DETECT_PARAGRAPHS

    _PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
    _TOPIC_SHIFT = re.compile(
        r"\b(now|so|next|anyway|meanwhile|however|therefore|"
        r"finally|firstly|secondly|additionally|furthermore|"
        r"in conclusion|to begin with|moving on|"
        r"let's talk about|another thing|"
        r"that brings us to|speaking of)\b",
        re.IGNORECASE,
    )
    _MAX_PARAGRAPH_SENTENCES = 8
    _MIN_PARAGRAPH_SENTENCES = 2

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            context["paragraphs"] = context.get("paragraphs", [])
            return context

        paragraphs = self._split_paragraphs(text)
        context["paragraphs"] = paragraphs
        context["text"] = "\n\n".join(paragraphs)
        return context

    def _split_paragraphs(self, text: str) -> list[str]:
        candidates = self._get_candidates(text)
        if len(candidates) <= 1:
            return candidates

        merged = self._merge_short(candidates)
        merged = self._split_long(merged)
        return [p.strip() for p in merged if p.strip()]

    def _get_candidates(self, text: str) -> list[str]:
        if self._PARAGRAPH_BREAK.search(text):
            return self._PARAGRAPH_BREAK.split(text)

        sentences = re.split(r"(?<=[.!?])\s+", text)
        sentences = [s.strip() for s in sentences if s.strip()]

        candidates: list[str] = []
        buffer: list[str] = []
        for s in sentences:
            buffer.append(s)
            if self._is_topic_shift(s) and len(buffer) >= self._MIN_PARAGRAPH_SENTENCES:
                candidates.append(" ".join(buffer))
                buffer = []
        if buffer:
            candidates.append(" ".join(buffer))
        return candidates or [text]

    @staticmethod
    def _is_topic_shift(sentence: str) -> bool:
        return bool(ParagraphProcessor._TOPIC_SHIFT.search(sentence))

    def _merge_short(self, paragraphs: list[str]) -> list[str]:
        result: list[str] = []
        for p in paragraphs:
            s_count = len(re.findall(r"[.!?]+", p))
            if result and s_count < self._MIN_PARAGRAPH_SENTENCES:
                result[-1] = result[-1] + " " + p
            else:
                result.append(p)
        return result

    def _split_long(self, paragraphs: list[str]) -> list[str]:
        result: list[str] = []
        for p in paragraphs:
            sentences = re.split(r"(?<=[.!?])\s+", p)
            if len(sentences) > self._MAX_PARAGRAPH_SENTENCES:
                mid = len(sentences) // 2
                result.append(" ".join(sentences[:mid]))
                result.append(" ".join(sentences[mid:]))
            else:
                result.append(p)
        return result
