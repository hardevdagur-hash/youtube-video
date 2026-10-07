from __future__ import annotations

"""Conftest for observability tests."""

import pytest
from prometheus_client import CollectorRegistry


@pytest.fixture(autouse=True)
def isolated_metrics_registry(monkeypatch):
    """Give each test its own Prometheus registry.

    MetricsManager defaults to prometheus_client's process-global REGISTRY, so metric
    names registered by one test would otherwise leak into every later test.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr("observability.metrics.REGISTRY", registry)
    return registry
