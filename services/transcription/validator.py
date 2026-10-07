"""Transcript quality validation and hallucination detection."""

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class TranscriptValidationError(Exception):
    """Base error for transcript quality failures."""

    def __init__(self, message: str, error_code: str = "TRANSCRIPT_QUALITY_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class TranscriptEmptyError(TranscriptValidationError):
    def __init__(self, message: str = "Transcript is empty or contains only whitespace.") -> None:
        super().__init__(message, error_code="TRANSCRIPT_EMPTY")


@dataclass
class ValidationReport:
    is_valid: bool
    word_count: int
    character_count: int
    confidence: float
    issues: list[str] = field(default_factory=list)


class TranscriptValidator:
    """Automated quality and hallucination checks on transcripts."""

    def validate(
        self,
        text: str,
        segments: list[dict[str, Any]] | None = None,
        duration_seconds: float | None = None,
    ) -> ValidationReport:
        """Validate transcript text and segments.

        Args:
            text: Full text of the transcript.
            segments: Optional list of segment dicts with start, end, text.
            duration_seconds: Optional duration of video in seconds.

        Returns:
            ValidationReport with confidence score and detected issues.

        Raises:
            TranscriptEmptyError: If transcript has no words.
            TranscriptValidationError: If transcript suffers from severe degenerate hallucination.
        """
        stripped = (text or "").strip()
        if not stripped:
            raise TranscriptEmptyError("Transcript contains no text.")

        words = stripped.split()
        word_count = len(words)
        char_count = len(stripped)

        if word_count == 0:
            raise TranscriptEmptyError("Transcript contains 0 words.")

        issues: list[str] = []

        # 1. Extreme brevity check
        if word_count < 3 and (duration_seconds is None or duration_seconds > 30):
            issues.append("Transcript contains fewer than 3 words for video.")

        # 2. ASR hallucination / repetition loop detection
        # Check if the same phrase or line repeats 5+ times consecutively
        hallucination_detected = self._detect_repetition_loops(words)
        if hallucination_detected:
            issues.append(f"Severe repetitive loop detected: {hallucination_detected}")
            raise TranscriptValidationError(
                f"Transcript quality failed: repetitive ASR hallucination loop detected ({hallucination_detected}).",
                error_code="TRANSCRIPT_QUALITY_FAILED",
            )

        # 3. Timestamp sanity checks
        if segments:
            for idx, seg in enumerate(segments):
                start = float(seg.get("start", 0.0))
                end = float(seg.get("end", start))
                if end < start:
                    issues.append(f"Segment {idx} has invalid timestamps: start={start}, end={end}")

        # Compute confidence score
        confidence = 0.95
        if issues:
            confidence = max(0.4, 0.95 - (0.15 * len(issues)))

        return ValidationReport(
            is_valid=True,
            word_count=word_count,
            character_count=char_count,
            confidence=round(confidence, 2),
            issues=issues,
        )

    def _detect_repetition_loops(self, words: list[str]) -> str | None:
        """Detect degenerate ASR repetition loops (e.g. same 3-word n-gram repeated 5+ times)."""
        if len(words) < 15:
            return None

        # Check single word repeating > 8 times in a row
        current_word = None
        run_length = 0
        for w in words:
            wl = w.lower()
            if wl == current_word:
                run_length += 1
                if run_length >= 8:
                    return f"word '{current_word}' repeated {run_length} times"
            else:
                current_word = wl
                run_length = 1

        # Check 2-word, 3-word, 4-word n-grams repeated >= 5 times consecutively
        for n in (2, 3, 4, 5):
            if len(words) < n * 5:
                continue
            for i in range(len(words) - (n * 5) + 1):
                ngram = tuple(w.lower() for w in words[i : i + n])
                consecutive = 1
                pos = i + n
                while pos + n <= len(words):
                    next_ngram = tuple(w.lower() for w in words[pos : pos + n])
                    if next_ngram == ngram:
                        consecutive += 1
                        pos += n
                    else:
                        break
                if consecutive >= 5:
                    return f"phrase '{' '.join(ngram)}' repeated {consecutive} times"

        return None
