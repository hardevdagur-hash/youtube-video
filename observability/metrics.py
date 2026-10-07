from __future__ import annotations

import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Generator, TypeVar

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram, start_http_server
from prometheus_client.metrics import MetricWrapperBase

from observability.config import ObservabilityConfig

F = TypeVar("F", bound=Callable[..., Any])


class MetricsManager:
    def __init__(self, config: ObservabilityConfig, registry: CollectorRegistry | None = None):
        self._config = config
        self._prefix = config.metrics_prefix
        self._initialized = False
        self._metrics: dict[str, MetricWrapperBase] = {}
        # Defaults to the process-wide registry served by start_http_server(); tests and
        # embedded uses can pass an isolated CollectorRegistry.
        self._registry = registry if registry is not None else REGISTRY

    def _create(self, cls: type, name: str, label_names: list[str] | tuple[str, ...] = ()) -> Any:
        """Register a metric, reusing an identical one already in the registry.

        Several MetricsManager instances share the default registry; registering the
        same metric twice would otherwise raise ``Duplicated timeseries``.
        """
        full_name = self._name(name)
        try:
            return cls(full_name, name, list(label_names), registry=self._registry)
        except ValueError:
            existing = getattr(self._registry, "_names_to_collectors", {}).get(full_name)
            if (
                isinstance(existing, cls)
                and tuple(getattr(existing, "_labelnames", ())) == tuple(label_names)
            ):
                return existing
            raise

    def initialize(self) -> None:
        if self._initialized or not self._config.metrics_enabled:
            return
        try:
            start_http_server(self._config.metrics_port)
        except OSError:
            pass
        self._initialized = True

    def _name(self, name: str) -> str:
        return f"{self._prefix}_{name}"

    def _labels(self, metric_type: str, name: str, label_names: list[str]) -> Any:
        key = f"{metric_type}:{name}:{':'.join(sorted(label_names))}"
        if key not in self._metrics:
            cls = {"counter": Counter, "gauge": Gauge, "histogram": Histogram}[metric_type]
            self._metrics[key] = self._create(cls, name, label_names)
        return self._metrics[key]

    def _get_counter(self, name: str) -> Counter:
        key = f"counter:{name}:"
        if key not in self._metrics:
            self._metrics[key] = self._create(Counter, name)
        return self._metrics[key]

    def _get_gauge(self, name: str) -> Gauge:
        key = f"gauge:{name}:"
        if key not in self._metrics:
            self._metrics[key] = self._create(Gauge, name)
        return self._metrics[key]

    def _get_histogram(self, name: str) -> Histogram:
        key = f"histogram:{name}:"
        if key not in self._metrics:
            self._metrics[key] = self._create(Histogram, name)
        return self._metrics[key]

    def counter(self, name: str, *label_names: str) -> Counter:
        if not label_names:
            return self._get_counter(name)
        return self._labels("counter", name, list(label_names))

    def gauge(self, name: str, *label_names: str) -> Gauge:
        if not label_names:
            return self._get_gauge(name)
        return self._labels("gauge", name, list(label_names))

    def histogram(self, name: str, *label_names: str) -> Histogram:
        if not label_names:
            return self._get_histogram(name)
        return self._labels("histogram", name, list(label_names))

    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        if not labels:
            self._get_counter(name).inc(value)
        else:
            self.counter(name, *labels.keys()).labels(*labels.values()).inc(value)

    def set(self, name: str, value: float, **labels: str) -> None:
        if not labels:
            self._get_gauge(name).set(value)
        else:
            self.gauge(name, *labels.keys()).labels(*labels.values()).set(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        if not labels:
            self._get_histogram(name).observe(value)
        else:
            self.histogram(name, *labels.keys()).labels(*labels.values()).observe(value)

    def observe_latency(self, name: str, **labels: str) -> Callable[[], None]:
        start = time.monotonic()
        def _record() -> None:
            self.observe(name, time.monotonic() - start, **labels)
        return _record

    @contextmanager
    def measure_duration(self, name: str, **labels: str) -> Generator[None, None, None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - start, **labels)

    def instrument(self, name: str, label_names: tuple[str, ...] | None = None) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                labels = {}
                if label_names:
                    for ln in label_names:
                        if ln in kwargs:
                            labels[ln] = str(kwargs[ln])
                self.inc(f"{name}_calls_total", **labels)
                start = time.monotonic()
                try:
                    result = func(*args, **kwargs)
                    self.inc(f"{name}_success_total", **labels)
                    return result
                except Exception:
                    self.inc(f"{name}_errors_total", **labels)
                    raise
                finally:
                    self.observe(f"{name}_duration_seconds", time.monotonic() - start, **labels)
            return wrapper
        return decorator
