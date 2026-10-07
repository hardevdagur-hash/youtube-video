"""Application settings.

All configuration comes from environment variables. A ``.env`` file in the project
root is loaded for local development, but real environment variables (Docker
``env_file``/``environment``, systemd, CI) always take precedence over it.

Invalid values fail fast at import time with a ``ConfigurationError`` that names
the variable, so a misconfigured deployment refuses to start instead of running
with surprising defaults. The YouTube API key is validated where it is used (see
``is_youtube_api_key_valid``) so the health endpoint can report it.

Storage layout (``DATA_DIR``, default ``<project>/data``; mount it as a volume)::

    DATA_DIR/transcripts/        transcript + translation cache (JSON per video)
    DATA_DIR/transcript_jobs/    background job checkpoints (JSON per job)
    DATA_DIR/tmp/audio/          short-lived audio downloads for speech-to-text
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

_ENV_FILE = BASE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(dotenv_path=_ENV_FILE, override=False)


class ConfigurationError(Exception):
    """Raised when there is an error in application configuration."""


def _get_env(key: str, default: str = "") -> str:
    """Raw environment value with surrounding whitespace removed."""
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int, minimum: int, maximum: int) -> int:
    raw = _get_env(key, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"{key} must be an integer, got {raw[:32]!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(key: str, default: float, minimum: float, maximum: float) -> float:
    raw = _get_env(key, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(f"{key} must be a number, got {raw[:32]!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{key} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_bool(key: str, default: bool) -> bool:
    raw = _get_env(key, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigurationError(f"{key} must be true or false, got {raw[:32]!r}")


def _env_choice(key: str, default: str, choices: tuple[str, ...]) -> str:
    value = _get_env(key, default).lower()
    if value not in choices:
        raise ConfigurationError(f"{key} must be one of {', '.join(choices)}, got {value[:32]!r}")
    return value


def _env_path(key: str, default: Path) -> Path:
    raw = _get_env(key)
    path = Path(raw) if raw else default
    return path if path.is_absolute() else (BASE_DIR / path)


_PLACEHOLDER_VALUES = {"", "your_youtube_api_key_here", "your_api_key_here", "your-api-key"}


def validate_youtube_api_key(v: str) -> str:
    """Return the stripped key; raise ConfigurationError if it is missing or a placeholder."""
    stripped = v.strip()
    if not stripped or stripped.lower() in _PLACEHOLDER_VALUES:
        raise ConfigurationError(
            "YouTube API key is missing or has a placeholder value. Set YOUTUBE_API_KEY."
        )
    return stripped


def is_youtube_api_key_valid() -> tuple[bool, str]:
    """Check if the YouTube API key is configured without raising."""
    try:
        validate_youtube_api_key(_get_env("YOUTUBE_API_KEY", ""))
        return True, ""
    except ConfigurationError as e:
        return False, str(e)


class Settings:
    """Typed, validated application settings (read once at import)."""

    def __init__(self) -> None:
        # Credentials (never logged; see infrastructure.log_redaction)
        self.youtube_api_key: str = _get_env("YOUTUBE_API_KEY", "")
        self.groq_api_key: str = _get_env("GROQ_API_KEY", "")

        # Groq: speech-to-text fallback and Simple English / Simple Hindi rewriting
        self.groq_whisper_model: str = _get_env("GROQ_WHISPER_MODEL", "whisper-large-v3")
        self.groq_translation_model: str = _get_env("GROQ_TRANSLATION_MODEL", "llama-3.3-70b-versatile")
        self.groq_timeout_seconds: int = _env_int("GROQ_TIMEOUT_SECONDS", 120, 5, 900)
        self.groq_max_retries: int = _env_int("GROQ_MAX_RETRIES", 3, 1, 10)

        # Speech-to-text fallback when a video has no captions
        self.whisper_enabled: bool = _env_bool("WHISPER_ENABLED", True)
        # groq: hosted Groq Whisper (production). local: faster-whisper (needs optional ML deps).
        self.stt_backend: str = _env_choice("STT_BACKEND", "groq", ("groq", "local"))
        self.whisper_model: str = _get_env("WHISPER_MODEL", "base")
        self.whisper_device: str = _get_env("WHISPER_DEVICE", "auto")
        self.whisper_compute_type: str = _get_env("WHISPER_COMPUTE_TYPE", "auto")

        # Logging
        self.log_level: str = _env_choice(
            "LOG_LEVEL", "INFO", ("debug", "info", "warning", "error", "critical")
        ).upper()
        # text (human readable) | json (one object per line, for log shippers)
        self.log_format: str = _env_choice("LOG_FORMAT", "text", ("text", "json"))
        self.log_to_file: bool = _env_bool("LOG_TO_FILE", True)
        self.logs_dir: Path = _env_path("LOG_DIR", BASE_DIR / "logs")

        # Persistent storage
        self.data_dir: Path = _env_path("DATA_DIR", BASE_DIR / "data")
        self.transcript_cache_dir: Path = self.data_dir / "transcripts"
        self.transcript_jobs_dir: Path = self.data_dir / "transcript_jobs"
        self.audio_temp_dir: Path = self.data_dir / "tmp" / "audio"

        # YouTube caption pacing and rate-limit handling (per instance)
        self.transcript_max_concurrency: int = _env_int("TRANSCRIPT_MAX_CONCURRENCY", 1, 1, 10)
        self.whisper_max_concurrency: int = _env_int("WHISPER_MAX_CONCURRENCY", 1, 1, 10)
        self.transcript_request_interval: float = _env_float("TRANSCRIPT_REQUEST_INTERVAL", 2.5, 0.0, 60.0)
        self.transcript_rate_limit_cooldown_base: float = _env_float("TRANSCRIPT_COOLDOWN_BASE", 30.0, 1.0, 3600.0)
        self.transcript_rate_limit_cooldown_max: float = _env_float("TRANSCRIPT_COOLDOWN_MAX", 300.0, 1.0, 86400.0)
        self.transcript_max_rate_limit_retries: int = _env_int("TRANSCRIPT_MAX_RETRIES", 3, 1, 20)

        # Abuse/resource limits (each video can cost YouTube quota and Groq credits)
        self.max_videos_per_job: int = _env_int("MAX_VIDEOS_PER_JOB", 100, 1, 5000)
        # Synchronous channel requests hold an HTTP connection for the whole run: keep them small.
        self.max_videos_sync_export: int = _env_int("MAX_VIDEOS_SYNC_EXPORT", 25, 1, 200)
        self.max_concurrent_sync_channel_runs: int = _env_int("MAX_CONCURRENT_SYNC_CHANNEL_RUNS", 2, 1, 20)
        # Running background jobs (each holds YouTube/Groq capacity for its whole run)
        self.max_active_jobs: int = _env_int("MAX_ACTIVE_JOBS", 4, 1, 50)
        self.max_active_jobs_per_user: int = _env_int("MAX_ACTIVE_JOBS_PER_USER", 2, 1, 50)

        # Job lifecycle: finished jobs stay on disk this long, then are deleted.
        self.job_retention_days: int = _env_int("JOB_RETENTION_DAYS", 30, 1, 3650)
        # Finished jobs kept in memory for fast polling/download (others are read from disk).
        self.job_memory_cache_size: int = _env_int("JOB_MEMORY_CACHE_SIZE", 8, 0, 1000)

        if self.transcript_rate_limit_cooldown_base > self.transcript_rate_limit_cooldown_max:
            raise ConfigurationError("TRANSCRIPT_COOLDOWN_BASE must not exceed TRANSCRIPT_COOLDOWN_MAX")
        if self.max_active_jobs_per_user > self.max_active_jobs:
            raise ConfigurationError("MAX_ACTIVE_JOBS_PER_USER must not exceed MAX_ACTIVE_JOBS")

    def ensure_directories(self) -> None:
        """Create the storage directories (raises OSError if the volume is not writable)."""
        for directory in (self.transcript_cache_dir, self.transcript_jobs_dir, self.audio_temp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if self.log_to_file:
            self.logs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
try:
    settings.ensure_directories()
except OSError as exc:
    raise ConfigurationError(f"Storage directories are not writable: {exc}") from exc


def get_settings() -> Settings:
    """Return the global settings instance."""
    return settings
