"""A tiny in-process TTL cache.

Transitous is a free, community-run service with a fair-use policy, so we avoid
asking it again for something we already have. Caching in process (rather than
in a shared store) suits this workload: a warm Lambda container serves many
requests, the data is small, and a per-container copy going stale independently
is harmless. Nothing here needs to survive a cold start.
"""

import threading
import time
from typing import Callable, Hashable, TypeVar

T = TypeVar("T")


class TTLCache:
    """Maps a key to a value for ``ttl`` seconds, holding at most ``maxsize``.

    Values are handed out by reference, so callers must treat them as read-only.
    """

    def __init__(self, ttl: float, maxsize: int = 128):
        self.ttl = ttl
        self.maxsize = maxsize
        self._entries: dict[Hashable, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get_or_call(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return the cached value for ``key``, else call ``factory`` to make it.

        A raising ``factory`` caches nothing, so an upstream outage doesn't get
        pinned in place for the whole TTL.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > time.monotonic():
                return entry[1]

        # Deliberately outside the lock: a slow upstream call must not block
        # lookups of other keys. Two concurrent misses on the same key both call
        # through, which costs one extra request and avoids per-key locking.
        value = factory()

        with self._lock:
            if len(self._entries) >= self.maxsize:
                self._evict()
            self._entries[key] = (time.monotonic() + self.ttl, value)
        return value

    def _evict(self) -> None:
        """Drop expired entries, then the nearest to expiry if still full."""
        now = time.monotonic()
        for key in [k for k, (expires, _) in self._entries.items() if expires <= now]:
            del self._entries[key]
        if len(self._entries) >= self.maxsize:
            del self._entries[min(self._entries, key=lambda k: self._entries[k][0])]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
