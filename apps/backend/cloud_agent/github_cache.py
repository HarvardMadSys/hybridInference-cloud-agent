"""Bounded repository answers; reads never extend an authorization's lifetime."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = get_logger(__name__)
REPOSITORY_CACHE_SIZE = 1024


class RepositoryCache[K, V]:
    """TTL by insertion time, LRU by access time, with amortized expiry work.

    Separate orderings let a hit promote an entry without keeping an old
    authorization alive or scanning the complete cache. TTL is supplied on
    each operation so saved setting changes take effect on the next read.
    """

    def __init__(self, name: str, *, max_size: int = REPOSITORY_CACHE_SIZE) -> None:
        self._name = name
        self._max_size = max_size
        self._entries: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lru: OrderedDict[K, None] = OrderedDict()
        self._expired = 0
        self._evicted = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[K]:
        return iter(self._entries)

    def pop(self, key: K) -> V | None:
        """Forget a key without returning any expired fallback on later reads."""
        entry = self._entries.pop(key, None)
        self._lru.pop(key, None)
        return entry[1] if entry is not None else None

    def _prune(self, now: float, ttl: float) -> None:
        removed = 0
        while self._entries:
            key = next(iter(self._entries))
            if now - self._entries[key][0] < ttl:
                break
            self.pop(key)
            removed += 1
        if removed:
            self._expired += removed
            self._report()

    def get(self, key: K, *, ttl: float) -> V | None:
        """Read a live entry, sweeping expired entries even on a cache miss."""
        self._prune(time.monotonic(), ttl)
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._lru.move_to_end(key)
        return entry[1]

    def remember(self, key: K, value: V, *, ttl: float) -> None:
        """Store a fresh answer and evict the least recently used excess."""
        now = time.monotonic()
        self._prune(now, ttl)
        self.pop(key)
        if ttl <= 0:
            return
        self._entries[key] = (now, value)
        self._lru[key] = None
        while len(self._entries) > self._max_size:
            self.pop(next(iter(self._lru)))
            self._evicted += 1
        self._report()

    def _report(self) -> None:
        logger.debug(
            "agent_github_repository_cache",
            extra={
                "cache": self._name,
                "entries": len(self),
                "expired_total": self._expired,
                "evicted_total": self._evicted,
            },
        )
