"""Centralized Rate Limiter, Pacing, and Circuit Breaker for YouTube Transcripts.

Provides global pacing, exponential backoff with jitter, and circuit breaker
cooldowns to prevent YouTube anti-bot and rate-limiting (HTTP 429) triggers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal operation: requests pass with pacing
    OPEN = "open"            # Cooldown active: requests paused/rejected
    HALF_OPEN = "half_open"  # Probing: one request allowed to verify recovery


@dataclass
class LimiterMetrics:
    total_requests: int = 0
    total_rate_limits: int = 0
    total_cooldowns: int = 0
    last_rate_limit_time: float = 0.0
    last_request_time: float = 0.0
    consecutive_rate_limits: int = 0
    current_cooldown_duration: float = 0.0


class TranscriptRateLimiter:
    """Thread-safe & async-safe global rate limiter and circuit breaker."""

    def __init__(
        self,
        min_interval_seconds: float = 2.5,
        jitter_seconds: float = 0.8,
        cooldown_base_seconds: float = 30.0,
        cooldown_max_seconds: float = 300.0,
        max_rate_limit_retries: int = 3,
    ) -> None:
        self.min_interval = min_interval_seconds
        self.jitter = jitter_seconds
        self.cooldown_base = cooldown_base_seconds
        self.cooldown_max = cooldown_max_seconds
        self.max_retries = max_rate_limit_retries

        self._state = CircuitState.CLOSED
        self._cooldown_until = 0.0
        self._last_request_time = 0.0
        self._consecutive_rate_limits = 0
        self._async_lock = asyncio.Lock()
        self.metrics = LimiterMetrics()

    @property
    def state(self) -> CircuitState:
        now = time.time()
        if self._state == CircuitState.OPEN and now >= self._cooldown_until:
            self._state = CircuitState.HALF_OPEN
        return self._state

    def is_in_cooldown(self) -> bool:
        return self.state == CircuitState.OPEN

    def remaining_cooldown_seconds(self) -> float:
        now = time.time()
        if self.state == CircuitState.OPEN and self._cooldown_until > now:
            return round(self._cooldown_until - now, 1)
        return 0.0

    async def acquire(self, video_id: str | None = None) -> float:
        """Acquire permission to execute a YouTube transcript request.

        If in cooldown, waits for the cooldown to expire.
        Enforces min_interval + jitter spacing between consecutive requests.

        Returns:
            The number of seconds waited.
        """
        async with self._async_lock:
            waited = 0.0
            now = time.time()

            # 1. Handle Cooldown if circuit is OPEN
            if self.state == CircuitState.OPEN:
                wait_time = self._cooldown_until - now
                if wait_time > 0:
                    logger.warning(
                        "[TranscriptLimiter] Circuit OPEN. Pausing %s for %.1fs cooldown...",
                        video_id or "request",
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    waited += wait_time
                    self._state = CircuitState.HALF_OPEN

            # 2. Enforce minimum request spacing + jitter
            now = time.time()
            elapsed_since_last = now - self._last_request_time
            jitter_offset = random.uniform(-self.jitter, self.jitter)
            target_delay = max(0.5, self.min_interval + jitter_offset)

            if elapsed_since_last < target_delay:
                pace_delay = target_delay - elapsed_since_last
                logger.debug(
                    "[TranscriptLimiter] Pacing delay %.2fs for %s",
                    pace_delay,
                    video_id or "request",
                )
                await asyncio.sleep(pace_delay)
                waited += pace_delay

            self._last_request_time = time.time()
            self.metrics.total_requests += 1
            self.metrics.last_request_time = self._last_request_time
            return waited

    def record_rate_limit(self, video_id: str | None = None) -> float:
        """Record a RATE_LIMITED (HTTP 429) event from YouTube.

        Trips the circuit breaker into OPEN and computes exponential
        backoff delay with jitter.

        Returns:
            Computed cooldown duration in seconds.
        """
        now = time.time()
        self._consecutive_rate_limits += 1
        self.metrics.total_rate_limits += 1
        self.metrics.consecutive_rate_limits = self._consecutive_rate_limits
        self.metrics.last_rate_limit_time = now

        # Exponential backoff: base * 2^(consecutive - 1) + jitter
        exponent = min(self._consecutive_rate_limits - 1, 5)
        raw_cooldown = self.cooldown_base * (2 ** exponent)
        jitter_amount = random.uniform(0.5, 3.0)
        cooldown = min(self.cooldown_max, raw_cooldown + jitter_amount)

        self._state = CircuitState.OPEN
        self._cooldown_until = now + cooldown
        self.metrics.total_cooldowns += 1
        self.metrics.current_cooldown_duration = cooldown

        logger.warning(
            "[TranscriptLimiter] YouTube RATE_LIMITED on %s! Circuit OPEN for %.1fs (attempt #%d)",
            video_id or "request",
            cooldown,
            self._consecutive_rate_limits,
        )
        return cooldown

    def record_success(self, video_id: str | None = None) -> None:
        """Record a successful transcript acquisition.

        Resets consecutive rate limit count and returns circuit to CLOSED.
        """
        if self._state != CircuitState.CLOSED or self._consecutive_rate_limits > 0:
            logger.info(
                "[TranscriptLimiter] Recovery confirmed on %s. Circuit returned to CLOSED.",
                video_id or "request",
            )
        self._state = CircuitState.CLOSED
        self._cooldown_until = 0.0
        self._consecutive_rate_limits = 0
        self.metrics.consecutive_rate_limits = 0

    def get_status(self) -> dict[str, Any]:
        """Return diagnostic state dictionary."""
        return {
            "state": self.state.value,
            "in_cooldown": self.is_in_cooldown(),
            "remaining_cooldown_seconds": self.remaining_cooldown_seconds(),
            "consecutive_rate_limits": self._consecutive_rate_limits,
            "min_interval_seconds": self.min_interval,
            "total_requests": self.metrics.total_requests,
            "total_rate_limits": self.metrics.total_rate_limits,
            "total_cooldowns": self.metrics.total_cooldowns,
        }


def get_transcript_limiter() -> TranscriptRateLimiter:
    """Return the global TranscriptRateLimiter initialized with settings."""
    try:
        from config.settings import settings
        return TranscriptRateLimiter(
            min_interval_seconds=getattr(settings, "transcript_request_interval", 2.5),
            cooldown_base_seconds=getattr(settings, "transcript_rate_limit_cooldown_base", 30.0),
            cooldown_max_seconds=getattr(settings, "transcript_rate_limit_cooldown_max", 300.0),
            max_rate_limit_retries=getattr(settings, "transcript_max_rate_limit_retries", 3),
        )
    except Exception:
        return TranscriptRateLimiter()


# Global singleton instance configured with production defaults
transcript_limiter = get_transcript_limiter()
