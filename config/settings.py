"""Application settings.

Loads from .env file and validates all required configuration.
Provides a singleton ``settings`` instance for use throughout the application.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

def _get_env(key: str, default: str = "") -> str:
    """Get environment variable with fallback default."""
    return os.getenv(key, default)


class ConfigurationError(Exception):
    """Exception raised when there is an error in application configuration."""
    pass


# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env before anything else
_ENV_FILE = BASE_DIR / ".env"
if _ENV_FILE.exists():
    load_dotenv(dotenv_path=_ENV_FILE, override=True)


_PLACEHOLDER_VALUES = {"", "your_youtube_api_key_here", "your_api_key_here", "your-api-key"}


def validate_youtube_api_key(v: str) -> str:
    """Validate that the YouTube API key is not empty or a placeholder.

    Raises ConfigurationError if the key is missing or a placeholder.
    """
    stripped = v.strip()
    if not stripped or stripped.lower() in _PLACEHOLDER_VALUES:
        raise ConfigurationError(
            f"YouTube API key is missing or has a placeholder value.\n"
            f"Please add a valid YOUTUBE_API_KEY to your .env file at: {BASE_DIR / '.env'}"
        )
    return stripped


def is_youtube_api_key_valid() -> tuple[bool, str]:
    """Check if the YouTube API key is configured without raising."""
    raw = _get_env("YOUTUBE_API_KEY", "")
    try:
        validate_youtube_api_key(raw)
        return True, ""
    except ConfigurationError as e:
        return False, str(e)


class Settings:
    """Centralized application settings.

    Loads values from environment variables (sourced from .env via python-dotenv).
    Does NOT raise on missing API key at construction — validation happens at
    point of use via ``validate_youtube_api_key()``.
    """

    # Required (validated at point of use)
    youtube_api_key: str = _get_env("YOUTUBE_API_KEY", "")

    # Optional API key for speech-to-text fallback (Whisper API)
    openai_api_key: str = _get_env("OPENAI_API_KEY", "")

    # Groq Settings (Primary Paid STT & On-Demand Translation)
    groq_api_key: str = _get_env("GROQ_API_KEY", "")
    groq_whisper_model: str = _get_env("GROQ_WHISPER_MODEL", "whisper-large-v3")
    groq_translation_model: str = _get_env("GROQ_TRANSLATION_MODEL", "llama-3.3-70b-versatile")
    groq_timeout_seconds: int = int(_get_env("GROQ_TIMEOUT_SECONDS", "120"))
    groq_max_retries: int = int(_get_env("GROQ_MAX_RETRIES", "3"))

    # Application settings
    log_level: str = _get_env("LOG_LEVEL", "INFO").upper()

    # Paths
    base_dir: Path = BASE_DIR
    logs_dir: Path = BASE_DIR / "logs"
    output_dir: Path = BASE_DIR / "output"

    # HTTP client settings
    http_connect_timeout: int = 15
    http_read_timeout: int = 30
    http_max_retries: int = 3
    http_backoff_factor: float = 1.0

    # YouTube API settings
    youtube_max_results: int = int(_get_env("YOUTUBE_MAX_RESULTS", "50"))
    youtube_batch_size: int = int(_get_env("YOUTUBE_BATCH_SIZE", "50"))
    youtube_quota_warning: int = int(_get_env("YOUTUBE_QUOTA_WARNING", "8000"))
    youtube_daily_quota: int = int(_get_env("YOUTUBE_DAILY_QUOTA", "10000"))

    # Redis
    redis_url: str = _get_env("REDIS_URL", "redis://localhost:6379/0")

    # Export engine
    export_max_videos: int = int(_get_env("EXPORT_MAX_VIDEOS", "50000"))
    export_csv_batch_size: int = int(_get_env("EXPORT_CSV_BATCH_SIZE", "50"))
    export_timeout_minutes: int = int(_get_env("EXPORT_TIMEOUT_MINUTES", "30"))

    # Transcript & Whisper Settings
    whisper_enabled: bool = _get_env("WHISPER_ENABLED", "true").lower() in ("true", "1", "yes")
    whisper_model: str = _get_env("WHISPER_MODEL", "base")
    whisper_device: str = _get_env("WHISPER_DEVICE", "auto")
    whisper_compute_type: str = _get_env("WHISPER_COMPUTE_TYPE", "auto")
    min_transcript_duration: int = int(_get_env("MIN_TRANSCRIPT_DURATION", "180"))
    max_transcript_duration: int = int(_get_env("MAX_TRANSCRIPT_DURATION", "1800"))
    transcript_max_concurrency: int = int(_get_env("TRANSCRIPT_MAX_CONCURRENCY", "1"))
    whisper_max_concurrency: int = int(_get_env("WHISPER_MAX_CONCURRENCY", "1"))
    transcript_request_interval: float = float(_get_env("TRANSCRIPT_REQUEST_INTERVAL", "2.5"))
    transcript_rate_limit_cooldown_base: float = float(_get_env("TRANSCRIPT_COOLDOWN_BASE", "30.0"))
    transcript_rate_limit_cooldown_max: float = float(_get_env("TRANSCRIPT_COOLDOWN_MAX", "300.0"))
    transcript_max_rate_limit_retries: int = int(_get_env("TRANSCRIPT_MAX_RETRIES", "3"))
    audio_temp_dir: Path = BASE_DIR / "tmp" / "audio"
    transcript_cache_dir: Path = BASE_DIR / "data" / "transcripts"
    transcript_jobs_dir: Path = BASE_DIR / "data" / "transcript_jobs"
    max_retries: int = int(_get_env("MAX_RETRIES", "3"))
    retry_backoff_base: float = float(_get_env("RETRY_BACKOFF_BASE", "2.0"))

    # Security
    rate_limit_per_minute: int = int(_get_env("RATE_LIMIT_PER_MINUTE", "30"))
    # Comma-separated browser origins allowed for CORS. Empty = same-origin only.
    cors_origins: str = _get_env("CORS_ORIGINS", "")
    # Abuse limits on job size (each video can cost YouTube quota and STT credits)
    max_videos_per_job: int = int(_get_env("MAX_VIDEOS_PER_JOB", "100"))
    # Synchronous channel requests hold an HTTP connection for the whole run: keep them small.
    max_videos_sync_export: int = int(_get_env("MAX_VIDEOS_SYNC_EXPORT", "25"))
    max_concurrent_sync_channel_runs: int = int(_get_env("MAX_CONCURRENT_SYNC_CHANNEL_RUNS", "2"))
    # Running background jobs (each holds YouTube/Groq capacity for its whole run)
    max_active_jobs: int = int(_get_env("MAX_ACTIVE_JOBS", "4"))
    max_active_jobs_per_user: int = int(_get_env("MAX_ACTIVE_JOBS_PER_USER", "2"))

    def __init__(self) -> None:
        """Create directories on initialization."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.audio_temp_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_cache_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_jobs_dir.mkdir(parents=True, exist_ok=True)


