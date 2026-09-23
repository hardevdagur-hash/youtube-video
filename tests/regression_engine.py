"""Regression detection engine for comparing baseline and current outputs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RegressionResult:
    """A single regression measurement.

    Attributes
    ----------
    metric : str
        Name of the metric being compared.
    baseline : float
        Reference value.
    current : float
        Current value.
    delta : float
        Absolute difference (current - baseline).
    delta_pct : float
        Relative difference as a percentage.
    passed : bool
        Whether the change is within the acceptable threshold.
    severity : str
        One of ``"none"``, ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.
    """

    metric: str
    baseline: float
    current: float
    delta: float
    delta_pct: float
    passed: bool
    severity: str = "none"


# ---------------------------------------------------------------------------
# Regression engine
# ---------------------------------------------------------------------------


class RegressionEngine:
    """Detect and quantify regressions between baseline and current metric sets.

    Typical usage::

        engine = RegressionEngine()
        results = engine.compare_metrics(baseline_data, current_data)
        report = engine.generate_regression_report(results)
    """

    #: Severity thresholds as (upper_bound_pct, label).
    SEVERITY_THRESHOLDS: list[tuple[float, str]] = [
        (0.0, "none"),
        (1.0, "low"),
        (5.0, "medium"),
        (10.0, "high"),
    ]

    # ------------------------------------------------------------------
    # Comparison methods
    # ------------------------------------------------------------------

    def compare_metrics(
        self,
        baseline_data: dict[str, float],
        current_data: dict[str, float],
    ) -> list[RegressionResult]:
        """Compare two flat metric dictionaries and produce regression results.

        Only keys present in both dictionaries are compared.

        Parameters
        ----------
        baseline_data : dict
            Reference metric values keyed by metric name.
        current_data : dict
            Current metric values.

        Returns
        -------
        list[RegressionResult]
        """
        results: list[RegressionResult] = []
        for key in sorted(set(baseline_data) & set(current_data)):
            b = float(baseline_data[key])
            c = float(current_data[key])
            delta = c - b
            delta_pct = ((delta / b) * 100.0) if b != 0 else 0.0
            severity = self._classify_severity(delta_pct)
            passed = severity in ("none", "low")
            results.append(
                RegressionResult(
                    metric=key,
                    baseline=b,
                    current=c,
                    delta=delta,
                    delta_pct=round(delta_pct, 4),
                    passed=passed,
                    severity=severity,
                )
            )
        return results

    def detect_performance_regression(
        self,
        baseline_perf: dict[str, float],
        current_perf: dict[str, float],
    ) -> list[RegressionResult]:
        """Compare performance metric dictionaries (durations, etc.).

        Parameters
        ----------
        baseline_perf : dict
        current_perf : dict

        Returns
        -------
        list[RegressionResult]
        """
        return self.compare_metrics(baseline_perf, current_perf)

    def detect_quality_regression(
        self,
        baseline_quality: dict[str, float],
        current_quality: dict[str, float],
    ) -> list[RegressionResult]:
        """Compare quality metric dictionaries (scores, etc.).

        Parameters
        ----------
        baseline_quality : dict
        current_quality : dict

        Returns
        -------
        list[RegressionResult]
        """
        return self.compare_metrics(baseline_quality, current_quality)

    # ------------------------------------------------------------------
    # Stability & statistics
    # ------------------------------------------------------------------

    @staticmethod
    def detect_output_stability(runs_data: list[dict[str, float]]) -> dict[str, dict[str, float]]:
        """Analyse stability across multiple runs of the same job.

        For each metric present in *runs_data*, compute mean, standard
        deviation, and variance across runs.

        Parameters
        ----------
        runs_data : list[dict]
            Each element is a metric-name → value mapping from one run.

        Returns
        -------
        dict
            Metric name → ``{"mean", "std", "variance"}``.
        """
        if not runs_data:
            return {}

        all_keys: set[str] = set()
        for rd in runs_data:
            all_keys.update(rd.keys())

        stability: dict[str, dict[str, float]] = {}
        for key in sorted(all_keys):
            values = [float(rd[key]) for rd in runs_data if key in rd]
            if len(values) < 2:
                stability[key] = {
                    "mean": mean(values) if values else 0.0,
                    "std": 0.0,
                    "variance": 0.0,
                }
            else:
                m = mean(values)
                s = stdev(values)
                stability[key] = {
                    "mean": m,
                    "std": s,
                    "variance": s**2,
                }
        return stability

    @staticmethod
    def compute_statistics(values: list[float]) -> dict[str, float]:
        """Compute descriptive statistics for a list of numeric values.

        Parameters
        ----------
        values : list[float]

        Returns
        -------
        dict
            Keys: ``"mean"``, ``"median"``, ``"std"``, ``"p95"``, ``"p99"``,
            ``"min"``, ``"max"``, ``"count"``.
        """
        n = len(values)
        if n == 0:
            return {
                "mean": 0.0,
                "median": 0.0,
                "std": 0.0,
                "p95": 0.0,
                "p99": 0.0,
                "min": 0.0,
                "max": 0.0,
                "count": 0,
            }

        sorted_vals = sorted(values)
        m = mean(sorted_vals)
        med = median(sorted_vals)
        s = stdev(sorted_vals) if n > 1 else 0.0
        p95_idx = max(0, min(n - 1, int(n * 0.95)))
        p99_idx = max(0, min(n - 1, int(n * 0.99)))

        return {
            "mean": m,
            "median": med,
            "std": s,
            "p95": sorted_vals[p95_idx],
            "p99": sorted_vals[p99_idx],
            "min": sorted_vals[0],
            "max": sorted_vals[-1],
            "count": n,
        }

    # ------------------------------------------------------------------
    # Drift & significance
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_drift_score(
        baseline: dict[str, float],
        current: dict[str, float],
    ) -> float:
        """Compute an aggregate drift score (0-1) between two metric sets.

        0 means identical, 1 means complete divergence.  The score is the
        average absolute relative change across all shared metric keys,
        clamped to [0, 1].

        Parameters
        ----------
        baseline : dict
        current : dict

        Returns
        -------
        float
        """
        common = set(baseline) & set(current)
        if not common:
            return 1.0

        changes: list[float] = []
        for k in common:
            b = float(baseline[k])
            c = float(current[k])
            if b == 0:
                changes.append(0.0 if c == 0 else 1.0)
            else:
                changes.append(min(abs((c - b) / b), 1.0))

        return sum(changes) / len(changes)

    @staticmethod
    def is_significant_regression(delta_pct: float, threshold: float = 10.0) -> bool:
        """Determine whether a percentage change constitutes a significant regression.

        Parameters
        ----------
        delta_pct : float
            Relative change in percent.
        threshold : float
            Absolute threshold in percent (default 10.0).

        Returns
        -------
        bool
        """
        return abs(delta_pct) > threshold

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def generate_regression_report(results: list[RegressionResult]) -> dict[str, Any]:
        """Compile regression results into a structured report dictionary.

        Parameters
        ----------
        results : list[RegressionResult]

        Returns
        -------
        dict
            Keys: ``"summary"``, ``"results"``, ``"passed"``, ``"failed"``,
            ``"timestamp"``.
        """
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        return {
            "summary": {
                "total": len(results),
                "passed": len(passed),
                "failed": len(failed),
                "pass_rate": f"{len(passed) / max(len(results), 1) * 100:.1f}%",
            },
            "results": [RegressionEngine._result_to_dict(r) for r in results],
            "passed": [RegressionEngine._result_to_dict(r) for r in passed],
            "failed": [RegressionEngine._result_to_dict(r) for r in failed],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_severity(self, delta_pct: float) -> str:
        abs_pct = abs(delta_pct)
        for threshold, label in sorted(self.SEVERITY_THRESHOLDS, reverse=True):
            if abs_pct >= threshold:
                return label
        return "none"

    @staticmethod
    def _result_to_dict(r: RegressionResult) -> dict[str, Any]:
        return {
            "metric": r.metric,
            "baseline": r.baseline,
            "current": r.current,
            "delta": r.delta,
            "delta_pct": r.delta_pct,
            "passed": r.passed,
            "severity": r.severity,
        }


# ---------------------------------------------------------------------------
# Regression tracker
# ---------------------------------------------------------------------------


class RegressionTracker:
    """Persist and retrieve baseline snapshots for regression tracking.

    Baselines are stored as JSON files under *base_dir* / ``{run_id}.json``.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        """Initialise the tracker.

        Parameters
        ----------
        base_dir : str, optional
            Directory for baseline files.  Defaults to ``tests/regression``.
        """
        if base_dir is None:
            base_dir = str(Path(__file__).resolve().parent / "regression" / "baselines")
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def load_baseline(self, run_id: str) -> dict[str, Any]:
        """Load a baseline snapshot by its run identifier.

        Parameters
        ----------
        run_id : str

        Returns
        -------
        dict

        Raises
        ------
        FileNotFoundError
            If the baseline file does not exist.
        """
        path = self._base / f"{run_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Baseline not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_baseline(self, run_id: str, data: dict[str, Any]) -> None:
        """Persist a baseline snapshot.

        Parameters
        ----------
        run_id : str
        data : dict
        """
        path = self._base / f"{run_id}.json"
        path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )

    def list_baselines(self) -> list[str]:
        """Return run IDs of all stored baselines.

        Returns
        -------
        list[str]
            Sorted list of run IDs (file names stripped of ``.json``).
        """
        return sorted(
            p.stem for p in self._base.glob("*.json") if p.is_file()
        )

    def get_trend(self, metric: str, last_n: int = 10) -> list[float]:
        """Extract a time-series of values for a specific metric across baselines.

        Parameters
        ----------
        metric : str
            Metric key to track.
        last_n : int
            Maximum number of most recent baseline files to examine.

        Returns
        -------
        list[float]
            Values in chronological order (oldest first).
        """
        run_ids = self.list_baselines()[-last_n:]
        values: list[float] = []
        for rid in run_ids:
            try:
                data = self.load_baseline(rid)
                val = data.get(metric)
                if val is not None:
                    values.append(float(val))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
        return values
