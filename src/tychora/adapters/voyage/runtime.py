"""Synchronized lazy runtime state shared by Voyage adapters."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from tychora.contracts.errors import TychoraError
from tychora.retrieval.cache import QueryCaches

from .request_types import VoyageTransport


class VoyageRuntime:
    """Owner-registered lazy transport with synchronized lifecycle."""

    def __init__(
        self,
        transport_factory: Callable[[], VoyageTransport],
        *,
        workspace_id: str,
        credential_binding_id: str,
        cache_capacity: int = 256,
        cache_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        query_caches: QueryCaches | None = None,
    ) -> None:
        if (
            not workspace_id
            or not credential_binding_id
            or type(cache_capacity) is not int
            or cache_capacity <= 0
            or cache_ttl <= 0
        ):
            raise ValueError("Runtime bindings must be nonempty")
        self._workspace_id = workspace_id
        self._credential_binding_id = credential_binding_id
        self._transport_factory = transport_factory
        self._transport: VoyageTransport | None = None
        self._lock = Lock()
        self._closed = False
        self._cache_capacity = cache_capacity
        self._cache_ttl = cache_ttl
        self._clock = clock
        self.query_caches = query_caches
        self._query_cache: OrderedDict[tuple[object, ...], tuple[float, bytes]] = (
            OrderedDict()
        )

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def credential_binding_id(self) -> str:
        return self._credential_binding_id

    def transport(self) -> VoyageTransport:
        with self._lock:
            if self._closed:
                raise TychoraError("WORKSPACE_BUSY", "Provider runtime is closed.")
            if self._transport is None:
                self._transport = self._transport_factory()
            return self._transport

    def close(self) -> None:
        with self._lock:
            self._closed = True
            transport, self._transport = self._transport, None
            self._query_cache.clear()
        if transport is not None:
            transport.close()

    def cached_query(self, key: tuple[object, ...]) -> bytes | None:
        with self._lock:
            if self._closed:
                return None
            if self.query_caches is not None:
                return self.query_caches.get_embedding(key)
            entry = self._query_cache.get(key)
            if entry is None:
                return None
            created, value = entry
            if self._clock() - created >= self._cache_ttl:
                del self._query_cache[key]
                return None
            self._query_cache.move_to_end(key)
            return value

    def cache_query(self, key: tuple[object, ...], value: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            if self.query_caches is not None:
                self.query_caches.put_embedding(key, value)
                return
            self._query_cache[key] = (self._clock(), value)
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > self._cache_capacity:
                self._query_cache.popitem(last=False)
