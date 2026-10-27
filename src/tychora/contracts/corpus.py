"""Typed immutable rows accepted by the corpus repository."""

from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .artifacts import ArtifactRef
from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .code_storage import CodeFileDescriptor
from .errors import TychoraError
from .evidence import EvidenceUnit, Locator, ReviewState
from .indexing import PreparedExtraction

AccessRank = Annotated[int, Field(ge=0, le=3)]
# How a source's granted uses narrow: a named narrowing, the uses to keep (a
# tuple or list), or ``None`` to remove the override.
AllowedUsesOverride = Literal["never_generate", "reference_only"] | Sequence[str] | None


class WorkspaceRow(Contract):
    workspace_id: Identifier
    created_at: NonEmptyStr


class CollectionRow(Contract):
    collection_id: Identifier
    workspace_id: Identifier
    slug: NonEmptyStr
    title: NonEmptyStr
    access_floor: AccessRank = 0
    policy_json: FrozenJsonMap = Field(
        default_factory=lambda: FrozenJsonMap(
            {"schema_version": 1, "upload_providers": []}
        )
    )


class SourceRow(Contract):
    source_id: Identifier
    collection_id: Identifier
    source_key: NonEmptyStr
    source_type: Literal["document", "repository"] = "document"
    registration_json: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    access_floor: AccessRank = 0
    created_at: NonEmptyStr


class RevisionRow(Contract):
    revision_id: Identifier
    source_id: Identifier
    kind_id: Identifier
    original_sha: Sha256
    edition_key: str
    metadata_sha: Sha256
    title: NonEmptyStr
    published_on: str | None = None
    effective_from: str | None = None
    effective_until: str | None = None
    access_rank: AccessRank = 0
    declared_review_state: ReviewState = "unreviewed"
    allowed_uses_json: tuple[NonEmptyStr, ...] = ()
    metadata_json: FrozenJsonMap
    captured_at: NonEmptyStr


class RevisionArtifactRow(Contract):
    revision_id: Identifier
    role: NonEmptyStr
    source_ref: NonEmptyStr
    artifact_sha: Sha256


class ExtractionRow(Contract):
    extraction_id: Identifier
    revision_id: Identifier
    parser_fingerprint: NonEmptyStr
    normalizer_fingerprint: NonEmptyStr
    document_artifact_sha: Sha256
    assertions_artifact_sha: Sha256 | None = None
    diagnostics_artifact_sha: Sha256 | None = None
    created_at: NonEmptyStr


class LocatorRow(Contract):
    locator_id: Identifier
    unit_id: Identifier
    fragment_ordinal: NonNegativeInt
    locator: Locator


class ChunkRow(Contract):
    chunk_id: Identifier
    unit_id: Identifier
    extraction_id: Identifier
    ordinal_in_unit: NonNegativeInt
    char_start: NonNegativeInt
    char_end: PositiveInt
    context_group_key: NonEmptyStr
    chunker_fingerprint: NonEmptyStr
    token_count: PositiveInt
    text: str


class AssetRow(Contract):
    asset_id: Identifier
    extraction_id: Identifier
    artifact_sha: Sha256
    asset_type: NonEmptyStr
    native_ref: NonEmptyStr
    locator: Locator
    caption: str = ""
    declared_review_state: ReviewState = "unreviewed"


class UnitAssetRow(Contract):
    unit_id: Identifier
    asset_id: Identifier
    role: NonEmptyStr


class AssetProxyRow(Contract):
    proxy_unit_id: Identifier
    asset_id: Identifier
    origin_type: Literal["caption_copy", "ocr", "description", "structured_values"]
    derivation_sha: Sha256
    required_unit_ids: frozenset[Identifier]


class StructuredRecordRow(Contract):
    unit_id: Identifier
    record_type: NonEmptyStr
    schema_hash: Sha256
    data_json: FrozenJsonMap


