from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from observability.config import ObservabilityConfig


class TestObservabilityConfig:
    def test_default_values(self):
        config = ObservabilityConfig()
        assert config.service_name == "youtube-export-platform"
        assert config.environment == "development"
        assert config.tracing_enabled is True
        assert config.metrics_enabled is True
        assert config.logging_level == "INFO"
        assert config.logging_format == "json"

    def test_from_env(self):
        config = ObservabilityConfig.from_env()
        assert config.service_name is not None

    def test_env_overrides(self):
        with patch.dict(os.environ, {
            "OTEL_SERVICE_NAME": "test-service",
            "APP_ENV": "production",
            "LOG_LEVEL": "DEBUG",
            "PROMETHEUS_METRICS_PORT": "9999",
        }):
            config = ObservabilityConfig()
            assert config.service_name == "test-service"
            assert config.environment == "production"
            assert config.logging_level == "DEBUG"
            assert config.metrics_port == 9999

    def test_to_dict(self):
        config = ObservabilityConfig()
        d = config.to_dict()
        assert d["service_name"] == "youtube-export-platform"
        assert d["tracing_enabled"] is True
        assert d["health_check_port"] == 8080
