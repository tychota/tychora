"""Transport-independent text embedding records."""

import math
import struct
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    Contract,
    FiniteFloat,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .identity import space_id as derive_space_id


class EmbeddingCapabilities(Contract):
    """Qualified immutable identity for one text-vector space."""

    provider: NonEmptyStr
    endpoint: NonEmptyStr
    model: NonEmptyStr
    dimensions: PositiveInt
    dtype: Literal["float32"] = "float32"
    input_types: frozenset[Literal["document", "query"]]
    contextualization: Literal["joint", "independent"]
    tokenizer_id: NonEmptyStr
    space_id: Sha256
    representation_fingerprint: Sha256
    metric: Literal["cosine"] = "cosine"
    normalization: Literal["l2"] = "l2"
    max_group_tokens: PositiveInt
    max_request_tokens: PositiveInt
    options: FrozenJsonMap = Field(default_factory=FrozenJsonMap)

    @model_validator(mode="after")
    def exact_space(self) -> Self:
        if self.input_types != frozenset({"document", "query"}):
            raise ValueError("Text embedding capability must support both input modes")
        expected = derive_space_id(
            "text",
            self.provider,
            self.model,
            self.dimensions,
            self.metric,
            self.representation_fingerprint,
        )
        if self.space_id != expected:
            raise ValueError("Embedding space identity differs from capabilities")
        return self


class QueryUploadAuthorization(Contract):
    """Exact query-only upload facts bound by trusted operation composition."""

    schema_version: Literal["1"] = "1"
    mode: Literal["query"] = "query"
    payload_sha: Sha256
    input_shas: Annotated[tuple[Sha256, ...], Field(min_length=1, max_length=256)]
    collection_ids: Annotated[
        frozenset[Identifier], Field(min_length=1, max_length=1000)
    ]
    dependency_ids: Annotated[frozenset[Identifier], Field(max_length=2048)] = (
        frozenset()
    )
    policy_epoch: NonNegativeInt


class EmbeddingRecord(Contract):
    """One immutable normalized vector and its exact storage identity."""

    record_id: Identifier
    input_sha: Sha256
    value_sha: Sha256
    space_id: Sha256
    values: tuple[FiniteFloat, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_value(self) -> Self:
        stored = struct.pack("<" + "f" * len(self.values), *self.values)
        norm = math.sqrt(sum(value * value for value in self.values))
        if sha256(stored).hexdigest() != self.value_sha or not math.isclose(
            norm, 1.0, rel_tol=2e-6, abs_tol=2e-6
        ):
            raise ValueError("Embedding record differs from normalized storage bytes")
        return self

    @classmethod
    def from_normalized_bytes(
        cls,
        *,
        record_id: str,
        input_sha: str,
        space_id: str,
        stored: bytes,
    ) -> Self:
        if not stored or len(stored) % 4:
            raise ValueError("Embedding bytes must contain complete float32 values")
        values = struct.unpack("<" + "f" * (len(stored) // 4), stored)
        return cls(
            record_id=record_id,
            input_sha=input_sha,
            value_sha=sha256(stored).hexdigest(),
            space_id=space_id,
            values=values,
        )
