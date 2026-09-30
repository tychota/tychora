"""Source-bound public selection uses retained parser pixels."""

from io import BytesIO

import pytest
from PIL import Image

from tests.qualification import test_docling_records as qualification
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import VisionSelection

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
    selected = VisionSelection(
        captured_original_sha256=parsed.original_ref.sha256,
        retained_extraction_sha256=parsed.raw_document_ref.sha256,
        source_native_ref=parsed.original_image.native_ref,
        geometry={"domain": "pixels", "x0": 1, "y0": 2, "x1": 5, "y1": 4},
        task="describe",
        task_schema={
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
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
