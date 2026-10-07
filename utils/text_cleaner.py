"""Text normalization and cleanup for transcript segments.

Cleans transcript text without hallucinating or rewriting content.
Preserves original meaning and ordering.
"""

import re
import unicodedata
from collections.abc import Sequence

from models.transcript import TranscriptSegment


class TextCleaner:
    """Normalizes transcript text.

    Removes:
        - Duplicate spaces
        - Broken punctuation
        - Invalid Unicode
        - Malformed characters

    Normalizes:
        - Line breaks
        - Spacing
        - Quotation marks
        - Sentence endings
    """

    # Control characters to remove (keep newlines, tabs, spaces)
    _CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

    # Multiple spaces
    _MULTI_SPACE = re.compile(r" {2,}")

    # Multiple consecutive punctuation (keep ellipsis)
    _MULTI_DOT = re.compile(r"\.{4,}")
    _MULTI_EXCLAMATION = re.compile(r"!{3,}")
    _MULTI_QUESTION = re.compile(r"\?{3,}")

    # Spaces before punctuation
    _SPACE_BEFORE_PERIOD = re.compile(r"\s+\.")
    _SPACE_BEFORE_COMMA = re.compile(r"\s+,")
    _SPACE_BEFORE_EXCLAMATION = re.compile(r"\s+!")
    _SPACE_BEFORE_QUESTION = re.compile(r"\s+\?")

    # Spaces after punctuation (only single punctuation, not part of ellipsis/double)
    _SPACE_AFTER_PERIOD = re.compile(r"(?<!\.)\.(?=[a-zA-Z])")
    _SPACE_AFTER_COMMA = re.compile(r",(?=[a-zA-Z])")
    _SPACE_AFTER_EXCLAMATION = re.compile(r"!(?=[a-zA-Z])")
    _SPACE_AFTER_QUESTION = re.compile(r"\?(?=[a-zA-Z])")

    # Spaces after opening and before closing quotes
    _QUOTE_SPACES = re.compile(r'[""\u201c\u201d\u201e\u201f]\s+|\s+[""\u201c\u201d\u201e\u201f]')

    # Zero-width characters
    _ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064]")

    def clean_text(self, text: str) -> str:
        """Clean and normalize a single text string.

        Args:
            text: Raw text to clean.

        Returns:
            Normalized text string.
        """
        if not text:
            return ""

        # Normalize Unicode to NFC form
        text = unicodedata.normalize("NFC", text)

        # Remove zero-width and control characters
        text = self._ZERO_WIDTH.sub("", text)
        text = self._CONTROL_CHARS.sub("", text)

        # Normalize whitespace
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\t", " ")

        # Remove duplicate spaces
        text = self._MULTI_SPACE.sub(" ", text)

        # Normalize repeated punctuation first
        text = self._MULTI_DOT.sub("...", text)
        text = self._MULTI_EXCLAMATION.sub("!!", text)
        text = self._MULTI_QUESTION.sub("??", text)

        # Fix spacing before punctuation
        text = self._SPACE_BEFORE_PERIOD.sub(".", text)
        text = self._SPACE_BEFORE_COMMA.sub(",", text)
        text = self._SPACE_BEFORE_EXCLAMATION.sub("!", text)
        text = self._SPACE_BEFORE_QUESTION.sub("?", text)

        # Fix spacing after punctuation
        text = self._SPACE_AFTER_PERIOD.sub(r". ", text)
        text = self._SPACE_AFTER_COMMA.sub(r", ", text)
        text = self._SPACE_AFTER_EXCLAMATION.sub(r"! ", text)
        text = self._SPACE_AFTER_QUESTION.sub(r"? ", text)

        # Fix quotation mark spacing
        text = self._QUOTE_SPACES.sub("", text)

        # Normalize quotation marks to standard form
        text = text.replace("\u201c", '"').replace("\u201d", '"')
        text = text.replace("\u201e", '"').replace("\u201f", '"')
        text = text.replace("\u2018", "'").replace("\u2019", "'")
        text = text.replace("\u201a", "'").replace("\u201b", "'")

        # Normalize dashes
        text = text.replace("\u2013", "-").replace("\u2014", " — ")

        # Ensure sentence endings have proper spacing
        text = re.sub(r"\.([A-Z])", r". \1", text)
        text = re.sub(r"\!([A-Z])", r"! \1", text)
        text = re.sub(r"\?([A-Z])", r"? \1", text)

        return text.strip()

    def clean_segments(
        self, segments: Sequence[TranscriptSegment]
    ) -> list[TranscriptSegment]:
        """Clean all segments in a transcript.

        Args:
            segments: List of transcript segments.

        Returns:
            New list of cleaned segments (original ordering preserved).
        """
        return [
            TranscriptSegment(
                start=seg.start,
                end=seg.end,
                duration=seg.duration,
                text=self.clean_text(seg.text),
            )
            for seg in segments
        ]

    def build_paragraphs(self, segments: Sequence[TranscriptSegment]) -> str:
        """Build paragraph text from timestamped segments.

        Groups segments by time gaps to form paragraphs.
        A gap > 2.0 seconds between segment ends indicates a new paragraph.

        Args:
            segments: List of transcript segments.

        Returns:
            Paragraph-formatted text string.
        """
        if not segments:
            return ""

        paragraphs: list[str] = []
        current_paragraph: list[str] = []

        for i, seg in enumerate(segments):
            text = seg.text.strip()
            if not text:
                continue

            # Check for paragraph break: significant time gap
            if (
                current_paragraph
                and i > 0
                and (seg.start - segments[i - 1].end) > 2.0
            ):
                paragraphs.append(" ".join(current_paragraph))
                current_paragraph = []

            current_paragraph.append(text)

        if current_paragraph:
            paragraphs.append(" ".join(current_paragraph))

        return "\n\n".join(paragraphs)
