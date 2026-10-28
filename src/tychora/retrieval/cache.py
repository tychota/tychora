"""Bounded, request-locality aware memory caches for query work."""

from __future__ import annotations

import pickle
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Protocol

from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphScope
from tychora.retrieval.compiler import (
    CompiledEligibility,
    EligibilityKey,
    eligible_cache_key,
)


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class _Entry:
    value: bytes
    size: int
    expires_at: float


class QueryCaches:
    """Two independent LRU pools sharing one hard byte ceiling."""

    _ENTRY_OVERHEAD = 256

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
        self._entries: tuple[OrderedDict[str, _Entry], ...] = (
            OrderedDict(),
            OrderedDict(),
        )
        self._recency: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @staticmethod
    def _key(key: Hashable) -> str:
        return sha256(pickle.dumps(key, protocol=pickle.HIGHEST_PROTOCOL)).hexdigest()

    def _drop(self, pool: int, key: str) -> None:
        entry = self._entries[pool].pop(key, None)
        self._recency.pop((pool, key), None)
        if entry is not None:
            self._bytes -= entry.size

    def _get(self, pool: int, key: Hashable) -> object | None:
        compact_key = self._key(key)
        with self._lock:
            entry = self._entries[pool].get(compact_key)
            if entry is None or self._clock.monotonic() >= entry.expires_at:
                self._drop(pool, compact_key)
                return None
            self._entries[pool].move_to_end(compact_key)
            self._recency.move_to_end((pool, compact_key))
            return entry.value

    def _put(self, pool: int, key: Hashable, value: bytes) -> None:
        compact_key = self._key(key)
        size = len(value) + self._ENTRY_OVERHEAD
        with self._lock:
            self._drop(pool, compact_key)
            if size > self._max_bytes or not self._capacity[pool]:
                return
            self._entries[pool][compact_key] = _Entry(
                value, size, self._clock.monotonic() + self._ttl
            )
            self._recency[(pool, compact_key)] = None
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

    def get_result(self, key: Hashable) -> bytes | None:
        """The canonical JSON entry kept under ``key``, if still fresh."""
        value = self._get(1, key)
        return value if isinstance(value, bytes) else None

    def put_result(self, key: Hashable, value: bytes) -> None:
        """Keep a canonical JSON entry; values are never pickled."""
        if not isinstance(value, bytes):
            raise TypeError("Cached results are canonical JSON bytes")
        self._put(1, key, value)

    def drop_result(self, key: Hashable) -> None:
        """Forget the entry kept under ``key``, if any."""
        compact_key = self._key(key)
        with self._lock:
            self._drop(1, compact_key)

    def purge_results(self) -> int:
        """Bypass final results while retaining authorized query embeddings."""
        with self._lock:
            keys = tuple(self._entries[1])
            for key in keys:
                self._drop(1, key)
            return len(keys)

    def purge(self) -> int:
        """Discard volatile query data without touching durable history."""
        with self._lock:
            count = sum(map(len, self._entries))
            for entries in self._entries:
                entries.clear()
            self._recency.clear()
            self._bytes = 0
            return count


ELIGIBILITY_CACHE_SIZE = 64


class EligibilityCache:
    """LRU of compiled views keyed by snapshot, policy epoch, scope, policy and request.

    Two concurrent misses on one key may both compile; the second result
    replaces the first, which is equal.
    """

    def __init__(self, capacity: int = ELIGIBILITY_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[EligibilityKey, CompiledEligibility] = OrderedDict()
        self._graphs: OrderedDict[EligibilityKey, GraphScope | None] = OrderedDict()
        self._lock = RLock()

    def get_or_compile(
        self, key: EligibilityKey, compile: Callable[[], CompiledEligibility]
    ) -> CompiledEligibility:
        """Return the cached entry for ``key``, or compile, check and keep one."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry
        entry = compile()
        if eligible_cache_key(entry.view) != key:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Compiled view does not answer its cache key",
                reason="invariant.unexpected",
            )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return entry

    def graph_scope(
        self, key: EligibilityKey, build: Callable[[], GraphScope | None]
    ) -> GraphScope | None:
        """The graph scope of a cached view's key, built once."""
        with self._lock:
            if key in self._graphs:
                self._graphs.move_to_end(key)
                return self._graphs[key]
        scope = build()
        with self._lock:
            self._graphs[key] = scope
            while len(self._graphs) > self._capacity:
                self._graphs.popitem(last=False)
        return scope

    def purge(self) -> int:
        """Drop every entry; return how many there were."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._graphs.clear()
            return count
