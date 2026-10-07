"""YouTube URL parser — single source of truth for all YouTube video URL parsing.

This module provides the ``YouTubeURLParser`` service class that can parse
any supported YouTube URL format into a structured ``VideoURLResult`` object.

Usage::

    from services.youtube_url_parser import YouTubeURLParser

    parser = YouTubeURLParser()
    result = parser.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    if result.valid:
        print(result.video_id)   # dQw4w9WgXcQ
"""

import logging
import uuid
from urllib.parse import urlparse

from exceptions import YouTubeURLError
from models.video_url import UrlType, VideoURLResult
from utils.url_helpers import (
    clean_url_input,
    extract_video_id_from_query,
    has_valid_scheme,
    is_supported_path,
    is_youtube_domain,
    normalize_url,
    validate_video_id,
)

logger = logging.getLogger(__name__)


class YouTubeURLParser:
    """Parse and validate YouTube video URLs.

    This is a stateless service class designed to be reused across
    all modules that need YouTube URL parsing.
    """

    def parse(self, raw_url: str) -> VideoURLResult:
        """Parse a YouTube URL and return a structured result.

        This method performs all validation and extraction in a single
        call. It never raises exceptions — all errors are captured in the
        returned ``VideoURLResult`` object.

        Args:
            raw_url: A YouTube video URL in any supported format.

        Returns:
            A ``VideoURLResult`` with ``valid``, ``video_id``,
            ``normalized_url``, ``url_type``, ``original_url``, and ``error``.
        """
        rid = uuid.uuid4().hex[:8]
        try:
            cleaned = clean_url_input(raw_url)

            if not has_valid_scheme(cleaned):
                cleaned = "https://" + cleaned

            parsed = urlparse(cleaned)

            if not is_youtube_domain(parsed.netloc):
                logger.info("[%s] Unsupported domain: %s", rid, parsed.netloc)
                return VideoURLResult(
                    original_url=raw_url,
                    error=f"Unsupported domain: '{parsed.netloc}'. Only YouTube URLs are supported.",
                )

            if not is_supported_path(parsed.path):
                return self._unsupported_path_result(raw_url, parsed)

            video_id: str | None = None
            url_type: UrlType = None

            path_lower = parsed.path.lower()
            if parsed.netloc.lower() in ("youtu.be",):
                video_id = self._extract_from_path(parsed.path)
                url_type = "youtu.be" if video_id else None
            elif path_lower.startswith("/shorts/"):
                video_id = self._extract_from_path(parsed.path, prefix="/shorts/")
                url_type = "shorts" if video_id else None
            elif path_lower.startswith("/live/"):
                video_id = self._extract_from_path(parsed.path, prefix="/live/")
                url_type = "live" if video_id else None
            elif path_lower.startswith("/embed/"):
                video_id = self._extract_from_path(parsed.path, prefix="/embed/")
                url_type = "embed" if video_id else None
            elif path_lower.startswith("/watch"):
                video_id = extract_video_id_from_query(parsed.query)
                url_type = "watch" if video_id else None
            elif path_lower == "/":
                # Root path — try query parameter v=
                video_id = extract_video_id_from_query(parsed.query)
                url_type = "watch" if video_id else None

            if not video_id:
                return VideoURLResult(
                    original_url=raw_url,
                    error="Video ID not found in the provided URL. "
                          "Ensure the URL contains an 11-character video ID.",
                )

            video_id = validate_video_id(video_id)

            logger.info(
                "[%s] Parsed OK: type=%s id=%s url=%s",
                rid, url_type, video_id, raw_url,
            )

            return VideoURLResult(
                valid=True,
                video_id=video_id,
                normalized_url=normalize_url(video_id),
                url_type=url_type,
                original_url=raw_url,
                error=None,
            )

        except YouTubeURLError as exc:
            logger.info("[%s] YouTube URL parse error: %s | url=%s", rid, exc, raw_url)
            return VideoURLResult(
                original_url=raw_url,
                error=str(exc),
            )
        except Exception as exc:
            logger.warning("[%s] Unexpected error parsing URL: %s | url=%s", rid, exc, raw_url)
            return VideoURLResult(
                original_url=raw_url,
                error=f"Invalid URL. Please enter a valid YouTube video URL. ({type(exc).__name__})",
            )

    def _unsupported_path_result(self, raw_url: str, parsed: urlparse) -> VideoURLResult:
        """Build a descriptive error for unsupported YouTube paths."""
        path_lower = parsed.path.lower()
        if path_lower.startswith("/playlist"):
            return VideoURLResult(
                original_url=raw_url,
                error="Playlist URL detected. Please provide a video URL, not a playlist.",
            )
        if any(path_lower.startswith(p) for p in ("/channel/", "/user/", "/@", "/c/")):
            return VideoURLResult(
                original_url=raw_url,
                error="Channel URL detected. Please provide a video URL, not a channel.",
            )
        return VideoURLResult(
            original_url=raw_url,
            error="Unsupported YouTube resource. Please provide a valid video URL.",
        )

    def _extract_from_path(self, path: str, prefix: str = "") -> str | None:
        """Extract the video ID from a URL path component (case-insensitive prefix).

        Uses ``urlparse`` internally — query strings are already stripped
        by the caller's ``urlparse`` call, so no manual query splitting
        is needed.

        Args:
            path: The URL path (e.g. ``/shorts/VIDEO_ID``).
            prefix: The path prefix to strip (e.g. ``/shorts/``).

        Returns:
            The video ID segment, or ``None`` if the path is empty.
        """
        remaining = path
        if prefix:
            remaining = path[len(prefix):] if path.lower().startswith(prefix.lower()) else path

        remaining = remaining.strip("/")
        if not remaining:
            return None

        # Take only the first path segment (ignore anything after /)
        segment = remaining.split("/")[0]
        return segment if segment else None
