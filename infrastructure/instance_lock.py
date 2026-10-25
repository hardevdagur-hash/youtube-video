"""Single-instance guard for the data directory.

Background jobs, rate limiters and the speech-to-text gate live in process memory, and
startup marks every job still "running" on disk as interrupted. A second worker or
replica on the same DATA_DIR would pause the first one's jobs and overwrite its
checkpoints, so startup takes an exclusive OS lock on ``DATA_DIR/.instance.lock`` and
refuses to serve when another process holds it. The OS drops the lock when the process
exits, so a crash or kill never leaves a stale lock behind.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

LOCK_FILE_NAME = ".instance.lock"


class InstanceLockError(RuntimeError):
    """Another process already serves this data directory."""


def _try_lock(handle: IO[str]) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)  # msvcrt locks bytes from the current position: always byte 0
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def check_worker_count() -> None:
    """Fail fast when the server was told to fork several workers (uvicorn/gunicorn read WEB_CONCURRENCY)."""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    try:
        workers = int(raw) if raw else 1
    except ValueError:
        workers = 1
    if workers > 1:
        raise InstanceLockError(
            f"WEB_CONCURRENCY={workers}: this service must run as a single worker process "
            "(job state and rate limits are held in memory). Set WEB_CONCURRENCY=1."
        )


class InstanceLock:
    """Exclusive, non-blocking lock on ``<data_dir>/.instance.lock`` held for the process lifetime."""

    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir) / LOCK_FILE_NAME
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 (held open while locked)
        if not _try_lock(handle):
            handle.close()
            raise InstanceLockError(
                f"Another process is already serving {self.path.parent} (lock {self.path.name} is held). "
                "Run exactly one worker/replica per DATA_DIR."
            )
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        logger.info("Acquired single-instance lock %s (pid %d)", self.path, os.getpid())

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError as exc:
            logger.warning("Could not release instance lock %s: %s", self.path, exc)
        finally:
            handle.close()

    @property
    def held(self) -> bool:
        return self._handle is not None
