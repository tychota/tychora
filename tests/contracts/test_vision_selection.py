"""Public selected vision request contracts."""

import pytest
from pydantic import ValidationError

from tychora.configuration.models import ProviderSettings
from tychora.contracts.corpus import SourceRow
from tychora.contracts.identity import source_id
from tychora.contracts.requests import SourceInput
from tychora.ports.vision import CapturedSourceRegistration


def _source(vision):
    return SourceInput.from_mapping(
        {
            "type": "file",
            "source_key": "diagram",
            "collection_id": "00000000-0000-4000-8000-000000000001",
            "kind": "standard@1",
            "metadata": {"title": "Diagram"},
            "format": "png",
            "path": "/synthetic/diagram.png",
            "vision": vision,
        }
    )


def test_public_selection_requires_explicit_original_and_integer_crop():
    selected = _source(
        [
            {
                "captured_original_sha256": "a" * 64,
                "retained_extraction_sha256": "b" * 64,
                "source_native_ref": "#/original",
                "geometry": {"domain": "pixels", "x0": 1, "y0": 2, "x1": 3, "y1": 4},
                "task": "ocr",
                "task_schema": {"type": "null"},
            }
        ]
    )
    assert selected.root.vision[0].geometry.x0 == 1
    with pytest.raises(ValidationError):
        _source(
            [
                {
                    "source_native_ref": "#/original",
                    "task": "ocr",
                    "task_schema": {"type": "null"},
                }
            ]
        )
    with pytest.raises(ValidationError):
        _source(
            [
                {
                    "captured_original_sha256": "a" * 64,
                    "retained_extraction_sha256": "b" * 64,
                    "source_native_ref": "#/original",
                    "geometry": {
                        "domain": "pixels",
                        "x0": 1.5,
                        "y0": 2,
                        "x1": 3,
                        "y1": 4,
                    },
                    "task": "invented",
                    "task_schema": {"type": "null"},
                }
            ]
        )


def test_anthropic_vision_requires_explicit_model_and_credential():
    assert ProviderSettings().vision.provider == "disabled"
    configured = ProviderSettings.model_validate(
        {
            "vision": {
                "provider": "anthropic",
                "model": "qualified-test-model",
                "credential_ref": "env:SYNTHETIC_VISION_KEY",
            }
        }
    )
    assert configured.vision.model == "qualified-test-model"
    with pytest.raises(ValidationError):
        ProviderSettings.model_validate({"vision": {"provider": "anthropic"}})


def test_captured_registration_accepts_exact_public_descriptor_only():
    source = _source([]).root
    descriptor = source.model_dump(
        mode="json", exclude={"metadata", "vision", "text", "data"}
    )
    row = SourceRow(
        source_id=source_id(source.collection_id, source.source_key),
        collection_id=str(source.collection_id),
        source_key=source.source_key,
        registration_json=descriptor,
        created_at="2026-01-01T00:00:00Z",
    )
    assert CapturedSourceRegistration(source=row, kind_id=source.kind).source == row
    with pytest.raises(ValidationError):
        CapturedSourceRegistration(
            source=row.model_copy(
                update={"registration_json": {**descriptor, "kind": "other@1"}}
            ),
            kind_id=source.kind,
        )
    with pytest.raises(ValidationError):
        CapturedSourceRegistration(
            source=row.model_copy(
                update={"registration_json": {**descriptor, "proof": "claimed"}}
            ),
            kind_id=source.kind,
        )
