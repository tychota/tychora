"""Published snapshot views by ID, read from SQL once while they stay cached."""

from collections import OrderedDict
from collections.abc import Iterable
from threading import Lock

from tychora.contracts.publication import SnapshotView
from tychora.ports.repositories import ReadTransaction

CATALOG_SIZE = 32


class SnapshotCatalog:
    """A thread-safe LRU of snapshot views; a published snapshot never changes.

    Collection removes a snapshot's rows, so the collector discards its view.
    """

    def __init__(self, capacity: int = CATALOG_SIZE) -> None:
        self._capacity = capacity
        self._views: OrderedDict[str, SnapshotView] = OrderedDict()
        self._lock = Lock()

    def get(self, tx: ReadTransaction, snapshot_id: str) -> SnapshotView:
        """Return the cached view, or read it from SQL in ``tx`` and cache it."""
        with self._lock:
            view = self._views.get(snapshot_id)
            if view is not None:
                self._views.move_to_end(snapshot_id)
                return view
        view = tx.snapshots.view(snapshot_id)
        with self._lock:
            self._views[snapshot_id] = view
            self._views.move_to_end(snapshot_id)
            while len(self._views) > self._capacity:
                self._views.popitem(last=False)
        return view

    def discard(self, snapshot_ids: Iterable[str]) -> None:
        """Forget the views of collected snapshots; unknown IDs are ignored."""
        with self._lock:
            for snapshot_id in snapshot_ids:
                self._views.pop(snapshot_id, None)
