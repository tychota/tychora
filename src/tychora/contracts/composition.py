"""Acyclic source-selected plans for complete document normalization."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_serializer, model_validator

from .artifacts import ArtifactRef
from .base import (
    Contract,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .evidence import Geometry
from .identity import JsonValue, canonical_bytes, digest


class CandidateContext(Contract):
    """Bounded exact interval from an authorized canonical source unit."""

    target_id: NonEmptyStr
    source_text_sha256: Sha256
    char_start: NonNegativeInt
    char_end: PositiveInt
    literal_text: Annotated[str, Field(min_length=1, max_length=65_536)]

    @model_validator(mode="after")
    def exact_interval(self) -> Self:
        if self.char_end - self.char_start != len(self.literal_text):
            raise ValueError("Candidate context interval differs from literal text")
        return self


def vision_context_identity(context: tuple[CandidateContext, ...]) -> str:
    return digest(
        "vision-context-v2", [item.model_dump(mode="json") for item in context]
    )


class PlannedRegion(Contract):
    """Retained region facts without embedding bounded image bytes in the plan."""

    source_native_ref: NonEmptyStr
    image_ref: ArtifactRef
    media_type: Literal["image/png", "image/jpeg"]
    image_sha256: Sha256
    original_image_sha256: Sha256
    original_pixel_sha256: Sha256
    region_pixel_sha256: Sha256
    original_width: PositiveInt
    original_height: PositiveInt
    width: PositiveInt
    height: PositiveInt
    color_mode: Literal["RGB"]
    original_pixel_transform: Literal["exif-transpose-rgb-v1"]
    region_pixel_transform: NonEmptyStr
    geometry: Geometry
    context_sha256: Sha256
    task: NonEmptyStr
    model: NonEmptyStr
    prompt_schema_version: NonEmptyStr
    task_schema_sha256: Sha256
    prompt_renderer_identity: Sha256 | None = None
    prompt_identity: Sha256
    allowed_candidate_target_ids: frozenset[NonEmptyStr] = frozenset()
    dependency_ids: frozenset[NonEmptyStr] = frozenset()
    candidate_context: Annotated[
        tuple[CandidateContext, ...], Field(max_length=100)
    ] = ()

    @model_validator(mode="after")
    def exact_retained_region(self) -> Self:
        context_ids = [item.target_id for item in self.candidate_context]
        if (
            self.image_ref.sha256 != self.image_sha256
            or self.image_ref.storage_key != self.image_sha256
            or self.image_ref.media_type != self.media_type
            or len(context_ids) != len(set(context_ids))
            or self.allowed_candidate_target_ids != frozenset(context_ids)
            or not frozenset(context_ids).issubset(self.dependency_ids)
            or self.context_sha256 != vision_context_identity(self.candidate_context)
            or self.prompt_identity
            != digest(
                *(
                    (
                        "vision-prompt-semantics-v2",
                        self.prompt_schema_version,
                        self.task_schema_sha256,
                        self.prompt_renderer_identity,
                    )
                    if self.prompt_renderer_identity is not None
                    else (
                        "vision-prompt-semantics-v1",
                        self.prompt_schema_version,
                        self.task_schema_sha256,
                    )
                )
            )
            or len(
                canonical_bytes(
                    [item.model_dump(mode="json") for item in self.candidate_context]
                )
            )
            > 1024 * 1024
        ):
            raise ValueError("Planned region differs from retained input facts")
        return self


class SourceSelector(Contract):
    native_ref: NonEmptyStr
    field: Literal["text", "marker", "table_cell"] | None = None
    cell_index: NonNegativeInt | None = None
    source_start: NonNegativeInt | None = None
    source_end: NonNegativeInt | None = None

    @model_validator(mode="after")
    def exact_input_domain(self) -> Self:
        if (
            self.field is None
            and any(
                value is not None
                for value in (self.cell_index, self.source_start, self.source_end)
            )
            or self.field is not None
            and (self.source_start is None or self.source_end is None)
            or (self.field == "table_cell") != (self.cell_index is not None)
            or self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
            or not self.native_ref.startswith("#/")
        ):
            raise ValueError(
                "Composition dependencies require original SDK field coordinates"
            )
        return self


class SourceContextSelection(Contract):
    """Exact retained raw SDK field interval used before canonical IDs exist."""

    captured_original_sha256: Sha256
    selector: SourceSelector
    source_text_sha256: Sha256
    literal_text: Annotated[str, Field(min_length=1, max_length=65_536)]
    target_id: NonEmptyStr

    @classmethod
    def bind(
        cls,
        captured_original_sha256: str,
        selector: SourceSelector,
        source_text: str,
    ) -> "SourceContextSelection":
        from hashlib import sha256

        if (
            selector.field is None
            or selector.source_start is None
            or selector.source_end is None
            or selector.source_start == selector.source_end
            or selector.source_end > len(source_text)
        ):
            raise ValueError("Source context requires one nonempty raw field interval")
        source_text_sha = sha256(source_text.encode()).hexdigest()
        literal = source_text[selector.source_start : selector.source_end]
        return cls(
            captured_original_sha256=captured_original_sha256,
            selector=selector,
            source_text_sha256=source_text_sha,
            literal_text=literal,
            target_id=digest(
                "vision-source-context-v1",
                captured_original_sha256,
                selector.model_dump(mode="json"),
                source_text_sha,
                literal,
            ),
        )

    def candidate_context(self) -> CandidateContext:
        assert self.selector.source_start is not None
        assert self.selector.source_end is not None
        return CandidateContext(
            target_id=self.target_id,
            source_text_sha256=self.source_text_sha256,
            char_start=self.selector.source_start,
            char_end=self.selector.source_end,
            literal_text=self.literal_text,
        )

    @model_validator(mode="after")
    def exact_source_context(self) -> Self:
        selector = self.selector
        if (
            selector.field is None
            or selector.source_start is None
            or selector.source_end is None
            or selector.source_start == selector.source_end
            or selector.source_end - selector.source_start != len(self.literal_text)
            or self.target_id
            != digest(
                "vision-source-context-v1",
                self.captured_original_sha256,
                selector.model_dump(mode="json"),
                self.source_text_sha256,
                self.literal_text,
            )
        ):
            raise ValueError("Selected source context differs from retained raw facts")
        return self


def source_context_identity(context: tuple[SourceContextSelection, ...]) -> str:
    """Bind the complete pre-ID selector-to-context mapping."""
    return digest(
        "vision-source-context-set-v1",
        [item.model_dump(mode="json") for item in context],
    )


class PlannedProxy(Contract):
    asset_native_ref: NonEmptyStr
    kind: Literal["caption_copy", "ocr", "description", "structured_values"]
    text: str
    text_sha: Sha256
    dependencies: tuple[SourceSelector, ...]
    derivation: ArtifactRef
    region: PlannedRegion | None = None
    source_context: tuple[SourceContextSelection, ...] = ()

    @model_validator(mode="after")
    def exact_text_input(self) -> Self:
        from hashlib import sha256

        if (
            not self.dependencies
            and self.region is None
            or sha256(self.text.encode()).hexdigest() != self.text_sha
            or self.region is None
            and self.source_context
            or self.region is not None
            and tuple(item.candidate_context() for item in self.source_context)
            != self.region.candidate_context
        ):
            raise ValueError(
                "Planned proxy must bind literal input text and original selectors"
            )
        if self.derivation.storage_key != self.derivation.sha256:
            raise ValueError("Planned derivation requires immutable content identity")
        return self


class VisionCapabilityGap(Contract):
    """Sanitized selected intent whose optional observation was unavailable."""

    selection_sha256: Sha256
    reason: Literal["provider_unavailable", "provider_failure"]


class PdfCoverage(Contract):
    page_count: Annotated[int, Field(ge=1, le=1000)]
    native_pages: tuple[PositiveInt, ...]
    ocr_pages: tuple[PositiveInt, ...]
    blank_pages: tuple[PositiveInt, ...]
    no_text_detected_pages: tuple[PositiveInt, ...]

    @model_validator(mode="after")
    def complete_partition(self):
        pages = (*self.native_pages, *self.ocr_pages, *self.blank_pages)
        if (
            sorted(pages) != list(range(1, self.page_count + 1))
            or any(
                tuple(sorted(set(group))) != group
                for group in (
                    self.native_pages,
                    self.ocr_pages,
                    self.blank_pages,
                    self.no_text_detected_pages,
                )
            )
            or not set(self.no_text_detected_pages) <= set(self.ocr_pages)
        ):
            raise ValueError("Invalid PDF coverage partition")
        return self


class VisionCapabilityDiagnostics(Contract):
    type: Literal["document_vision_capability"] = "document_vision_capability"
    schema_version: Literal["1"] = "1"
    gaps: Annotated[tuple[VisionCapabilityGap, ...], Field(max_length=100)]
    pdf_coverage: PdfCoverage | None = None

    @model_validator(mode="after")
    def meaningful(self):
        if not self.gaps and self.pdf_coverage is None:
            raise ValueError("Empty processing diagnostic")
        return self

    @model_serializer(mode="wrap")
    def legacy_diagnostic(self, handler):
        data = handler(self)
        if self.pdf_coverage is None:
            data.pop("pdf_coverage", None)
        return data


class DocumentCompositionPlan(Contract):
    base_normalizer_fingerprint: Sha256
    input_strategy: Literal["pdf-streamed-images@1"] | None = None
    table_strategy: Literal["table-fragments@1"] = "table-fragments@1"
    proxy_strategy: Literal["asset-proxies@1"] = "asset-proxies@1"
    infer_table_continuations: bool = True
    caption_copies: bool = True
    proxies: tuple[PlannedProxy, ...] = ()
    pixel_regions: tuple[PlannedRegion, ...] = ()
    vision_gaps: Annotated[tuple[VisionCapabilityGap, ...], Field(max_length=100)] = ()

    @model_serializer(mode="wrap")
    def legacy_input_strategy(self, handler):
        data = handler(self)
        if self.input_strategy is None:
            data.pop("input_strategy", None)
        return data

    def semantic_data(self) -> JsonValue:
        """Serialize the plan unchanged when no region enrichment exists, so its digest stays stable."""
        data = self.model_dump(mode="json")
        for proxy in data["proxies"]:
            if proxy.get("region") is None:
                proxy.pop("region", None)
            if not proxy.get("source_context"):
                proxy.pop("source_context", None)
        if not data["pixel_regions"]:
            data.pop("pixel_regions")
        if not data["vision_gaps"]:
            data.pop("vision_gaps")
        return data

    @model_validator(mode="after")
    def bounded_unique_inputs(self) -> Self:
        if (
            len(self.proxies) > 10_000
            or len(self.pixel_regions) > 1000
            or len({region.source_native_ref for region in self.pixel_regions})
            != len(self.pixel_regions)
            or len({gap.selection_sha256 for gap in self.vision_gaps})
            != len(self.vision_gaps)
            or len(canonical_bytes(self.semantic_data())) > 16 * 1024 * 1024
        ):
            raise ValueError("Document composition plan exceeds input capacity")
        keys = [
            canonical_bytes(
                {
                    key: value
                    for key, value in proxy.model_dump(mode="json").items()
                    if (key != "region" or value is not None)
                    and (key != "source_context" or value)
                }
            )
            for proxy in self.proxies
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate planned proxy input")
        return self
