from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ObservabilityConfig:
    service_name: str = field(
        default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "youtube-export-platform")
    )
    service_version: str = field(
        default_factory=lambda: os.getenv("APP_VERSION", "1.0.0")
    )
    environment: str = field(
        default_factory=lambda: os.getenv("APP_ENV", "development")
    )

    tracing_enabled: bool = field(
        default_factory=lambda: os.getenv("OTEL_TRACING_ENABLED", "true").lower() == "true"
    )
    tracing_exporter: Literal["console", "otlp", "jaeger", "zipkin", "none"] = field(
        default_factory=lambda: os.getenv("OTEL_TRACING_EXPORTER", "none")
    )
    tracing_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    )
    tracing_sample_rate: float = field(
        default_factory=lambda: float(os.getenv("OTEL_TRACE_SAMPLE_RATE", "1.0"))
    )

    metrics_enabled: bool = field(
        default_factory=lambda: os.getenv("OTEL_METRICS_ENABLED", "true").lower() == "true"
    )
    metrics_port: int = field(
        default_factory=lambda: int(os.getenv("PROMETHEUS_METRICS_PORT", "9090"))
    )
    metrics_prefix: str = field(
        default_factory=lambda: os.getenv("METRICS_PREFIX", "youtube_seo")
    )

    logging_enabled: bool = field(
        default_factory=lambda: os.getenv("STRUCTURED_LOGGING_ENABLED", "true").lower() == "true"
    )
    logging_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )
    logging_format: Literal["json", "console"] = field(
        default_factory=lambda: os.getenv("LOG_FORMAT", "json")
    )
    logging_file: str | None = field(
        default_factory=lambda: os.getenv("LOG_FILE", None)
    )

    sentry_enabled: bool = field(
        default_factory=lambda: os.getenv("SENTRY_ENABLED", "false").lower() == "true"
    )
    sentry_dsn: str = field(
        default_factory=lambda: os.getenv("SENTRY_DSN", "")
    )
    sentry_sample_rate: float = field(
        default_factory=lambda: float(os.getenv("SENTRY_SAMPLE_RATE", "1.0"))
    )

    otel_enabled: bool = field(
        default_factory=lambda: os.getenv("OTEL_ENABLED", "true").lower() == "true"
    )
    otel_instrument_fastapi: bool = field(
        default_factory=lambda: os.getenv("OTEL_INSTRUMENT_FASTAPI", "true").lower() == "true"
    )
    otel_instrument_sqlalchemy: bool = field(
        default_factory=lambda: os.getenv("OTEL_INSTRUMENT_SQLALCHEMY", "true").lower() == "true"
    )
    otel_instrument_httpx: bool = field(
        default_factory=lambda: os.getenv("OTEL_INSTRUMENT_HTTPX", "true").lower() == "true"
    )
    otel_instrument_celery: bool = field(
        default_factory=lambda: os.getenv("OTEL_INSTRUMENT_CELERY", "true").lower() == "true"
    )

    health_check_port: int = field(
        default_factory=lambda: int(os.getenv("HEALTH_CHECK_PORT", "8080"))
    )
    health_check_interval: int = field(
        default_factory=lambda: int(os.getenv("HEALTH_CHECK_INTERVAL", "30"))
    )

    cost_tracking_enabled: bool = field(
        default_factory=lambda: os.getenv("COST_TRACKING_ENABLED", "true").lower() == "true"
    )
    token_tracking_enabled: bool = field(
        default_factory=lambda: os.getenv("TOKEN_TRACKING_ENABLED", "true").lower() == "true"
    )

    audit_logging_enabled: bool = field(
        default_factory=lambda: os.getenv("AUDIT_LOGGING_ENABLED", "true").lower() == "true"
    )

    alerting_enabled: bool = field(
        default_factory=lambda: os.getenv("ALERTING_ENABLED", "true").lower() == "true"
    )

    @classmethod
    def from_env(cls) -> ObservabilityConfig:
        return cls()

    def to_dict(self) -> dict[str, str | int | float | bool | None]:
        return {
            "service_name": self.service_name,
            "service_version": self.service_version,
            "environment": self.environment,
            "tracing_enabled": self.tracing_enabled,
            "metrics_enabled": self.metrics_enabled,
            "metrics_port": self.metrics_port,
            "logging_enabled": self.logging_enabled,
            "logging_level": self.logging_level,
            "logging_format": self.logging_format,
            "sentry_enabled": self.sentry_enabled,
            "otel_enabled": self.otel_enabled,
            "health_check_port": self.health_check_port,
        }
