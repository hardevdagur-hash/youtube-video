"""Caption merging processor — merges fragmented captions into natural sentences."""
from typing import Any

from models.processing_result import ProcessingStepName
from pipeline.base_processor import BaseProcessor


class CaptionMerger(BaseProcessor):
    step_name = ProcessingStepName.MERGE_CAPTIONS

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        segments = context.get("segments", [])
        if not segments:
            context["text"] = context.get("text", "")
            return context
        merged = self._merge(segments)
        context["text"] = merged
        return context

    @staticmethod
    def _merge(segments: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for seg in segments:
            text = seg.get("text", "").strip()
            if not text:
                continue
            if parts and not text[0].isupper() and not text.startswith(("“", '"', "'", "(")):
                parts[-1] = parts[-1] + " " + text
            else:
                parts.append(text)
        return " ".join(parts)