# Singleton settings instance
try:
    settings = Settings()
except Exception:
    import logging
    logging.warning("Failed to initialize Settings directories", exc_info=True)

    class _FallbackSettings:
        youtube_api_key = _get_env("YOUTUBE_API_KEY", "")
        log_level = _get_env("LOG_LEVEL", "INFO")
        base_dir = BASE_DIR
        logs_dir = BASE_DIR / "logs"
        output_dir = BASE_DIR / "output"
        http_connect_timeout = 15
        http_read_timeout = 30
        http_max_retries = 3
        http_backoff_factor = 1.0
        youtube_max_results = 50
        youtube_batch_size = 50
        youtube_quota_warning = 8000
        youtube_daily_quota = 10000
        redis_url = _get_env("REDIS_URL", "redis://localhost:6379/0")
        export_max_videos = 50000
        export_csv_batch_size = 50
        export_timeout_minutes = 30
        rate_limit_per_minute = 30
        cors_origins = _get_env("CORS_ORIGINS", "")
        max_videos_per_job = int(_get_env("MAX_VIDEOS_PER_JOB", "1000"))
        max_videos_sync_export = int(_get_env("MAX_VIDEOS_SYNC_EXPORT", "1000"))
        openai_api_key = _get_env("OPENAI_API_KEY", "")
        groq_api_key = _get_env("GROQ_API_KEY", "")
        groq_whisper_model = _get_env("GROQ_WHISPER_MODEL", "whisper-large-v3")
        groq_translation_model = _get_env("GROQ_TRANSLATION_MODEL", "llama-3.3-70b-versatile")
        groq_timeout_seconds = int(_get_env("GROQ_TIMEOUT_SECONDS", "120"))
        groq_max_retries = int(_get_env("GROQ_MAX_RETRIES", "3"))
    settings = _FallbackSettings()  # type: ignore[assignment]


def get_settings() -> Settings:
    """Return the global settings instance."""
    return settings
