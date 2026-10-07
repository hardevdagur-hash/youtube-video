from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from observability.config import ObservabilityConfig
from observability.metrics import MetricsManager


def _mm(registry: CollectorRegistry | None = None) -> MetricsManager:
    return MetricsManager(ObservabilityConfig(metrics_enabled=False), registry=registry)


def test_two_managers_share_identical_metric_on_same_registry():
    registry = CollectorRegistry()
    first = _mm(registry).counter("shared_total", "route")
    second = _mm(registry).counter("shared_total", "route")
    assert first is second
    first.labels(route="/a").inc()
    assert second.labels(route="/a")._value.get() == 1.0


def test_unlabelled_metrics_reused_across_managers():
    registry = CollectorRegistry()
    _mm(registry).inc("jobs_total")
    _mm(registry).inc("jobs_total")
    assert registry.get_sample_value("youtube_seo_jobs_total") == 2.0


def test_conflicting_definition_still_raises():
    registry = CollectorRegistry()
    _mm(registry).counter("conflict_total", "route")
    with pytest.raises(ValueError, match="Duplicated timeseries"):
        _mm(registry).counter("conflict_total", "status")  # different label set
    with pytest.raises(ValueError, match="Duplicated timeseries"):
        _mm(registry).gauge("conflict_total", "route")  # different metric type


def test_separate_registries_are_isolated():
    a, b = CollectorRegistry(), CollectorRegistry()
    _mm(a).inc("isolated_total")
    assert a.get_sample_value("youtube_seo_isolated_total") == 1.0
    assert b.get_sample_value("youtube_seo_isolated_total") is None


def test_default_registry_is_process_global(isolated_metrics_registry):
    mm = MetricsManager(ObservabilityConfig(metrics_enabled=False))
    assert mm._registry is isolated_metrics_registry  # the conftest-patched default
