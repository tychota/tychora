"""Transport-independent text embedding provider boundary."""

from contextlib import AbstractContextManager
from typing import Literal, Protocol

from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    EmbeddingRecord,
    QueryEmbeddingProbe,
    TextOrImage,
    VisualInput,
    VisualQueryEmbeddingProbe,
)
from tychora.contracts.indexing import ContextGroup
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


class TextEmbedder(Protocol):
    capabilities: EmbeddingCapabilities

    def embed_groups(
        self, groups: tuple[ContextGroup, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]: ...

    def embed_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]: ...

    def probe_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> QueryEmbeddingProbe: ...


type VisualOperation = TextOrImage | tuple[VisualInput, ...]


class VisualEmbedder(Protocol):
    capabilities: EmbeddingCapabilities

    def embed_assets(
        self, items: tuple[VisualInput, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]: ...

    def probe_query(
        self, query: TextOrImage, ctx: ExecutionContext
    ) -> VisualQueryEmbeddingProbe: ...

    def embed_query(
        self, query: TextOrImage, ctx: ExecutionContext
    ) -> EmbeddingRecord: ...


class VisualOwnerGate(Protocol):
    """Narrow trusted-composition port, not a sandbox for Python extensions."""

    @property
    def scope(self) -> RequestScope: ...

    @property
    def epoch(self) -> int: ...

    def binding(self, owner: object, scope: RequestScope) -> None: ...

    def operation(
        self, query_or_items: VisualOperation, mode: Literal["query", "document"]
    ) -> tuple[tuple[VisualInput, ...], tuple[str, ...], str]: ...

    def authorize(
        self,
        query_or_items: VisualOperation,
        mode: Literal["query", "document"],
        ctx: ExecutionContext,
        *,
        body_sha: str | None = None,
    ) -> None: ...

    def admitted(self) -> AbstractContextManager[None]: ...


class OwnerBoundVisualEmbedder(VisualEmbedder, Protocol):
    @property
    def _access(self) -> VisualOwnerGate: ...
