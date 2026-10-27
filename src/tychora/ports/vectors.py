"""Storage-independent vector index boundary."""

from collections.abc import Sequence
from typing import Protocol

from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import (
    BatchReceipt,
    FamilyHandle,
    VectorBatch,
    Verification,
)
from tychora.ports.execution import ExecutionContext
from tychora.ports.search import VectorSearch


class VectorIndex(VectorSearch, Protocol):
    """A native vector family: immutable batch writes and view-restricted search."""

    def create_family(self, spec: FamilySpec) -> FamilyHandle: ...

    def upsert_batch(
        self, batch: VectorBatch, ctx: ExecutionContext
    ) -> BatchReceipt: ...

    def verify_batch(self, receipt: BatchReceipt) -> Verification: ...

    def drop_records(
        self, record_ids: Sequence[str], ctx: ExecutionContext
    ) -> None: ...
