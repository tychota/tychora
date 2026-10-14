"""Pure rich-document normalization and reconstruction from retained SDK JSON."""

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, cast

from tychora.adapters.docling.ocr_evidence import normalize_native_document_units
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    AssetRow,
    ExtractionBundle,
    ExtractionRow,
    IdentifierRow,
    LocatorRow,
    RevisionArtifactRow,
    StructuredRecordRow,
    UnitAssetRow,
)
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import canonical_bytes, digest, extraction_id, unit_id

from .document_identity import DocumentNormalizationContext


def normalize_document(
    parsed: ExtractionArtifact, revision_id: str, registry: Registry
) -> ExtractionBundle:
    registration = cast(DocumentRegistration, parsed.registration)
    if (
        revision_id != registration.revision.revision_id
        or registry.registry_digest != parsed.registry_digest
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Document normalization revision or registry binding differs",
        )
    registry.validate_metadata(
        registration.revision.kind_id, registration.revision.metadata_json
    )
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    from .document_records import document_normalizer_fingerprint

    if parsed.normalizer_fingerprint != document_normalizer_fingerprint(kind, registry):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Document normalizer fingerprint differs from registry binding",
        )
    if registration.format in ("png", "jpeg"):
        return normalize_standalone_image(parsed, revision_id, registry)
    if kind["normalizer"] != "structured_sections@1":
        from .document_records import normalize_document_records

        return normalize_document_records(parsed, revision_id, registry)
    if parsed.normalizer_fingerprint != digest(
        "docling-normalizer-v2", kind, registry.schema_digests
    ):
        raise TychoraError("INTEGRITY_ERROR", "Document normalizer fingerprint differs")
    eid = extraction_id(
        revision_id, parsed.parser_fingerprint, parsed.normalizer_fingerprint
    )
    document_units = normalize_native_document_units(parsed)
    ids = {
        u.native_ref: unit_id(
            eid, u.native_ref, u.ordinal, sha256(u.text.encode()).hexdigest()
        )
        for u in document_units
    }
    units = []
    locator_rows = []
    for unit in document_units:
        locators = tuple(
            loc.model_copy(update={"native_ref": registration.source.source_key})
            for loc in unit.locators
        )
        attributes = FrozenJsonMap(
            {
                "raw_item": {
                    key: value for key, value in unit.raw_item.items() if key != "image"
                },
                "provenance": [p.model_dump(mode="json") for p in unit.provenance],
                "cells": unit.cells,
                "heading_level": unit.heading_level,
            }
        )
        units.append(
            EvidenceUnit(
                unit_id=ids[unit.native_ref],
                extraction_id=eid,
                parent_id=ids.get(unit.parent_ref),
                ordinal=unit.ordinal,
                unit_type=unit.unit_type,
                native_ref=unit.native_ref,
                boundary_key=unit.native_ref,
                heading_path=unit.heading_path,
                text=unit.text,
                text_sha=sha256(unit.text.encode()).hexdigest(),
                locators=locators,
                attributes=attributes,
            )
        )
        locator_rows.extend(
            LocatorRow(
                locator_id=digest("locator-v1", ids[unit.native_ref], ordinal),
                unit_id=ids[unit.native_ref],
                fragment_ordinal=ordinal,
                locator=locator,
            )
            for ordinal, locator in enumerate(locators)
        )
    return assemble_document_bundle(
        parsed,
        tuple(units),
        tuple(locator_rows),
        {ref: (uid,) for ref, uid in ids.items()},
    )


def normalize_standalone_image(
    parsed: ExtractionArtifact,
    revision_id: str,
    registry: Registry,
    *,
    context: DocumentNormalizationContext | None = None,
) -> ExtractionBundle:
    registration = cast(DocumentRegistration, parsed.registration)
    image = parsed.original_image
    if (
        image is None
        or revision_id != registration.revision.revision_id
        or registry.registry_digest != parsed.registry_digest
    ):
        raise TychoraError("INTEGRITY_ERROR", "Standalone image binding differs")
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    from .document_records import document_normalizer_fingerprint

    if parsed.normalizer_fingerprint != document_normalizer_fingerprint(
        kind, registry, legacy_v1=context.legacy_v1 if context else False
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Standalone image normalizer binding differs"
        )
    registry.validate_metadata(
        revision_id and registration.revision.kind_id,
        registration.revision.metadata_json,
    )
    fingerprint = (
        context.normalizer_fingerprint if context else parsed.normalizer_fingerprint
    )
    eid = extraction_id(revision_id, parsed.parser_fingerprint, fingerprint)
    text_sha = sha256(b"").hexdigest()
    uid = unit_id(eid, image.native_ref, 0, text_sha)
    unit = EvidenceUnit(
        unit_id=uid,
        extraction_id=eid,
        parent_id=None,
        ordinal=0,
        unit_type="image_original",
        native_ref=image.native_ref,
        boundary_key=image.native_ref,
        text="",
        text_sha=text_sha,
        text_origin="source",
        citable=True,
        locators=(image.locator,),
        attributes=FrozenJsonMap(
            {
                "encoded_sha256": image.encoded_sha256,
                "pixel_sha256": image.pixel_sha256,
                "width": image.width,
                "height": image.height,
                "color_mode": image.color_mode,
                "pixel_transform": image.pixel_transform,
                "document_representation": "docling-image-source@1",
            }
        ),
    )
    return assemble_document_bundle(
        parsed,
        (unit,),
        (
            LocatorRow(
                locator_id=digest("locator-v1", uid, 0),
                unit_id=uid,
                fragment_ordinal=0,
                locator=image.locator,
            ),
        ),
        {image.native_ref: (uid,)},
        context=context,
    )


