"""Which transcript failures are facts about the video and which are temporary.

A failure may be cached (and a channel-job item closed for good) only when it describes
the video itself: it has no captions, captions are disabled, or it is unavailable.
Everything else (YouTube rate limits or bot checks, speech-to-text outages, timeouts)
says nothing about the video and must stay retryable; caching it as "no captions" would
hide a perfectly transcribable video forever.
"""

from __future__ import annotations

# Facts about the video: safe to cache and to report as final.
PERMANENT_FAILURE_CODES = frozenset({
    "NO_CAPTIONS",
    "CAPTIONS_DISABLED",
    "VIDEO_UNAVAILABLE",
    "PRIVATE_VIDEO",
})

# Speech-to-text attempts that failed for reasons outside the video: retry later.
TRANSIENT_STT_CODES = frozenset({
    "BOT_BLOCKED",
    "STT_RATE_LIMITED",
    "STT_TIMEOUT",
    "STT_FAILED",
    "STT_BUSY",
})

# YouTube refuses this server (datacenter IP / bot check); retrying immediately or
# downloading audio instead only makes it worse.
YOUTUBE_BLOCK_CODES = frozenset({"RATE_LIMITED", "CAPTIONS_RATE_LIMITED", "BOT_BLOCKED"})

# Not the bare "sign in to confirm": "Sign in to confirm your age" is an age-restricted
# video (a property of the video), not a block of this server.
_BOT_BLOCK_MARKERS = (
    "not a bot",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "blocking requests from your ip",
    "requestblocked",
    "ipblocked",
    "ip has been blocked",
    "po token",
)


def looks_bot_blocked(text: str) -> bool:
    """True if an error message is YouTube refusing this server rather than the video."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _BOT_BLOCK_MARKERS)


def classify_stt_error(exc: BaseException) -> str:
    """Error code for a failed speech-to-text attempt (download or transcription).

    Uses the ``error_code`` carried by the provider exception (or its cause) first, then
    the message. ``AUDIO_TOO_LONG`` is a property of the video and the configured limit;
    everything else is transient.
    """
    codes = [getattr(exc, "error_code", None), getattr(exc.__cause__, "error_code", None)]
    text = f"{exc} {exc.__cause__ or ''}"
    if "BOT_BLOCKED" in codes or looks_bot_blocked(text):
        return "BOT_BLOCKED"
    if "AUDIO_TOO_LONG" in codes:
        return "AUDIO_TOO_LONG"
    if "STT_BUSY" in codes:
        return "STT_BUSY"
    if {"GROQ_RATE_LIMIT", "STT_RATE_LIMITED"} & set(codes):
        return "STT_RATE_LIMITED"
    if {"GROQ_TIMEOUT", "STT_TIMEOUT"} & set(codes):
        return "STT_TIMEOUT"
    lowered = text.lower()
    if "rate limit" in lowered or "too many requests" in lowered or " 429" in lowered:
        return "STT_RATE_LIMITED"
    if "timed out" in lowered or "timeout" in lowered:
        return "STT_TIMEOUT"
    return "STT_FAILED"
