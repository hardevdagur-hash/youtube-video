"""Language detection for transcript text.

Uses lightweight heuristics and optionally langdetect library.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    from langdetect import DetectorFactory, LangDetectException
    from langdetect import detect as langdetect_detect
    DetectorFactory.seed = 42
    _HANG_LANGDETECT = True
except ImportError:
    _HANG_LANGDETECT = False


@dataclass
class LanguageInfo:
    language: str
    confidence: float | None = None


class LanguageDetector:
    """Detect language of transcript text.

    Uses langdetect library when available, falls back to
    character-based heuristics for common languages.
    """

    def __init__(self) -> None:
        self._has_langdetect = _HANG_LANGDETECT

    def detect(self, text: str) -> LanguageInfo | None:
        """Detect the language of a text string.

        Args:
            text: Text to analyze (minimum ~50 characters for accuracy).

        Returns:
            ``LanguageInfo`` with ISO language code and optional confidence,
            or None if detection fails.
        """
        if not text or len(text.strip()) < 10:
            return None

        if self._has_langdetect:
            try:
                lang = langdetect_detect(text)
                return LanguageInfo(language=lang, confidence=None)
            except LangDetectException as exc:
                logger.debug("Language detection failed: %s", exc)
                return None

        # Fallback: basic character-based detection
        return self._heuristic_detect(text)

    @staticmethod
    def _heuristic_detect(text: str) -> LanguageInfo | None:
        """Basic heuristic language detection based on character ranges."""
        if not text:
            return None

        # Count characters in various Unicode ranges
        total = max(len(text), 1)
        latin = sum(1 for c in text if 'A' <= c <= 'Z' or 'a' <= c <= 'z')
        cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
        cyrillic = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
        arabic = sum(1 for c in text if '\u0600' <= c <= '\u06ff')
        devanagari = sum(1 for c in text if '\u0900' <= c <= '\u097f')
        korean = sum(1 for c in text if '\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff')

        ratio_latin = latin / total

        if cjk / total > 0.1:
            return LanguageInfo("zh", confidence=ratio_latin)
        if korean / total > 0.1:
            return LanguageInfo("ko", confidence=ratio_latin)
        if cyrillic / total > 0.1:
            return LanguageInfo("ru", confidence=ratio_latin)
        if arabic / total > 0.1:
            return LanguageInfo("ar", confidence=ratio_latin)
        if devanagari / total > 0.1:
            return LanguageInfo("hi", confidence=ratio_latin)
        if ratio_latin > 0.8:
            return LanguageInfo("en", confidence=ratio_latin)

        return LanguageInfo("en", confidence=0.5)
