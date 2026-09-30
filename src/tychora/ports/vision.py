"""Typed ingestion-only vision boundary."""

from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import Field, model_validator

from tychora.contracts.acquisition import CaptureManifest
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import (
    Contract,
    FrozenJsonMap,
    NonEmptyStr,
    PositiveInt,
    Sha256,
)
from tychora.contracts.composition import (
    CandidateContext,
    SourceContextSelection,
    vision_context_identity,
)
from tychora.contracts.corpus import RevisionRow, SourceRow
from tychora.contracts.documents import DocumentRegistration, image_pixel_identity
from tychora.contracts.evidence import Geometry
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import JsonValue, canonical_bytes, digest, source_id
from tychora.contracts.requests import SourceInput, source_registration_descriptor
from tychora.ports.execution import ExecutionContext

MAX_VISION_REGION_BYTES = 20 * 1024 * 1024
MAX_VISION_CONTEXT_BYTES = 1024 * 1024


class RegionInput(Contract):
    source_native_ref: NonEmptyStr
    image: Annotated[bytes, Field(max_length=MAX_VISION_REGION_BYTES)]
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
    allowed_candidate_target_ids: frozenset[NonEmptyStr] = frozenset()
    dependency_ids: Annotated[frozenset[NonEmptyStr], Field(min_length=1)]
    candidate_context: Annotated[
        tuple[CandidateContext, ...], Field(max_length=100)
    ] = ()
    source_context: Annotated[
        tuple[SourceContextSelection, ...], Field(max_length=100)
    ] = ()

    @model_validator(mode="after")
    def exact_region(self) -> Self:
        from io import BytesIO

        from PIL import Image, ImageOps, UnidentifiedImageError

        context_ids = [item.target_id for item in self.candidate_context]
        source_context_ids = [item.target_id for item in self.source_context]
        expected_region_transform = "exif-transpose-rgb-v1:crop:" + ",".join(
            str(int(value))
            for value in (
                self.geometry.x0,
                self.geometry.y0,
                self.geometry.x1,
                self.geometry.y1,
            )
        )
        invalid = (
            not self.image
            or sha256(self.image).hexdigest() != self.image_sha256
            or self.image_ref.sha256 != self.image_sha256
            or self.image_ref.storage_key != self.image_sha256
            or self.image_ref.size_bytes != len(self.image)
            or self.image_ref.media_type != self.media_type
            or self.geometry.domain != "pixels"
            or self.geometry.x1 > self.original_width
            or self.geometry.y1 > self.original_height
            or self.geometry.x1 - self.geometry.x0 != self.width
            or self.geometry.y1 - self.geometry.y0 != self.height
            or any(
                int(value) != value
                for value in (
                    self.geometry.x0,
                    self.geometry.y0,
                    self.geometry.x1,
                    self.geometry.y1,
                )
            )
            or self.region_pixel_transform != expected_region_transform
            or len(context_ids) != len(set(context_ids))
            or len(source_context_ids) != len(set(source_context_ids))
            or self.source_context
            and tuple(item.candidate_context() for item in self.source_context)
            != self.candidate_context
            or self.allowed_candidate_target_ids != frozenset(context_ids)
            or not frozenset(context_ids).issubset(self.dependency_ids)
            or self.context_sha256 != vision_context_identity(self.candidate_context)
            or len(
                canonical_bytes(
                    {
                        "candidate_context": [
                            item.model_dump(mode="json")
                            for item in self.candidate_context
                        ],
                        "source_context": [
                            item.model_dump(mode="json") for item in self.source_context
                        ],
                    }
                )
            )
            > MAX_VISION_CONTEXT_BYTES
        )
        try:
            with Image.open(BytesIO(self.image)) as encoded:
                expected = "PNG" if self.media_type == "image/png" else "JPEG"
                if (
                    encoded.format != expected
                    or encoded.width * encoded.height > 40_000_000
                    or encoded.size != (self.width, self.height)
                ):
                    invalid = True
                else:
                    decoded = ImageOps.exif_transpose(encoded).convert("RGB")
                    decoded.load()
                    invalid = invalid or (
                        decoded.size != (self.width, self.height)
                        or self.color_mode != "RGB"
                        or image_pixel_identity(
                            decoded.tobytes(),
                            decoded.width,
                            decoded.height,
                            "RGB",
                            self.region_pixel_transform,
                        )
                        != self.region_pixel_sha256
                    )
        except (
            Image.DecompressionBombError,
            OSError,
            ValueError,
            UnidentifiedImageError,
        ):
            invalid = True
        if invalid:
            raise ValueError("Vision region differs from retained pixel facts")
        return self


