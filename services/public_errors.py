"""Client-safe error messages.

Provider and library exceptions can carry upstream response bodies, file paths or
request identifiers. Nothing derived from ``str(exc)`` is returned to API clients;
responses use the fixed wording below (keyed by error code) and the full detail is
logged server-side with the request/job id.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicError:
    status_code: int
    message: str
    retryable: bool


_DEFAULT = PublicError(500, "The transcript could not be retrieved.", True)

_ERRORS: dict[str, PublicError] = {
    "INVALID_YOUTUBE_URL": PublicError(400, "That is not a valid YouTube video URL or ID.", False),
    "INVALID_REQUEST": PublicError(400, "The request is invalid.", False),
    "NO_CAPTIONS": PublicError(404, "No transcript or caption track is available for this video.", False),
    "CAPTIONS_UNAVAILABLE": PublicError(404, "No transcript or caption track is available for this video.", False),
    "CAPTIONS_DISABLED": PublicError(404, "Captions are disabled for this video.", False),
    "TRANSCRIPT_EMPTY": PublicError(422, "The transcript for this video is empty.", False),
    "TRANSCRIPT_INVALID": PublicError(422, "The transcript failed quality validation.", False),
    "VIDEO_UNAVAILABLE": PublicError(404, "This video is unavailable, private, or deleted.", False),
    "PRIVATE_VIDEO": PublicError(404, "This video is unavailable, private, or deleted.", False),
    "RATE_LIMITED": PublicError(429, "Rate limited by YouTube. Please retry later.", True),
    "CAPTIONS_RATE_LIMITED": PublicError(429, "Rate limited by YouTube. Please retry later.", True),
    "TIMEOUT": PublicError(504, "Connection to YouTube timed out.", True),
    "NETWORK_ERROR": PublicError(502, "Network error while contacting YouTube.", True),
    "AUDIO_EXTRACTION_FAILED": PublicError(
        502, "Audio could not be extracted from this video for speech-to-text.", True,
    ),
    "STT_UNAVAILABLE": PublicError(503, "Speech-to-text is not available on this server.", False),
    "STT_RATE_LIMITED": PublicError(429, "The speech-to-text provider is rate limiting requests. Please retry later.", True),
    "STT_TIMEOUT": PublicError(504, "The speech-to-text provider timed out.", True),
    "STT_FAILED": PublicError(502, "Speech-to-text failed for this video.", True),
    "TRANSLATION_FAILED": PublicError(502, "The transcript could not be translated.", True),
    "EXTRACTION_ERROR": PublicError(500, "Transcript extraction failed.", True),
    "UNEXPECTED_ERROR": _DEFAULT,
    "TRANSCRIPTION_FAILED": _DEFAULT,
}

# Provider-specific codes folded into the public vocabulary above.
_ALIASES = {
    "GROQ_AUTH_ERROR": "STT_UNAVAILABLE",
    "GROQ_RATE_LIMIT": "STT_RATE_LIMITED",
    "GROQ_TIMEOUT": "STT_TIMEOUT",
    "TRANSCRIPT_QUALITY_FAILED": "TRANSCRIPT_INVALID",
}


def public_code(code: str | None) -> str:
    """Normalise an internal error code to one documented in the public API."""
    if not code:
        return "UNEXPECTED_ERROR"
    code = _ALIASES.get(code, code)
    return code if code in _ERRORS else "UNEXPECTED_ERROR"


def public_error(code: str | None) -> tuple[str, PublicError]:
    """Return ``(public_code, PublicError)`` for an internal error code."""
    normalized = public_code(code)
    return normalized, _ERRORS[normalized]


def public_message(code: str | None) -> str:
    return public_error(code)[1].message
