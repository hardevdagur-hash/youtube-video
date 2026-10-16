"""Process-wide limit on concurrent speech-to-text work (WHISPER_MAX_CONCURRENCY).

Each speech-to-text run downloads a video's audio with yt-dlp and uploads it to Groq
(or runs local Whisper): minutes of a worker thread, bandwidth and paid credits. Every
STT path (the single-video endpoint, channel jobs and exports) takes a slot here first,
so the total stays bounded however many requests and jobs run at once. A request that
cannot get a slot within STT_QUEUE_TIMEOUT_SECONDS fails with the retryable STT_BUSY.

Waiting must not tie up the transcript worker pool (TRANSCRIPT_WORKER_THREADS): a thread
blocked on a slot for up to the queue timeout is a thread caption fetches cannot use.

* Callers on the event loop that know they need speech-to-text (channel jobs and
  channel runs, after captions failed) wait with :meth:`STTGate.reserve` *before*
  handing work to the pool. The reservation travels with the context into the worker
  thread, where :meth:`STTGate.slot` then uses it instead of taking a second slot.
* Code that only finds out inside a worker thread (the single-video pipeline) waits in
  :meth:`STTGate.slot`, but at most STT_MAX_THREAD_WAITERS threads wait at once; any
  further request fails fast with STT_BUSY instead of parking another pool thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

logger = logging.getLogger(__name__)

# Set while the current context holds a slot taken by :meth:`STTGate.reserve`.
_reserved: contextvars.ContextVar[STTGate | None] = contextvars.ContextVar("stt_reserved_slot", default=None)

_POLL_MIN_SECONDS = 0.05
_POLL_MAX_SECONDS = 1.0


class STTBusyError(RuntimeError):
    """No speech-to-text slot became free in time (retryable)."""

    error_code = "STT_BUSY"


class STTGate:
    def __init__(
        self,
        max_concurrency: int,
        queue_timeout_seconds: float,
        max_thread_waiters: int | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_thread_waiters is not None and max_thread_waiters < 0:
            raise ValueError("max_thread_waiters must not be negative")
        self.max_concurrency = max_concurrency
        self._timeout = queue_timeout_seconds
        self._max_thread_waiters = max_thread_waiters  # None: unbounded
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0  # all waiters (threads and event-loop reservations)
        self._thread_waiting = 0

    def _busy(self, video_id: str, reason: str) -> STTBusyError:
        logger.warning("No speech-to-text slot for %s: %s (limit %d)", video_id, reason, self.max_concurrency)
        return STTBusyError(f"speech-to-text is busy (limit {self.max_concurrency})")

    @contextmanager
    def _held(self) -> Iterator[None]:
        with self._lock:
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._slots.release()

    @contextmanager
    def slot(self, video_id: str) -> Iterator[None]:
        """Hold a slot for blocking speech-to-text work (call from a worker thread)."""
        if _reserved.get() is self:
            yield  # the event loop already reserved this context's slot
            return
        if self._slots.acquire(blocking=False):
            with self._held():
                yield
            return
        with self._lock:
            if self._max_thread_waiters is not None and self._thread_waiting >= self._max_thread_waiters:
                raise self._busy(video_id, f"{self._thread_waiting} worker thread(s) already waiting")
            self._thread_waiting += 1
            self._waiting += 1
        try:
            acquired = self._slots.acquire(timeout=self._timeout)
        finally:
            with self._lock:
                self._thread_waiting -= 1
                self._waiting -= 1
        if not acquired:
            raise self._busy(video_id, f"none free within {self._timeout:.0f}s")
        with self._held():
            yield

    @asynccontextmanager
    async def reserve(self, video_id: str) -> AsyncIterator[None]:
        """Wait for a slot on the event loop, without occupying a worker thread.

        Work handed to the transcript pool inside this block (``run_transcript_work``
        copies the context) runs its :meth:`slot` on the reserved slot. Tasks created
        inside the block inherit the reservation too, so start one video's work per block.
        """
        if _reserved.get() is self:
            yield
            return
        acquired = self._slots.acquire(blocking=False)
        if not acquired:
            with self._lock:
                self._waiting += 1
            try:
                deadline = time.monotonic() + self._timeout
                delay = _POLL_MIN_SECONDS
                while not (acquired := self._slots.acquire(blocking=False)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(delay, remaining))
                    delay = min(delay * 2, _POLL_MAX_SECONDS)
            finally:
                with self._lock:
                    self._waiting -= 1
        if not acquired:
            raise self._busy(video_id, f"none free within {self._timeout:.0f}s")
        token = _reserved.set(self)
        try:
            with self._held():
                yield
        finally:
            _reserved.reset(token)

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"limit": self.max_concurrency, "active": self._active, "waiting": self._waiting}


def _build_gate() -> STTGate:
    from config.settings import settings

    return STTGate(
        settings.whisper_max_concurrency,
        settings.stt_queue_timeout_seconds,
        max_thread_waiters=settings.stt_max_thread_waiters,
    )


stt_gate = _build_gate()
