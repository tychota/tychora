"""Native graph generation boundary."""

from typing import Protocol

from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import (
    GraphBatchReceipt,
    GraphGenerationSpec,
    GraphManifest,
)
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.execution import ExecutionContext


class GraphStore(Protocol):
    def begin_generation(self, spec: GraphGenerationSpec) -> str: ...

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
