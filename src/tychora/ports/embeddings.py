"""Transport-independent text embedding provider boundary."""

from typing import Protocol

from tychora.contracts.embeddings import EmbeddingCapabilities, EmbeddingRecord
from tychora.contracts.indexing import ContextGroup
from tychora.ports.execution import ExecutionContext


class TextEmbedder(Protocol):
    capabilities: EmbeddingCapabilities

    def embed_groups(
        self, groups: tuple[ContextGroup, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]: ...

    def embed_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]: ...
