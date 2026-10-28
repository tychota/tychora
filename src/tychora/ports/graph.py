"""Native graph generation boundary."""

from typing import Protocol

from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import (
    AssertionPage,
    AssertionQuery,
    GraphBatchReceipt,
    GraphBudget,
    GraphCandidates,
    GraphGenerationSpec,
    GraphManifest,
    GraphScope,
)
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.execution import ExecutionContext


class GraphStore(Protocol):
    def begin_generation(self, spec: GraphGenerationSpec) -> str: ...

    def sealed_receipt(self, generation_id: str) -> GraphBatchReceipt | None: ...

    def upsert_batch(
        self,
        generation_id: str,
        assertions: tuple[RelationshipAssertion, ...],
        units: tuple[EvidenceUnit, ...],
        ctx: ExecutionContext,
    ) -> None: ...

    def seal_generation(
        self, generation_id: str, expected: GraphManifest, ctx: ExecutionContext
    ) -> GraphBatchReceipt: ...

    def drop_generation(self, generation_id: str, ctx: ExecutionContext) -> None: ...

    def expand(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates: ...

    def structural_context(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates: ...

    def get_assertions(
        self,
        view: GraphScope,
        query: AssertionQuery,
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> AssertionPage: ...
