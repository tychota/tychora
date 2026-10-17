"""Recreate a workspace in the current format from an older workspace's originals.

The old workspace is read through a read-only inventory. Every retained original
is re-added as a source job in the new workspace, so extraction, indexing and
publication run exactly as for a fresh add. Nothing is written to the old
workspace.
"""

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from tychora.adapters.sqlite.workspace_inventory import (
    InventoryCollection,
    InventoryRevision,
    InventorySource,
    WorkspaceInventory,
    read_inventory,
    read_original,
)
from tychora.application.job_wait import SETTLED_STATES
from tychora.contracts.base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ports.execution import ExecutionContext

RebuildSkipReason = Literal[
    "withdrawn",
    "repository_source",
    "unsupported_kind",
    "original_removed",
    "original_missing",
    "job_failed",
    "timed_out",
    "review_target_missing",
]
_INLINE_DROPPED = (
    "path",
    "url",
    "excludes",
    "content_sha256",
    "size_bytes",
    "sidecars",
)


class RebuildSkip(Contract):
    """One item the rebuild did not carry over, with the reason."""

    subject: Literal["source", "revision", "review"]
    key: NonEmptyStr
    reason: RebuildSkipReason
    detail: str = ""


class RebuiltSource(Contract):
    """A source re-added in the new workspace and the jobs that did it."""

    source_key: NonEmptyStr
    collection_id: Identifier
    revisions: NonNegativeInt
    job_ids: tuple[Identifier, ...]


class WorkspaceRebuildReport(Contract):
    """What a rebuild carried over and what it left behind."""

    old_workspace_id: Identifier
    new_workspace_id: Identifier
    collections: NonNegativeInt
    sources: tuple[RebuiltSource, ...]
    skipped: tuple[RebuildSkip, ...]
    reviews_replayed: NonNegativeInt


class RebuildTarget(Protocol):
    """The new workspace, opened with a grant over every rebuilt collection."""

    workspace_id: str

    def kinds(self) -> frozenset[str]:
        """The kind identifiers the new workspace's registry knows."""
        ...

    def seed_collections(self, collections: tuple[InventoryCollection, ...]) -> int:
        """Create or refresh the collections; return how many were created."""
        ...

    def add(self, source: SourceInput) -> str:
        """Start a source job and return its identifier."""
        ...

    def wait(self, job_id: str) -> str:
        """Wait for a job to settle and return its final state."""
        ...

    def cancel(self, job_id: str) -> str:
        """Cancel a job, wait briefly for it to settle and return its state."""
        ...

    def restore_policy(self, sources: tuple[InventorySource, ...]) -> int:
        """Carry access floors, ranks, uses and declared review states over."""
        ...

    def record_review(self, target: ReviewTarget, state: str, actor: str) -> None:
        """Record a review event for a revision, unit or asset."""
        ...

    def close(self) -> None:
        """Release the new workspace."""
        ...


@dataclass
class _Progress:
    sources: list[RebuiltSource] = field(default_factory=list)
    skipped: list[RebuildSkip] = field(default_factory=list)
    rebuilt_revisions: set[str] = field(default_factory=set)
    in_flight: set[str] = field(default_factory=set)

    def skip(
        self,
        subject: Literal["source", "revision", "review"],
        key: str,
        reason: RebuildSkipReason,
        detail: str = "",
    ) -> None:
        self.skipped.append(
            RebuildSkip(subject=subject, key=key, reason=reason, detail=detail)
        )


@dataclass
class _Replay:
    """Everything one source replay reads or writes besides the source itself."""

    old_root: Path
    target: RebuildTarget
    kind_map: Mapping[str, str]
    progress: _Progress
    ctx: ExecutionContext


def _not_empty() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        "The rebuild destination must be a new, empty directory outside the old workspace.",
        reason="rebuild.destination_not_empty",
    )


def _checked_paths(old: Path, destination: Path) -> tuple[Path, Path]:
    old_root = old.resolve(strict=True)
    new_root = destination.resolve(strict=False)
    if (
        new_root == old_root
        or new_root.is_relative_to(old_root)
        or old_root.is_relative_to(new_root)
    ):
        raise _not_empty()
    if new_root.exists() and (not new_root.is_dir() or any(new_root.iterdir())):
        raise _not_empty()
    return old_root, new_root


def rebuild_scope(collections: tuple[InventoryCollection, ...]) -> RequestScope:
    """Grant over every rebuilt collection, with the uploads they had consented to."""
    providers: set[str] = set()
    for item in collections:
        providers.update(json.loads(item.policy_json).get("upload_providers", ()))
    return RequestScope(
        principal_id="workspace-rebuild",
        collection_ids=frozenset(item.collection_id for item in collections),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage"}),
        upload_providers=frozenset(providers),
    )


