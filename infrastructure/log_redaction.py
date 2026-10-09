"""Secret redaction for all log output.

Two layers, so a secret cannot reach stdout, log files or any other handler:

1. ``install_secret_redaction()`` wraps the global LogRecord factory. Every record
   created by any logger has its message (msg % args) and exception/stack text
   redacted *before* it is handed to handlers, including handlers added later by
   third-party libraries.
2. ``RedactingFormatter`` / ``redact()`` redact the final formatted string, which
   also covers structured ``extra`` fields attached after record creation.

Redaction targets: credential query parameters (``key=``, ``api_key=``, ``token=``, OAuth
``code=``/``state=``...),
Google/Groq/OpenAI/app API key formats, ``Authorization`` and ``X-API-Key`` header
values, JWTs, session cookies, password/secret assignments, scrypt password hashes,
and the exact values of secrets configured in the environment.
"""

from __future__ import annotations

import logging
import os
import re
import threading

REDACTED = "[REDACTED]"

# Environment variables whose exact values must never appear in logs.
_SECRET_ENV_VARS = (
    "YOUTUBE_API_KEY",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "JWT_SECRET_KEY",
    "GOOGLE_CLIENT_SECRET",
    "YOUTUBE_PROXY_URL",  # may embed proxy credentials
    "SENTRY_DSN",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credential-bearing query parameters: ?key=..., &api_key=..., &access_token=...
    (re.compile(
        r"([?&](?:key|api[_-]?key|access[_-]?token|id[_-]?token|refresh[_-]?token|token|auth|password|secret|"
        r"client[_-]?secret|sig|signature|code|state|code[_-]?verifier)=)"
        r"[^&\s\"'#<>]+",
        re.IGNORECASE,
    ), r"\1" + REDACTED),
    # Authorization header values (Bearer/Basic/ApiKey/Token schemes)
    (re.compile(r"(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic|apikey|token)\s+)[^\s\"',;]+",
                re.IGNORECASE), r"\1" + REDACTED),
    # Bare bearer tokens wherever they appear (e.g. {"auth": "Bearer ..."})
    (re.compile(r"(\bbearer\s+)(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\1" + REDACTED),
    # X-API-Key header values
    (re.compile(r"(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", re.IGNORECASE), r"\1" + REDACTED),
    # Session cookie values
    (re.compile(r"(\bsession=)[^;\s\"',]+", re.IGNORECASE), r"\1" + REDACTED),
    # password / secret / token style assignments: password=..., "api_key": "..."
    (re.compile(
        r"([\"']?\b(?:password|passwd|secret|client_secret|api[_-]?key|access_token|refresh_token|"
        r"jwt_secret_key|youtube_api_key|groq_api_key|openai_api_key)[\"']?\s*[:=]\s*[\"']?)"
        r"(?!\[REDACTED\])[^\s\"',&}]+",
        re.IGNORECASE,
    ), r"\1" + REDACTED),
    # Well-known key formats
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), REDACTED),               # Google / YouTube
    (re.compile(r"gsk_[0-9A-Za-z]{20,}"), REDACTED),                # Groq
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),             # OpenAI-style
    (re.compile(r"\bysk_[A-Za-z0-9_-]{20,}"), REDACTED),            # this app's API keys
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # scrypt password hashes (AUTH_USERS)
    (re.compile(r"scrypt\.\d+\.\d+\.\d+\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}"), REDACTED),
)

_lock = threading.Lock()
_installed = False
_known_secrets: tuple[str, ...] = ()


def refresh_known_secrets(extra: tuple[str, ...] | list[str] = ()) -> None:
    """Load exact secret values from the environment (call again after env changes)."""
    global _known_secrets
    values = {os.environ.get(name, "").strip() for name in _SECRET_ENV_VARS}
    values.update(v.strip() for v in extra)
    # Very short values would cause false-positive redaction of ordinary text.
    _known_secrets = tuple(sorted((v for v in values if len(v) >= 8), key=len, reverse=True))


def redact(text: str) -> str:
    """Return ``text`` with credentials replaced by ``[REDACTED]``."""
    if not text:
        return text
    for secret in _known_secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
    except Exception:  # malformed format args: keep the raw template only
        message = str(record.msg)
    record.msg = redact(message)
    # Args are merged into the redacted message; remember they existed for formatters
    # whose behaviour depends on it (see infrastructure.logging.StructuredFormatter).
    record.had_args = bool(record.args)
    record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    return record


def install_secret_redaction() -> None:
    """Install the redacting LogRecord factory process-wide (idempotent)."""
    global _installed
    with _lock:
        refresh_known_secrets()
        if _installed:
            return
        previous_factory = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            return _redact_record(previous_factory(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _installed = True


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the fully formatted output (covers ``extra`` fields)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


# Third-party loggers that log full request URLs (including ?key=...) at INFO/DEBUG.
NOISY_HTTP_LOGGERS = ("httpx", "httpcore", "urllib3", "hpack", "h2")


def quiet_http_loggers(level: int = logging.WARNING) -> None:
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(level)
