"""Transport-independent text embedding records."""

import math
import struct
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .artifacts import ArtifactRef
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
from .corpus import AssetRow
from .evidence import ImageLocator
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
    modality: Literal["text", "visual"] = "text"
    visual_limits: "VisualEmbeddingLimits | None" = None

    @model_validator(mode="after")
    def exact_space(self) -> Self:
        if self.input_types != frozenset({"document", "query"}):
            raise ValueError("Text embedding capability must support both input modes")
        if (self.modality == "visual") != (self.visual_limits is not None):
            raise ValueError("Visual capability limits differ from embedding modality")
        expected = derive_space_id(
            self.modality,
            self.provider,
            self.model,
            self.dimensions,
            self.metric,
            self.representation_fingerprint,
        )
        if self.space_id != expected:
            raise ValueError("Embedding space identity differs from capabilities")
        return self


def stable_capability_json(capabilities: EmbeddingCapabilities) -> dict:
    """Canonicalize unordered input modes for newly published family identity."""
    projected = capabilities.model_dump(mode="json")
    projected["input_types"] = sorted(capabilities.input_types)
    return projected


class VisualEmbeddingLimits(Contract):
    """Qualified local accounting and request-resource limits for one visual model."""

    accounting_version: Literal["voyage-multimodal-accounting-v1"]
    model_revision: NonEmptyStr
    tokenizer_sha256: Sha256
    client_config_sha256: Sha256
    accounting_image_pixels_min: PositiveInt
    accounting_image_pixels_max: PositiveInt
    image_pixels_per_token: PositiveInt
    source_pixels_max: PositiveInt
    image_encoded_bytes_max: PositiveInt
    input_tokens_max: PositiveInt
    request_assets_max: PositiveInt
    request_tokens_max: PositiveInt
    request_wire_bytes_max: PositiveInt
    decoded_image_bytes_max: PositiveInt

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            self.accounting_image_pixels_min > self.accounting_image_pixels_max
            or self.source_pixels_max > 16_000_000
            or self.request_assets_max > 32
            or self.request_tokens_max > 100_000
            or self.image_encoded_bytes_max > 20_000_000
            or self.request_wire_bytes_max > 64 * 1024 * 1024
            or self.input_tokens_max > 32_000
            or self.decoded_image_bytes_max < self.source_pixels_max * 3
        ):
            raise ValueError("Visual capability limits exceed qualified bounds")
        return self


class VisualResizeDerivation(Contract):
    """Explicit representation derivation; no implicit resize is permitted."""

    source_width: PositiveInt
    source_height: PositiveInt
    width: PositiveInt
    height: PositiveInt
    mode: Literal["none", "LANCZOS"]

    @model_validator(mode="after")
    def recorded_resize(self) -> Self:
        if (self.mode == "none") != (
            self.source_width == self.width and self.source_height == self.height
        ):
            raise ValueError("Visual resize derivation is inconsistent")
        return self


class VisualInput(Contract):
    """A canonical visual descriptor; pixel bytes are resolved by a trusted reader."""

    asset: AssetRow
    artifact_ref: ArtifactRef
    revision_id: Identifier
    context: str
    dependency_ids: Annotated[
        frozenset[Identifier], Field(min_length=1, max_length=2048)
    ]
    pixel_sha256: Sha256
    width: PositiveInt
    height: PositiveInt
    pixel_transform: Literal["exif-transpose-rgb-v1"] = "exif-transpose-rgb-v1"
    resize: VisualResizeDerivation
    input_sha: Sha256

    @classmethod
    def from_registered(
        cls,
        *,
        asset: AssetRow,
        artifact_ref: ArtifactRef,
        revision_id: str,
        context: str,
        dependency_ids: frozenset[str],
        pixel_sha256: str,
        width: int,
        height: int,
        resize: VisualResizeDerivation,
        pixel_transform: Literal["exif-transpose-rgb-v1"] = "exif-transpose-rgb-v1",
    ) -> Self:
        return cls(
            asset=asset,
            artifact_ref=artifact_ref,
            revision_id=revision_id,
            context=context,
            dependency_ids=dependency_ids,
            pixel_sha256=pixel_sha256,
            width=width,
            height=height,
            pixel_transform=pixel_transform,
            resize=resize,
            input_sha=_visual_input_sha(
                asset,
                artifact_ref,
                revision_id,
                context,
                dependency_ids,
                pixel_sha256,
                width,
                height,
                pixel_transform,
                resize,
            ),
        )

    @model_validator(mode="after")
    def canonical_descriptor(self) -> Self:
        if (
            not isinstance(self.asset.locator, ImageLocator)
            or self.asset.artifact_sha != self.artifact_ref.sha256
            or self.artifact_ref.storage_key != self.artifact_ref.sha256
            or self.resize.width != self.width
            or self.resize.height != self.height
            or self.input_sha
            != _visual_input_sha(
                self.asset,
                self.artifact_ref,
                self.revision_id,
                self.context,
                self.dependency_ids,
                self.pixel_sha256,
                self.width,
                self.height,
                self.pixel_transform,
                self.resize,
            )
        ):
            raise ValueError(
                "Visual input differs from its registered pixel descriptor"
            )
        return self