class FacetRow(Contract):
    revision_id: Identifier | None = None
    unit_id: Identifier | None = None
    facet_key: NonEmptyStr
    ordinal: NonNegativeInt = 0
    value: str | int | float | bool

    @model_validator(mode="after")
    def one_subject(self) -> Self:
        if (self.revision_id is None) == (self.unit_id is None):
            raise ValueError("Facet requires exactly one revision or unit")
        return self


class IdentifierRow(Contract):
    namespace: NonEmptyStr
    normalized_value: NonEmptyStr
    original_value: NonEmptyStr
    unit_id: Identifier


class ExtractionBundle(Contract):
    workspace: WorkspaceRow
    collection: CollectionRow
    source: SourceRow
    artifacts: tuple[ArtifactRef, ...]
    revision: RevisionRow
    revision_artifacts: tuple[RevisionArtifactRow, ...]
    extraction: ExtractionRow
    units: tuple[EvidenceUnit, ...]
    locators: tuple[LocatorRow, ...]
    chunks: tuple[ChunkRow, ...] = ()
    assets: tuple[AssetRow, ...] = ()
    unit_assets: tuple[UnitAssetRow, ...] = ()
    proxies: tuple[AssetProxyRow, ...] = ()
    records: tuple[StructuredRecordRow, ...] = ()
    facets: tuple[FacetRow, ...] = ()
    identifiers: tuple[IdentifierRow, ...] = ()
    code_files: tuple[CodeFileDescriptor, ...] = ()


class RetainedRepresentationRows(Contract):
    """Only canonical rows required to revalidate text ownership after storage."""

    source: SourceRow
    artifacts: tuple[ArtifactRef, ...]
    revision: RevisionRow
    revision_artifacts: tuple[RevisionArtifactRow, ...]
    extraction: ExtractionRow
    units: tuple[EvidenceUnit, ...]
    locators: tuple[LocatorRow, ...]
    assets: tuple[AssetRow, ...]
    unit_assets: tuple[UnitAssetRow, ...]
    proxies: tuple[AssetProxyRow, ...]
    records: tuple[StructuredRecordRow, ...]
    identifiers: tuple[IdentifierRow, ...]
    code_files: tuple[CodeFileDescriptor, ...] = ()


class SliceFootprint(Contract):
    """What hydrating rows costs, measured in SQLite before any row is read.

    ``row_bytes`` estimates each row as 1024 bytes plus its payload columns.
    """

    units: NonNegativeInt = 0
    rows: NonNegativeInt = 0
    text_bytes: NonNegativeInt = 0
    row_bytes: NonNegativeInt = 0

    def plus(self, other: "SliceFootprint") -> "SliceFootprint":
        """The footprint of both."""
        return SliceFootprint(
            units=self.units + other.units,
            rows=self.rows + other.rows,
            text_bytes=self.text_bytes + other.text_bytes,
            row_bytes=self.row_bytes + other.row_bytes,
        )

    def require_within(self, limit: "SliceFootprint") -> None:
        """Refuse with ``request.limit_exceeded`` when any measure exceeds ``limit``."""
        if (
            self.units > limit.units
            or self.rows > limit.rows
            or self.text_bytes > limit.text_bytes
            or self.row_bytes > limit.row_bytes
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Representation reads exceed their resource limit.",
                reason="request.limit_exceeded",
            )


# The bounds of canonical representation preparation, per slice and per operation.
REPRESENTATION_LIMIT = SliceFootprint(
    units=20_000,
    rows=100_000,
    text_bytes=16 * 1024 * 1024,
    row_bytes=32 * 1024 * 1024,
)


class RepresentationSlice(Contract):
    """Some units of one extraction: their retained rows and persisted ownership."""

    rows: RetainedRepresentationRows
    prepared: PreparedExtraction
    footprint: SliceFootprint


class ReviewTarget(Contract):
    revision_id: Identifier | None = None
    unit_id: Identifier | None = None
    asset_id: Identifier | None = None

    @model_validator(mode="after")
    def one_subject(self) -> Self:
        if (
            sum(x is not None for x in (self.revision_id, self.unit_id, self.asset_id))
            != 1
        ):
            raise ValueError("Review requires exactly one revision, unit or asset")
        return self
