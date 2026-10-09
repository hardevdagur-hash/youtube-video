"""Process-wide limit on concurrent speech-to-text work (WHISPER_MAX_CONCURRENCY).

Each speech-to-text run downloads a video's audio with yt-dlp and uploads it to Groq
(or runs local Whisper): minutes of a worker thread, bandwidth and paid credits. Every
STT path (the single-video endpoint, channel jobs and exports) takes a slot here first,
so the total stays bounded however many requests and jobs run at once. A request that
cannot get a slot within STT_QUEUE_TIMEOUT_SECONDS fails with the retryable STT_BUSY.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class STTBusyError(RuntimeError):
    """No speech-to-text slot became free in time (retryable)."""

    error_code = "STT_BUSY"


class STTGate:
    def __init__(self, max_concurrency: int, queue_timeout_seconds: float) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.max_concurrency = max_concurrency
        self._timeout = queue_timeout_seconds
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0

    @contextmanager
    def slot(self, video_id: str) -> Iterator[None]:
        with self._lock:
            self._waiting += 1
        try:
            acquired = self._slots.acquire(timeout=self._timeout)
        finally:
            with self._lock:
                self._waiting -= 1
        if not acquired:
            logger.warning("No speech-to-text slot for %s within %.0fs (limit %d)",
                           video_id, self._timeout, self.max_concurrency)
            raise STTBusyError(f"speech-to-text is busy (limit {self.max_concurrency})")
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._slots.release()

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"limit": self.max_concurrency, "active": self._active, "waiting": self._waiting}


def _build_gate() -> STTGate:
    from config.settings import settings

    return STTGate(settings.whisper_max_concurrency, settings.stt_queue_timeout_seconds)


stt_gate = _build_gate()
