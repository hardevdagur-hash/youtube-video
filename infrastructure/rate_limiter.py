"""In-process sliding-window rate limiter with bounded memory.

Keys are attacker-influenced (client IPs, attempted usernames), so the limiter must
not grow without bound: empty buckets are dropped, read-only checks never create
keys, and the number of tracked keys is capped (stale keys are evicted first, then
the least recently used ones).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock

MAX_KEYS = 50_000


class SlidingWindowRateLimiter:
    """Allow at most ``max_requests`` events per ``window_seconds`` per key."""

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0, max_keys: int = MAX_KEYS) -> None:
        if max_requests < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("max_requests, window_seconds and max_keys must be positive")
        self._max_requests = max_requests
        self._window = window_seconds
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = Lock()

    def _live(self, key: str, now: float) -> list[float]:
        """Unexpired timestamps for ``key`` (caller holds the lock); drops empty buckets."""
        timestamps = self._buckets.get(key)
        if timestamps is None:
            return []
        cutoff = now - self._window
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if not timestamps:
            del self._buckets[key]
        return timestamps

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        for stale in [k for k, ts in self._buckets.items() if not ts or ts[-1] <= cutoff]:
            del self._buckets[stale]
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)

    def allow(self, key: str = "default") -> bool:
        now = time.time()
        with self._lock:
            timestamps = self._live(key, now)
            if len(timestamps) >= self._max_requests:
                return False
            if not timestamps:
                self._buckets[key] = timestamps
            timestamps.append(now)
            self._buckets.move_to_end(key)
            if len(self._buckets) > self._max_keys:
                self._evict(now)
            return True

    def remaining(self, key: str = "default") -> int:
        now = time.time()
        with self._lock:
            return max(0, self._max_requests - len(self._live(key, now)))

    def reset(self, key: str = "default") -> None:
        with self._lock:
            self._buckets.pop(key, None)

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)
