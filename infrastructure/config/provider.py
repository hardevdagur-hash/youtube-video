"""Centralized configuration provider.

All configuration is read from environment variables (or .env file)
through this provider. No module should read os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Environment(str, Enum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    TESTING = "testing"
    QA = "qa"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass
class DatabaseConfig:
    url: str = "sqlite+aiosqlite:///./data/app.db"
    pool_size: int = 10
    max_overflow: int = 20
    echo: bool = False
    migrate: bool = True


@dataclass
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    enabled: bool = True


@dataclass
class CacheConfig:
    backend: str = "memory"
    default_ttl: int = 300
    enabled: bool = True


@dataclass
class AIConfig:
    openai_api_key: str = ""
    max_retries: int = 3
    timeout: int = 120


@dataclass
class YouTubeConfig:
    api_key: str = ""
    api_service_name: str = "youtube"
    api_version: str = "v3"
    quota_limit: int = 10000


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "*"
    log_level: str = "INFO"
    workers: int = 4
    debug: bool = False


@dataclass
class ExportConfig:
    output_dir: str = "./exports"
    max_file_size: int = 50 * 1024 * 1024
    allowed_formats: list[str] = field(
        default_factory=lambda: ["csv", "json"]
    )


@dataclass
class SecurityConfig:
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    rate_limit_per_minute: int = 60
    max_body_size: int = 10 * 1024 * 1024


@dataclass
class ObservabilityConfig:
    sentry_dsn: str = ""
    otel_enabled: bool = False
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    log_format: str = "json"


@dataclass
class FeatureFlagConfig:
    env_prefix: str = "FF_"
    store_backend: str = "env"


@dataclass
class AppConfig:
    environment: Environment = Environment.LOCAL
    app_name: str = "youtube-export-platform"
    version: str = "3.0.0"
    debug: bool = False
    secret_key: str = ""

    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    feature_flags: FeatureFlagConfig = field(default_factory=FeatureFlagConfig)


class ConfigProvider:
    """Provides typed configuration from environment variables.

    Usage:
        config = ConfigProvider.load()
        db_url = config.database.url
    """

    @staticmethod
    def load(env_file: str | None = None) -> AppConfig:
        if env_file:
            _load_dotenv(env_file)

        env_name = os.environ.get("APP_ENV", "local").lower()
        try:
            environment = Environment(env_name)
        except ValueError:
            environment = Environment.LOCAL

        return AppConfig(
            environment=environment,
            app_name=os.environ.get("APP_NAME", "youtube-export-platform"),
            version=os.environ.get("APP_VERSION", "3.0.0"),
            debug=os.environ.get("DEBUG", "0").lower() in ("1", "true"),
            secret_key=os.environ.get("SECRET_KEY", ""),
            database=DatabaseConfig(
                url=os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db"),
                pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
                max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
                echo=os.environ.get("DB_ECHO", "0").lower() in ("1", "true"),
                migrate=os.environ.get("DB_MIGRATE", "1").lower() in ("1", "true"),
            ),
            redis=RedisConfig(
                url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                enabled=os.environ.get("REDIS_ENABLED", "1").lower() in ("1", "true"),
            ),
            cache=CacheConfig(
                backend=os.environ.get("CACHE_BACKEND", "memory"),
                default_ttl=int(os.environ.get("CACHE_TTL", "300")),
                enabled=os.environ.get("CACHE_ENABLED", "1").lower() in ("1", "true"),
            ),
            ai=AIConfig(
                openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
                max_retries=int(os.environ.get("AI_MAX_RETRIES", "3")),
                timeout=int(os.environ.get("AI_TIMEOUT", "120")),
            ),
            youtube=YouTubeConfig(
                api_key=os.environ.get("YOUTUBE_API_KEY", ""),
                api_service_name=os.environ.get("YOUTUBE_API_NAME", "youtube"),
                api_version=os.environ.get("YOUTUBE_API_VERSION", "v3"),
                quota_limit=int(os.environ.get("YOUTUBE_QUOTA", "10000")),
            ),
            server=ServerConfig(
                host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")),
                cors_origins=os.environ.get("CORS_ORIGINS", "*"),
                log_level=os.environ.get("LOG_LEVEL", "INFO"),
                workers=int(os.environ.get("WORKERS", "4")),
                debug=os.environ.get("DEBUG", "0").lower() in ("1", "true"),
            ),
            export=ExportConfig(
                output_dir=os.environ.get("EXPORT_DIR", "./exports"),
                max_file_size=int(os.environ.get("EXPORT_MAX_SIZE", str(50 * 1024 * 1024))),
            ),
            security=SecurityConfig(
                jwt_secret=os.environ.get("JWT_SECRET", ""),
                jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
                jwt_expiry_hours=int(os.environ.get("JWT_EXPIRY", "24")),
                rate_limit_per_minute=int(os.environ.get("RATE_LIMIT", "60")),
                max_body_size=int(os.environ.get("MAX_BODY_SIZE", str(10 * 1024 * 1024))),
            ),
            observability=ObservabilityConfig(
                sentry_dsn=os.environ.get("SENTRY_DSN", ""),
                otel_enabled=os.environ.get("OTEL_ENABLED", "0").lower() in ("1", "true"),
                metrics_enabled=os.environ.get("METRICS_ENABLED", "1").lower() in ("1", "true"),
                tracing_enabled=os.environ.get("TRACING_ENABLED", "0").lower() in ("1", "true"),
            ),
        )


class Config:
    """Singleton accessor for app configuration."""
    _instance: AppConfig | None = None

    @classmethod
    def get(cls) -> AppConfig:
        if cls._instance is None:
            cls._instance = ConfigProvider.load()
        return cls._instance

    @classmethod
    def set(cls, config: AppConfig) -> None:
        cls._instance = config

    @classmethod
    def reload(cls) -> AppConfig:
        cls._instance = ConfigProvider.load()
        return cls._instance


def _load_dotenv(path: str | None = None) -> None:
    """Load .env file if it exists."""
    if path is None:
        path = ".env"
    env_path = Path(path)
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and not os.environ.get(key):
                os.environ.setdefault(key, value)
