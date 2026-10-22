"""Collect unretained snapshots: SQL rows, native records and released payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.repositories import typed_storage_errors
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.publication import BatchReceiptVariant
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import BatchRow

from .removal import RemovalService

if TYPE_CHECKING:
    from .owner import WorkspaceOwner


@dataclass(frozen=True)
class GcSummary:
    """Counts of one collection pass."""

    snapshots: int
    pages: int
    batches: int
    deferred_batches: int
    released_artifacts: int
    unpublished_skipped: bool


@dataclass(frozen=True)
class _NativeDrop:
    """One orphan batch and what its native store needs to forget it."""

    row: BatchRow
    record_ids: tuple[str, ...]
    receipt: BatchReceiptVariant | None


class SnapshotGc:
    """Collects snapshots outside the retention rule on the owner thread.

    ``collect`` deletes snapshot and page rows, drops the native records of the
    batches only they bound and purges those batches, leaving their payloads
    marked ``delete_pending``. ``reclaim`` unlinks marked payloads that no root
    binds any more. Every step is idempotent, so running both again after a
    crash converges.
    """

    def __init__(self, owner: WorkspaceOwner) -> None:
        self.owner = owner

    def collect(
        self,
        ctx: ExecutionContext,
        *,
        limit: int = 64,
        include_unpublished: bool = False,
    ) -> GcSummary:
        """Delete up to ``limit`` unretained snapshots and purge their orphan batches.

        A batch whose native store is not open stays ``removed`` with its rows,
        and every later pass retries it.
        """
        store = self.owner.store
        with typed_storage_errors():
            with store.uow.write() as tx:
                report = tx.snapshots.collect(
                    limit,
                    leased=store.leases.pinned(),
                    include_unpublished=include_unpublished,
                )
            store.batch_cache.discard(
                tuple(row.batch_id for row in report.orphan_batches)
            )
            dropped, deferred = self._drop_native(report.orphan_batches, ctx)
            with store.uow.write() as tx:
                released = tx.batches.purge(dropped)
        return GcSummary(
            snapshots=len(report.snapshots),
            pages=len(report.pages),
            batches=len(dropped),
            deferred_batches=len(deferred),
            released_artifacts=len({*report.released_artifacts, *released}),
            unpublished_skipped=report.unpublished_skipped,
        )

    def reclaim(self) -> dict[str, int]:
        """Unlink every ``delete_pending`` payload that no root binds any more."""
        jobs = JobRepository(
            self.owner.db,
            artifacts=self.owner.artifacts,
            workspace_id=self.owner.workspace_id,
        )
        with typed_storage_errors():
            return RemovalService(self.owner, jobs).reconcile_pending_deletions()

    def _drop_native(
        self, rows: tuple[BatchRow, ...], ctx: ExecutionContext
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Remove native records first; return dropped and deferred batch IDs."""
        with self.owner.store.uow.read() as tx:
            drops = [
                _NativeDrop(
                    row,
                    tx.batches.record_ids(row.batch_id),
                    tx.batches.receipt(row.batch_id),
                )
                for row in rows
            ]
        dropped: list[str] = []
        deferred: list[str] = []
        for drop in drops:
            ctx.checkpoint()
            done = self._drop_one(drop, ctx)
            (dropped if done else deferred).append(drop.row.batch_id)
        return tuple(dropped), tuple(deferred)

    def _drop_one(self, drop: _NativeDrop, ctx: ExecutionContext) -> bool:
        """Drop one batch's native records; ``False`` when its store is not open.

        A pruned (``pending``) batch already lost its native records, so it
        never waits for its store. A graph generation of an unmounted snapshot
        drops through the attached graph store, which serves every generation.
        """
        store = self.owner.store
        pruned = drop.row.state == "pending"
        if drop.row.slot in ("text", "image"):
            index = store.vector_indexes.get(drop.row.family_id)
            if index is None:
                return pruned or not drop.record_ids
            if drop.record_ids:
                index.drop_records(drop.record_ids, ctx)
            return True
        if drop.row.slot == "graph" and isinstance(drop.receipt, GraphBatchReceipt):
            generation = drop.receipt.generation_id
            graph = store.graph_indexes.get(generation) or store.graph_store
            if graph is None:
                return pruned
            graph.drop_generation(generation, ctx)
            store.graph_indexes.pop(generation, None)
        return True
