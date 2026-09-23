"""Transcript cleaning and normalization pipeline."""

import logging
import unicodedata
from typing import Any, Dict, List

from services.transcript_processor import TranscriptProcessor

logger = logging.getLogger(__name__)


class TranscriptCleaner:
    """Standardizes and cleans raw transcript segments and text.

    Executes:
      1. Unicode NFC normalization
      2. Timestamp sorting & continuity validation
      3. Sentence boundary restoration & capitalization
      4. Disfluency / stutter reduction
      5. Segment alignment
    """

    def __init__(self) -> None:
        self._processor = TranscriptProcessor()

    def clean(
        self,
        raw_segments: List[Dict[str, Any]],
        video_id: str = "",
        raw_text: str = "",
    ) -> Dict[str, Any]:
        """Clean raw segments and return structured normalized text and segments.

        Args:
            raw_segments: List of dicts with 'text', 'start', 'duration' or 'end'.
            video_id: Optional 11-char YouTube video ID.
            raw_text: Optional unsegmented raw text fallback.

        Returns:
            Dict containing:
              - 'text': Normalized complete transcript text
              - 'segments': List of cleaned segment dicts (start, end, duration, text)
              - 'paragraphs': List of paragraph strings
              - 'word_count': Total word count
              - 'character_count': Total character count
        """
        # Ensure unicode normalization on all raw segment texts
        normalized_segments: List[Dict[str, Any]] = []
        for s in raw_segments:
            text = unicodedata.normalize("NFC", str(s.get("text", "")).strip())
            if not text:
                continue
            start = round(float(s.get("start", 0.0)), 2)
            duration = float(s.get("duration", 0.0))
            end = round(float(s.get("end", start + duration)), 2)
            if duration <= 0:
                duration = max(0.1, round(end - start, 2))

            normalized_segments.append(
                {
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "text": text,
                }
            )

        # Sort segments by start time
        normalized_segments.sort(key=lambda x: x["start"])

        # Run through NLP processor if segments exist
        if normalized_segments:
            try:
                proc_result = self._processor.process(
                    normalized_segments, video_id=video_id or "UNKNOWN_VID"
                )
                if proc_result.success and proc_result.clean_transcript:
                    clean_text = proc_result.clean_transcript
                    paragraphs = proc_result.paragraphs
                    # Update segments if timestamps are available
                    segments_out = normalized_segments
                    word_count = proc_result.statistics.word_count
                    char_count = proc_result.statistics.character_count
                    return {
                        "text": clean_text,
                        "segments": segments_out,
                        "paragraphs": paragraphs,
                        "word_count": word_count,
                        "character_count": char_count,
                    }
            except Exception as e:
                logger.warning("NLP processor fallback triggered: %s", e)

        # Fallback if NLP pipeline could not run
        if not normalized_segments and raw_text:
            text = unicodedata.normalize("NFC", raw_text.strip())
            words = text.split()
            return {
                "text": text,
                "segments": [],
                "paragraphs": [text],
                "word_count": len(words),
                "character_count": len(text),
            }

        full_text = " ".join(s["text"] for s in normalized_segments)
        words = full_text.split()
        return {
            "text": full_text,
            "segments": normalized_segments,
            "paragraphs": [full_text] if full_text else [],
            "word_count": len(words),
            "character_count": len(full_text),
        }
