import time

import pytest

from services.transcript_limiter import CircuitState, TranscriptRateLimiter


def test_limiter_initial_state():
    limiter = TranscriptRateLimiter(
        min_interval_seconds=0.1,
        cooldown_base_seconds=2.0,
        cooldown_max_seconds=10.0,
    )
    assert limiter.state == CircuitState.CLOSED
    assert limiter.is_in_cooldown() is False
    assert limiter.remaining_cooldown_seconds() == 0.0
    status = limiter.get_status()
    assert status["state"] == "closed"
    assert status["in_cooldown"] is False
    assert status["consecutive_rate_limits"] == 0


@pytest.mark.asyncio
async def test_limiter_pacing():
    limiter = TranscriptRateLimiter(min_interval_seconds=0.15, jitter_seconds=0.0)
    t0 = time.time()
    await limiter.acquire("test_vid_1")
    await limiter.acquire("test_vid_2")
    elapsed = time.time() - t0
    # Second acquire must have waited around min_interval
    assert elapsed >= 0.12


def test_limiter_record_rate_limit_and_backoff():
    limiter = TranscriptRateLimiter(
        min_interval_seconds=0.01,
        jitter_seconds=0.0,
        cooldown_base_seconds=5.0,
        cooldown_max_seconds=20.0,
    )

    # Trigger 1st rate limit (5 * 2^0 + jitter)
    cooldown1 = limiter.record_rate_limit("vid_1")
    assert 5.0 <= cooldown1 <= 8.5
    assert limiter.state == CircuitState.OPEN
    assert limiter.is_in_cooldown() is True
    assert limiter.remaining_cooldown_seconds() > 0

    # Trigger 2nd rate limit -> exponential backoff (5 * 2^1 = 10 + jitter)
    cooldown2 = limiter.record_rate_limit("vid_2")
    assert 10.0 <= cooldown2 <= 13.5
    assert limiter._consecutive_rate_limits == 2

    # Trigger 3rd rate limit -> exponential backoff (5 * 2^2 = 20 max capped)
    cooldown3 = limiter.record_rate_limit("vid_3")
    assert cooldown3 == 20.0


def test_limiter_cooldown_expiration_and_recovery():
    limiter = TranscriptRateLimiter(
        min_interval_seconds=0.01,
        jitter_seconds=0.0,
        cooldown_base_seconds=0.05,
        cooldown_max_seconds=0.5,
    )
    # Mock jitter to 0 for instant test
    limiter.record_rate_limit("test_cooldown")
    # Manually set cooldown until to short time in past/future
    limiter._cooldown_until = time.time() + 0.1
    assert limiter.state == CircuitState.OPEN

    # Sleep past cooldown
    time.sleep(0.12)
    assert limiter.remaining_cooldown_seconds() == 0.0
    assert limiter.state == CircuitState.HALF_OPEN

    # Record success -> resets to CLOSED
    limiter.record_success("test_recovery")
    assert limiter.state == CircuitState.CLOSED
    assert limiter._consecutive_rate_limits == 0
    assert limiter.is_in_cooldown() is False
