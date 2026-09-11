"""Exact source evidence and canonical result contracts."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .base import (
    CodepointRange,
    Contract,
    FiniteFloat,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .requests import BudgetContract, RequestScope

ReviewState = Literal["unreviewed", "needs_review", "accepted", "rejected"]
TextOrigin = Literal["source", "ocr", "derived"]


class Geometry(Contract):
    domain: Literal["pixels", "points", "normalized"]
    x0: FiniteFloat
    y0: FiniteFloat
    x1: FiniteFloat
    y1: FiniteFloat

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if min(self.x0, self.y0) < 0 or self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("Invalid geometry bounds")
        if self.domain == "normalized" and max(self.x1, self.y1) > 1:
            raise ValueError("Normalized coordinates must be within [0, 1]")
        return self


class TextLocator(CodepointRange):
    type: Literal["text"] = "text"
    artifact_ref: Identifier
    native_ref: NonEmptyStr


class DocumentLocator(CodepointRange):
    type: Literal["document"] = "document"
    artifact_ref: Identifier
    native_ref: NonEmptyStr
    physical_page: PositiveInt | None = None
    printed_page: str | None = None
    geometry: Geometry | None = None


class CodeLocator(CodepointRange):
    type: Literal["code"] = "code"
    artifact_ref: Identifier
    native_ref: NonEmptyStr
    relative_path: NonEmptyStr
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt

    @model_validator(mode="after")
    def code_bounds(self) -> Self:
        if self.byte_end < self.byte_start or self.line_end < self.line_start:
            raise ValueError("Code range end precedes start")
        return self


class ImageLocator(Contract):
    type: Literal["image"] = "image"
    artifact_ref: Identifier
    native_ref: NonEmptyStr
    physical_page: PositiveInt | None = None
    printed_page: str | None = None
    geometry: Geometry | None = None


type Locator = Annotated[
    TextLocator | DocumentLocator | CodeLocator | ImageLocator,
    Field(discriminator="type"),
]


class EvidenceUnit(Contract):
    unit_id: Identifier
    extraction_id: Identifier
    parent_id: Identifier | None
    ordinal: NonNegativeInt
    unit_type: NonEmptyStr
    native_ref: NonEmptyStr
    boundary_key: NonEmptyStr
    heading_path: tuple[str, ...] = ()
    text: str
    text_sha: Sha256
    text_origin: TextOrigin = "source"
    citable: bool = True
    review_state: ReviewState = "unreviewed"
    locators: tuple[Locator, ...]
    attributes: FrozenJsonMap = Field(default_factory=FrozenJsonMap)


class AnchorRange(CodepointRange):
    unit_id: Identifier
    origin: NonEmptyStr
    indexed_window_id: Identifier | None = None


class Citation(Contract):
    unit_id: Identifier | None = None
    asset_id: Identifier | None = None
    source_id: Identifier | None = None
    revision_id: Identifier | None = None
    source_ref: str | None = None
    physical_page: PositiveInt | None = None
    printed_page: str | None = None
    locators: tuple[Locator, ...] = ()


class Excerpt(CodepointRange):
    unit_id: Identifier
    text: str


class Omission(Contract):
    reason: NonEmptyStr
    unit_ids: tuple[Identifier, ...] = ()
    count: NonNegativeInt = 0


class StructuredRecord(Contract):
    record_id: Identifier
    unit_id: Identifier
    record_type: NonEmptyStr
    schema_id: Identifier
    value: FrozenJsonMap
    dependencies: frozenset[Identifier] = frozenset()


class Segment(Contract):
    segment_id: Identifier
    source_id: Identifier
    revision_id: Identifier
    extraction_id: Identifier
    kind: NonEmptyStr
    role: NonEmptyStr
    lane: NonEmptyStr
    excerpts: tuple[Excerpt, ...]
    text: str
    matched_unit_ids: tuple[Identifier, ...]
    context_only_unit_ids: tuple[Identifier, ...] = ()
    context_excerpts: tuple[Excerpt, ...] = ()
    anchors: tuple[AnchorRange, ...] = ()
    signals: tuple[NonEmptyStr, ...] = ()
    citations: tuple[Citation, ...] = ()
    complete: bool = True
    truncated: bool = False
    omissions: tuple[Omission, ...] = ()
    structured_records: tuple[StructuredRecord, ...] = ()


class LabelledProxy(Contract):
    proxy_id: Identifier
    label: NonEmptyStr
    text: str
    origin: TextOrigin
    dependencies: frozenset[Identifier]
    review_state: ReviewState


class VisualEvidence(Contract):
    asset_id: Identifier
    source_id: Identifier
    revision_id: Identifier
    asset_ref: Identifier
    locator: ImageLocator
    caption: str | None = None
    proxies: tuple[LabelledProxy, ...] = ()
    selection_reasons: tuple[NonEmptyStr, ...] = ()
    citations: tuple[Citation, ...] = ()


class RelationPath(Contract):
    unit_ids: tuple[Identifier, ...]
    assertion_ids: tuple[Identifier, ...]
    predicates: tuple[NonEmptyStr, ...]
    support_unit_ids: frozenset[Identifier] = frozenset()
    provenance: FrozenJsonMap = Field(default_factory=FrozenJsonMap)


class Coverage(Contract):
    status: Literal["complete", "partial", "unavailable"]
    completed_channels: tuple[NonEmptyStr, ...] = ()
    unavailable_channels: tuple[NonEmptyStr, ...] = ()
    warnings: tuple[str, ...] = ()


class CoverageGap(Contract):
    code: NonEmptyStr
    message: str
    channel: NonEmptyStr | None = None


class DecisionRequirement(Contract):
    requirement_id: Identifier
    reason: str
    unit_ids: tuple[Identifier, ...] = ()
    assertion_ids: tuple[Identifier, ...] = ()


class RelationChecks(Contract):
    status: Literal["complete", "partial", "not_requested"] = "not_requested"
    predicates: tuple[NonEmptyStr, ...] = ()
    assertion_ids: tuple[Identifier, ...] = ()
    decisions: tuple[DecisionRequirement, ...] = ()
    truncated: bool = False


class QueryResult(Contract):
    schema_version: Literal["1"] = "1"
    run_id: Identifier
    snapshot_id: Identifier
    applied_scope: RequestScope
    segments: tuple[Segment, ...] = ()
    visuals: tuple[VisualEvidence, ...] = ()
    relations: tuple[RelationPath, ...] = ()
    citations: tuple[Citation, ...] = ()
    coverage: Coverage
    checks: RelationChecks = Field(default_factory=RelationChecks)
    coverage_gaps: tuple[CoverageGap, ...] = ()
    decision_requirements: tuple[DecisionRequirement, ...] = ()
    warnings: tuple[str, ...] = ()
    budget_contract: BudgetContract = Field(default_factory=BudgetContract)
    trace: FrozenJsonMap | None = None
