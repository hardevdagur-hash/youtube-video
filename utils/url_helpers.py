"""URL helper utilities for YouTube URL parsing and normalization."""

import logging
import re
from urllib.parse import parse_qs, urlparse

from exceptions import YouTubeURLError

logger = logging.getLogger(__name__)

YOUTUBE_DOMAINS = (
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube.com",
    "youtu.be",
    "www.youtube-nocookie.com",
    "youtube-nocookie.com",
)

UNSUPPORTED_PATHS = (
    "/channel/",
    "/user/",
    "/@",
    "/c/",
    "/playlist",
    "/feed/",
    "/account",
    "/t/",
    "/hashtag/",
    "/results",
    "/post/",
    "/community/",
    "/about/",
    "/store/",
    "/channels",
    "/premium",
    "/gaming",
)

_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_youtube_domain(domain: str) -> bool:
    """Check if the domain is a recognized YouTube domain (case-insensitive)."""
    return domain.lower() in YOUTUBE_DOMAINS


def is_supported_path(path: str) -> bool:
    """Check if the URL path is not an unsupported YouTube resource."""
    lower_path = path.lower()
    return all(not lower_path.startswith(unsupported) for unsupported in UNSUPPORTED_PATHS)


def extract_video_id_from_query(query: str) -> str | None:
    """Extract the ``v`` parameter from a URL query string (case-insensitive).

    Args:
        query: The query component of a URL (e.g. ``v=abc123&t=120``).

    Returns:
        The video ID value, or ``None`` if not found.
    """
    params = parse_qs(query, keep_blank_values=True)
    for key in ("v", "V"):
        v_values = params.get(key)
        if v_values and v_values[0]:
            return v_values[0]
    return None


def validate_video_id(video_id: str) -> str:
    """Validate that a string is a properly formatted YouTube video ID.

    Args:
        video_id: The extracted video ID string.

    Returns:
        The validated video ID.

    Raises:
        YouTubeURLError: If the video ID is invalid.
    """
    if not video_id or not video_id.strip():
        raise YouTubeURLError("Video ID is missing or empty.")

    if not _VIDEO_ID_PATTERN.match(video_id):
        raise YouTubeURLError(
            f"Invalid video ID format: '{video_id}'. "
            "YouTube video IDs are exactly 11 characters "
            "containing letters, numbers, hyphens, and underscores."
        )

    return video_id


def normalize_url(video_id: str) -> str:
    """Build a canonical YouTube watch URL from a video ID.

    Args:
        video_id: The validated video ID.

    Returns:
        Standard ``https://www.youtube.com/watch?v=VIDEO_ID`` URL.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def clean_url_input(raw: str) -> str:
    """Sanitize and strip whitespace from raw user input.

    Args:
        raw: Raw user input string.

    Returns:
        Cleaned, stripped URL string.

    Raises:
        YouTubeURLError: If the input is empty or not a string.
    """
    if not raw or not isinstance(raw, str):
        raise YouTubeURLError("Empty or invalid input. Please enter a YouTube URL.")

    cleaned = raw.strip()
    if not cleaned:
        raise YouTubeURLError("Empty or invalid input. Please enter a YouTube URL.")

    return cleaned


def has_valid_scheme(url: str) -> bool:
    """Check if the URL has a valid HTTP or HTTPS scheme using urlparse.

    Returns:
        ``True`` if the scheme is ``http`` or ``https``.
    """
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https")