def source_input(
    source: InventorySource, revision: InventoryRevision, kind: str, payload: bytes
) -> SourceInput:
    """The add request that reproduces one retained revision from its original."""
    registration = json.loads(source.registration_json)
    for name in _INLINE_DROPPED:
        registration.pop(name, None)
    registration.update(
        kind=kind,
        metadata=json.loads(revision.metadata_json),
        source_key=source.source_key,
    )
    if registration.get("type") == "text":
        registration["text"] = payload.decode("utf-8")
        return SourceInput.from_mapping(registration)
    registration.update(type="bytes", data=payload)
    registration["collection_id"] = UUID(registration["collection_id"])
    return SourceInput.model_validate(registration)


def _replay_revision(
    replay: _Replay, source: InventorySource, revision: InventoryRevision, kind: str
) -> str | None:
    progress = replay.progress
    if revision.original is None:
        progress.skip("revision", revision.revision_id, "original_removed")
        return None
    payload = read_original(replay.old_root, revision.original)
    if payload is None:
        progress.skip("revision", revision.revision_id, "original_missing")
        return None
    job_id = replay.target.add(source_input(source, revision, kind, payload))
    progress.in_flight.add(job_id)
    state = replay.target.wait(job_id)
    if state not in SETTLED_STATES:
        replay.target.cancel(job_id)
        progress.in_flight.discard(job_id)
        progress.skip("revision", revision.revision_id, "timed_out", state)
        return None
    progress.in_flight.discard(job_id)
    if state != "succeeded":
        progress.skip("revision", revision.revision_id, "job_failed", state)
        return None
    progress.rebuilt_revisions.add(revision.revision_id)
    return job_id


def _replay_source(replay: _Replay, source: InventorySource) -> None:
    progress = replay.progress
    if source.withdrawn:
        progress.skip("source", source.source_key, "withdrawn")
        return
    if source.source_type == "repository":
        progress.skip(
            "source", source.source_key, "repository_source", "re-add it from its path"
        )
        return
    kinds = replay.target.kinds()
    jobs: list[str] = []
    for revision in source.revisions:
        replay.ctx.checkpoint()
        kind = replay.kind_map.get(revision.kind_id, revision.kind_id)
        if kind not in kinds:
            progress.skip("revision", revision.revision_id, "unsupported_kind", kind)
            continue
        if job_id := _replay_revision(replay, source, revision, kind):
            jobs.append(job_id)
    if jobs:
        progress.sources.append(
            RebuiltSource(
                source_key=source.source_key,
                collection_id=source.collection_id,
                revisions=len(jobs),
                job_ids=tuple(jobs),
            )
        )


def _replay_reviews(
    inventory: WorkspaceInventory, target: RebuildTarget, progress: _Progress
) -> int:
    replayed = 0
    for review in inventory.reviews:
        subject = ReviewTarget.model_validate(
            {f"{review.target_kind}_id": review.target_id}
        )
        try:
            target.record_review(subject, review.state, review.actor_id)
        except TychoraError as error:
            if error.code != "NOT_FOUND":
                raise
            progress.skip(
                "review",
                f"{review.target_kind}:{review.target_id}",
                "review_target_missing",
                review.state,
            )
        else:
            replayed += 1
    return replayed


def _settle_and_restore(
    target: RebuildTarget, inventory: WorkspaceInventory, progress: _Progress
) -> None:
    """Cancel what is still running, then carry the old policy over."""
    try:
        for job_id in sorted(progress.in_flight):
            target.cancel(job_id)
    finally:
        target.restore_policy(inventory.sources)


def rebuild_workspace(
    old: Path,
    destination: Path,
    ctx: ExecutionContext,
    *,
    open_target: Callable[[Path, RequestScope], RebuildTarget],
    kind_map: Mapping[str, str] | None = None,
) -> WorkspaceRebuildReport:
    """Recreate ``destination`` in the current format from ``old``'s originals.

    The destination must not exist or be empty, and must not be or contain the old
    workspace; nothing is written before that is checked. Access floors, ranks,
    allowed uses and declared review states of the old workspace are carried over
    and never lowered.
    """
    old_root, new_root = _checked_paths(old, destination)
    inventory = read_inventory(old_root)
    new_root.mkdir(parents=True, exist_ok=True)
    if (old_root / "workspace.toml").is_file():
        shutil.copyfile(old_root / "workspace.toml", new_root / "workspace.toml")
    target = open_target(new_root, rebuild_scope(inventory.collections))
    progress = _Progress()
    try:
        target.seed_collections(inventory.collections)
        replay = _Replay(old_root, target, kind_map or {}, progress, ctx)
        for source in inventory.sources:
            _replay_source(replay, source)
        replayed = _replay_reviews(inventory, target, progress)
        return WorkspaceRebuildReport(
            old_workspace_id=inventory.workspace_id,
            new_workspace_id=target.workspace_id,
            collections=len(inventory.collections),
            sources=tuple(progress.sources),
            skipped=tuple(progress.skipped),
            reviews_replayed=replayed,
        )
    finally:
        try:
            _settle_and_restore(target, inventory, progress)
        finally:
            target.close()
