"""Source-bound public selection uses retained parser pixels."""

from io import BytesIO

import pytest
from PIL import Image

from tests.qualification import test_docling_records as qualification
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import VisionCrop, VisionSelection

document_record_case = qualification.document_record_case


def _picture(path, _content):
    image = Image.new("RGB", (8, 6), "blue")
    image.save(path)


def test_selected_crop_comes_from_retained_original(document_record_case):
    from tychora.application.vision_selection import derive_selected_region

    case = document_record_case(
        "standard@1", "png", source_builder=_picture, commit=False
    )
    parsed = case.parsed
    assert parsed.original_ref.media_type == "application/octet-stream"
    assert parsed.original_image is not None
    selected = VisionSelection(
        captured_original_sha256=parsed.original_ref.sha256,
        source_native_ref=parsed.original_image.native_ref,
        geometry=VisionCrop(domain="pixels", x0=1, y0=2, x1=5, y1=4),
        task="describe",
        task_schema=FrozenJsonMap(
            {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
                "additionalProperties": False,
            }
        ),
    )
    region, proof = case.owner.run_blocking_sync(
        lambda: derive_selected_region(case.owner, parsed, selected), case.ctx
    )
    assert proof.capture == parsed.registration.capture
    assert region.image_ref.sha256 == region.image_sha256
    assert region.image_sha256 != parsed.original_ref.sha256
    with Image.open(BytesIO(region.image)) as image:
        assert image.size == (4, 2)
        assert image.convert("RGB").getpixel((1, 0)) == (0, 0, 255)
    with pytest.raises(TychoraError) as stale:
        case.owner.run_blocking_sync(
            lambda: derive_selected_region(
                case.owner,
                parsed,
                selected.model_copy(update={"retained_extraction_sha256": "f" * 64}),
            ),
            case.ctx,
        )
    assert stale.value.code == "INVALID_REQUEST"


def test_supplied_new_parser_root_requires_retained_revision(document_record_case):
    import os
    from pathlib import Path

    from tychora.adapters.docling.parser import DoclingParser
    from tychora.application.vision_selection import derive_selected_region
    from tychora.contracts.composition import DocumentCompositionPlan
    from tychora.ingestion.composition import compose_document
    from tychora.ports.execution import ExecutionContext

    case = document_record_case(
        "standard@1", "png", source_builder=_picture, commit=True
    )
    parsed_a = case.parsed
    assert (
        case.bundle.extraction.document_artifact_sha == parsed_a.raw_document_ref.sha256
    )
    config = case.owner.config
    changed = config.model_copy(
        update={
            "values": config.values.model_copy(
                update={
                    "extraction": config.values.extraction.model_copy(
                        update={"representation_version": "2"}
                    )
                }
            )
        }
    )
    parsed_b = DoclingParser(
        case.owner,
        parsed_a.registration,
        Path(os.environ["TYCHORA_TEST_DOCLING_MODELS"]),
    ).extract(parsed_a.original_ref, changed, ExecutionContext())
    assert parsed_b.original_ref.sha256 == parsed_a.original_ref.sha256
    assert parsed_b.raw_document_ref.sha256 != parsed_a.raw_document_ref.sha256
    assert parsed_b.original_image is not None
    selected = VisionSelection(
        captured_original_sha256=parsed_b.original_ref.sha256,
        retained_extraction_sha256=parsed_b.raw_document_ref.sha256,
        source_native_ref=parsed_b.original_image.native_ref,
        geometry=VisionCrop(domain="pixels", x0=1, y0=2, x1=5, y1=4),
        task="describe",
        task_schema=FrozenJsonMap(
            {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
                "additionalProperties": False,
            }
        ),
    )
    with pytest.raises(TychoraError) as unretained:
        case.owner.run_blocking_sync(
            lambda: derive_selected_region(case.owner, parsed_b, selected),
            ExecutionContext(),
        )
    assert unretained.value.code == "INVALID_REQUEST"
    compose_ctx = ExecutionContext()
    composed_b = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed_b,
            case.registry,
            DocumentCompositionPlan(
                base_normalizer_fingerprint=parsed_b.normalizer_fingerprint
            ),
            case.owner.artifacts,
            compose_ctx,
        ),
        compose_ctx,
    )
    case.owner.run_blocking_sync(
        lambda: case.owner.store.commit_extraction(composed_b.bundle),
        ExecutionContext(),
    )
    region, _ = case.owner.run_blocking_sync(
        lambda: derive_selected_region(case.owner, parsed_b, selected),
        ExecutionContext(),
    )
    assert region.original_image_sha256 == parsed_b.original_ref.sha256


def test_incomplete_vision_diagnostic_changes_identity_and_replays(
    document_record_case,
):
    from tychora.contracts.composition import (
        DocumentCompositionPlan,
        VisionCapabilityDiagnostics,
        VisionCapabilityGap,
    )
    from tychora.ingestion.composition import compose_document, replay_composed_document

    case = document_record_case(
        "standard@1", "png", source_builder=_picture, commit=False
    )
    parsed = case.parsed
    ordinary = DocumentCompositionPlan(
        base_normalizer_fingerprint=parsed.normalizer_fingerprint,
    )
    semantic_data = ordinary.semantic_data()
    assert isinstance(semantic_data, dict)
    assert "vision_gaps" not in semantic_data
    gap = VisionCapabilityGap(selection_sha256="a" * 64, reason="provider_failure")
    incomplete = ordinary.model_copy(update={"vision_gaps": (gap,)})
    base = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed, case.registry, ordinary, case.owner.artifacts, case.ctx
        ),
        case.ctx,
    )
    result = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed, case.registry, incomplete, case.owner.artifacts, case.ctx
        ),
        case.ctx,
    )
    assert (
        result.bundle.extraction.extraction_id != base.bundle.extraction.extraction_id
    )
    sha = result.bundle.extraction.diagnostics_artifact_sha
    assert sha is not None
    assert sha in {ref.sha256 for ref in result.bundle.artifacts}
    diagnostic = VisionCapabilityDiagnostics.model_validate_json(result.payloads[sha])
    assert diagnostic.gaps == (gap,)
    retained = dict(result.payloads)
    for asset in parsed.assets:
        with case.owner.artifacts.open_verified(asset.artifact_ref) as stream:
            retained[asset.artifact_ref.sha256] = stream.read()
    replay = replay_composed_document(
        result.payloads[result.manifest_ref.sha256],
        result.manifest_ref,
        parsed,
        case.registry,
        retained,
    )
    assert replay.bundle == result.bundle
