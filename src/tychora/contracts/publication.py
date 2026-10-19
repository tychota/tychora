"""Complete, immutable inputs to one canonical snapshot activation."""

from typing import Self

from pydantic import field_serializer, model_validator

from .artifacts import ArtifactRef
from .base import (
    Contract,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .graph import GraphBatchReceipt, GraphManifest
from .identity import digest
from .snapshots import IndexFamily
from .vectors import BatchReceipt


class SnapshotMember(Contract):
    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    is_default: bool = True


class LexicalRecord(Contract):
    chunk_id: Identifier
    identifiers: str = ""
    title: str = ""
    headings: str = ""
    body: str = ""
    context: str = ""


class VectorRecordBinding(Contract):
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
        return sorted(dependencies)

    @model_validator(mode="after")
    def one_owner(self) -> Self:
        if (self.chunk_id is None) == (self.asset_id is None):
            raise ValueError("Vector record requires exactly one canonical owner")
        if (self.group_id is None) != (not self.dependency_ids):
            raise ValueError("Text group identity and dependencies must be paired")
        return self


class LexicalBatchReceipt(Contract):
    family_id: Sha256
    count: NonNegativeInt
    entry_ids_sha: Sha256


class PublicationBatch(Contract):
    batch_id: Identifier
    family_id: Sha256
    extraction_id: Identifier | None = None
    input_manifest: ArtifactRef
    expected_count: NonNegativeInt
    receipt: LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt
    graph_manifest: GraphManifest | None = None
    lexical_records: tuple[LexicalRecord, ...] = ()
    vector_records: tuple[VectorRecordBinding, ...] = ()
    vectors: ArtifactRef | None = None

    @model_validator(mode="after")
    def vectors_match_records(self) -> Self:
        """Pair the records with their one batch artifact, rows in record order."""
        if bool(self.vector_records) != (self.vectors is not None):
            raise ValueError("Vector records and their batch artifact must be paired")
        ordered = sorted(self.vector_records, key=lambda row: row.record_id)
        if [row.row_index for row in ordered] != list(range(len(ordered))):
            raise ValueError("Vector row indexes must follow record order")
        return self


class PublicationSlot(Contract):
    slot: NonEmptyStr
    family: IndexFamily
    required: bool = True
    batches: tuple[PublicationBatch, ...]

    @model_validator(mode="after")
    def bound_batches(self) -> Self:
        if any(batch.family_id != self.family.family_id for batch in self.batches):
            raise ValueError("Snapshot slot batch family differs")
        if len({batch.batch_id for batch in self.batches}) != len(self.batches):
            raise ValueError("Snapshot slot duplicates a batch")
        return self


class PublicationPlan(Contract):
    job_id: Identifier
    snapshot_id: Identifier
    workspace_id: Identifier
    base_snapshot_id: Identifier | None = None
    config_sha: Sha256
    manifest: ArtifactRef
    members: tuple[SnapshotMember, ...]
    slots: tuple[PublicationSlot, ...]

    @model_validator(mode="after")
    def exact_membership(self) -> Self:
        if len(
            {member.source_id for member in self.members if member.is_default}
        ) != sum(member.is_default for member in self.members):
            raise ValueError("Snapshot has duplicate default source")
        if len({member.revision_id for member in self.members}) != len(self.members):
            raise ValueError("Snapshot revision membership differs")
        if len({slot.slot for slot in self.slots}) != len(self.slots):
            raise ValueError("Snapshot slot membership differs")
        expected = digest(
            "snapshot-v1",
            self.workspace_id,
            self.base_snapshot_id,
            self.job_id,
            self.config_sha,
            [member.model_dump(mode="json") for member in self.members],
            [
                {
                    "slot": slot.slot,
                    "family_id": slot.family.family_id,
                    "required": slot.required,
                    "batch_ids": [batch.batch_id for batch in slot.batches],
                }
                for slot in self.slots
            ],
        )
        if self.snapshot_id != expected:
            raise ValueError("Snapshot identity differs from complete bindings")
        return self


class SnapshotView(Contract):
    snapshot_id: Identifier
    workspace_id: Identifier
    parent_snapshot_id: Identifier | None = None
    membership_sha: Sha256
    config_sha: Sha256
    manifest: ArtifactRef
    members: tuple[SnapshotMember, ...]
    slots: tuple[PublicationSlot, ...]
    lexical_statistics_epoch: NonNegativeInt = 0
