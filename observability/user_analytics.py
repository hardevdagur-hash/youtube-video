"""User Analytics — tracks user activity, feature usage, and engagement metrics.

Tracks:
- Daily/Monthly Active Users (DAU/MAU)
- Login and signup rates
- Session duration
- Feature usage (projects created, exports, publishing)
- Per-project activity
- User growth rate
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from observability.logger import get_logger

logger = get_logger(__name__)

PREFIX = "youtube_seo_user"

# Counters
user_logins = Counter(f"{PREFIX}_logins_total", "User logins", ["auth_method"])
user_signups = Counter(f"{PREFIX}_signups_total", "User signups", ["source"])
user_logouts = Counter(f"{PREFIX}_logouts_total", "User logouts")

projects_created = Counter(f"{PREFIX}_projects_created_total", "Projects created", ["user_id"])
exports_performed = Counter(f"{PREFIX}_exports_performed_total", "Exports performed", ["user_id", "format"])
publishing_actions = Counter(f"{PREFIX}_publishing_actions_total", "Publishing actions", ["user_id", "target"])

# Gauges
daily_active_users = Gauge(f"{PREFIX}_daily_active_users", "Daily Active Users")
monthly_active_users = Gauge(f"{PREFIX}_monthly_active_users", "Monthly Active Users")
user_growth_rate = Gauge(f"{PREFIX}_growth_rate", "User growth rate (0-1)")
user_retention_rate = Gauge(f"{PREFIX}_retention_rate", "User retention rate (0-1)")
active_sessions = Gauge(f"{PREFIX}_active_sessions", "Currently active sessions")
total_users = Gauge(f"{PREFIX}_total_users", "Total registered users")

# Feature usage gauges
feature_usage = Gauge(f"{PREFIX}_feature_usage", "Feature usage count", ["feature"])
projects_per_user = Gauge(f"{PREFIX}_projects_per_user", "Average projects per user")


@dataclass
class UserAnalyticsSnapshot:
    daily_active_users: int = 0
    monthly_active_users: int = 0
    total_users: int = 0
    new_users_today: int = 0
    total_logins: int = 0
    total_projects: int = 0
    total_exports: int = 0
    dau_mau_ratio: float = 0.0
    timestamp: str = ""


class UserAnalyticsCollector:
    """Tracks user activity, growth, and feature engagement."""

    def __init__(self):
        self._daily_logins: dict[str, int] = defaultdict(int)
        self._daily_signups: dict[str, int] = defaultdict(int)
        self._feature_counts: dict[str, int] = defaultdict(int)

    def record_login(self, auth_method: str = "email") -> None:
        user_logins.labels(auth_method=auth_method).inc()
        today = date.today().isoformat()
        self._daily_logins[today] += 1
        daily_active_users.inc()

    def record_signup(self, source: str = "direct") -> None:
        user_signups.labels(source=source).inc()
        today = date.today().isoformat()
        self._daily_signups[today] += 1
        total_users.inc()

    def record_logout(self) -> None:
        user_logouts.inc()

    def record_project_created(self, user_id: str = "") -> None:
        projects_created.labels(user_id=user_id or "unknown").inc()
        self._record_feature("projects_created")


    def record_export(self, user_id: str = "", export_format: str = "") -> None:
        exports_performed.labels(user_id=user_id or "unknown", format=export_format or "unknown").inc()
        self._record_feature("exports")

    def record_publishing(self, user_id: str = "", target: str = "") -> None:
        publishing_actions.labels(user_id=user_id or "unknown", target=target or "unknown").inc()
        self._record_feature("publishing")

    def update_dau(self, count: int) -> None:
        daily_active_users.set(count)

    def update_mau(self, count: int) -> None:
        monthly_active_users.set(count)

    def update_growth_rate(self, rate: float) -> None:
        user_growth_rate.set(max(0.0, rate))

    def update_retention_rate(self, rate: float) -> None:
        user_retention_rate.set(max(0.0, rate))

    def update_active_sessions(self, count: int) -> None:
        active_sessions.set(count)

    def update_total_users(self, count: int) -> None:
        total_users.set(count)

    def update_projects_per_user(self, avg: float) -> None:
        projects_per_user.set(avg)

    def get_dau_mau_ratio(self) -> float:
        dau = daily_active_users.collect()
        mau = monthly_active_users.collect()
        dau_val = 0
        mau_val = 1
        for s in dau:
            for sample in s.samples:
                dau_val = int(sample.value)
        for s in mau:
            for sample in s.samples:
                mau_val = int(sample.value)
        return dau_val / mau_val if mau_val > 0 else 0.0

    def get_snapshot(self) -> UserAnalyticsSnapshot:
        total_dau = sum(self._daily_logins.get(
            (date.today() - timedelta(days=i)).isoformat(), 0
        ) for i in range(1))
        total_mau = sum(self._daily_logins.get(
            (date.today() - timedelta(days=i)).isoformat(), 0
        ) for i in range(30))
        return UserAnalyticsSnapshot(
            daily_active_users=total_dau,
            monthly_active_users=total_mau or max(1, total_dau * 5),
            total_users=self._get_total_user_count(),
            new_users_today=self._daily_signups.get(date.today().isoformat(), 0),
            total_logins=self._daily_logins.get(date.today().isoformat(), 0),
            dau_mau_ratio=total_dau / max(1, total_mau),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _record_feature(self, feature: str) -> None:
        self._feature_counts[feature] += 1
        feature_usage.labels(feature=feature).set(self._feature_counts[feature])

    def _get_total_user_count(self) -> int:
        samples = list(total_users.collect())
        for s in samples:
            for sample in s.samples:
                return int(sample.value)
        return 0


_user_analytics: UserAnalyticsCollector | None = None


def get_user_analytics() -> UserAnalyticsCollector:
    global _user_analytics
    if _user_analytics is None:
        _user_analytics = UserAnalyticsCollector()
    return _user_analytics
