"""Store-agnostic repository ports and the keyset cursor they page with.

Ports speak in contracts, identifiers, scalars, tuples and cursors. Each store
adapter binds lists and transactions its own way, so a PostgreSQL adapter can
implement the same protocols as the SQLite one.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Mapping
from typing import Literal, NamedTuple, Protocol

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import (
    Contract,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.indexing import IntervalRow, RepresentationFacts
from tychora.contracts.publication import (
    BatchReceiptVariant,
    LexicalRecord,
    NewBatch,
    PublicationPlan,
    SnapshotMember,
    SnapshotView,
)
from tychora.contracts.snapshots import IndexFamily, MembershipPage, PageBatch

MAX_CURSOR_CHARS = 1024


def _invalid_cursor() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST", "Cursor is invalid.", reason="cursor.invalid"
    )


class Cursor(Contract):
    """Keyset position: the sort key of the last returned row and its ID."""

    sort_key: tuple[str | int, ...]
    last_id: NonEmptyStr

    def encode(self) -> str:
        """Return an opaque URL-safe token for this position."""
        raw = canonical_bytes({"id": self.last_id, "k": list(self.sort_key)})
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        """Parse a token produced by ``encode``; anything else is refused."""
        try:
            if not isinstance(token, str) or not 0 < len(token) <= MAX_CURSOR_CHARS:
                raise ValueError("cursor length")
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"id", "k"}:
                raise ValueError("cursor shape")
            if not isinstance(value["k"], list):
                raise TypeError("cursor key")
            cursor = cls(sort_key=tuple(value["k"]), last_id=value["id"])
            if cursor.encode() != token:
                raise ValueError("cursor is not canonical")
            return cursor
        except (ValueError, TypeError):
            raise _invalid_cursor() from None


class WorkspaceRepository(Protocol):
    """The single workspace row: identity, epochs and decision watermark."""

    def workspace_id(self) -> str:
        """Return the workspace identifier."""
        ...

    def policy_epoch(self) -> int:
        """Return the current policy epoch."""
        ...

    def lexical_epoch(self) -> int:
        """Return the current lexical statistics epoch."""
        ...

    def decision_watermark(self) -> int:
        """Return the highest recorded decision sequence number."""
        ...

    def bump_lexical_epoch(self) -> int:
        """Advance the lexical statistics epoch once and return the new value."""
        ...

    def bump_policy_epoch(self) -> int:
        """Advance the policy epoch once and return the new value."""
        ...

    def collection_ids(self) -> frozenset[str]:
        """Return every collection of the workspace."""
        ...


class ArtifactRepository(Protocol):
    """Registered content-addressed artifacts."""

    def get(self, sha256: str) -> ArtifactRef | None:
        """Return the registered artifact, or ``None`` when absent."""
        ...

    def register(self, ref: ArtifactRef) -> bool:
        """Register an artifact; return whether it was newly added."""
        ...

    def revive(self, ref: ArtifactRef) -> bool:
        """Mark a released artifact present again; return whether it was released."""
        ...


class FamilyRepository(Protocol):
    """Index families and the creation nonces bound into their identifiers."""

    def get(self, family_id: str) -> IndexFamily | None:
        """Return the stored family, or ``None`` when absent."""
        ...

    def nonce(self, channel: str, identity_sha: str) -> str | None:
        """Return the nonce of the earliest family for this identity (deterministic pick)."""
        ...

    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None:
        """Store a family once; an identical repeat is a no-op, a different row fails."""
        ...


class BatchRow(Contract):
    """One stored index batch and the state of its build."""

    batch_id: Identifier
    family_id: Sha256
    slot: NonEmptyStr
    extraction_id: Identifier | None
    state: Literal["pending", "building", "ready", "failed", "removed"]
    expected_count: NonNegativeInt
    actual_count: NonNegativeInt | None
    receipt_digest: Sha256 | None
    input_manifest: ArtifactRef
    vectors: ArtifactRef | None

    def page_batch(self) -> PageBatch:
        """Return the page binding of a ready batch; any other state is refused."""
        if (
            self.state != "ready"
            or self.receipt_digest is None
            or self.actual_count is None
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE",
                "Batch is not ready for publication",
                reason="batch.missing",
            )
        return PageBatch(
            slot=self.slot,
            batch_id=self.batch_id,
            family_id=self.family_id,
            count=self.actual_count,
            receipt_digest=self.receipt_digest,
            input_manifest=self.input_manifest,
            vectors=self.vectors,
        )


class VectorRecordRow(Contract):
    """One vector record of a batch and its row in the batch artifact."""

    record_id: Identifier
    chunk_id: Identifier | None
    asset_id: Identifier | None
    group_id: Identifier | None = None
    input_sha: Sha256
    row_index: NonNegativeInt


class BatchRepository(Protocol):
    """Index batches, their lexical and vector rows, and their build state."""

    def begin(self, batch: NewBatch) -> None:
        """Store a building batch once; an identical repeat is a no-op."""
        ...

    def write_lexical(self, batch_id: str, records: tuple[LexicalRecord, ...]) -> None:
        """Store a building batch's lexical records; stored records never change."""
        ...

    def write_vectors(self, batch_id: str, rows: tuple[VectorRecordRow, ...]) -> None:
        """Store a building batch's vector record rows; stored rows never change."""
        ...

    def mark_ready(self, batch_id: str, actual_count: int) -> None:
        """Mark a building batch ready with its final count."""
        ...

    def count_ready(self, batch_ids: tuple[str, ...]) -> int:
        """Return how many of these batches are ready."""
        ...

    def get(self, batch_id: str) -> BatchRow | None:
        """Return the stored batch, or ``None`` when absent."""
        ...

    def find(self, family_id: str, extraction_id: str) -> BatchRow | None:
        """Return the batch of one family for one extraction, or ``None``."""
        ...

    def for_snapshot(self, snapshot_id: str, slot: str) -> tuple[BatchRow, ...]:
        """Return the batches a snapshot serves for one slot, by batch ID."""
        ...

    def receipt(self, batch_id: str) -> BatchReceiptVariant | None:
        """Return the stored receipt of a batch, or ``None`` when absent."""
        ...

    def record_ids(self, batch_id: str) -> tuple[str, ...]:
        """Return the batch's vector record IDs in ID order."""
        ...

    def purge(self, batch_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Delete removed batches and their rows; return artifacts released for deletion."""
        ...


class GcReport(Contract):
    """What one collection pass deleted, marked removed and released."""

    snapshots: tuple[Identifier, ...]
    pages: tuple[Identifier, ...]
    orphan_batches: tuple[BatchRow, ...]
    released_artifacts: tuple[Sha256, ...]
    unpublished_skipped: bool = False


class PublicationFootprint(Contract):
    """Bytes one publication wrote: its root document and the pages new to it."""

    snapshot_id: Identifier
    root_bytes: NonNegativeInt
    new_page_bytes: NonNegativeInt
    new_pages: NonNegativeInt
    largest_artifact_bytes: NonNegativeInt


class SnapshotRepository(Protocol):
    """Snapshots, the active pointer and content-addressed membership pages."""

    def active_id(self) -> str | None:
        """Return the active snapshot ID, or ``None`` before the first publication."""
        ...

    def set_active(self, snapshot_id: str) -> None:
        """Point the workspace at a published snapshot."""
        ...

    def page(self, snapshot_id: str, bucket: int) -> MembershipPage | None:
        """Return a snapshot's page for one bucket, or ``None`` when it has none."""
        ...

    def page_manifests(self, page_ids: tuple[str, ...]) -> Mapping[str, ArtifactRef]:
        """Return the manifest artifact of each page; an unknown page is refused."""
        ...

    def insert_page(self, page: MembershipPage, manifest: ArtifactRef) -> None:
        """Store a page and its rows once; an identical repeat is a no-op."""
        ...

    def view(self, snapshot_id: str) -> SnapshotView:
        """Read a published snapshot from its rows; no artifact is opened."""
        ...

    def member(self, snapshot_id: str, extraction_id: str) -> SnapshotMember | None:
        """Return the member that selects this extraction, or ``None``.

        A snapshot that was collected or never published is refused.
        """
        ...

    def all_members(self, snapshot_id: str) -> Iterator[SnapshotMember]:
        """Yield every member, page by page in bucket order, in linear time.

        A collected snapshot or a member count that differs from the row is refused.
        """
        ...

    def members(
        self, snapshot_id: str, *, after: Cursor | None = None, limit: int = 1000
    ) -> tuple[tuple[SnapshotMember, ...], Cursor | None]:
        """Return one page of members in source, revision and extraction order.

        The returned cursor continues after the page; it is ``None`` after the last.
        """
        ...

    def insert_snapshot(self, plan: PublicationPlan) -> None:
        """Store a snapshot over pages already stored, with its slots and root batches."""
        ...

    def footprint(self, snapshot_id: str) -> PublicationFootprint:
        """Return the root bytes and the bytes of pages new relative to the parent."""
        ...

    def retained_ids(self, keep_last: int = 16) -> frozenset[str]:
        """Return the snapshots the retention rule keeps."""
        ...

    def collect(
        self,
        limit: int = 64,
        *,
        leased: frozenset[str] = frozenset(),
        include_unpublished: bool = False,
    ) -> GcReport:
        """Delete unretained snapshots and orphan pages; mark orphan batches removed."""
        ...


class UnitRow(Contract):
    """One evidence unit's identity, place and text digest, without its text."""

    unit_id: Identifier
    extraction_id: Identifier
    boundary_key: NonEmptyStr
    ordinal: NonNegativeInt
    text_sha: Sha256
    char_count: NonNegativeInt


class ChunkRow(Contract):
    """One chunk's range in its unit and its context group."""

    chunk_id: Identifier
    unit_id: Identifier
    extraction_id: Identifier
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    context_group_key: NonEmptyStr


class RepresentationRepository(Protocol):
    """Write-time representation facts; each read binds its ID tuple once."""

    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]:
        """Return the stored units among these IDs; unknown IDs are skipped."""
        ...

    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]:
        """Return the stored chunks among these IDs; unknown IDs are skipped."""
        ...

    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]:
        """Return the dependency set of each stored owner among these IDs."""
        ...

    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]:
        """Return the ownership intervals of these units in unit and start order."""
        ...

    def write(self, facts: RepresentationFacts) -> None:
        """Store facts once; a repeat is a no-op and a different set is refused."""
        ...


