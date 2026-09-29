"""Thread-safe process-local resource leases; payload pins belong to the CAS."""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock


class LeaseSet:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    @contextmanager
    def pin(self, keys: frozenset[str]) -> Iterator[None]:
        with self._lock:
            self._counts.update(keys)
        try:
            yield
        finally:
            with self._lock:
                self._counts.subtract(keys)
                self._counts += Counter()

    def is_pinned(self, key: str) -> bool:
        with self._lock:
            return self._counts[key] > 0

    def any_pinned(self) -> bool:
        with self._lock:
            return bool(self._counts)
