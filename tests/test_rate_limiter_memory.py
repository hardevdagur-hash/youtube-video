"""The rate limiter is keyed by attacker-influenced values and must stay bounded."""

from __future__ import annotations

from types import SimpleNamespace

import infrastructure.rate_limiter as rl
from infrastructure.rate_limiter import SlidingWindowRateLimiter


def test_username_spraying_cannot_grow_memory_without_bound():
    limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=900, max_keys=1000)
    for i in range(5000):
        limiter.allow(f"user:attacker-{i}")
    assert limiter.tracked_keys <= 1000


def test_read_only_checks_do_not_create_keys():
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    for i in range(100):
        assert limiter.remaining(f"user:nobody-{i}") == 3
    assert limiter.tracked_keys == 0


def test_expired_buckets_are_dropped(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(rl, "time", SimpleNamespace(time=lambda: now[0]))
    limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60)
    assert limiter.allow("ip:1")
    assert limiter.allow("ip:1")
    assert not limiter.allow("ip:1")
    now[0] += 61
    assert limiter.remaining("ip:1") == 2
    assert limiter.tracked_keys == 0
    assert limiter.allow("ip:1")


def test_eviction_keeps_active_keys_limited():
    limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=600, max_keys=2)
    assert limiter.allow("a")
    assert limiter.allow("b")
    assert limiter.allow("c")  # evicts the least recently used key
    assert limiter.tracked_keys == 2