class MemberRow(NamedTuple):
    """One snapshot member and the policy facts its revision is judged on."""

    source_id: str
    revision_id: str
    extraction_id: str
    is_default: bool
    kind_id: str
    definition_hash: str
    collection_id: str
    collection_policy_json: str
    collection_floor: int
    source_floor: int
    withdrawn: bool
    allowed_uses_override: str | None
    access_rank: int
    allowed_uses_json: str
    review_state: str
    published_on: str | None
    effective_from: str | None
    effective_until: str | None
    edition_key: str
    workspace_id: str


class ReviewRow(NamedTuple):
    """A unit or asset whose review state may exclude it, with that state."""

    target_id: str
    kind: Literal["unit", "asset"]
    state: str
    proxy: bool


class UnitFlags(NamedTuple):
    """A unit's extraction, citability and proxy status."""

    unit_id: str
    extraction_id: str
    citable: bool
    proxy: bool


class FacetValue(NamedTuple):
    """One facet value of a revision or unit, as its typed value."""

    owner_id: str
    facet_key: str
    value: str | float | int


class OwnerFact(Contract):
    """One unit, asset, chunk or vector record with what admitting it reads."""

    owner_id: Identifier
    kind: Literal["unit", "asset", "chunk", "record"]
    extraction_id: Identifier
    dependency_ids: frozenset[Identifier]
    unit_id: Identifier | None = None
    chunk_id: Identifier | None = None
    asset_id: Identifier | None = None
    proxy: bool = False
    citable: bool = False
    batch_id: Identifier | None = None
    source_owner_ids: frozenset[Identifier] = frozenset()


