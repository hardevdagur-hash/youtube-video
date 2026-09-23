"""Benchmark execution framework for timing and measuring pipeline stages."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    """Outcome of a single benchmark execution.

    Attributes
    ----------
    name : str
        Human-readable benchmark name.
    category : str
        Logical category (e.g. ``"pipeline"``, ``"llm"``, ``"export"``).
    duration_ms : float
        Wall-clock execution time in milliseconds.
    metrics : dict
        Arbitrary key-value metrics collected during the benchmark.
    success : bool
        Whether the benchmark completed without error.
    error : str | None
        Error message if *success* is ``False``.
    timestamp : datetime
        When the benchmark was executed.
    """

    name: str
    category: str
    duration_ms: float
    metrics: dict = field(default_factory=dict)
    success: bool = True
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Execute benchmark functions, collect metrics, and produce reports.

    Typical usage::

        runner = BenchmarkRunner()
        result = runner.run_single("my_bench", my_func, arg1, arg2)
        runner.export_json([result], "bench_results.json")
    """

    def __init__(self, baseline_dir: str | None = None) -> None:
        """Initialise the runner.

        Parameters
        ----------
        baseline_dir : str, optional
            Directory for storing baseline comparison data.  Defaults to
            ``tests/benchmarks``.
        """
        if baseline_dir is None:
            baseline_dir = str(Path(__file__).resolve().parent / "benchmarks")
        self._baseline_dir = Path(baseline_dir)
        self._baseline_dir.mkdir(parents=True, exist_ok=True)

    # -- Execution -----------------------------------------------------------

    def run_single(
        self,
        benchmark_name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> BenchmarkResult:
        """Execute a single benchmark function and time it.

        Parameters
        ----------
        benchmark_name : str
            Unique name for this benchmark.
        func : Callable
            The function to benchmark.
        *args, **kwargs :
            Passed through to *func*.

        Returns
        -------
        BenchmarkResult
        """
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            duration_s = time.perf_counter() - start
            duration_ms = duration_s * 1000.0

            metrics: dict[str, Any] = {}
            if isinstance(result, dict):
                metrics = result
            elif hasattr(result, "_asdict"):
                metrics = result._asdict()
            elif hasattr(result, "__dataclass_fields__"):
                metrics = asdict(result)

            return BenchmarkResult(
                name=benchmark_name,
                category=kwargs.pop("_category", "general"),
                duration_ms=duration_ms,
                metrics=metrics,
                success=True,
            )
        except Exception as exc:
            duration_s = time.perf_counter() - start
            return BenchmarkResult(
                name=benchmark_name,
                category=kwargs.pop("_category", "general"),
                duration_ms=duration_s * 1000.0,
                metrics={},
                success=False,
                error=str(exc),
            )

    def run_category(self, category: str) -> list[BenchmarkResult]:
        """Run all benchmarks registered in a given category.

        Subclasses or external code should register benchmarks via
        :meth:`register_category`.  This default implementation returns an
        empty list; override for actual benchmark registration.

        Parameters
        ----------
        category : str

        Returns
        -------
        list[BenchmarkResult]
        """
        _ = category
        return []

    def run_all(self) -> list[BenchmarkResult]:
        """Run every registered benchmark across all categories.

        Returns
        -------
        list[BenchmarkResult]
        """
        # Override in subclasses with concrete benchmark registrations.
        return []

    # -- Baseline comparison -------------------------------------------------

    def compare_with_baseline(self, results: list[BenchmarkResult]) -> dict[str, Any]:
        """Compare benchmark results against previously stored baselines.

        For each result, if a matching baseline file exists
        (``{name}.json``), the duration and metrics are compared.

        Parameters
        ----------
        results : list[BenchmarkResult]

        Returns
        -------
        dict
            Keys: ``"comparisons"`` (list of per-benchmark comparison dicts),
            ``"regressions"`` (list of names where duration increased > 10%).
        """
        comparisons: list[dict] = []
        regressions: list[str] = []

        for r in results:
            baseline_path = self._baseline_dir / f"{r.name}.json"
            comp: dict[str, Any] = {
                "name": r.name,
                "current_duration_ms": r.duration_ms,
                "baseline_duration_ms": None,
                "delta_ms": None,
                "delta_pct": None,
                "regression": False,
            }

            if baseline_path.is_file():
                try:
                    baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
                    baseline_ms = baseline_data.get("duration_ms", 0.0)
                    comp["baseline_duration_ms"] = baseline_ms
                    if baseline_ms > 0:
                        delta = r.duration_ms - baseline_ms
                        delta_pct = (delta / baseline_ms) * 100.0
                        comp["delta_ms"] = round(delta, 2)
                        comp["delta_pct"] = round(delta_pct, 2)
                        comp["regression"] = delta_pct > 10.0
                        if delta_pct > 10.0:
                            regressions.append(r.name)
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            comparisons.append(comp)

        return {
            "comparisons": comparisons,
            "regressions": regressions,
            "total_compared": len(comparisons),
            "total_regressions": len(regressions),
        }

    # -- Reporting -----------------------------------------------------------

    def generate_report(self, results: list[BenchmarkResult]) -> str:
        """Generate a Markdown report from benchmark results.

        Parameters
        ----------
        results : list[BenchmarkResult]

        Returns
        -------
        str
            Markdown-formatted report.
        """
        lines: list[str] = [
            "# Benchmark Report",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total Benchmarks | {len(results)} |",
            f"| Passed | {sum(1 for r in results if r.success)} |",
            f"| Failed | {sum(1 for r in results if not r.success)} |",
            "",
            "## Detailed Results",
            "",
        ]

        for r in results:
            status = "✓" if r.success else "✗"
            lines.append(f"### {status} {r.name}")
            lines.append("")
            lines.append(f"- **Category**: {r.category}")
            lines.append(f"- **Duration**: {r.duration_ms:.2f} ms")
            if r.error:
                lines.append(f"- **Error**: {r.error}")
            if r.metrics:
                lines.append("- **Metrics**:")
                for k, v in r.metrics.items():
                    lines.append(f"  - {k}: {v}")
            lines.append("")

        return "\n".join(lines)

    def export_json(self, results: list[BenchmarkResult], path: str) -> None:
        """Export benchmark results as a JSON file.

        Parameters
        ----------
        results : list[BenchmarkResult]
        path : str
            Output file path.
        """
        data = [_benchmark_to_dict(r) for r in results]
        Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    # -- Utility -------------------------------------------------------------

    @staticmethod
    def measure_time(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
        """Execute *func* and return ``(result, duration_ms)``.

        Parameters
        ----------
        func : Callable
        *args, **kwargs :
            Passed through to *func*.

        Returns
        -------
        tuple[Any, float]
            ``(return_value, duration_in_milliseconds)``.
        """
        start = time.perf_counter()
        result = func(*args, **kwargs)
        duration_ms = (time.perf_counter() - start) * 1000.0
        return result, duration_ms


# ---------------------------------------------------------------------------
# Pipeline-specific benchmark
# ---------------------------------------------------------------------------


class PipelineBenchmark:
    """Pre-defined benchmarks for pipeline stages, LLM calls, and exports.

    Usage::

        pb = PipelineBenchmark()
        result = pb.benchmark_full_pipeline()
    """

    def __init__(self, runner: BenchmarkRunner | None = None) -> None:
        """Initialise with an optional :class:`BenchmarkRunner` instance.

        Parameters
        ----------
        runner : BenchmarkRunner, optional
            If not provided, a new one is created.
        """
        self._runner = runner or BenchmarkRunner()

    def benchmark_full_pipeline(self) -> BenchmarkResult:
        """Benchmark an end-to-end pipeline execution.

        Returns
        -------
        BenchmarkResult
        """
        return self._runner.run_single(
            "full_pipeline",
            self._stub_pipeline,
            _category="pipeline",
        )

    def benchmark_stage(self, stage_name: str) -> BenchmarkResult:
        """Benchmark a single pipeline stage.

        Parameters
        ----------
        stage_name : str
            E.g. ``"transcript"``, ``"metadata"``.

        Returns
        -------
        BenchmarkResult
        """
        return self._runner.run_single(
            f"stage_{stage_name}",
            self._stub_stage,
            stage_name,
            _category="pipeline",
        )

    def benchmark_llm_call(self, model_name: str) -> BenchmarkResult:
        """Benchmark an LLM invocation for a given model.

        Parameters
        ----------
        model_name : str
            E.g. ``"gpt-4"``, ``"claude-3"``.

        Returns
        -------
        BenchmarkResult
        """
        return self._runner.run_single(
            f"llm_{model_name}",
            self._stub_llm_call,
            model_name,
            _category="llm",
        )

    def benchmark_export(self, fmt: str) -> BenchmarkResult:
        """Benchmark export of a project in the specified format.

        Parameters
        ----------
        fmt : str
            E.g. ``"markdown"``, ``"html"``, ``"pdf"``.

        Returns
        -------
        BenchmarkResult
        """
        return self._runner.run_single(
            f"export_{fmt}",
            self._stub_export,
            fmt,
            _category="export",
        )

    # -- Stubs (override in subclasses with real logic) ----------------------

    @staticmethod
    def _stub_pipeline() -> dict[str, Any]:
        """Stub: simulate a full pipeline run.

        Returns
        -------
        dict
        """
        time.sleep(0.05)
        return {"status": "completed"}

    @staticmethod
    def _stub_stage(stage_name: str) -> dict[str, Any]:
        """Stub: simulate a single pipeline stage.

        Parameters
        ----------
        stage_name : str

        Returns
        -------
        dict
        """
        _ = stage_name
        time.sleep(0.01)
        return {"stage": stage_name, "status": "ok"}

    @staticmethod
    def _stub_llm_call(model_name: str) -> dict[str, Any]:
        """Stub: simulate an LLM API call.

        Parameters
        ----------
        model_name : str

        Returns
        -------
        dict
        """
        _ = model_name
        time.sleep(0.02)
        return {"model": model_name, "tokens": 150, "status": "ok"}

    @staticmethod
    def _stub_export(fmt: str) -> dict[str, Any]:
        """Stub: simulate an export operation.

        Parameters
        ----------
        fmt : str

        Returns
        -------
        dict
        """
        _ = fmt
        time.sleep(0.03)
        return {"format": fmt, "pages": 5, "status": "exported"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _benchmark_to_dict(r: BenchmarkResult) -> dict[str, Any]:
    return {
        "name": r.name,
        "category": r.category,
        "duration_ms": r.duration_ms,
        "metrics": r.metrics,
        "success": r.success,
        "error": r.error,
        "timestamp": r.timestamp.isoformat(),
    }