def assemble_document_bundle(
    parsed: ExtractionArtifact,
    units: tuple[EvidenceUnit, ...],
    locators: tuple[LocatorRow, ...],
    source_owners: Mapping[str, tuple[str, ...]],
    records: tuple[StructuredRecordRow, ...] = (),
    identifiers: tuple[IdentifierRow, ...] = (),
    *,
    context: DocumentNormalizationContext | None = None,
) -> ExtractionBundle:
    """Assemble shared original/asset bindings for canonical document units."""
    registration = cast(DocumentRegistration, parsed.registration)
    revision_id = registration.revision.revision_id
    fingerprint = (
        context.normalizer_fingerprint if context else parsed.normalizer_fingerprint
    )
    eid = extraction_id(revision_id, parsed.parser_fingerprint, fingerprint)
    assets = []
    unit_assets = []
    ocr_refs = {
        unit.native_ref
        for unit in parsed.units
        if getattr(unit, "text_origin", "source") == "ocr"
    }
    for asset in parsed.assets:
        caption = asset.caption
        if ocr_refs.intersection(asset.source_units):
            caption = "\n".join(
                unit.text
                for unit in normalize_native_document_units(parsed)
                if unit.native_ref in asset.source_units
                and unit.native_ref != asset.native_ref
            )
        aid = digest("asset-v1", eid, asset.native_ref, asset.artifact_ref.sha256)
        assets.append(
            AssetRow(
                asset_id=aid,
                extraction_id=eid,
                artifact_sha=asset.artifact_ref.sha256,
                asset_type=asset.asset_type,
                native_ref=asset.native_ref,
                locator=asset.locator,
                caption=caption,
            )
        )
        for ref in asset.source_units:
            if ref in ocr_refs:
                continue
            if ref not in source_owners:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Document asset references missing source unit"
                )
            unit_assets.extend(
                UnitAssetRow(
                    unit_id=uid,
                    asset_id=aid,
                    role="source"
                    if ref == asset.native_ref
                    or parsed.original_image is not None
                    and ref == parsed.original_image.native_ref
                    else "caption",
                )
                for uid in source_owners[ref]
            )
    refs = {
        ref.sha256: ref
        for ref in (
            parsed.original_ref,
            parsed.raw_document_ref,
            *registration.capture.sidecar_refs,
            cast(ArtifactRef, parsed.docling_json_ref),
            *(a.artifact_ref for a in parsed.assets),
            *(
                ref
                for ref in (parsed.ocr_evidence_ref, parsed.image_bindings_ref)
                if ref is not None
            ),
        )
    }
    return ExtractionBundle(
        workspace=registration.workspace,
        collection=registration.collection,
        source=registration.source,
        revision=registration.revision,
        artifacts=tuple(refs.values()),
        revision_artifacts=(
            RevisionArtifactRow(
                revision_id=revision_id,
                role="original",
                source_ref=registration.source.source_key,
                artifact_sha=parsed.original_ref.sha256,
            ),
            *(
                RevisionArtifactRow(
                    revision_id=revision_id,
                    role="sidecar",
                    source_ref=f"sidecar:{i}",
                    artifact_sha=ref.sha256,
                )
                for i, ref in enumerate(registration.capture.sidecar_refs)
            ),
        ),
        extraction=ExtractionRow(
            extraction_id=eid,
            revision_id=revision_id,
            parser_fingerprint=parsed.parser_fingerprint,
            normalizer_fingerprint=fingerprint,
            document_artifact_sha=parsed.raw_document_ref.sha256,
            created_at=registration.revision.captured_at,
        ),
        units=tuple(units),
        locators=locators,
        assets=tuple(assets),
        unit_assets=tuple(unit_assets),
        records=records,
        identifiers=identifiers,
    )


def replay_document(
    manifest: bytes, raw_json: bytes, manifest_ref: ArtifactRef, *, read_declared=None
) -> ExtractionArtifact:
    """Reconstruct from retained JSON bytes alone, including independent bindings."""
    if (
        sha256(manifest).hexdigest() != manifest_ref.sha256
        or len(manifest) != manifest_ref.size_bytes
    ):
        raise ValueError("Extraction manifest differs from retained reference")
    root = json.loads(manifest)
    if root.get("type") != "extraction" or root.get("schema_version") != "1":
        raise ValueError("Unknown extraction manifest")
    document = json.loads(raw_json)
    if canonical_bytes(document) != raw_json:
        raise ValueError("Docling JSON must be canonical")
    data = root["data"]
    for unit in data["units"]:
        node = document
        for segment in unit["native_ref"][2:].split("/"):
            node = node[int(segment)] if isinstance(node, list) else node[segment]
        unit["raw_item"] = node
    parsed = ExtractionArtifact.model_validate_json(
        canonical_bytes(
            {
                **data,
                "document_json": document,
                "raw_document_ref": manifest_ref.model_dump(mode="json"),
            }
        )
    )

    if parsed.image_representation is not None:
        from tychora.adapters.docling.ocr_evidence import validate_external_images

        declared = {
            item["sha256"]: ArtifactRef.model_validate(item)
            for item in root.get("required_artifacts", [])
        }

        def read(ref):
            if read_declared is None or declared.get(ref.sha256) != ref:
                raise ValueError(
                    "External image is not authorized by extraction manifest"
                )
            return read_declared(ref)

        validate_external_images(parsed, read)
        for ref in (parsed.ocr_evidence_ref, parsed.image_bindings_ref):
            assert ref is not None
            payload = read(ref)
            if (
                len(payload) != ref.size_bytes
                or sha256(payload).hexdigest() != ref.sha256
            ):
                raise ValueError("Retained PDF sidecar differs")
    return parsed
