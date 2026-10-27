"""Backend-independent vector batch, receipt and partition contracts."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .base import Contract, Identifier, NonNegativeInt, Sha256
from .embeddings import EmbeddingRecord
from .indexing import FamilySpec, IndexRecordDigest


class FamilyHandle(Contract):
    spec: FamilySpec
    namespace_hash: Sha256
    config_sha: Sha256


class VectorOwner(Contract):
    """The revision, extraction and owning unit or asset one vector record belongs to."""

    record_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    owner_kind: Literal["unit", "asset"]
    owner_id: Identifier


class VectorBatch(Contract):
    family_id: Sha256
    records: tuple[EmbeddingRecord, ...] = Field(min_length=1)
    owners: tuple[VectorOwner, ...] = ()

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if len({item.record_id for item in self.records}) != len(self.records):
            raise ValueError("Duplicate vector record ID")
        if self.owners and sorted(o.record_id for o in self.owners) != sorted(
            r.record_id for r in self.records
        ):
            raise ValueError("Vector owners must cover each record once")
        return self

    def partitions(self, dimension: int) -> tuple["VectorBatch", ...]:
        """Split into batches of at most ``partition_size(dimension)`` records with their owners."""
        size = partition_size(dimension)
        owners = {owner.record_id: owner for owner in self.owners}
        return tuple(
            VectorBatch(
                family_id=self.family_id,
                records=self.records[start : start + size],
                owners=tuple(
                    owners[record.record_id]
                    for record in self.records[start : start + size]
                    if record.record_id in owners
                ),
            )
            for start in range(0, len(self.records), size)
        )


PARTITION_BYTES = 2_000_000


def partition_size(dimension: int) -> int:
    """Records per native write so that one call carries at most 2 MB of float32."""
    return max(1, PARTITION_BYTES // (4 * dimension))


class BatchReceipt(Contract):
    family_id: Sha256
    namespace_hash: Sha256
    config_sha: Sha256
    records: tuple[IndexRecordDigest, ...]
    digest: Sha256
    count: NonNegativeInt


class Verification(Contract):
    ready: bool
    missing_record_ids: tuple[Identifier, ...] = ()
    mismatched_record_ids: tuple[Identifier, ...] = ()
    config_matches: bool = True
