"""Backend-independent vector batch, receipt, and candidate contracts."""

import math
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import Contract, Identifier, NonNegativeInt, PositiveInt, Sha256
from .candidates import CandidateKey
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


class WorkLimits(Contract):
    max_partitions: PositiveInt = Field(default=64, le=64)
    max_returned_records: PositiveInt = Field(default=20_000, le=20_000)
    deadline: float | None = None

    @model_validator(mode="after")
    def valid_deadline(self) -> Self:
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("Work deadline must be finite")
        return self


class DenseCandidate(Contract):
    key: CandidateKey
    distance: float
    record_ids: tuple[Identifier, ...]


class CandidatePage(Contract):
    candidates: tuple[DenseCandidate, ...]
    missing_record_ids: tuple[Identifier, ...]
    coverage_complete: bool
    truncated: bool
    exhausted: bool
    returned_records: NonNegativeInt
    queried_partitions: NonNegativeInt
