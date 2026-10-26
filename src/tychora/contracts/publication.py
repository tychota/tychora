"""Publication plans, retained batch contents and snapshot views."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Self

from pydantic import field_serializer, model_validator

from .artifacts import ArtifactRef, ManifestRef
from .base import (
    Contract,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .graph import GraphBatchReceipt, GraphManifest
from .identity import JsonValue, canonical_bytes, digest
from .snapshots import (
    PAGE_BUCKETS,
    MembershipPage,
    PageBatch,
    SlotFamily,
    SnapshotBinding,
    membership_sha,
    snapshot_id_v2,
)
from .vectors import BatchReceipt


class SnapshotMember(Contract):
    """One source revision a snapshot selects, with its extraction."""

    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    is_default: bool = True


class LexicalRecord(Contract):
    """The searchable fields of one chunk in a lexical batch."""

    chunk_id: Identifier
    identifiers: str = ""
    title: str = ""
    headings: str = ""
    body: str = ""
    context: str = ""


class VectorRecordBinding(Contract):
    """One vector record, its canonical owner and its row in the batch artifact."""

    record_id: Identifier
    input_sha: Sha256
    value_sha: Sha256
    chunk_id: Identifier | None = None
    asset_id: Identifier | None = None
    row_index: NonNegativeInt
    group_id: Identifier | None = None
    dependency_ids: frozenset[Identifier] = frozenset()

    @field_serializer("dependency_ids")
    def sorted_dependencies(self, dependencies: frozenset[str]) -> list[str]:
        """Serialise dependencies in sorted order so bytes are deterministic."""
        return sorted(dependencies)

    @model_validator(mode="after")
    def one_owner(self) -> Self:
        """Require exactly one owner and paired group identity and dependencies."""
        if (self.chunk_id is None) == (self.asset_id is None):
            raise ValueError("Vector record requires exactly one canonical owner")
        if (self.group_id is None) != (not self.dependency_ids):
            raise ValueError("Text group identity and dependencies must be paired")
        return self


class LexicalBatchReceipt(Contract):
    """The receipt of a lexical batch: its family, count and entry digest."""

    family_id: Sha256
    count: NonNegativeInt
    entry_ids_sha: Sha256


type BatchReceiptVariant = LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt


def receipt_document_digest(receipt: BatchReceiptVariant) -> str:
    """SHA-256 of a batch receipt's canonical JSON, as pages bind it."""
    return sha256(canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()


def receipt_count(receipt: BatchReceiptVariant) -> int:
    """Return the receipt's record count (evidence count for graph batches)."""
    if isinstance(receipt, GraphBatchReceipt):
        return receipt.evidence_count
    return receipt.count


class NewBatch(Contract):
    """An index batch about to be built, bound to its exact input manifest."""

    slot: NonEmptyStr
    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: BatchReceiptVariant
    vectors: ArtifactRef | None = None

    @model_validator(mode="after")
    def bound_identity(self) -> Self:
        """Require the batch ID to bind the family, extraction and input manifest."""
        expected = digest(
            "index-batch-v1",
            self.family_id,
            self.extraction_id,
            self.input_manifest.sha256,
        )
        if self.batch_id != expected:
            raise ValueError("Index batch identity differs from exact input")
        return self

    @property
    def count(self) -> int:
        """Return the receipt's record count (evidence count for graph batches)."""
        return receipt_count(self.receipt)

    def page_batch(self) -> PageBatch:
        """Return the binding a membership page records for this batch."""
        return PageBatch(
            slot=self.slot,
            batch_id=self.batch_id,
            family_id=self.family_id,
            count=self.count,
            receipt_digest=receipt_document_digest(self.receipt),
            input_manifest=self.input_manifest,
            vectors=self.vectors,
        )


class RetainedBatch(Contract):
    """One published batch with the rows read paths need, rebuilt from SQL."""

    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None = None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: BatchReceiptVariant
    vectors: ArtifactRef | None = None
    graph_manifest: GraphManifest | None = None
    lexical_records: tuple[LexicalRecord, ...] = ()
    vector_records: tuple[VectorRecordBinding, ...] = ()

    @model_validator(mode="after")
    def vectors_match_records(self) -> Self:
        """Pair the records with their one batch artifact, rows in record order."""
        if bool(self.vector_records) != (self.vectors is not None):
            raise ValueError("Vector records and their batch artifact must be paired")
        ordered = sorted(self.vector_records, key=lambda row: row.record_id)
        if [row.row_index for row in ordered] != list(range(len(ordered))):
            raise ValueError("Vector row indexes must follow record order")
        return self


def _page_batches(pages: tuple[MembershipPage, ...]) -> list[PageBatch]:
    return [
        batch for page in pages for member in page.members for batch in member.batches
    ]


class PublicationPlan(Contract):
    """Everything one activation writes: new pages, new batches and the root."""

    job_id: Identifier
    snapshot_id: Identifier
    workspace_id: Identifier
    base_snapshot_id: Identifier | None = None
    config_sha: Sha256
    families: tuple[SlotFamily, ...]
    member_count: NonNegativeInt
    new_pages: tuple[MembershipPage, ...]
    reused_page_ids: tuple[Identifier, ...]
    new_batches: tuple[NewBatch, ...]
    root_batches: tuple[PageBatch, ...] = ()
    graph_watermark: NonNegativeInt | None = None
    manifest: ArtifactRef

    @property
    def page_ids(self) -> tuple[str, ...]:
        """Return every page of the snapshot, new and reused, sorted."""
        return tuple(
            sorted({*self.reused_page_ids, *(p.page_id for p in self.new_pages)})
        )

    def binding(self) -> SnapshotBinding:
        """Return the bindings, besides pages, that the snapshot identity covers."""
        return SnapshotBinding(
            workspace_id=self.workspace_id,
            base_snapshot_id=self.base_snapshot_id,
            job_id=self.job_id,
            config_sha=self.config_sha,
            families=self.families,
            root_batch_ids=tuple(batch.batch_id for batch in self.root_batches),
        )

    def _require_bound_families(self) -> None:
        families = {item.slot: item.family.family_id for item in self.families}
        if len(families) != len(self.families):
            raise ValueError("Snapshot slot membership differs")
        batches = [*_page_batches(self.new_pages), *self.root_batches]
        if any(families.get(batch.slot) != batch.family_id for batch in batches):
            raise ValueError("A page batch is not served by its slot family")

    def _require_bound_pages(self) -> None:
        buckets = [page.bucket for page in self.new_pages]
        if len(buckets) != len(set(buckets)) or len(self.page_ids) > PAGE_BUCKETS:
            raise ValueError("Snapshot pages overlap")
        if {p.page_id for p in self.new_pages} & set(self.reused_page_ids):
            raise ValueError("A new page is also listed as reused")
        bound = {batch.batch_id for batch in _page_batches(self.new_pages)}
        bound |= {batch.batch_id for batch in self.root_batches}
        if not {batch.batch_id for batch in self.new_batches} <= bound:
            raise ValueError("A new batch is not bound by any page")

    @model_validator(mode="after")
    def exact_bindings(self) -> Self:
        """Require bound families, disjoint pages and the exact snapshot identity."""
        self._require_bound_families()
        self._require_bound_pages()
        if self.snapshot_id != snapshot_id_v2(self.binding(), self.page_ids):
            raise ValueError("Snapshot identity differs from complete bindings")
        return self


def snapshot_root_document(
    plan: PublicationPlan, page_manifests: Mapping[str, ArtifactRef]
) -> dict[str, JsonValue]:
    """The snapshot's retention document: page references, no member content."""
    links = [
        ManifestRef(
            artifact=page_manifests[page_id],
            type="membership_page",
            schema_version="2",
        )
        for page_id in plan.page_ids
    ] + [
        ManifestRef(
            artifact=batch.input_manifest, type="index_input", schema_version="1"
        )
        for batch in plan.root_batches
    ]
    return {
        "type": "snapshot",
        "schema_version": "2",
        "required_manifests": [
            link.model_dump(mode="json")
            for link in sorted(links, key=lambda link: link.artifact.sha256)
        ],
        "data": {
            "workspace_id": plan.workspace_id,
            "base_snapshot_id": plan.base_snapshot_id,
            "job_id": plan.job_id,
            "config_sha": plan.config_sha,
            "families": [item.model_dump(mode="json") for item in plan.families],
            "membership_sha": membership_sha(plan.page_ids),
            "member_count": plan.member_count,
            "page_ids": list(plan.page_ids),
            "root_batches": [
                batch.model_dump(mode="json") for batch in plan.root_batches
            ],
        },
    }


def publication_plan_document(plan: PublicationPlan) -> dict[str, JsonValue]:
    """The job checkpoint that lets a resumed job publish the same plan."""
    return {
        "type": "publication_plan",
        "schema_version": "2",
        "required_manifests": [
            ManifestRef(
                artifact=plan.manifest, type="snapshot", schema_version="2"
            ).model_dump(mode="json")
        ],
        "data": plan.model_dump(mode="json"),
    }


class SnapshotView(Contract):
    """A published snapshot as read back from SQL rows, without its members.

    Members are read in pages through ``SnapshotRepository.members``.
    """

    snapshot_id: Identifier
    workspace_id: Identifier
    parent_snapshot_id: Identifier | None = None
    config_sha: Sha256
    membership_sha: Sha256
    member_count: NonNegativeInt
    page_ids: tuple[Identifier, ...]
    families: tuple[SlotFamily, ...]

    def family(self, slot: str) -> SlotFamily | None:
        """Return the family serving ``slot``, or ``None`` when the slot is absent."""
        return next((item for item in self.families if item.slot == slot), None)
