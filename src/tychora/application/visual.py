"""Canonical owner composition for visual embedding operations.

This is a trusted Python composition boundary, not a sandbox for extensions.
Python code with access to an owner/runtime can bypass application interfaces.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from threading import get_ident, local
from typing import TYPE_CHECKING, Literal, NoReturn

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.corpus import AssetRow
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    QueryUploadAuthorization,
    RegisteredImageQuery,
    TextQuery,
    VisualInput,
    VisualQueryAdmission,
    VisualResizeDerivation,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.ports.embeddings import VisualOperation
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.adapters.voyage.runtime import VoyageRuntime
    from tychora.adapters.voyage.visual import VoyageVisualEmbedder
    from tychora.application.owner import WorkspaceOwner
    from tychora.application.runtime import Priority
    from tychora.contracts.requests import RequestScope
    from tychora.ports.repositories import ReadTransaction
    from tychora.ports.tokens import TokenCounter


def denied() -> NoReturn:
    raise TychoraError(
        "FORBIDDEN_SCOPE", "Visual input is not available in this scope."
    )


@dataclass(frozen=True)
class OwnerVisualAccess:
    """Concrete operation-local owner gate; constructed by WorkspaceOwner.

    A private name or isinstance check is not a security boundary against trusted
    Python. Standard composition never accepts a caller authorization callback.
    """

    owner: WorkspaceOwner
    scope: RequestScope
    epoch: int
    _lane: local = field(default_factory=local, compare=False, repr=False)

    def binding(self, owner: object, scope: RequestScope) -> None:
        if owner is not self.owner or self.owner.scope.narrow(scope) != self.scope:
            denied()

    @property
    def cache_scope(self) -> str:
        # Remote grants/offline do not define permission to read cached values.
        return digest(
            "visual-read-scope-v1",
            self.scope.principal_id,
            sorted(self.scope.collection_ids),
            self.scope.access_ceiling,
            sorted(self.scope.uses),
            sorted(self.scope.capabilities),
            self.epoch,
        )

    def _on_owner[T](
        self, call: Callable[[], T], ctx: ExecutionContext, priority: Priority
    ) -> T:
        if get_ident() == self.owner._thread_id:
            ctx.checkpoint()
            return call()
        return self.owner.run_write_sync(call, ctx, priority)

    def read_scope(
        self,
        items: tuple[VisualInput, ...],
        ctx: ExecutionContext,
        priority: Priority = "background",
    ) -> None:
        self._on_owner(lambda: self._resolve(items), ctx, priority)

    def _resolve(self, items: tuple[VisualInput, ...]) -> None:
        owner = self.owner
        if owner.db.in_transaction:
            raise TychoraError(
                "INTEGRITY_ERROR", "Visual resolution requires a fresh read."
            )
        with owner.store.uow.read() as tx:
            try:
                owner.upload_policy._check_epoch(self.epoch)
                if not self.scope.collection_ids:
                    denied()
                for cid in self.scope.collection_ids:
                    row = tx.workspace.collection(cid)
                    if (
                        row is None
                        or row[0] != owner.workspace_id
                        or row[1] > self.scope.access_ceiling
                    ):
                        denied()
                for item in items:
                    self._resolve_item(tx, item)
            except (ValueError, TypeError, KeyError):
                denied()

    def _resolve_item(self, tx: ReadTransaction, item: VisualInput) -> None:
        # Pydantic model_copy(update=...) does not run field or model validators.
        # Revalidate the complete caller descriptor before using its input hash
        # for admission, cache identity, or a returned embedding record.
        if (
            type(item) is not VisualInput
            or type(item.asset) is not AssetRow
            or type(item.artifact_ref) is not ArtifactRef
            or type(item.resize) is not VisualResizeDerivation
            or type(item.dependency_ids) is not frozenset
        ):
            denied()
        if VisualInput.model_validate(item.model_dump(mode="python")) != item:
            denied()
        registered = tx.assets.registered(item.asset.asset_id)
        if registered is None:
            denied()
        ref = registered.ref
        if (
            registered.asset != item.asset
            or registered.revision_id != item.revision_id
            or (
                ref.size_bytes,
                ref.media_type,
                ref.storage_key,
                registered.payload_state,
            )
            != (
                item.artifact_ref.size_bytes,
                item.artifact_ref.media_type,
                item.artifact_ref.storage_key,
                "present",
            )
        ):
            denied()
        # Canonical source/caption links are leaf dependencies, never substituted
        # by the containing revision. Arbitrary caller prose is not attested.
        links = tx.assets.links(item.asset.asset_id, ("source", "caption"), 2049)
        if (
            not links
            or len(links) > 2048
            or not any(link.role == "source" for link in links)
            or any(
                link.extraction_id != item.asset.extraction_id
                or link.text_origin != "source"
                or not link.citable
                for link in links
            )
        ):
            denied()
        if item.context not in ("", registered.asset.caption):
            denied()
        if item.context and not any(link.role == "caption" for link in links):
            denied()
        required = frozenset(
            {
                item.asset.asset_id,
                *(
                    link.unit_id
                    for link in links
                    if link.role == "source" or item.context
                ),
            }
        )
        if item.dependency_ids != required:
            denied()
        # Reuse canonical dependency expansion and revision visibility checks.
        self.owner.upload_policy._authorize_dependencies(required, None, self.scope)

    def operation(
        self, query_or_items: VisualOperation, mode: Literal["query", "document"]
    ) -> tuple[tuple[VisualInput, ...], tuple[str, ...], str]:
        if isinstance(query_or_items, TextQuery):
            items = ()
            ids = (digest("visual-text-query-v1", query_or_items.text),)
        elif isinstance(query_or_items, RegisteredImageQuery):
            items = (query_or_items.image,)
            ids = (query_or_items.image.input_sha,)
        else:
            items = query_or_items
            ids = tuple(item.input_sha for item in items)
        operation_sha = digest(
            "visual-operation-v1", self.owner.workspace_id, self.cache_scope, mode, ids
        )
        return items, ids, operation_sha

    def authorize(
        self,
        query_or_items: VisualOperation,
        mode: Literal["query", "document"],
        ctx: ExecutionContext,
        *,
        body_sha: str | None = None,
    ) -> None:
        items, ids, operation_sha = self.operation(query_or_items, mode)
        if body_sha is not None and (
            len(body_sha) != 64 or any(c not in "0123456789abcdef" for c in body_sha)
        ):
            denied()

        def check() -> None:
            self._resolve(items)
            dependencies = frozenset().union(*(i.dependency_ids for i in items))
            if mode == "document":
                self.owner.upload_policy.authorize(
                    self.scope, "voyage", dependencies, self.epoch
                )
                return
            authorization: VisualQueryAdmission | QueryUploadAuthorization
            if body_sha is None:
                authorization = VisualQueryAdmission(
                    operation_sha=operation_sha,
                    input_shas=ids,
                    collection_ids=self.scope.collection_ids,
                    dependency_ids=dependencies,
                    policy_epoch=self.epoch,
                )
            else:
                authorization = QueryUploadAuthorization(
                    payload_sha=body_sha,
                    input_shas=ids,
                    collection_ids=self.scope.collection_ids,
                    dependency_ids=dependencies,
                    policy_epoch=self.epoch,
                )
            self.owner.upload_policy.authorize_query(
                self.scope, "voyage", authorization
            )

        self._on_owner(check, ctx, "interactive" if mode == "query" else "background")

    @contextmanager
    def admitted(self) -> Iterator[None]:
        if getattr(self._lane, "active", False):
            raise TychoraError("INTEGRITY_ERROR", "Visual lane is already active.")
        self._lane.active = True
        try:
            yield
        finally:
            self._lane.active = False

    def require_lane(self) -> None:
        if not getattr(self._lane, "active", False):
            raise TychoraError(
                "INVALID_REQUEST", "Use the owner visual embedding operation."
            )

    def read(
        self,
        item: VisualInput,
        mode: Literal["query", "document"],
        ctx: ExecutionContext,
    ) -> bytes:
        self.require_lane()
        # Pre-read admission is deliberately not a claim about a wire hash.
        self.authorize((item,), mode, ctx)
        if not 0 < item.artifact_ref.size_bytes <= 20_000_000:
            raise TychoraError(
                "INVALID_REQUEST", "Visual source exceeds the encoded limit."
            )
        with self.owner.artifacts.open_verified(item.artifact_ref) as stream:
            value = stream.read(item.artifact_ref.size_bytes + 1)
        if (
            len(value) != item.artifact_ref.size_bytes
            or sha256(value).hexdigest() != item.artifact_ref.sha256
        ):
            raise TychoraError("INTEGRITY_ERROR", "Visual artifact identity differs.")
        return value


async def create_visual_embedder(
    owner: WorkspaceOwner,
    *,
    capabilities: EmbeddingCapabilities,
    counter: TokenCounter,
    runtime: VoyageRuntime,
    scope: RequestScope,
    ctx: ExecutionContext,
) -> VoyageVisualEmbedder:
    """Trusted composition entry point; no transport is constructed here.

    The generic owner consumes an owner-bound provider port. Application
    composition alone selects the concrete adapter, reader and mandatory gate.
    Runtime/counter injection is privileged Python, including recording tests.
    """
    from tychora.adapters.voyage.visual import VoyageVisualEmbedder

    def create() -> VoyageVisualEmbedder:
        granted = owner.scope.narrow(scope)
        epoch = owner.policy_epoch()
        if runtime.workspace_id != owner.workspace_id:
            raise TychoraError("INTEGRITY_ERROR", "Visual runtime workspace differs.")
        access = OwnerVisualAccess(owner, granted, epoch)
        access.read_scope((), ctx)
        embedder = VoyageVisualEmbedder(
            capabilities=capabilities, counter=counter, runtime=runtime, access=access
        )
        owner.register_runtime(runtime)
        return embedder

    return await owner.run_write(create, ctx)
