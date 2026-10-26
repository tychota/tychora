"""Voyage implementations of the owner-bound query provider factories."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tychora.adapters.voyage.text import VoyageTextEmbedder
from tychora.adapters.voyage.visual import VoyageVisualEmbedder
from tychora.application.visual import create_visual_embedder
from tychora.contracts.embeddings import EmbeddingCapabilities, QueryUploadAuthorization
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext
from tychora.ports.tokens import TokenCounter
from tychora.retrieval.cache import QueryCaches

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.contracts.requests import RequestScope


class VoyageTextQueryProvider:
    remote = True

    def __init__(self, base: VoyageTextEmbedder) -> None:
        self._base = base

    @property
    def capabilities(self) -> EmbeddingCapabilities:
        return self._base.capabilities

    @property
    def counter(self) -> TokenCounter:
        return self._base._counter

    @property
    def credential_binding_id(self) -> str:
        return self._base._runtime.credential_binding_id

    @property
    def caches(self) -> QueryCaches:
        value = self._base._runtime.query_caches
        if value is None:
            raise TychoraError("INTEGRITY_ERROR", "Query cache owner is missing.")
        return value

    @property
    def available(self) -> bool:
        return self._base._runtime.available

    def bind(
        self, owner: WorkspaceOwner, scope: RequestScope, policy_epoch: int
    ) -> VoyageTextEmbedder:
        from tychora.application.query import query_binding_from_owner

        binding = query_binding_from_owner(owner, scope, policy_epoch)

        def authorize_query(
            authorization: QueryUploadAuthorization, execution: ExecutionContext
        ) -> None:
            owner.run_write_sync(
                lambda: owner.upload_policy.authorize_query(
                    scope, self.capabilities.provider, authorization
                ),
                execution,
                "interactive",
            )

        return VoyageTextEmbedder(
            capabilities=self.capabilities,
            counter=self.counter,
            runtime=self._base._runtime,
            authorize_document=self._base._authorize_document,
            query_binding=binding,
            authorize_query=authorize_query,
        )


class VoyageVisualQueryProvider:
    remote = True

    def __init__(self, base: VoyageVisualEmbedder) -> None:
        self._base = base

    @property
    def capabilities(self) -> EmbeddingCapabilities:
        return self._base.capabilities

    @property
    def counter(self) -> TokenCounter:
        return self._base._counter

    @property
    def credential_binding_id(self) -> str:
        return self._base._runtime.credential_binding_id

    @property
    def caches(self) -> QueryCaches:
        value = self._base._runtime.query_caches
        if value is None:
            raise TychoraError("INTEGRITY_ERROR", "Query cache owner is missing.")
        return value

    @property
    def available(self) -> bool:
        return self._base._runtime.available

    async def bind(
        self, owner: WorkspaceOwner, scope: RequestScope, ctx: ExecutionContext
    ) -> VoyageVisualEmbedder:
        return await create_visual_embedder(
            owner,
            capabilities=self.capabilities,
            counter=self.counter,
            runtime=self._base._runtime,
            scope=scope,
            ctx=ctx,
        )
