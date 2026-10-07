"""YouTube URL and Video ID resolution service."""

import logging
import re

from exceptions import YouTubeURLError
from services.youtube_url_parser import YouTubeURLParser
from utils.url_helpers import validate_video_id

logger = logging.getLogger(__name__)


class YouTubeResolver:
    """Validate and resolve YouTube URLs and Video IDs."""

    def __init__(self) -> None:
        self._parser = YouTubeURLParser()

    def resolve_video_id(self, url_or_id: str) -> str:
        """Extract and validate 11-character video ID from a URL or raw ID.

        Args:
            url_or_id: Full YouTube URL, shortened URL, or bare video ID.

        Returns:
            Validated 11-character video ID string.

        Raises:
            YouTubeURLError: If the input is not a valid YouTube URL or ID.
        """
        raw = (url_or_id or "").strip()
        if not raw:
            raise YouTubeURLError("Video URL or ID cannot be empty.")

        # Check if it's already a bare 11-character ID
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
            try:
                return validate_video_id(raw)
            except Exception as e:
                raise YouTubeURLError(f"Invalid video ID format: {e}") from e

        # Otherwise parse as URL
        result = self._parser.parse(raw)
        if not result.valid or not result.video_id:
            raise YouTubeURLError(result.error or f"Invalid YouTube URL: {raw}")

        return result.video_id

    def get_canonical_url(self, video_id: str) -> str:
        """Return the canonical standard watch URL."""
        validated = validate_video_id(video_id)
        return f"https://www.youtube.com/watch?v={validated}"
