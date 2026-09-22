"""Immutable indexing inputs, source windows, and receipt identities."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .evidence import EvidenceUnit
from .identity import chunk_id as derive_chunk_id
from .identity import digest
from .identity import family_id as derive_family_id


class OwnedInterval(Contract):
    """One unit-local range with every identity required to use its text."""

    char_start: NonNegativeInt
    char_end: PositiveInt
    dependency_ids: frozenset[Identifier] = Field(min_length=1)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("Owned interval must contain source text")
        return self


class RepresentationUnit(Contract):
    """One canonical unit prepared entirely from a retained extraction bundle."""

    unit: EvidenceUnit
    revision_id: Identifier
    title: NonEmptyStr
    kind_id: NonEmptyStr
    symbol_path: tuple[str, ...] = ()
    ownership: tuple[OwnedInterval, ...] = ()

    @model_validator(mode="after")
    def exact_partition(self) -> Self:
        if not self.unit.text:
            if self.ownership:
                raise ValueError("Empty units cannot own text intervals")
            return self
        cursor = 0
        for interval in self.ownership:
            if interval.char_start != cursor or interval.char_end > len(self.unit.text):
                raise ValueError("Owned intervals must exactly partition unit text")
            if not {self.revision_id, self.unit.unit_id} <= interval.dependency_ids:
                raise ValueError("Owned interval omits canonical unit dependencies")
            cursor = interval.char_end
        if cursor != len(self.unit.text):
            raise ValueError("Owned intervals must cover all unit text")
        return self


class PreparedExtraction(Contract):
    """One validated extraction-wide representation and ownership view."""

    extraction_id: Identifier
    revision_id: Identifier
    units: tuple[RepresentationUnit, ...]

    @model_validator(mode="after")
    def unique_membership(self) -> Self:
        if len({item.unit.unit_id for item in self.units}) != len(self.units) or any(
            item.unit.extraction_id != self.extraction_id
            or item.revision_id != self.revision_id
            for item in self.units
        ):
            raise ValueError("Prepared extraction membership differs")
        return self

    def representation(self, unit_id: str) -> RepresentationUnit:
        try:
            return next(item for item in self.units if item.unit.unit_id == unit_id)
        except StopIteration:
            raise ValueError("Representation unit is absent from extraction") from None


class SearchWindow(Contract):
    chunk_id: Identifier
    unit_id: Identifier
    extraction_id: Identifier
    revision_id: Identifier
    unit_ordinal: NonNegativeInt
    unit_type: NonEmptyStr
    char_start: NonNegativeInt
    char_end: PositiveInt
    text: str
    token_count: PositiveInt
    tokenizer_id: NonEmptyStr
    chunker_fingerprint: Sha256
    boundary_key: NonEmptyStr
    context_group_key: Identifier
    dependency_ids: frozenset[Identifier] = Field(min_length=1)
    title: NonEmptyStr
    kind_id: NonEmptyStr
    heading_path: tuple[str, ...] = ()
    symbol_path: tuple[str, ...] = ()

    @model_validator(mode="after")
    def exact_text_range(self) -> Self:
        if (
            self.char_end <= self.char_start
            or not self.text
            or len(self.text) != self.char_end - self.char_start
        ):
            raise ValueError("Search window text differs from exact range length")
        if self.chunk_id != derive_chunk_id(
            self.unit_id,
            self.char_start,
            self.char_end,
            self.chunker_fingerprint,
        ) or self.context_group_key != digest(
            "context-group-v1", self.extraction_id, self.boundary_key
        ):
            raise ValueError("Search window chunk identity differs")
        if not {self.unit_id, self.revision_id} <= self.dependency_ids:
            raise ValueError("Search window omits canonical dependencies")
        return self


class ContextGroup(Contract):
    group_id: Identifier
    context_group_key: Identifier
    extraction_id: Identifier
    revision_id: Identifier
    capsule: NonEmptyStr
    input_texts: tuple[NonEmptyStr, ...] = Field(min_length=2)
    window_ids: tuple[Identifier, ...] = Field(min_length=1)
    window_input_shas: tuple[Sha256, ...] = Field(min_length=1)
    input_sha: Sha256
    dependency_ids: frozenset[Identifier] = Field(min_length=1)
    token_count: PositiveInt
    tokenizer_id: NonEmptyStr
    provider: NonEmptyStr
    model: NonEmptyStr
    dimensions: PositiveInt
    dtype: Literal["float32"] = "float32"
    representation_version: NonEmptyStr
    contextualization: Literal["joint", "independent"]
    options: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    request_index: NonNegativeInt = 0

    @model_validator(mode="after")
    def exact_mapping(self) -> Self:
        if (
            self.input_texts[0] != self.capsule
            or len(self.input_texts) != len(self.window_ids) + 1
            or len(self.window_input_shas) != len(self.window_ids)
            or self.contextualization == "joint"
            and any(value != self.input_sha for value in self.window_input_shas)
            or self.contextualization == "independent"
            and len(self.window_ids) != 1
        ):
            raise ValueError("Context group input-to-window mapping differs")
        expected_input = digest(
            "embedding-input-v1",
            self.input_texts,
            self.provider,
            self.model,
            self.dimensions,
            self.dtype,
            self.representation_version,
            self.contextualization,
            self.options,
        )
        if self.input_sha != expected_input:
            raise ValueError("Context group input identity differs")
        if self.group_id != digest(
            "context-group-input-v1",
            self.extraction_id,
            self.context_group_key,
            self.window_ids,
            self.input_sha,
        ):
            raise ValueError("Context group immutable identity differs")
        if self.revision_id not in self.dependency_ids:
            raise ValueError("Context group omits revision dependency")
        return self


class FamilySpec(Contract):
    """Immutable physical index-family identity and representation configuration."""

    family_id: Sha256
    workspace_id: Identifier
    config_sha: Sha256
    creation_uuid: NonEmptyStr
    modality: NonEmptyStr
    provider: NonEmptyStr
    model: NonEmptyStr
    dimensions: PositiveInt
    metric: NonEmptyStr
    normalization: NonEmptyStr
    representation_fingerprint: Sha256
    backend_namespace: NonEmptyStr
    backend_space: NonEmptyStr = "ip"

    @field_validator("creation_uuid")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Family creation UUID must be canonical")
        return value

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        if self.family_id != derive_family_id(
            self.workspace_id, self.config_sha, self.creation_uuid
        ):
            raise ValueError("Family identity differs from creation configuration")
        return self


class IndexRecordDigest(Contract):
    record_id: Identifier
    input_sha: Sha256
    value_sha: Sha256
