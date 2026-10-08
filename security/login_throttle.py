"""Password-login brute-force protection that cannot be turned against other users.

A plain "lock the username after N failures" rule lets anyone lock any account out
by deliberately failing its login. This throttle instead slows the *source* of the
failures, with three independent checks:

* per client IP: at most ``ip_per_minute`` attempts per minute, whatever the username;
* per (client IP, username): after ``free_failures`` failures, every further attempt
  must wait ``backoff_base_seconds * 2**(failures - free_failures)`` seconds (capped at
  ``max_backoff_seconds``). Only the failing IP is slowed for that account; the record
  is cleared by a successful login and forgotten after ``failure_ttl_seconds`` without
  failures;
* per username across all IPs: at most ``user_per_minute`` attempts per minute, which
  bounds distributed guessing. Because each IP is already throttled, saturating this
  needs many IPs at once, and the legitimate user is affected for at most one minute
  after such an attack stops.

All state is in process (single-instance deployment) and bounded in memory: keys are
attacker-influenced, so stale records are dropped first and then the least recently
used ones.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock

from infrastructure.rate_limiter import MAX_KEYS, SlidingWindowRateLimiter

# 2**32 * any positive base already exceeds every sensible cap; bounds the arithmetic.
_MAX_EXPONENT = 32


@dataclass(frozen=True)
class LoginDecision:
    """Outcome of a pre-authentication throttle check."""

    allowed: bool
    retry_after: int = 0  # whole seconds (>= 1) when not allowed
    reason: str = ""  # "ip_rate" | "backoff" | "user_rate"; for server logs only


class LoginThrottle:
    """Throttle password logins per IP, per (IP, username) and per username."""

    def __init__(
        self,
        *,
        ip_per_minute: int,
        user_per_minute: int,
        free_failures: int,
        backoff_base_seconds: float,
        max_backoff_seconds: float,
        failure_ttl_seconds: float | None = None,
        max_keys: int = MAX_KEYS,
    ) -> None:
        if free_failures < 1 or backoff_base_seconds <= 0 or max_backoff_seconds < backoff_base_seconds:
            raise ValueError("free_failures >= 1 and 0 < backoff_base_seconds <= max_backoff_seconds are required")
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._ip = SlidingWindowRateLimiter(ip_per_minute, 60.0, max_keys)
        self._user = SlidingWindowRateLimiter(user_per_minute, 60.0, max_keys)
        self._free_failures = free_failures
        self._base = float(backoff_base_seconds)
        self._max_backoff = float(max_backoff_seconds)
        # Must outlive the longest backoff, or a patient attacker would start over.
        self._ttl = float(failure_ttl_seconds) if failure_ttl_seconds is not None else max(3600.0, 2 * self._max_backoff)
        if self._ttl <= self._max_backoff:
            raise ValueError("failure_ttl_seconds must exceed max_backoff_seconds")
        self._max_keys = max_keys
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()  # pair -> (count, last failure)
        self._lock = Lock()

    # -- keys -----------------------------------------------------------------

    @staticmethod
    def _user_key(username: str) -> str:
        return username.strip().lower()[:64]

    @classmethod
    def _pair_key(cls, ip: str, username: str) -> str:
        return f"{ip}\x00{cls._user_key(username)}"

    # -- backoff state (caller holds the lock) ----------------------------------

    def _live_record(self, pair: str, now: float) -> tuple[int, float] | None:
        record = self._failures.get(pair)
        if record is not None and now - record[1] > self._ttl:
            del self._failures[pair]
            return None
        return record

    def _delay_for(self, failures: int) -> float:
        if failures < self._free_failures:
            return 0.0
        exponent = min(failures - self._free_failures, _MAX_EXPONENT)
        return min(self._base * (2 ** exponent), self._max_backoff)

    def _evict(self, now: float) -> None:
        for stale in [k for k, (_, last) in self._failures.items() if now - last > self._ttl]:
            del self._failures[stale]
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)

    # -- public API -------------------------------------------------------------

    def backoff_remaining(self, ip: str, username: str) -> float:
        """Seconds the (IP, username) pair must still wait; 0 when it may try now."""
        now = time.time()
        with self._lock:
            record = self._live_record(self._pair_key(ip, username), now)
            if record is None:
                return 0.0
            failures, last = record
            return max(0.0, last + self._delay_for(failures) - now)

    def check(self, ip: str, username: str) -> LoginDecision:
        """Decide whether a login attempt may proceed; counts it against the rate budgets."""
        wait = self.backoff_remaining(ip, username)
        if wait > 0:
            return LoginDecision(False, max(1, math.ceil(wait)), "backoff")
        if not self._ip.allow(ip):
            return LoginDecision(False, 60, "ip_rate")
        if not self._user.allow(self._user_key(username)):
            return LoginDecision(False, 60, "user_rate")
        return LoginDecision(True)

    def record_failure(self, ip: str, username: str) -> int:
        """Register a failed attempt; returns the pair's failure count."""
        now = time.time()
        pair = self._pair_key(ip, username)
        with self._lock:
            record = self._live_record(pair, now)
            failures = (record[0] if record else 0) + 1
            self._failures[pair] = (failures, now)
            self._failures.move_to_end(pair)
            if len(self._failures) > self._max_keys:
                self._evict(now)
            return failures

    def record_success(self, ip: str, username: str) -> None:
        """A successful login clears that pair's failure history."""
        with self._lock:
            self._failures.pop(self._pair_key(ip, username), None)

    @property
    def tracked_pairs(self) -> int:
        with self._lock:
            return len(self._failures)
