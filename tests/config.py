"""Test configuration using pydantic-settings with sensible defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings


class TestSettings(BaseSettings):
    """Centralised test configuration loaded from environment variables.

    All fields have sensible defaults for local development.  In CI they are
    typically overridden via environment variables or a ``.env`` file.
    """

    model_config: ClassVar[dict] = {
        "env_prefix": "TEST_",
        "extra": "ignore",
    }

    # -- Mode flags -----------------------------------------------------------
    TEST_MODE: bool = True
    """Globally enable/disable test mode."""

    COVERAGE_THRESHOLD: float = 95.0
    """Minimum acceptable line-coverage percentage (0-100)."""

    BENCHMARK_DIR: str = str(Path(__file__).resolve().parent / "benchmarks")
    """Directory where benchmark result files are stored."""

    GOLDEN_OUTPUT_DIR: str = str(Path(__file__).resolve().parent / "golden_outputs")
    """Directory containing golden (expected) output files."""

    # -- Mock flags -----------------------------------------------------------
    MOCK_YOUTUBE: bool = True
    """When True, replace YouTube API calls with canned responses."""

    # -- Test-suite toggles ---------------------------------------------------
    PERFORMANCE_ENABLED: bool = False
    """Enable performance / benchmark tests (often skipped by default)."""

    STRESS_ENABLED: bool = False
    """Enable long-running stress tests."""

    CHAOS_ENABLED: bool = False
    """Enable chaos-engineering tests that deliberately inject faults."""

    SECURITY_ENABLED: bool = True
    """Enable security-oriented tests (SAST, dependency checks, etc.)."""

    E2E_ENABLED: bool = False
    """Enable full end-to-end tests that exercise the entire pipeline."""

    REGRESSION_ENABLED: bool = True
    """Enable regression detection tests."""

    # -- CI / reporting -------------------------------------------------------
    CI_MODE: bool = False
    """Set to True when running in a CI pipeline (adjusts output formats)."""

    REPORT_DIR: str = str(Path(__file__).resolve().parent / "reports")
    """Directory where generated test reports are written."""

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, **override: bool | str | float) -> TestSettings:
        """Create an instance populated from the environment, with optional overrides.

        Parameters
        ----------
        **override :
            Keyword arguments that override specific fields.

        Returns
        -------
        TestSettings
            Fully resolved settings instance.
        """
        return cls(**override)

    def __init__(self, **kwargs: bool | str | float) -> None:
        """Initialise settings, applying any keyword overrides on top of env vars."""
        super().__init__(**kwargs)

        # Ensure output directories exist at construction time.
        for d in (self.BENCHMARK_DIR, self.GOLDEN_OUTPUT_DIR, self.REPORT_DIR):
            Path(d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_ci(self) -> bool:
        """Short-hand for checking whether we are in a CI environment."""
        return self.CI_MODE

    @property
    def use_mocks(self) -> bool:
        """Whether *any* external-service mocking is enabled."""
        return self.MOCK_YOUTUBE
