"""Exact YouTube video duration filter.

Business Rule:
    3:00 <= duration < 30:00 (180 seconds <= duration_seconds < 1800 seconds)
"""

import re
from typing import NamedTuple

MIN_DURATION_SECONDS = 180   # 3 minutes inclusive
MAX_DURATION_SECONDS = 1800  # 30 minutes exclusive


class DurationFilterResult(NamedTuple):
    is_eligible: bool
    skip_reason: str | None
    duration_seconds: int
    duration_formatted: str


def parse_iso_duration(iso: str) -> int:
    """Parse ISO 8601 duration string (e.g. PT15M51S, PT1H2M3S) to total seconds."""
    match = re.match(r'^PT?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', iso or "")
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int) -> str:
    """Format seconds into HH:MM:SS or MM:SS string."""
    if seconds < 0:
        seconds = 0
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def evaluate_duration(
    duration_seconds: int | None,
    live_status: str = "none",
    min_seconds: int = MIN_DURATION_SECONDS,
    max_seconds: int = MAX_DURATION_SECONDS,
) -> DurationFilterResult:
    """Evaluate whether a video is eligible based on exact duration boundaries.

    Rules:
        - Must not be live or upcoming broadcast (liveBroadcastContent != "none").
        - duration_seconds >= min_seconds (180s / 3:00).
        - duration_seconds < max_seconds (1800s / 30:00).

    Returns:
        DurationFilterResult with is_eligible flag and machine-readable skip_reason.
    """
    sec = duration_seconds or 0
    formatted = format_duration(sec)

    if live_status != "none":
        return DurationFilterResult(
            is_eligible=False,
            skip_reason="LIVE_STREAM",
            duration_seconds=sec,
            duration_formatted=formatted,
        )

    if duration_seconds is None or duration_seconds <= 0:
        return DurationFilterResult(
            is_eligible=False,
            skip_reason="INVALID_DURATION",
            duration_seconds=0,
            duration_formatted="0:00",
        )

    if sec < min_seconds:
        return DurationFilterResult(
            is_eligible=False,
            skip_reason="TOO_SHORT",
            duration_seconds=sec,
            duration_formatted=formatted,
        )

    if sec >= max_seconds:
        return DurationFilterResult(
            is_eligible=False,
            skip_reason="TOO_LONG",
            duration_seconds=sec,
            duration_formatted=formatted,
        )

    return DurationFilterResult(
        is_eligible=True,
        skip_reason=None,
        duration_seconds=sec,
        duration_formatted=formatted,
    )
