from __future__ import annotations

import json
import logging
import logging.config
import sys
import time
from pathlib import Path
from typing import Any

from config.settings import settings
from infrastructure.log_redaction import (
    NOISY_HTTP_LOGGERS,
    RedactingFormatter,
    install_secret_redaction,
    redact,
)


class StructuredFormatter(logging.Formatter):
    """JSON-structured log formatter for production logging."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        log_entry: dict[str, Any] = {
            "timestamp": f"{ts}.{record.msecs:03.0f}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if hasattr(record, "correlation_id"):
            log_entry["correlation_id"] = record.correlation_id
        if hasattr(record, "job_id"):
            log_entry["job_id"] = record.job_id
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if record.args or getattr(record, "had_args", False):
            extra = {k: v for k, v in record.__dict__.items() if k not in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName", "had_args",
            )}
            if extra:
                log_entry["extra"] = extra
        return redact(json.dumps(log_entry, default=str))


class ExportLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that adds job_id and correlation_id context."""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        extra = kwargs.get("extra", {})
        extra.setdefault("correlation_id", getattr(self, "correlation_id", ""))
        extra.setdefault("job_id", getattr(self, "job_id", ""))
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging() -> None:
    """Configure structured logging for the entire application."""
    # Redact credentials from every log record before any handler sees it.
    install_secret_redaction()
    log_dir = settings.logs_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": StructuredFormatter,
            },
            "console": {
                "class": f"{RedactingFormatter.__module__}.{RedactingFormatter.__qualname__}",
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "console",
                "level": settings.log_level,
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "export-engine.log"),
                "maxBytes": 50 * 1024 * 1024,
                "backupCount": 5,
                "formatter": "structured",
                "level": "DEBUG",
            },
            "errors": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "errors.log"),
                "maxBytes": 50 * 1024 * 1024,
                "backupCount": 3,
                "formatter": "structured",
                "level": "WARNING",
            },
        },
        "loggers": {
            "": {
                "handlers": ["console", "file", "errors"],
                "level": settings.log_level,
            },
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "googleapiclient": {
                "handlers": ["console", "file"],
                "level": "WARNING",
                "propagate": False,
            },
            "httplib2": {
                "level": "WARNING",
                "propagate": False,
            },
            # HTTP client libraries log full request URLs (YouTube sends ?key=...) at
            # INFO/DEBUG; keep only their warnings and errors.
            **{name: {"level": "WARNING"} for name in NOISY_HTTP_LOGGERS},
        },
    }
    logging.config.dictConfig(config)


def get_logger(name: str, job_id: str = "", correlation_id: str = "") -> ExportLoggerAdapter:
    logger = logging.getLogger(name)
    adapter = ExportLoggerAdapter(logger, {"correlation_id": correlation_id, "job_id": job_id})
    return adapter


class ExportTimer:
    """Context manager for timing export operations."""

    def __init__(self, logger: ExportLoggerAdapter, operation: str, **context: Any) -> None:
        self._logger = logger
        self._operation = operation
        self._context = context
        self._start: float = 0.0

    def __enter__(self) -> ExportTimer:
        self._start = time.time()
        self._logger.info("Starting %s", self._operation, extra=self._context)
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = time.time() - self._start
        self._logger.info(
            "Completed %s in %.3fs", self._operation, elapsed,
            extra={**self._context, "elapsed_seconds": round(elapsed, 3)},
        )
