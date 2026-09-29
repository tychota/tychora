"""Trusted owner-bound query provider composition ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from tychora.contracts.embeddings import EmbeddingCapabilities
from tychora.ports.embeddings import OwnerBoundVisualEmbedder, TextEmbedder
from tychora.ports.execution import ExecutionContext
from tychora.ports.tokens import TokenCounter
from tychora.retrieval.cache import QueryCaches

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.contracts.requests import RequestScope


class TextQueryProvider(Protocol):
    """Qualified provider factory; owner still authorizes every query miss."""

    @property
    def capabilities(self) -> EmbeddingCapabilities: ...

    @property
    def counter(self) -> TokenCounter: ...

    @property
    def credential_binding_id(self) -> str: ...

    @property
    def caches(self) -> QueryCaches: ...

    @property
    def remote(self) -> bool: ...

    @property
    def available(self) -> bool: ...

    def bind(
        self, owner: WorkspaceOwner, scope: RequestScope, policy_epoch: int
    ) -> TextEmbedder: ...


class VisualQueryProvider(Protocol):
    """Factory of current owner-gated visual query embedders."""

    @property
    def capabilities(self) -> EmbeddingCapabilities: ...

    @property
    def counter(self) -> TokenCounter: ...

    @property
    def credential_binding_id(self) -> str: ...

    @property
    def caches(self) -> QueryCaches: ...

    @property
    def remote(self) -> bool: ...

    @property
    def available(self) -> bool: ...

    async def bind(
        self, owner: WorkspaceOwner, scope: RequestScope, ctx: ExecutionContext
    ) -> OwnerBoundVisualEmbedder: ...
