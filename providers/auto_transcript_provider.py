"""Stage 2: Official YouTube auto-generated transcript provider.

Uses the smart transcript selection engine from YouTubeTranscriptClient
to enumerate ALL available transcripts, log each candidate with full
metadata, and auto-select the best auto-generated transcript.
"""

import logging

from clients.youtube_transcript_client import (
    NoTranscriptFoundError,
    TooManyRequestsError,
    TranscriptsDisabledError,
    VideoUnavailableError,
    YouTubeTranscriptClient,
)
from interfaces.transcript_provider import TranscriptProvider
from models.transcript import (
    TranscriptProviderName,
    TranscriptResult,
    TranscriptSource,
)
from utils.language_detector import LanguageDetector
from utils.read_time import estimate_read_time
from utils.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)


class AutoTranscriptProvider(TranscriptProvider):
    """Stage 2 provider: retrieves YouTube auto-generated transcripts.

    Uses ``YouTubeTranscriptClient.find_best_transcript()`` with
    ``transcript_type="auto"`` to enumerate ALL available transcripts,
    log every candidate, and auto-select the best auto-generated match.
    """

    def __init__(
        self,
        client: YouTubeTranscriptClient | None = None,
        text_cleaner: TextCleaner | None = None,
        language_detector: LanguageDetector | None = None,
    ) -> None:
        self._client = client or YouTubeTranscriptClient()
        self._text_cleaner = text_cleaner or TextCleaner()
        self._language_detector = language_detector or LanguageDetector()

    def name(self) -> str:
        return "Auto Transcript"

    def get_transcript(
        self, video_id: str, language: str | None = None
    ) -> TranscriptResult:
        """Fetch the best auto-generated transcript for this video.

        Delegates to ``YouTubeTranscriptClient.find_best_transcript()``
        with ``transcript_type="auto"``, which:
          1. Lists ALL available transcripts via a single API call
          2. Logs each candidate with language, code, generated, translatable
          3. Selects the best match using priority scoring (auto only)

        Args:
            video_id: 11-character YouTube video ID.
            language: Optional language code hint.

        Returns:
            ``TranscriptResult`` populated with auto transcript data.

        Raises:
            NoTranscriptFoundError: No auto transcript available.
        """

        try:
            preferred = [language] if language else None
            raw_segments, detected_lang, is_manual, translation_source = (
                self._client.find_best_transcript(
                    video_id,
                    preferred_languages=preferred,
                    transcript_type="auto",
                )
            )
        except (NoTranscriptFoundError, TranscriptsDisabledError,
                VideoUnavailableError, TooManyRequestsError):
            raise
        except Exception as exc:
            logger.warning("Auto transcript fetch failed for %s: %s", video_id, exc)
            raise NoTranscriptFoundError(f"Auto transcript unavailable: {exc}") from exc

        if not raw_segments:
            raise NoTranscriptFoundError(f"No auto transcript segments for {video_id}")

        final_language = detected_lang
        if translation_source:
            logger.info(
                "Auto transcript for %s was translated from %s to en",
                video_id, translation_source,
            )
            final_language = "en"

        segments = self._client.parse_segments(raw_segments)
        segments = self._text_cleaner.clean_segments(segments)

        plain_text = " ".join(seg.text for seg in segments)
        paragraph_text = self._text_cleaner.build_paragraphs(segments)
        word_count = len(plain_text.split())
        char_count = len(plain_text)
        duration = segments[-1].end if segments else 0

        detected_lang_result = self._language_detector.detect(plain_text)

        available_languages = self._get_available_languages(video_id)

        return TranscriptResult(
            success=True,
            video_id=video_id,
            source=TranscriptSource.AUTO,
            provider=TranscriptProviderName.YOUTUBE_AUTO,
            language=final_language,
            language_confidence=detected_lang_result.confidence if detected_lang_result else None,
            segments=segments,
            plain_text=plain_text,
            paragraph_text=paragraph_text,
            word_count=word_count,
            character_count=char_count,
            estimated_read_time=estimate_read_time(word_count),
            duration_seconds=duration,
            available_languages=available_languages,
            translation_source=translation_source,
            method="caption",
            error=None,
        )

    def _get_available_languages(self, video_id: str) -> list[dict]:
        """Get serializable list of available transcript languages."""
        try:
            available = self._client.list_all_transcripts(video_id)
            return [
                {
                    "language": t["language"],
                    "language_code": t["language_code"],
                    "is_generated": t["is_generated"],
                    "is_translatable": t["is_translatable"],
                }
                for t in available
            ]
        except Exception:
            return []