class TextQuery(Contract):
    kind: Literal["text"] = "text"
    text: NonEmptyStr


class RegisteredImageQuery(Contract):
    kind: Literal["image"] = "image"
    image: VisualInput


type TextOrImage = Annotated[
    TextQuery | RegisteredImageQuery, Field(discriminator="kind")
]


def _visual_input_sha(
    asset: AssetRow,
    artifact_ref: ArtifactRef,
    revision_id: str,
    context: str,
    dependency_ids: frozenset[str],
    pixel_sha256: str,
    width: int,
    height: int,
    pixel_transform: str,
    resize: VisualResizeDerivation,
) -> str:
    from .identity import digest

    return digest(
        "visual-input-v1",
        asset.model_dump(mode="json"),
        artifact_ref.model_dump(mode="json"),
        revision_id,
        context,
        sorted(dependency_ids),
        pixel_sha256,
        width,
        height,
        pixel_transform,
        resize.model_dump(mode="json"),
    )


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


class QueryEmbeddingProbe(Contract):
    """Immutable local-cache outcome for one owner-admitted query operation."""

    workspace_id: Identifier
    provider: NonEmptyStr
    query_batch_sha: Sha256
    input_count: Annotated[NonNegativeInt, Field(le=256)]
    state: Literal["hit", "miss"]
    records: tuple[EmbeddingRecord, ...] = ()
    authorization: QueryUploadAuthorization | None = None

    @model_validator(mode="after")
    def exact_outcome(self) -> Self:
        if self.state == "hit" and (
            self.authorization is not None or len(self.records) != self.input_count
        ):
            raise ValueError("Query cache hit outcome is inconsistent")
        if self.state == "miss" and (
            self.authorization is None
            or bool(self.records)
            or len(self.authorization.input_shas) > self.input_count
        ):
            raise ValueError("Query cache miss outcome is inconsistent")
        return self


class VisualQueryEmbeddingProbe(Contract):
    """Pre-read operation identity is distinct from a prepared request-body hash."""

    workspace_id: Identifier
    provider: NonEmptyStr
    query_sha: Sha256
    operation_sha: Sha256
    state: Literal["hit", "miss"]
    record: EmbeddingRecord | None = None

    @model_validator(mode="after")
    def exact_outcome(self) -> Self:
        if (self.state == "hit") != (self.record is not None):
            raise ValueError("Visual query probe outcome is inconsistent")
        return self


class VisualQueryAdmission(Contract):
    """Canonical pre-read query admission; this contains no request-body digest."""

    operation_sha: Sha256
    input_shas: Annotated[tuple[Sha256, ...], Field(min_length=1, max_length=1)]
    collection_ids: Annotated[
        frozenset[Identifier], Field(min_length=1, max_length=1000)
    ]
    dependency_ids: Annotated[frozenset[Identifier], Field(max_length=2048)] = (
        frozenset()
    )
    policy_epoch: NonNegativeInt
