"""Unit tests for security/login_throttle.py (no HTTP)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import infrastructure.rate_limiter as rl
import security.login_throttle as lt
from security.login_throttle import LoginThrottle

pytestmark = pytest.mark.security


@pytest.fixture
def clock(monkeypatch):
    now = [5_000_000.0]
    fake = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(rl, "time", fake)
    monkeypatch.setattr(lt, "time", fake)
    return now


def _throttle(**overrides) -> LoginThrottle:
    values = {
        "ip_per_minute": 1000, "user_per_minute": 1000, "free_failures": 3,
        "backoff_base_seconds": 2.0, "max_backoff_seconds": 900,
    }
    values.update(overrides)
    return LoginThrottle(**values)


def test_free_failures_then_backoff(clock):
    t = _throttle()
    for _ in range(2):
        t.record_failure("1.1.1.1", "alice")
        assert t.check("1.1.1.1", "alice").allowed
    t.record_failure("1.1.1.1", "alice")
    decision = t.check("1.1.1.1", "alice")
    assert not decision.allowed and decision.reason == "backoff" and decision.retry_after == 2


def test_backoff_is_scoped_to_the_ip_and_username_pair(clock):
    t = _throttle()
    for _ in range(10):
        t.record_failure("1.1.1.1", "alice")
    assert not t.check("1.1.1.1", "alice").allowed
    assert t.check("2.2.2.2", "alice").allowed  # same user, other address
    assert t.check("1.1.1.1", "bob").allowed  # same address, other user


def test_username_is_case_insensitive(clock):
    t = _throttle()
    for _ in range(3):
        t.record_failure("1.1.1.1", "Alice")
    assert not t.check("1.1.1.1", " alice ").allowed


def test_failure_history_expires(clock):
    t = _throttle(failure_ttl_seconds=3600)
    for _ in range(20):
        t.record_failure("1.1.1.1", "alice")
    clock[0] += 3601
    assert t.check("1.1.1.1", "alice").allowed
    assert t.record_failure("1.1.1.1", "alice") == 1  # counting restarted


def test_huge_failure_counts_stay_capped(clock):
    t = _throttle()
    for _ in range(500):
        t.record_failure("1.1.1.1", "alice")
    assert t.backoff_remaining("1.1.1.1", "alice") == 900


def test_ip_rate_applies_across_usernames(clock):
    t = _throttle(ip_per_minute=3)
    assert all(t.check("1.1.1.1", f"user{i}").allowed for i in range(3))
    decision = t.check("1.1.1.1", "user99")
    assert decision.reason == "ip_rate" and decision.retry_after == 60


def test_memory_is_bounded_against_key_spraying(clock):
    t = _throttle(max_keys=100)
    for i in range(1000):
        t.record_failure(f"10.0.{i // 256}.{i % 256}", f"user{i}")
    assert t.tracked_pairs <= 100


@pytest.mark.parametrize("overrides", [
    {"free_failures": 0},
    {"backoff_base_seconds": 0},
    {"max_backoff_seconds": 1, "backoff_base_seconds": 2},
    {"failure_ttl_seconds": 900},
    {"max_keys": 0},
])
def test_rejects_unsafe_configuration(overrides):
    with pytest.raises(ValueError):
        _throttle(**overrides)
