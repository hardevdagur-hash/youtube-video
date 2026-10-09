"""Dedicated threads for long, blocking transcript work.

Caption fetches, audio downloads, speech-to-text and translation can hold a thread for
minutes. Running them on asyncio's default executor (``asyncio.to_thread``) would let a
burst of transcript work occupy every default thread, so short blocking calls that
also use it (password hashing at sign-in, the Google code exchange, health checks)
would queue behind it. Transcript work runs here instead, on its own bounded pool
(TRANSCRIPT_WORKER_THREADS); the default executor stays free for everything else.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            from config.settings import settings

            _executor = ThreadPoolExecutor(
                max_workers=settings.transcript_worker_threads, thread_name_prefix="transcript-work",
            )
        return _executor


async def run_transcript_work(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """``asyncio.to_thread`` on the transcript pool (request/job logging context included)."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(_get_executor(), call)


def shutdown(wait: bool = False) -> None:
    """Stop accepting work (application shutdown); running calls finish in the background."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)