class EligibilityRepository(Protocol):
    """The store-side reads that compile and apply a revision-level view."""

    def members(
        self, snapshot_id: str, extraction_ids: tuple[str, ...] = ()
    ) -> tuple[MemberRow, ...]:
        """One row per member of the snapshot; with ``extraction_ids``, per listed extraction instead."""
        ...

    def kind_definitions(
        self, kind_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(kind_id, definition_hash, definition_json)`` of each listed kind."""
        ...

    def batches(self, snapshot_id: str) -> tuple[tuple[str, str, str | None], ...]:
        """``(slot, batch_id, extraction_id)`` of every batch the snapshot serves."""
        ...

    def review_rows(
        self, extraction_ids: tuple[str, ...], excluded_states: tuple[str, ...]
    ) -> tuple[ReviewRow, ...]:
        """Reviewed units and assets, proxy units, and units or assets declared in ``excluded_states``."""
        ...

    def unit_flags(self, extraction_ids: tuple[str, ...]) -> tuple[UnitFlags, ...]:
        """Every unit of these extractions; read only for filters and graph scopes."""
        ...

    def dependents(
        self, owner_ids: tuple[str, ...]
    ) -> Mapping[str, Literal["unit", "asset"]]:
        """The listed units and assets with every unit or asset depending on them, transitively."""
        ...

    def revision_facets(
        self, revision_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Revision-level values of the listed facets."""
        ...

    def unit_facets(
        self, extraction_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Unit-level values of the listed facets for units of these extractions."""
        ...

    def revision_metadata(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(revision_id, metadata_json, metadata_sha)`` of each listed revision."""
        ...

    def global_facet_markers(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, ArtifactRef | None], ...]:
        """``(revision_id, source_ref, artifact)`` of each global-facet binding row."""
        ...

    def owner_facts(self, owner_ids: tuple[str, ...]) -> tuple[OwnerFact, ...]:
        """Facts of the listed units, assets, chunks and vector records; unknown IDs are skipped.

        A known owner without a persisted dependency set is refused with
        ``INTEGRITY_ERROR`` reason ``canonical.facts_missing``.
        """
        ...


class ReadTransaction(Protocol):
    """Repositories bound to one read transaction; writes through them are refused."""

    workspace: WorkspaceRepository
    artifacts: ArtifactRepository
    families: FamilyRepository
    batches: BatchRepository
    snapshots: SnapshotRepository
    representations: RepresentationRepository
    eligibility: EligibilityRepository


class WriteTransaction(ReadTransaction, Protocol):
    """Repositories bound to one write transaction that commits or rolls back whole."""
