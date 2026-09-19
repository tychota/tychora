"""Materialize reviewed vision observations before the first corpus commit."""

from hashlib import sha256

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.composition import PlannedProxy, PlannedRegion
from tychora.contracts.corpus import ExtractionBundle, LocatorRow, UnitAssetRow
from tychora.contracts.evidence import EvidenceUnit, Geometry
from tychora.contracts.identity import canonical_bytes, digest, unit_id
from tychora.ports.vision import DerivedObservation, RegionInput, vision_cache_key


def planned_vision_proxy(
    region: RegionInput,
    observation: DerivedObservation,
    *,
    asset_native_ref: str | None = None,
) -> PlannedProxy:
    """Capture all selected-region and observation facts before final IDs exist."""
    if (
        observation.derivation is None
        or observation.task != region.task
        or observation.image_sha256 != region.image_sha256
        or observation.original_image_sha256 != region.original_image_sha256
        or observation.original_pixel_sha256 != region.original_pixel_sha256
        or observation.region_pixel_sha256 != region.region_pixel_sha256
        or observation.context_sha256 != region.context_sha256
        or observation.cache_key
        != vision_cache_key(
            region.image_sha256,
            region.task,
            observation.model,
            observation.prompt_identity,
            region.context_sha256,
        )
        or observation.target_id is not None
        and observation.target_id not in region.allowed_candidate_target_ids
    ):
        raise ValueError("Vision observation differs from selected source region")
    if region.task == "ocr":
        if (
            not observation.literal_transcription
            or observation.interpretation is not None
        ):
            raise ValueError("OCR proxy requires a separate literal transcription")
        kind = "ocr"
        text = observation.literal_transcription
    else:
        if observation.interpretation is None:
            raise ValueError(
                "Vision interpretation requires structured derived content"
            )
        kind = "description"
        text = canonical_bytes(observation.interpretation).decode("utf-8")
    planned = PlannedRegion(
        source_native_ref=region.source_native_ref,
        image_ref=region.image_ref,
        media_type=region.media_type,
        image_sha256=region.image_sha256,
        original_image_sha256=region.original_image_sha256,
        original_pixel_sha256=region.original_pixel_sha256,
        region_pixel_sha256=region.region_pixel_sha256,
        original_width=region.original_width,
        original_height=region.original_height,
        width=region.width,
        height=region.height,
        color_mode=region.color_mode,
        original_pixel_transform=region.original_pixel_transform,
        region_pixel_transform=region.region_pixel_transform,
        geometry=region.geometry,
        context_sha256=region.context_sha256,
        task=region.task,
        model=observation.model,
        prompt_schema_version=observation.prompt_schema_version,
        task_schema_sha256=observation.task_schema_sha256,
        prompt_renderer_identity=observation.prompt_renderer_identity,
        prompt_identity=observation.prompt_identity,
        allowed_candidate_target_ids=region.allowed_candidate_target_ids,
        dependency_ids=region.dependency_ids,
        candidate_context=region.candidate_context,
    )
    return PlannedProxy(
        asset_native_ref=asset_native_ref or region.source_native_ref,
        kind=kind,
        text=text,
        text_sha=sha256(text.encode()).hexdigest(),
        dependencies=(),
        derivation=observation.derivation,
        region=planned,
        source_context=region.source_context,
    )


def pixel_source_owner_id(
    bundle: ExtractionBundle,
    asset_id: str,
    pixel_sha256: str,
    width: int,
    height: int,
    pixel_transform: str,
) -> str:
    asset = next((row for row in bundle.assets if row.asset_id == asset_id), None)
    if asset is None:
        raise ValueError("Pixel source view requires an existing asset")
    native_ref = "pixel-source:" + digest(
        "pixel-source-view-v1",
        asset.asset_id,
        asset.artifact_sha,
        pixel_sha256,
        width,
        height,
        pixel_transform,
    )
    ordinal = max((unit.ordinal for unit in bundle.units), default=-1) + 1
    return unit_id(
        bundle.extraction.extraction_id,
        native_ref,
        ordinal,
        sha256(b"").hexdigest(),
    )


def _materialize_pixel_source_view(
    bundle: ExtractionBundle, asset_id: str, region: PlannedRegion
) -> tuple[ExtractionBundle, str]:
    """Add a citable original-pixel owner after final identity is known."""
    asset = next((row for row in bundle.assets if row.asset_id == asset_id), None)
    if asset is None:
        raise ValueError("Pixel source view requires an existing asset")
    existing = next(
        (
            unit
            for unit in bundle.units
            if unit.unit_type == "image_region_original"
            and unit.attributes.get("asset_id") == asset_id
            and unit.attributes.get("pixel_sha256") == region.original_pixel_sha256
            and unit.attributes.get("width") == region.original_width
            and unit.attributes.get("height") == region.original_height
            and unit.attributes.get("pixel_transform")
            == region.original_pixel_transform
        ),
        None,
    )
    if existing is not None:
        return bundle, existing.unit_id
    uid = pixel_source_owner_id(
        bundle,
        asset_id,
        region.original_pixel_sha256,
        region.original_width,
        region.original_height,
        region.original_pixel_transform,
    )
    geometry = region.geometry
    if asset.artifact_sha != region.original_image_sha256 or region.color_mode != "RGB":
        raise ValueError("Selected page pixels differ from the retained original asset")
    ordinal = max((unit.ordinal for unit in bundle.units), default=-1) + 1
    native_ref = "pixel-source:" + digest(
        "pixel-source-view-v1",
        asset.asset_id,
        asset.artifact_sha,
        region.original_pixel_sha256,
        region.original_width,
        region.original_height,
        region.original_pixel_transform,
    )
    text_sha = sha256(b"").hexdigest()
    unit = EvidenceUnit(
        unit_id=uid,
        extraction_id=bundle.extraction.extraction_id,
        parent_id=None,
        ordinal=ordinal,
        unit_type="image_region_original",
        native_ref=native_ref,
        boundary_key=native_ref,
        text="",
        text_sha=text_sha,
        text_origin="source",
        citable=True,
        locators=(
            asset.locator.model_copy(
                update={
                    "geometry": Geometry(
                        domain="pixels",
                        x0=0,
                        y0=0,
                        x1=region.original_width,
                        y1=region.original_height,
                    )
                }
            ),
        ),
        attributes=FrozenJsonMap(
            {
                "asset_id": asset.asset_id,
                "encoded_sha256": region.original_image_sha256,
                "pixel_sha256": region.original_pixel_sha256,
                "width": region.original_width,
                "height": region.original_height,
                "color_mode": region.color_mode,
                "pixel_transform": region.original_pixel_transform,
                "selected_region_geometry": geometry.model_dump(mode="json"),
                "selected_region_pixel_sha256": region.region_pixel_sha256,
            }
        ),
    )
    locator = LocatorRow(
        locator_id=digest("locator-v1", uid, 0),
        unit_id=uid,
        fragment_ordinal=0,
        locator=unit.locators[0],
    )
    return (
        bundle.model_copy(
            update={
                "units": (*bundle.units, unit),
                "locators": (*bundle.locators, locator),
                "unit_assets": (
                    *bundle.unit_assets,
                    UnitAssetRow(unit_id=uid, asset_id=asset.asset_id, role="source"),
                ),
            }
        ),
        uid,
    )
