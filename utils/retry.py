"""Retry utility with exponential backoff for flaky API calls."""

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)


def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable:
    """Decorator: retry a function with exponential backoff on failure.

    Args:
        max_retries: Maximum number of retries (default 3).
        base_delay: Initial delay in seconds (default 1.0).
        max_delay: Maximum delay cap in seconds (default 60.0).
        backoff: Multiplier for each retry (default 2.0 → 1, 2, 4, 8…).
        exceptions: Tuple of exception types that trigger a retry.

    Usage::

        @retry(exceptions=(IOError, TimeoutError))
        def fetch_data():
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            delay = base_delay
            for attempt in range(1 + max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        logger.warning(
                            "%s attempt %d/%d failed: %s. Retrying in %.1fs…",
                            func.__name__,
                            attempt + 1,
                            max_retries,
                            exc,
                            delay,
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff, max_delay)
                    else:
                        logger.error(
                            "%s failed after %d retries: %s",
                            func.__name__,
                            max_retries,
                            exc,
                        )
            if last_exc:
                raise last_exc
            return None
        return wrapper
    return decorator
