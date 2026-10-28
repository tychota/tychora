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
    Each discard bumps a generation: a view whose read began before a discard
    may come from a transaction that predates the collection, so it is
    returned to its reader but not cached.
    """

    def __init__(self, capacity: int = CATALOG_SIZE) -> None:
        self._capacity = capacity
        self._views: OrderedDict[str, SnapshotView] = OrderedDict()
        self._generation = 0
        self._lock = Lock()

    def get(self, tx: ReadTransaction, snapshot_id: str) -> SnapshotView:
        """Return the cached view, or read it from SQL in ``tx`` and cache it.

        On a read-pool transaction, call it before any other read in ``tx``
        so the transaction's WAL snapshot starts after the generation is taken;
        a writer-thread read cannot overlap the collector, which runs there.
        """
        with self._lock:
            view = self._views.get(snapshot_id)
            if view is not None:
                self._views.move_to_end(snapshot_id)
                return view
            generation = self._generation
        view = tx.snapshots.view(snapshot_id)
        with self._lock:
            if generation != self._generation:
                return view
            self._views[snapshot_id] = view
            self._views.move_to_end(snapshot_id)
            while len(self._views) > self._capacity:
                self._views.popitem(last=False)
        return view

    def discard(self, snapshot_ids: Iterable[str]) -> None:
        """Forget the views of collected snapshots; unknown IDs are ignored."""
        collected = tuple(snapshot_ids)
        if not collected:
            return
        with self._lock:
            self._generation += 1
            for snapshot_id in collected:
                self._views.pop(snapshot_id, None)
