"""Membership pages: the bucketed, content-addressed binding of one snapshot."""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from .artifacts import ArtifactRef, ManifestRef
from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .errors import TychoraError
from .identity import JsonValue, canonical_bytes, digest

PAGE_BUCKETS = 256
MAX_PAGE_BYTES = 4 * 1024 * 1024


def bucket_of(source_id: str) -> int:
    """Return the page bucket of a source: the first byte of its SHA-256."""
    return int(sha256(source_id.encode("utf-8")).hexdigest()[:2], 16)


class IndexFamily(Contract):
    """One index family: a backend configuration bound to a channel."""

    family_id: Sha256
    channel: Literal["lexical", "text", "image", "graph"]
    backend: NonEmptyStr
    provider: str | None = None
    model: str | None = None
    dimension: int | None = None
    config_sha: Sha256
    config_json: FrozenJsonMap
    backend_namespace: NonEmptyStr
    created_at: NonEmptyStr


class SlotFamily(Contract):
    """The family a snapshot serves one slot from."""

    slot: NonEmptyStr
    family: IndexFamily
    required: bool = True

    @model_validator(mode="after")
    def slot_is_channel(self) -> Self:
        """Require the family channel to equal its slot."""
        if self.family.channel != self.slot:
            raise ValueError("Slot family channel differs from its slot")
        return self


class PageBatch(Contract):
    """One index batch a page member publishes for a slot."""

    slot: NonEmptyStr
    batch_id: Identifier
    family_id: Sha256
    count: NonNegativeInt
    receipt_digest: Sha256
    input_manifest: ArtifactRef
    vectors: ArtifactRef | None = None


class PageMember(Contract):
    """One source revision in a page with the batches it publishes."""

    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    is_default: bool = True
    batches: tuple[PageBatch, ...] = ()

    @model_validator(mode="after")
    def one_batch_per_slot(self) -> Self:
        """Require batches unique and sorted by slot."""
        slots = [batch.slot for batch in self.batches]
        if slots != sorted(set(slots)):
            raise ValueError("Page member batches must be unique and sorted by slot")
        return self


def compute_page_id(bucket: int, members: Sequence[PageMember]) -> str:
    """Bind one bucket's members and every batch they publish."""
    return digest(
        "membership-page-v2",
        bucket,
        [member.model_dump(mode="json", exclude={"batches"}) for member in members],
        [
            [batch.model_dump(mode="json") for batch in member.batches]
            for member in members
        ],
    )


class MembershipPage(Contract):
    """The members of one bucket of a snapshot, identified by their content."""

    bucket: NonNegativeInt
    members: tuple[PageMember, ...]
    page_id: Identifier

    @model_validator(mode="after")
    def exact_page(self) -> Self:
        """Require sorted members of this bucket, one default each, and exact identity."""
        if self.bucket >= PAGE_BUCKETS:
            raise ValueError("Page bucket is outside 0..255")
        keys = [(m.source_id, m.revision_id) for m in self.members]
        if keys != sorted(set(keys)):
            raise ValueError("Page members must be unique and sorted")
        if any(bucket_of(m.source_id) != self.bucket for m in self.members):
            raise ValueError("Page member belongs to another bucket")
        defaults = [m.source_id for m in self.members if m.is_default]
        if len(defaults) != len(set(defaults)):
            raise ValueError("Page has two default revisions for one source")
        if self.page_id != compute_page_id(self.bucket, self.members):
            raise ValueError("Page identity differs from its bindings")
        return self

    def manifest_document(self) -> dict[str, JsonValue]:
        """Return the retention document: inputs and vector batches, no content."""
        batches = [batch for member in self.members for batch in member.batches]
        inputs = {
            batch.input_manifest.sha256: batch.input_manifest for batch in batches
        }
        vectors = {
            batch.vectors.sha256: batch.vectors
            for batch in batches
            if batch.vectors is not None
        }
        return {
            "type": "membership_page",
            "schema_version": "2",
            "required_manifests": [
                ManifestRef(
                    artifact=ref, type="index_input", schema_version="1"
                ).model_dump(mode="json")
                for ref in sorted(inputs.values(), key=lambda ref: ref.sha256)
            ],
            "required_artifacts": [
                ref.model_dump(mode="json")
                for ref in sorted(vectors.values(), key=lambda ref: ref.sha256)
            ],
            "data": {
                "bucket": self.bucket,
                "page_id": self.page_id,
                "members": [member.model_dump(mode="json") for member in self.members],
            },
        }

    def manifest_bytes(self, *, limit: int = MAX_PAGE_BYTES) -> bytes:
        """Return the canonical manifest bytes or fail when they exceed the limit."""
        raw = canonical_bytes(self.manifest_document())
        if len(raw) > limit:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Membership page exceeds its byte limit",
                reason="invariant.page_oversize",
            )
        return raw


@dataclass(frozen=True)
class SnapshotBinding:
    """Everything but the pages that a snapshot identity binds."""

    workspace_id: str
    base_snapshot_id: str | None
    job_id: str
    config_sha: str
    families: Sequence[SlotFamily]
    root_batch_ids: Sequence[str] = ()


def _family_bindings(families: Sequence[SlotFamily]) -> list[JsonValue]:
    return [
        {
            "slot": item.slot,
            "family_id": item.family.family_id,
            "required": item.required,
        }
        for item in sorted(families, key=lambda item: item.slot)
    ]


def snapshot_id_v2(binding: SnapshotBinding, page_ids: Sequence[str]) -> str:
    """Bind a snapshot to its base, job, configuration, families and pages."""
    return digest(
        "snapshot-v2",
        binding.workspace_id,
        binding.base_snapshot_id,
        binding.job_id,
        binding.config_sha,
        _family_bindings(binding.families),
        sorted(page_ids),
        sorted(binding.root_batch_ids),
    )


def membership_sha(page_ids: Sequence[str]) -> str:
    """Return the digest of a snapshot's page set, independent of order."""
    return digest("snapshot-membership-v2", sorted(page_ids))
