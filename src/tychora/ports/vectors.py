"""Storage-independent vector index boundary."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import (
    BatchReceipt,
    CandidateKey,
    CandidatePage,
    FamilyHandle,
    VectorBatch,
    Verification,
    WorkLimits,
)
from tychora.ports.execution import ExecutionContext


class VectorIndex(Protocol):
    def create_family(self, spec: FamilySpec) -> FamilyHandle: ...

    def upsert_batch(
        self, batch: VectorBatch, ctx: ExecutionContext
    ) -> BatchReceipt: ...

    def verify_batch(self, receipt: BatchReceipt) -> Verification: ...

    def search(
        self,
        query_vector: bytes,
        eligible_records_and_owners: Mapping[str, CandidateKey],
        candidate_limit: int,
        work_limits: WorkLimits,
        ctx: ExecutionContext,
    ) -> CandidatePage: ...

    def drop_records(
        self, record_ids: Sequence[str], ctx: ExecutionContext
    ) -> None: ...
