"""Bounded, request-locality aware memory caches for query work."""

from __future__ import annotations

import pickle
import time
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class _Entry:
    value: object
    size: int
    expires_at: float


class QueryCaches:
    """Two independent LRU pools sharing one hard byte ceiling."""

    def __init__(
        self,
        *,
        clock: MonotonicClock = time,
        embedding_capacity: int = 256,
        result_capacity: int = 128,
        ttl_seconds: float = 300.0,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if (
            embedding_capacity < 0
            or result_capacity < 0
            or ttl_seconds <= 0
            or not 0 < max_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("Invalid query cache limits")
        self._clock = clock
        self._capacity = (embedding_capacity, result_capacity)
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._entries: tuple[OrderedDict[Hashable, _Entry], ...] = (
            OrderedDict(),
            OrderedDict(),
        )
        self._recency: OrderedDict[tuple[int, Hashable], None] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def _drop(self, pool: int, key: Hashable) -> None:
        entry = self._entries[pool].pop(key, None)
        self._recency.pop((pool, key), None)
        if entry is not None:
            self._bytes -= entry.size

    def _get(self, pool: int, key: Hashable) -> object | None:
        with self._lock:
            entry = self._entries[pool].get(key)
            if entry is None or self._clock.monotonic() >= entry.expires_at:
                self._drop(pool, key)
                return None
            self._entries[pool].move_to_end(key)
            self._recency.move_to_end((pool, key))
            return entry.value

    def _put(self, pool: int, key: Hashable, value: object) -> None:
        size = len(value) if isinstance(value, bytes) else len(pickle.dumps(value))
        with self._lock:
            self._drop(pool, key)
            if size > self._max_bytes or not self._capacity[pool]:
                return
            self._entries[pool][key] = _Entry(
                value, size, self._clock.monotonic() + self._ttl
            )
            self._recency[(pool, key)] = None
            self._bytes += size
            while len(self._entries[pool]) > self._capacity[pool]:
                self._drop(pool, next(iter(self._entries[pool])))
            while self._bytes > self._max_bytes:
                eldest_pool, eldest_key = next(iter(self._recency))
                self._drop(eldest_pool, eldest_key)

    def get_embedding(self, key: Hashable) -> bytes | None:
        value = self._get(0, key)
        return value if isinstance(value, bytes) else None

    def put_embedding(self, key: Hashable, value: bytes) -> None:
        self._put(0, key, value)

    def get_result(self, key: Hashable) -> object | None:
        return self._get(1, key)

    def put_result(self, key: Hashable, value: object) -> None:
        self._put(1, key, value)

    def purge(self) -> int:
        """Discard volatile query data without touching durable history."""
        with self._lock:
            count = sum(map(len, self._entries))
            for entries in self._entries:
                entries.clear()
            self._recency.clear()
            self._bytes = 0
            return count
