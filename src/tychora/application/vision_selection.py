"""Resolve public vision intent against retained source and parser facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from io import BytesIO
from typing import TYPE_CHECKING, NoReturn, cast

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.composition import (
    SourceContextSelection,
    SourceSelector,
    source_context_identity,
)
from tychora.contracts.documents import DocumentRegistration, DocumentUnit
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Geometry
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import VisionSelection
from tychora.ports.vision import (
    MAX_VISION_REGION_BYTES,
    CapturedOriginalRegionProof,
    DerivedObservation,
    RegionInput,
    RetainedDocumentRegionProof,
    pixel_identity,
    vision_context_identity,
)

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner


def _invalid() -> NoReturn:
    raise TychoraError(
        "INVALID_REQUEST", "Vision selection differs from retained source facts."
    )


def _source_context(
    parsed: ExtractionArtifact, selected: VisionSelection
) -> tuple[SourceContextSelection, ...]:
    context = []
    units = {
        item.native_ref: item for item in parsed.units if isinstance(item, DocumentUnit)
    }
    for item in selected.context:
        unit = units.get(item.native_ref)
        if unit is None:
            _invalid()
        selector = SourceSelector.model_validate(item.model_dump(mode="json"))
        if selector.field == "table_cell":
            data = unit.raw_item.get("data")
            cells = data.get("table_cells") if isinstance(data, Mapping) else None
            if (
                not isinstance(cells, tuple)
                or selector.cell_index is None
                or selector.cell_index >= len(cells)
            ):
                _invalid()
            cell = cells[selector.cell_index]
            source_text = cell.get("text") if isinstance(cell, Mapping) else None
        else:
            source_text = unit.raw_item.get(cast(str, selector.field))
        if not isinstance(source_text, str):
            _invalid()
        try:
            context.append(
                SourceContextSelection.bind(
                    parsed.original_ref.sha256, selector, source_text
                )
            )
        except ValueError:
            _invalid()
    if len({item.target_id for item in context}) != len(context):
        _invalid()
    return tuple(context)


def _verified_selected_extraction(
    owner: WorkspaceOwner, parsed: ExtractionArtifact, selected_sha: str
) -> bool:
    """Match an inspected retained parser root to the current original facts."""
    from tychora.ingestion.documents import replay_document

    rows = owner.db.execute(
        "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
        "FROM extractions e JOIN artifacts a ON a.artifact_sha=e.document_artifact_sha "
        "WHERE e.revision_id=? ORDER BY e.extraction_id LIMIT 1001",
        (parsed.registration.revision.revision_id,),
    ).fetchall()
    if len(rows) > 1000:
        _invalid()
    for row in rows:
        ref = ArtifactRef(
            sha256=row[0], size_bytes=row[1], media_type=row[2], storage_key=row[3]
        )
        if ref.size_bytes > 16 * 1024 * 1024:
            continue
        with owner.artifacts.open_verified(ref) as stream:
            root = json.load(stream)
        for link in root.get("required_manifests", []):
            if link.get("type") != "extraction":
                continue
            raw_ref = ArtifactRef.model_validate(link["artifact"])
            if raw_ref.sha256 != selected_sha or raw_ref.size_bytes > 16 * 1024 * 1024:
                continue
            with owner.artifacts.open_verified(raw_ref) as stream:
                raw = stream.read(raw_ref.size_bytes + 1)
            doc_ref = ArtifactRef.model_validate(
                json.loads(raw)["data"]["docling_json_ref"]
            )
            if doc_ref.size_bytes > 16 * 1024 * 1024:
                _invalid()
            with owner.artifacts.open_verified(doc_ref) as stream:
                document = stream.read(doc_ref.size_bytes + 1)
            old = replay_document(raw, document, raw_ref)
            return (
                old.original_ref.sha256 == parsed.original_ref.sha256
                and old.parser_fingerprint == parsed.parser_fingerprint
                and old.normalizer_fingerprint == parsed.normalizer_fingerprint
                and old.document_json == parsed.document_json
                and old.assets == parsed.assets
                and old.original_image == parsed.original_image
            )
    return False


def derive_selected_region(
    owner: WorkspaceOwner, parsed: ExtractionArtifact, selected: VisionSelection
) -> tuple[RegionInput, CapturedOriginalRegionProof | RetainedDocumentRegionProof]:
    """Create one trusted crop and context without taking proof data from a caller."""
    from PIL import Image, ImageOps

    registration = parsed.registration
    if (
        not isinstance(registration, DocumentRegistration)
        or selected.captured_original_sha256 != parsed.original_ref.sha256
    ):
        _invalid()
    if (
        selected.retained_extraction_sha256 is None
        and owner.db.execute(
            "SELECT 1 FROM extractions WHERE revision_id=? LIMIT 1",
            (registration.revision.revision_id,),
        ).fetchone()
        is not None
    ):
        _invalid()
    if (
        selected.retained_extraction_sha256 is not None
        and selected.retained_extraction_sha256 != parsed.raw_document_ref.sha256
        and not _verified_selected_extraction(
            owner, parsed, selected.retained_extraction_sha256
        )
    ):
        _invalid()
    if registration.format in ("png", "jpeg"):
        original = parsed.original_image
        if original is None or selected.source_native_ref != original.native_ref:
            _invalid()
        image_ref = original.artifact_ref
        proof: CapturedOriginalRegionProof | RetainedDocumentRegionProof = (
            CapturedOriginalRegionProof(capture=registration.capture)
        )
    else:
        matches = [
            asset
            for asset in parsed.assets
            if asset.native_ref == selected.source_native_ref
            and asset.asset_type in ("page", "picture")
        ]
        if len(matches) != 1:
            _invalid()
        image_ref = matches[0].artifact_ref
        proof = RetainedDocumentRegionProof(
            capture=registration.capture,
            extraction=parsed,
            asset_native_ref=selected.source_native_ref,
        )
    if image_ref.media_type not in ("image/png", "image/jpeg") and not (
        registration.format in ("png", "jpeg")
        and image_ref.media_type == "application/octet-stream"
    ):
        _invalid()
    box = (
        selected.geometry.x0,
        selected.geometry.y0,
        selected.geometry.x1,
        selected.geometry.y1,
    )
    with (
        owner.artifacts.open_verified(image_ref) as stream,
        Image.open(stream) as encoded,
    ):
        if (
            encoded.format != ("PNG" if registration.format == "png" else "JPEG")
            and registration.format in ("png", "jpeg")
            or (encoded.width * encoded.height > 40_000_000)
        ):
            _invalid()
        pixels = ImageOps.exif_transpose(encoded).convert("RGB")
        pixels.load()
    if box[2] > pixels.width or box[3] > pixels.height:
        _invalid()
    crop = pixels.crop(box)
    output = BytesIO()
    crop.save(output, format="PNG")
    payload = output.getvalue()
    if len(payload) > MAX_VISION_REGION_BYTES:
        _invalid()
    retained = owner.artifacts.put_stream(BytesIO(payload), "image/png")
    transform = "exif-transpose-rgb-v1"
    crop_transform = transform + ":crop:" + ",".join(str(value) for value in box)
    context = _source_context(parsed, selected)
    candidates = tuple(item.candidate_context() for item in context)
    return (
        RegionInput(
            source_native_ref=selected.source_native_ref,
            image=payload,
            image_ref=retained,
            media_type="image/png",
            image_sha256=sha256(payload).hexdigest(),
            original_image_sha256=image_ref.sha256,
            original_pixel_sha256=pixel_identity(
                pixels.tobytes(), pixels.width, pixels.height, "RGB", transform
            ),
            region_pixel_sha256=pixel_identity(
                crop.tobytes(), crop.width, crop.height, "RGB", crop_transform
            ),
            original_width=pixels.width,
            original_height=pixels.height,
            width=crop.width,
            height=crop.height,
            color_mode="RGB",
            original_pixel_transform=transform,
            region_pixel_transform=crop_transform,
            geometry=Geometry(
                domain="pixels", x0=box[0], y0=box[1], x1=box[2], y1=box[3]
            ),
            context_sha256=vision_context_identity(candidates),
            task=selected.task,
            allowed_candidate_target_ids=frozenset(item.target_id for item in context),
            dependency_ids=frozenset(
                {registration.source.source_id, *(item.target_id for item in context)}
            ),
            candidate_context=candidates,
            source_context=context,
        ),
        proof,
    )


def retain_remote_observation(
    owner: WorkspaceOwner, region: RegionInput, observation: DerivedObservation
) -> DerivedObservation:
    """Retain the exact nonsecret result before the composition plan is built."""
    if observation.derivation is not None:
        return observation
    data = {
        "task": region.task,
        "source_native_ref": region.source_native_ref,
        "image_sha256": region.image_sha256,
        "original_image_sha256": region.original_image_sha256,
        "original_pixel_sha256": region.original_pixel_sha256,
        "region_pixel_sha256": region.region_pixel_sha256,
        "context_sha256": region.context_sha256,
        "source_context_sha256": source_context_identity(region.source_context),
        "dependency_ids": sorted(region.dependency_ids),
        "model": observation.model,
        "prompt_schema_version": observation.prompt_schema_version,
        "task_schema_sha256": observation.task_schema_sha256,
        "prompt_identity": observation.prompt_identity,
        "result": {
            "target_id": observation.target_id,
            "literal_transcription": observation.literal_transcription,
            "interpretation": observation.interpretation,
            "uncertainties": list(observation.uncertainties),
        },
    }
    if observation.prompt_renderer_identity is not None:
        data["prompt_renderer_identity"] = observation.prompt_renderer_identity
    ref = owner.artifacts.put_stream(
        BytesIO(
            canonical_bytes(
                {"type": "vision_derivation", "schema_version": "1", "data": data}
            )
        ),
        "application/json",
    )
    return observation.model_copy(update={"derivation": ref})
