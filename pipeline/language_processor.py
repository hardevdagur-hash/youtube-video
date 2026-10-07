"""Enhanced language detection processor with mixed-language support."""
import logging
from typing import Any

from models.processing_result import LanguageDistribution, ProcessingStepName
from pipeline.base_processor import BaseProcessor

logger = logging.getLogger(__name__)

try:
    from langdetect import DetectorFactory, LangDetectException
    from langdetect import detect as langdetect_detect
    DetectorFactory.seed = 42
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False


_DEVANAGARI_RANGE = range(0x0900, 0x0980)
_LATIN_RANGE = range(0x0041, 0x007B)
_CJK_RANGES = [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)]
_CYRILLIC_RANGE = range(0x0400, 0x0500)


class LanguageProcessor(BaseProcessor):
    step_name = ProcessingStepName.DETECT_LANGUAGE

    def process(self, context: dict[str, Any]) -> dict[str, Any]:
        text = context.get("text", "")
        if not text:
            return context

        lang_info = self._detect(text)
        context["language"] = lang_info.model_dump() if lang_info else None
        return context

    def _detect(self, text: str) -> LanguageDistribution | None:
        sample = text[:2000]
        if not sample.strip():
            return None

        script_ratios = self._script_analysis(sample)
        primary, primary_conf = self._primary_from_script(script_ratios)
        secondary, secondary_conf = self._secondary_from_script(script_ratios, primary)
        mixed_ratio = self._mixed_ratio(script_ratios)

        if _HAS_LANGDETECT:
            try:
                detected = langdetect_detect(sample)
                if detected and detected != primary:
                    secondary = detected
                    secondary_conf = 0.5
                primary_conf = max(primary_conf, 0.6)
            except LangDetectException:
                pass

        return LanguageDistribution(
            primary=primary or "en",
            secondary=secondary,
            primary_confidence=round(primary_conf, 2),
            secondary_confidence=round(secondary_conf, 2) if secondary_conf else None,
            mixed_ratio=round(mixed_ratio, 4) if mixed_ratio > 0.01 else None,
        )

    @staticmethod
    def _script_analysis(text: str) -> dict[str, float]:
        total = max(len(text), 1)
        devanagari = sum(1 for c in text if ord(c) in _DEVANAGARI_RANGE)
        latin = sum(1 for c in text if c.isascii() and c.isalpha())
        cjk = sum(
            1 for c in text
            if any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES)
        )
        cyrillic = sum(1 for c in text if ord(c) in _CYRILLIC_RANGE)
        return {
            "devanagari": devanagari / total,
            "latin": latin / total,
            "cjk": cjk / total,
            "cyrillic": cyrillic / total,
        }

    @staticmethod
    def _primary_from_script(ratios: dict[str, float]) -> tuple[str, float]:
        if ratios["devanagari"] > 0.1:
            return ("hi", ratios["devanagari"])
        if ratios["cjk"] > 0.1:
            return ("zh", ratios["cjk"])
        if ratios["cyrillic"] > 0.1:
            return ("ru", ratios["cyrillic"])
        return ("en", ratios["latin"])

    @staticmethod
    def _secondary_from_script(
        ratios: dict[str, float], primary: str,
    ) -> tuple[str | None, float | None]:
        candidates = {
            "hi": ratios["devanagari"],
            "en": ratios["latin"],
        }
        candidates.pop(primary, None)
        for lang, ratio in sorted(candidates.items(), key=lambda x: -x[1]):
            if ratio > 0.05:
                return (lang, ratio)
        return (None, None)

    @staticmethod
    def _mixed_ratio(ratios: dict[str, float]) -> float:
        sorted_ratios = sorted(ratios.values(), reverse=True)
        if len(sorted_ratios) < 2:
            return 0.0
        return sorted_ratios[1] / max(sorted_ratios[0], 0.001)
