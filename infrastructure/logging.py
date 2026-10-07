"""Logging configuration.

- Every record passes the secret-redaction factory (infrastructure.log_redaction)
  and the ContextFilter (request_id / job_id / user from infrastructure.request_context).
- Console: human-readable text (LOG_FORMAT=text, default) or one JSON object per line
  (LOG_FORMAT=json, used in Docker so log shippers can parse it).
- Files (LOG_TO_FILE=true, local runs): LOG_DIR/transcript-service.log (all levels)
  and LOG_DIR/errors.log (warnings and errors), JSON, rotated.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from typing import Any

from config.settings import settings
from infrastructure.log_redaction import NOISY_HTTP_LOGGERS, install_secret_redaction, redact

_CONTEXT_FIELDS = ("request_id", "job_id", "user")
_STANDARD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname",
    "levelno", "lineno", "module", "msecs", "message", "msg", "name", "pathname", "process",
    "processName", "relativeCreated", "stack_info", "thread", "threadName", "had_args",
    "taskName", "context", *_CONTEXT_FIELDS,
})


class StructuredFormatter(logging.Formatter):
    """One JSON object per record: timestamp, level, logger, message, context, extras."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        entry: dict[str, Any] = {
            "timestamp": f"{ts}.{record.msecs:03.0f}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, "-")
            if value and value != "-":
                entry[field] = value
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        if extra:
            entry["extra"] = extra
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["exception"] = record.exc_text
        return redact(json.dumps(entry, default=str, ensure_ascii=False))


class ContextTextFormatter(logging.Formatter):
    """``time LEVEL logger [rid job=… user=…] message`` with secrets redacted."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(context)s%(message)s", "%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        rid = getattr(record, "request_id", "-")
        if rid and rid != "-":
            parts.append(rid)
        for field in ("job_id", "user"):
            value = getattr(record, field, "-")
            if value and value != "-":
                parts.append(f"{'job' if field == 'job_id' else field}={value}")
        record.context = f"[{' '.join(parts)}] " if parts else ""
        return redact(super().format(record))


def setup_logging() -> None:
    """Configure logging for the whole process (idempotent)."""
    # Redact credentials from every log record before any handler sees it.
    install_secret_redaction()

    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": "json" if settings.log_format == "json" else "text",
            "filters": ["context"],
            "level": settings.log_level,
        },
    }
    if settings.log_to_file:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(settings.logs_dir / "transcript-service.log"),
            "maxBytes": 50 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "json",
            "filters": ["context"],
            "level": "DEBUG",
        }
        handlers["errors"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(settings.logs_dir / "errors.log"),
            "maxBytes": 50 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
            "formatter": "json",
            "filters": ["context"],
            "level": "WARNING",
        }
    all_handlers = list(handlers)

    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"context": {"()": "infrastructure.request_context.ContextFilter"}},
        "formatters": {
            "json": {"()": StructuredFormatter},
            "text": {"()": ContextTextFormatter},
        },
        "handlers": handlers,
        "loggers": {
            "": {"handlers": all_handlers, "level": settings.log_level},
            "uvicorn": {"handlers": all_handlers, "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": all_handlers, "level": "INFO", "propagate": False},
            # Requests are logged once by the app's request middleware (with request id and user).
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": False},
            "googleapiclient": {"level": "WARNING"},
            "httplib2": {"level": "WARNING"},
            # HTTP client libraries log full request URLs (YouTube sends ?key=...) at
            # INFO/DEBUG; keep only their warnings and errors.
            **{name: {"level": "WARNING"} for name in NOISY_HTTP_LOGGERS},
        },
    }
    logging.config.dictConfig(config)
