"""YouTube Caption fetching service."""

import logging
from typing import Any, List, Optional, Tuple

from clients.youtube_transcript_client import (
    YouTubeTranscriptClient,
    YouTubeTranscriptClientError,
    NoTranscriptFoundError,
    TranscriptsDisabledError,
    VideoUnavailableError,
    TooManyRequestsError,
)

logger = logging.getLogger(__name__)


class CaptionsUnavailableError(Exception):
    """Raised when YouTube captions cannot be retrieved (disabled, not found, or private)."""

    def __init__(self, message: str, error_code: str = "CAPTIONS_UNAVAILABLE") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class YouTubeCaptionsService:
    """Service to fetch captions directly from YouTube.

    Prioritizes manual captions over auto-generated captions.
    Raises CaptionsUnavailableError when captions are absent or disabled,
    triggering the Groq Whisper fallback.
    """

    def __init__(self, client: Optional[YouTubeTranscriptClient] = None) -> None:
        self._client = client or YouTubeTranscriptClient()

    def fetch_captions(
        self, video_id: str, preferred_languages: Optional[List[str]] = None
    ) -> Tuple[List[dict], str, bool]:
        """Attempt to fetch captions from YouTube for the given video ID.

        Args:
            video_id: 11-character YouTube video ID.
            preferred_languages: Optional prioritized list of language codes.

        Returns:
            Tuple of (segments, language_code, is_manual):
            - segments: List of dicts with 'text', 'start', 'duration'
            - language_code: e.g. 'en', 'hi'
            - is_manual: True if manually created captions, False if auto-generated

        Raises:
            CaptionsUnavailableError: If captions are disabled, not found, or inaccessible.
        """
        logger.info("Attempting to fetch YouTube captions for video %s", video_id)
        try:
            segments, lang, is_manual, _ = self._client.find_best_transcript(
                video_id=video_id,
                preferred_languages=preferred_languages,
                transcript_type="any",
            )
            if not segments:
                raise CaptionsUnavailableError(
                    f"No caption segments returned for video {video_id}",
                    error_code="TRANSCRIPT_EMPTY",
                )

            logger.info(
                "Successfully fetched YouTube captions for %s: lang=%s, manual=%s, segments=%d",
                video_id,
                lang,
                is_manual,
                len(segments),
            )
            return segments, lang, is_manual

        except TranscriptsDisabledError as e:
            logger.info("Captions disabled for video %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"Captions are disabled for video {video_id}",
                error_code="CAPTIONS_DISABLED",
            ) from e
        except NoTranscriptFoundError as e:
            logger.info("No captions found for video %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"No captions found for video {video_id}",
                error_code="CAPTIONS_UNAVAILABLE",
            ) from e
        except VideoUnavailableError as e:
            logger.warning("Video unavailable for video %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"Video {video_id} is unavailable or private",
                error_code="VIDEO_UNAVAILABLE",
            ) from e
        except TooManyRequestsError as e:
            logger.warning("Rate limited by YouTube fetching captions for %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"Rate limited by YouTube when fetching captions: {e}",
                error_code="CAPTIONS_RATE_LIMITED",
            ) from e
        except YouTubeTranscriptClientError as e:
            logger.warning("Error fetching captions for %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"YouTube captions unavailable: {e}",
                error_code="CAPTIONS_UNAVAILABLE",
            ) from e
        except Exception as e:
            logger.warning("Unexpected error fetching captions for %s: %s", video_id, e)
            raise CaptionsUnavailableError(
                f"Could not retrieve YouTube captions: {e}",
                error_code="CAPTIONS_UNAVAILABLE",
            ) from e