class CapturedSourceRegistration(Contract):
    """Immutable metadata identity required before a first-source upload."""

    source: SourceRow
    kind_id: NonEmptyStr

    @model_validator(mode="after")
    def exact_registration(self) -> Self:
        legacy = FrozenJsonMap(
            {
                "schema_version": 1,
                "kind_id": self.kind_id,
            }
        )
        if self.source.registration_json == legacy:
            return self
        descriptor = dict(self.source.registration_json)
        try:
            variant = descriptor.get("type")
            if variant == "text":
                descriptor["text"] = ""
            elif variant == "bytes":
                descriptor["data"] = ""
            parsed = SourceInput.from_mapping(descriptor)
            exact = source_registration_descriptor(parsed)
            valid = (
                exact == self.source.registration_json
                and parsed.root.kind == self.kind_id
                and str(parsed.root.collection_id) == self.source.collection_id
                and parsed.root.source_key == self.source.source_key
                and self.source.source_type
                == ("repository" if variant == "repository" else "document")
                and self.source.source_id
                == source_id(UUID(self.source.collection_id), self.source.source_key)
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise ValueError("Captured source registration metadata differs")
        return self


class CapturedOriginalRegionProof(Contract):
    type: Literal["captured_original"] = "captured_original"
    capture: CaptureManifest

    @model_validator(mode="after")
    def standalone_capture(self) -> Self:
        fmt = captured_image_format(self.capture)
        if (
            self.capture.source.get("type")
            in (
                "folder",
                "repository",
            )
            or fmt is None
        ):
            raise ValueError("Captured original proof requires one PNG/JPEG source")
        return self


def captured_image_format(capture: CaptureManifest) -> Literal["png", "jpeg"] | None:
    """Check declared or source-suffix format against any declared image MIME."""
    source = capture.source
    declared = source.get("format")
    if declared is not None and not isinstance(declared, str):
        return None
    suffix_source = (
        source.get("path", source.get("source_key", ""))
        if source.get("type") in ("file", "url")
        else ""
    )
    suffix = Path(str(suffix_source)).suffix.lower()
    inferred = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(suffix)
    fmt = {"jpg": "jpeg"}.get(declared, declared) if declared is not None else inferred
    media = source.get("media_type")
    expected = "image/png" if fmt == "png" else "image/jpeg" if fmt == "jpeg" else None
    if (
        expected is None
        or declared is not None
        and inferred is not None
        and inferred != fmt
        or media is not None
        and media != expected
        or capture.original_ref.media_type not in ("application/octet-stream", expected)
    ):
        return None
    return cast(Literal["png", "jpeg"], fmt)


class RetainedDocumentRegionProof(Contract):
    type: Literal["retained_document"] = "retained_document"
    capture: CaptureManifest
    extraction: ExtractionArtifact
    asset_native_ref: NonEmptyStr

    @model_validator(mode="after")
    def retained_asset(self) -> Self:
        registration = self.extraction.registration
        assets = [
            asset
            for asset in self.extraction.assets
            if asset.native_ref == self.asset_native_ref
        ]
        if (
            not isinstance(registration, DocumentRegistration)
            or registration.format not in ("pdf", "docx")
            or registration.capture != self.capture
            or len(assets) != 1
        ):
            raise ValueError("Document region proof differs from retained extraction")
        return self

    @property
    def asset_ref(self) -> ArtifactRef:
        return next(
            asset.artifact_ref
            for asset in self.extraction.assets
            if asset.native_ref == self.asset_native_ref
        )


type CapturedRegionProof = Annotated[
    CapturedOriginalRegionProof | RetainedDocumentRegionProof,
    Field(discriminator="type"),
]


class CapturedRegionAuthorization(Contract):
    """Candidate revision and retained region authorized before extraction commit."""

    registration: CapturedSourceRegistration
    revision: RevisionRow
    proof: CapturedRegionProof
    region: RegionInput

    @model_validator(mode="after")
    def exact_candidate(self) -> Self:
        source = self.registration.source
        capture = self.proof.capture
        capture_source = capture.source
        original_sha = (
            capture.original_sha
            if isinstance(self.proof, CapturedOriginalRegionProof)
            else self.proof.asset_ref.sha256
        )
        if (
            self.revision.source_id != source.source_id
            or self.revision.kind_id != self.registration.kind_id
            or self.revision.original_sha != capture.original_sha
            or capture.source_key != source.source_key
            or capture_source.get("source_key") != source.source_key
            or capture_source.get("collection_id") != source.collection_id
            or capture_source.get("kind") != self.registration.kind_id
            or digest("metadata-v1", capture_source.get("metadata"))
            != self.revision.metadata_sha
            or isinstance(self.proof, RetainedDocumentRegionProof)
            and self.proof.extraction.registration.revision != self.revision
            or self.region.original_image_sha256 != original_sha
            or isinstance(self.proof, CapturedOriginalRegionProof)
            and self.region.source_native_ref
            not in ("#/original", "image-original:" + source.source_key)
            or isinstance(self.proof, RetainedDocumentRegionProof)
            and self.region.source_native_ref != self.proof.asset_native_ref
            or self.region.dependency_ids
            != frozenset(
                {
                    source.source_id,
                    *(item.target_id for item in self.region.candidate_context),
                }
            )
            or not self.region.allowed_candidate_target_ids.issubset(
                frozenset(item.target_id for item in self.region.candidate_context)
            )
            or self.region.candidate_context
            and not isinstance(self.proof, RetainedDocumentRegionProof)
            or tuple(item.candidate_context() for item in self.region.source_context)
            != self.region.candidate_context
        ):
            raise ValueError("Captured region differs from registered candidate source")
        return self

    @property
    def capture(self) -> CaptureManifest:
        return self.proof.capture


def verify_retained_source_context(
    extraction: ExtractionArtifact,
    context: tuple[SourceContextSelection, ...],
) -> None:
    """Verify exact selector facts against the retained raw SDK document."""
    from hashlib import sha256

    from tychora.contracts.documents import DocumentRegistration, DocumentUnit

    if not isinstance(extraction.registration, DocumentRegistration):
        raise TypeError("Source context requires a retained rich document")
    units = {
        item.native_ref: item
        for item in extraction.units
        if isinstance(item, DocumentUnit)
    }
    for selected in context:
        selector = selected.selector
        item = units.get(selector.native_ref)
        if (
            selected.captured_original_sha256 != extraction.original_ref.sha256
            or item is None
            or selector.field is None
            or selector.source_start is None
            or selector.source_end is None
        ):
            raise ValueError("Source context differs from retained document facts")
        if selector.field == "table_cell":
            data = item.raw_item.get("data")
            if not isinstance(data, Mapping):
                raise ValueError("Source context differs from retained document facts")
            cells = data.get("table_cells")
            if not isinstance(cells, tuple):
                raise ValueError("Source context differs from retained document facts")
            if selector.cell_index is None or selector.cell_index >= len(cells):
                raise ValueError("Source context differs from retained document facts")
            cell = cells[selector.cell_index]
            if not isinstance(cell, Mapping):
                raise ValueError("Source context differs from retained document facts")
            source_text = cell.get("text")
        else:
            source_text = item.raw_item.get(selector.field)
        if (
            not isinstance(source_text, str)
            or selector.source_start == selector.source_end
            or selector.source_end > len(source_text)
            or sha256(source_text.encode()).hexdigest() != selected.source_text_sha256
            or source_text[selector.source_start : selector.source_end]
            != selected.literal_text
        ):
            raise ValueError("Source context differs from retained document facts")


class DerivedObservation(Contract):
    cache_key: Sha256
    task: NonEmptyStr
    image_sha256: Sha256
    original_image_sha256: Sha256
    original_pixel_sha256: Sha256
    region_pixel_sha256: Sha256
    context_sha256: Sha256
    target_id: str | None = None
    literal_transcription: str | None = None
    interpretation: FrozenJsonMap | None = None
    uncertainties: Annotated[tuple[NonEmptyStr, ...], Field(max_length=100)] = ()
    model: NonEmptyStr
    prompt_schema_version: NonEmptyStr
    task_schema_sha256: Sha256
    prompt_renderer_identity: Sha256 | None = None
    prompt_identity: Sha256
    derivation: ArtifactRef | None = None
    review_state: Literal["needs_review"] = "needs_review"

    @model_validator(mode="after")
    def exact_cache_identity(self) -> Self:
        if self.prompt_identity != digest(
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
        ) or self.cache_key != vision_cache_key(
            self.image_sha256,
            self.task,
            self.model,
            self.prompt_identity,
            self.context_sha256,
        ):
            raise ValueError("Vision observation cache identity differs")
        return self


class VisionProvider(Protocol):
    def interpret(
        self, region: RegionInput, task_schema: JsonValue, ctx: ExecutionContext
    ) -> DerivedObservation: ...


def selective_interpret(
    region: RegionInput,
    task_schema: JsonValue,
    local_ocr: VisionProvider,
    remote_call: Callable[[], DerivedObservation],
    ctx: ExecutionContext,
) -> DerivedObservation:
    """Use local OCR only for the selected OCR task; other tasks remain explicit."""
    ctx.checkpoint()
    if region.task == "ocr":
        observation = local_ocr.interpret(region, task_schema, ctx)
        if (
            observation.task != region.task
            or observation.image_sha256 != region.image_sha256
            or observation.original_image_sha256 != region.original_image_sha256
            or observation.original_pixel_sha256 != region.original_pixel_sha256
            or observation.region_pixel_sha256 != region.region_pixel_sha256
            or observation.context_sha256 != region.context_sha256
        ):
            raise ValueError(
                "Local OCR observation differs from selected source region"
            )
        if observation.literal_transcription:
            return observation
    ctx.checkpoint()
    return remote_call()


def pixel_identity(
    pixels: bytes, width: int, height: int, color_mode: str, transform: str
) -> str:
    return image_pixel_identity(pixels, width, height, color_mode, transform)


def verify_region_source(
    region: RegionInput,
    original: bytes,
    original_ref: ArtifactRef,
    source_native_ref: str,
) -> None:
    """Verify retained original pixels and the exact decoded crop before use."""
    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    invalid = (
        region.source_native_ref != source_native_ref
        or original_ref.sha256 != region.original_image_sha256
        or original_ref.storage_key != original_ref.sha256
        or original_ref.size_bytes != len(original)
        or sha256(original).hexdigest() != original_ref.sha256
        or original_ref.media_type not in ("image/png", "image/jpeg")
        and not (
            original_ref.media_type == "application/octet-stream"
            and source_native_ref.startswith("image-original:")
        )
    )
    try:
        with Image.open(BytesIO(original)) as encoded:
            expected = (
                "PNG"
                if original_ref.media_type == "image/png"
                else "JPEG"
                if original_ref.media_type == "image/jpeg"
                else encoded.format
            )
            if (
                expected not in ("PNG", "JPEG")
                or encoded.format != expected
                or encoded.width * encoded.height > 40_000_000
                or encoded.size != (region.original_width, region.original_height)
            ):
                invalid = True
            else:
                decoded = ImageOps.exif_transpose(encoded).convert("RGB")
                decoded.load()
                crop = decoded.crop(
                    (
                        int(region.geometry.x0),
                        int(region.geometry.y0),
                        int(region.geometry.x1),
                        int(region.geometry.y1),
                    )
                )
                with Image.open(BytesIO(region.image)) as encoded_region:
                    selected = ImageOps.exif_transpose(encoded_region).convert("RGB")
                    selected.load()
                invalid = invalid or (
                    decoded.size != (region.original_width, region.original_height)
                    or image_pixel_identity(
                        decoded.tobytes(),
                        decoded.width,
                        decoded.height,
                        "RGB",
                        region.original_pixel_transform,
                    )
                    != region.original_pixel_sha256
                    or crop.size != selected.size
                    or crop.tobytes() != selected.tobytes()
                )
    except (Image.DecompressionBombError, OSError, ValueError, UnidentifiedImageError):
        invalid = True
    if invalid:
        raise ValueError("Vision region differs from verified retained original pixels")


def vision_cache_key(
    image_sha: str,
    task: str,
    model: str,
    prompt_schema_version: str,
    context_sha: str,
) -> str:
    return digest(
        "vision-cache-v1",
        image_sha,
        task,
        model,
        prompt_schema_version,
        context_sha,
    )


def vision_prompt_identity(
    prompt_schema_version: str,
    task_schema: JsonValue,
    prompt_renderer_identity: str | None = None,
) -> str:
    """Bind versioned prompt behavior to the exact bounded task schema."""
    task_schema_sha = digest("vision-task-schema-v1", task_schema)
    if prompt_renderer_identity is None:
        return digest(
            "vision-prompt-semantics-v1", prompt_schema_version, task_schema_sha
        )
    return digest(
        "vision-prompt-semantics-v2",
        prompt_schema_version,
        task_schema_sha,
        prompt_renderer_identity,
    )
