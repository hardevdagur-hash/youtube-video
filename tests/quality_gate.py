"""Quality gate enforcement for test and deployment pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QualityGateError(Exception):
    """Raised when a quality gate check fails."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class QualityGateResult:
    """Outcome of a single quality-gate evaluation.

    Attributes
    ----------
    name : str
        Human-readable gate name (e.g. ``"coverage"``).
    passed : bool
        Whether the gate passed.
    score : float
        The actual measured value.
    threshold : float
        The minimum acceptable value.
    details : dict
        Supplementary data (e.g. breakdown per module).
    severity : {"critical", "high", "medium", "low"}
        Severity level of this gate.
    """

    name: str
    passed: bool
    score: float
    threshold: float
    details: dict = field(default_factory=dict)
    severity: str = "medium"


# ---------------------------------------------------------------------------
# Gate definitions
# ---------------------------------------------------------------------------


class QualityGate:
    """Collection of quality-gate checks used in CI/CD pipelines.

    Typical usage::

        gates = QualityGate()
        results = gates.evaluate_all({"coverage": 94.2, "bugs": 0, ...})
        if gates.should_deploy(results):
            print("All critical/high gates passed.")
    """

    #: Default gate names and their thresholds.
    ALL_GATES: dict[str, float] = {
        "coverage": 95.0,
        "grammar_score": 95.0,
        "readability_score": 80.0,
        "regression_drift": 0.1,
        "performance_degradation_pct": 10.0,
        "critical_bugs": 0,
    }

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def check_coverage(
        coverage_pct: float,
        threshold: float = 95.0,
    ) -> QualityGateResult:
        """Check that line-coverage meets the minimum threshold.

        Parameters
        ----------
        coverage_pct : float
            Current line-coverage percentage.
        threshold : float
            Minimum acceptable coverage (default 95.0).

        Returns
        -------
        QualityGateResult
        """
        passed = coverage_pct >= threshold
        return QualityGateResult(
            name="coverage",
            passed=passed,
            score=coverage_pct,
            threshold=threshold,
            severity="critical",
        )

    @staticmethod
    def check_grammar_score(
        score: float,
        threshold: float = 95.0,
    ) -> QualityGateResult:
        """Check that the grammar score meets the threshold.

        Parameters
        ----------
        score : float
            Grammar score (0-100).
        threshold : float
            Minimum acceptable score (default 95.0).

        Returns
        -------
        QualityGateResult
        """
        passed = score >= threshold
        return QualityGateResult(
            name="grammar_score",
            passed=passed,
            score=score,
            threshold=threshold,
            severity="high",
        )

    @staticmethod
    def check_readability_score(
        score: float,
        threshold: float = 80.0,
    ) -> QualityGateResult:
        """Check that the readability score meets the threshold.

        Parameters
        ----------
        score : float
            Readability score (0-100).
        threshold : float
            Minimum acceptable score (default 80.0).

        Returns
        -------
        QualityGateResult
        """
        passed = score >= threshold
        return QualityGateResult(
            name="readability_score",
            passed=passed,
            score=score,
            threshold=threshold,
            severity="medium",
        )

    @staticmethod
    def check_regression_drift(
        baseline: dict[str, float],
        current: dict[str, float],
        threshold: float = 0.1,
    ) -> QualityGateResult:
        """Measure drift between baseline and current metric sets.

        The drift score is the average absolute relative change across all
        shared metric keys.  A score below *threshold* passes.

        Parameters
        ----------
        baseline : dict
            Reference metric values.
        current : dict
            Current metric values.
        threshold : float
            Maximum acceptable drift (default 0.1 = 10 %).

        Returns
        -------
        QualityGateResult
        """
        common_keys = set(baseline) & set(current)
        if not common_keys:
            return QualityGateResult(
                name="regression_drift",
                passed=True,
                score=0.0,
                threshold=threshold,
                details={"note": "no shared metrics to compare"},
                severity="high",
            )

        rel_changes: list[float] = []
        diffs: dict[str, float] = {}
        for k in common_keys:
            b = baseline[k]
            c = current[k]
            diffs[k] = c - b
            if b != 0:
                rel_changes.append(abs((c - b) / b))

        drift = sum(rel_changes) / len(rel_changes) if rel_changes else 0.0
        passed = drift <= threshold
        return QualityGateResult(
            name="regression_drift",
            passed=passed,
            score=drift,
            threshold=threshold,
            details={"keys_compared": list(common_keys), "diffs": diffs},
            severity="high",
        )

    @staticmethod
    def check_performance_degradation(
        baseline_ms: float,
        current_ms: float,
        threshold_pct: float = 10.0,
    ) -> QualityGateResult:
        """Check that execution time hasn't degraded beyond a percentage.

        Parameters
        ----------
        baseline_ms : float
            Reference execution time in milliseconds.
        current_ms : float
            Current execution time in milliseconds.
        threshold_pct : float
            Maximum acceptable degradation as a percentage (default 10.0).

        Returns
        -------
        QualityGateResult
        """
        if baseline_ms <= 0:
            return QualityGateResult(
                name="performance_degradation",
                passed=True,
                score=0.0,
                threshold=threshold_pct,
                details={"note": "baseline_ms was zero or negative; skipping"},
                severity="high",
            )

        degradation_pct = ((current_ms - baseline_ms) / baseline_ms) * 100.0
        passed = degradation_pct <= threshold_pct
        return QualityGateResult(
            name="performance_degradation",
            passed=passed,
            score=degradation_pct,
            threshold=threshold_pct,
            details={
                "baseline_ms": baseline_ms,
                "current_ms": current_ms,
                "degradation_pct": round(degradation_pct, 2),
            },
            severity="critical",
        )

    @staticmethod
    def check_security(result: dict[str, Any]) -> QualityGateResult:
        """Evaluate a security-scan result.

        The gate passes when the result contains no critical or high findings.

        Parameters
        ----------
        result : dict
            Security scan output.  Expected keys: ``"critical"``, ``"high"``,
            ``"medium"``, ``"low"`` with integer counts.

        Returns
        -------
        QualityGateResult
        """
        critical = result.get("critical", 0)
        high = result.get("high", 0)
        passed = critical == 0 and high == 0
        total = critical + high + result.get("medium", 0) + result.get("low", 0)
        return QualityGateResult(
            name="security",
            passed=passed,
            score=float(total),
            threshold=0.0,
            details=result,
            severity="critical",
        )

    @staticmethod
    def check_critical_bugs(bug_count: int) -> QualityGateResult:
        """Fail the gate when there is at least one critical bug.

        Parameters
        ----------
        bug_count : int
            Number of known critical bugs.

        Returns
        -------
        QualityGateResult
        """
        passed = bug_count == 0
        return QualityGateResult(
            name="critical_bugs",
            passed=passed,
            score=float(bug_count),
            threshold=0.0,
            severity="critical",
        )

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def evaluate_all(self, results_dict: dict[str, Any]) -> list[QualityGateResult]:
        """Run every applicable gate check based on keys present in *results_dict*.

        Parameters
        ----------
        results_dict : dict
            Dictionary of metric values keyed by gate name.  For example::

                {
                    "coverage": 94.2,
                    "seo_score": 88.0,
                    "grammar_score": 96.0,
                    "readability_score": 82.0,
                    "regression_drift": {"baseline": {...}, "current": {...}},
                    "performance_degradation": {"baseline_ms": 100, "current_ms": 115},
                    "security": {"critical": 0, "high": 1},
                    "critical_bugs": 0,
                }

        Returns
        -------
        list[QualityGateResult]
        """
        gates: list[QualityGateResult] = []

        if "coverage" in results_dict:
            gates.append(self.check_coverage(float(results_dict["coverage"])))


        if "grammar_score" in results_dict:
            gates.append(self.check_grammar_score(float(results_dict["grammar_score"])))

        if "readability_score" in results_dict:
            gates.append(self.check_readability_score(float(results_dict["readability_score"])))

        if "regression_drift" in results_dict:
            rd = results_dict["regression_drift"]
            if isinstance(rd, dict) and "baseline" in rd and "current" in rd:
                gates.append(self.check_regression_drift(rd["baseline"], rd["current"]))

        if "performance_degradation" in results_dict:
            pd = results_dict["performance_degradation"]
            if isinstance(pd, dict) and "baseline_ms" in pd and "current_ms" in pd:
                gates.append(
                    self.check_performance_degradation(
                        float(pd["baseline_ms"]), float(pd["current_ms"])
                    )
                )

        if "security" in results_dict:
            sec = results_dict["security"]
            if isinstance(sec, dict):
                gates.append(self.check_security(sec))

        if "critical_bugs" in results_dict:
            gates.append(self.check_critical_bugs(int(results_dict["critical_bugs"])))

        if not gates:
            gates.append(
                QualityGateResult(
                    name="no_gates_configured",
                    passed=True,
                    score=0.0,
                    threshold=0.0,
                    details={"note": "no matching keys found in results_dict"},
                    severity="low",
                )
            )

        return gates

    # ------------------------------------------------------------------
    # Decision helpers
    # ------------------------------------------------------------------

    @staticmethod
    def should_deploy(gate_results: list[QualityGateResult]) -> bool:
        """Return ``True`` only if every critical and high gate passed.

        Parameters
        ----------
        gate_results : list[QualityGateResult]

        Returns
        -------
        bool
        """
        for g in gate_results:
            if g.severity in ("critical", "high") and not g.passed:
                return False
        return True

    @staticmethod
    def summarize(gate_results: list[QualityGateResult]) -> str:
        """Produce a human-readable summary of all gate results.

        Parameters
        ----------
        gate_results : list[QualityGateResult]

        Returns
        -------
        str
        """
        lines: list[str] = ["=== Quality Gate Summary ==="]
        passed_count = sum(1 for g in gate_results if g.passed)
        lines.append(f"  Passed: {passed_count}/{len(gate_results)}")
        lines.append("")

        failed = [g for g in gate_results if not g.passed]
        if failed:
            lines.append("  Failed Gates:")
            for g in failed:
                lines.append(
                    f"    - {g.name} [{g.severity}]: score={g.score:.2f}, "
                    f"threshold={g.threshold:.2f}"
                )
            lines.append("")

        for g in gate_results:
            status = "PASS" if g.passed else "FAIL"
            lines.append(f"  [{status}] {g.name:30s} {g.score:8.2f} / {g.threshold:.2f}")

        lines.append("")
        deploy = QualityGate.should_deploy(gate_results)
        lines.append(f"  Deploy decision: {'APPROVED' if deploy else 'BLOCKED'}")
        return "\n".join(lines)
