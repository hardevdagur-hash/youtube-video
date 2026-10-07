"""Bounded in-memory cache with TTL and LRU eviction.

Used by the transcript repository as the hot layer in front of the on-disk cache.
It is shared by request handlers running in worker threads, so all operations take
a lock, and it never grows past ``max_entries`` (least recently used entries are
evicted first; expired entries are dropped on access and on insert).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar

T = TypeVar("T")

DEFAULT_MAX_ENTRIES = 256


class CacheEntry:
    """A single cache entry with expiry."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl_seconds: float) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class TTLCache(Generic[T]):
    """Thread-safe TTL cache bounded to ``max_entries`` items (LRU eviction).

    Usage::

        cache = TTLCache[str](ttl_seconds=300, max_entries=100)
        cache.set("key", "value")
        value = cache.get("key")  # "value" or None
    """

    def __init__(self, ttl_seconds: float = 300, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> T | None:
        """Return the cached value, or ``None`` if missing or expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return entry.value  # type: ignore[no-any-return]

    def set(self, key: str, value: T, ttl_seconds: float | None = None) -> None:
        """Insert or replace ``key``; evicts expired, then least recently used, entries."""
        with self._lock:
            self._store[key] = CacheEntry(value, ttl_seconds or self._ttl)
            self._store.move_to_end(key)
            if len(self._store) > self._max_entries:
                for stale in [k for k, e in self._store.items() if e.is_expired()]:
                    del self._store[stale]
                while len(self._store) > self._max_entries:
                    self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        """Remove a key from the cache."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        """Number of entries currently held (expired entries may be counted until evicted)."""
        with self._lock:
            return len(self._store)
