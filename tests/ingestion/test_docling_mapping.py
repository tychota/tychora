from hashlib import sha256

from tychora.adapters.docling.mapping import map_document
from tychora.contracts.artifacts import ArtifactRef


def mapping_case():
    original = ArtifactRef(
        sha256=sha256(b"%PDF-original").hexdigest(),
        size_bytes=13,
        media_type="application/pdf",
        storage_key=sha256(b"%PDF-original").hexdigest(),
    )
    export = {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "name": "Synthetic",
        "body": {"self_ref": "#/body", "children": [{"$ref": "#/texts/0"}]},
        "furniture": {"self_ref": "#/furniture", "children": []},
        "groups": [],
        "tables": [],
        "pictures": [],
        "texts": [
            {
                "self_ref": "#/texts/0",
                "parent": {"$ref": "#/body"},
                "children": [],
                "label": "text",
                "text": "Retain the sample.",
                "orig": "Retain the sample.",
                "prov": [
                    {
                        "page_no": 2,
                        "charspan": [0, 18],
                        "bbox": {
                            "l": 10.0,
                            "t": 180.0,
                            "r": 110.0,
                            "b": 160.0,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ],
            }
        ],
        "pages": {"2": {"page_no": 2, "size": {"width": 200.0, "height": 300.0}}},
    }
    return export, original


def test_mapping_preserves_item_domain_and_recorded_geometry_transform():
    export, original = mapping_case()
    mapped = map_document(export, original)
    unit = mapped.units[0]
    assert unit.native_ref == "#/texts/0"
    assert unit.text == "Retain the sample."
    assert unit.provenance[0].char_domain == "docling_text_item"
    assert unit.provenance[0].charspan == (0, 18)
    assert unit.provenance[0].transform == "bottom_left_to_top_left"
    assert unit.locators[0].physical_page == 2
    assert unit.locators[0].geometry is not None
    assert unit.locators[0].geometry.y0 == 120.0
    assert unit.locators[0].char_start == 0
    assert unit.locators[0].char_end == 18


def test_document_manifest_externalizes_raw_item_json():
    import json

    from tychora.contracts.extraction import extraction_document

    ref = {
        "sha256": "a" * 64,
        "storage_key": "a" * 64,
        "size_bytes": 9,
        "media_type": "application/json",
    }
    data = {
        "docling_json_ref": ref,
        "assets": [],
        "units": [
            {
                "native_ref": "#/texts/0",
                "raw_item": {"image": {"uri": "data:image/png;base64,abc"}},
                "text": "source",
            }
        ],
    }
    retained = json.loads(extraction_document(data))
    assert "raw_item" not in retained["data"]["units"][0]
    assert retained["required_artifacts"] == [ref]


import pytest

from tests.qualification import test_docling as qualification

execution = qualification.execution
rich_capture = qualification.rich_capture


@pytest.mark.parametrize(
    "change", ["span", "page", "bbox", "duplicate", "parent", "origin"]
)
def test_mapping_rejects_invalid_native_facts(change):
    export, original = mapping_case()
    item = export["texts"][0]
    if change == "span":
        item["prov"][0]["charspan"] = [0, 999]
    if change == "page":
        item["prov"][0]["page_no"] = 99
    if change == "bbox":
        item["prov"][0]["bbox"]["r"] = 500.0
    if change == "duplicate":
        export["texts"].append(item)
    if change == "parent":
        item["parent"] = {"$ref": "#/missing"}
    if change == "origin":
        item["prov"][0]["bbox"]["coord_origin"] = "unknown"
    with pytest.raises(ValueError):
        map_document(export, original)


def test_document_unit_validation_rejects_omitted_source_items():
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.documents import validate_document_units

    export, _original = mapping_case()
    with pytest.raises(ValueError, match="coverage"):
        validate_document_units(FrozenJsonMap(export), ())


def test_replay_requires_exact_retained_payload():
    from tychora.ingestion.documents import replay_document

    _, original = mapping_case()
    with pytest.raises(ValueError, match="manifest differs"):
        replay_document(b"{}", b"{}", original)


@pytest.mark.parametrize(
    "field,value", [("heading_level", 8), ("heading_path", ("Invented heading",))]
)
def test_document_validation_rejects_invented_heading_context(field, value):
    from tychora.contracts.documents import validate_document_units

    mapped = map_document(*mapping_case())
    with pytest.raises(ValueError, match="heading"):
        validate_document_units(
            mapped.document_json, (mapped.units[0].model_copy(update={field: value}),)
        )


@pytest.fixture
def retained_case(execution, rich_capture):
    import base64
    import io
    import json
    from pathlib import Path

    from tychora.adapters.docling.parser import INVENTORY, DoclingParser
    from tychora.application.owner import StagedOutput
    from tychora.configuration.registry import Registry
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.identity import canonical_bytes

    export, _ = mapping_case()
    pixels = (
        Path(__file__).parents[1] / "fixtures/documents/rich-figure.png"
    ).read_bytes()
    picture = {
        "self_ref": "#/pictures/0",
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "picture",
        "prov": [],
        "captions": [],
        "image": {
            "uri": "data:image/png;base64," + base64.b64encode(pixels).decode(),
            "size": {"width": 640.0, "height": 320.0},
            "dpi": 72,
            "mimetype": "image/png",
        },
    }
    export["pictures"] = [picture]
    export["body"]["children"].append({"$ref": "#/pictures/0"})
    if execution.format == "docx":
        export["texts"][0]["prov"] = []
        export["pages"] = {}
    registry = Registry.from_config(execution.config)
    parser = DoclingParser(
        execution.owner,
        execution.registration,
        Path("/nonexistent-explicit-model-bundle"),
    )
    payload = canonical_bytes(export)
    kind = registry.kinds["manual@1"]
    assert isinstance(kind, FrozenJsonMap)
    artifact = execution.owner.run_blocking_sync(
        lambda: parser._retain(
            (
                (
                    StagedOutput("document.json", "application/json", len(payload)),
                    io.BytesIO(payload),
                ),
            ),
            rich_capture,
            execution.config,
            registry,
            kind,
            json.loads(INVENTORY.read_text()),
        ),
        execution.ctx,
    )
    return artifact, execution, registry


@pytest.mark.parametrize(
    "change",
    ["asset_payload", "asset_page", "asset_caption", "asset_width", "asset_omitted"],
)
def test_retained_asset_facts_bound_to_complete_json(retained_case, change):
    from tychora.contracts.extraction import ExtractionArtifact, extraction_document
    from tychora.contracts.identity import canonical_bytes

    artifact, _, _ = retained_case
    data = artifact.model_dump(mode="json", exclude={"raw_document_ref"})
    if change == "asset_payload":
        data["assets"][0]["artifact_ref"] = artifact.original_ref.model_dump(
            mode="json"
        )
    if change == "asset_page":
        data["assets"][0]["locator"]["physical_page"] = 99
    if change == "asset_caption":
        data["assets"][0]["caption"] = "Invented source caption"
    if change == "asset_width":
        data["assets"][0]["width"] = 1
    if change == "asset_omitted":
        data["assets"] = []
    payload = extraction_document(
        {k: v for k, v in data.items() if k != "document_json"}
    )
    h = sha256(payload).hexdigest()
    data["raw_document_ref"] = {
        "sha256": h,
        "size_bytes": len(payload),
        "storage_key": h,
        "media_type": "application/json",
    }
    with pytest.raises(ValueError):
        ExtractionArtifact.model_validate_json(canonical_bytes(data))


def test_missing_models_fail_with_explicit_setup_guidance(tmp_path):
    from tychora.adapters.docling.parser import validate_models
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError, match="Explicitly provision") as exc:
        validate_models(tmp_path)
    assert exc.value.code == "CAPABILITY_MISSING"


@pytest.mark.asyncio
async def test_async_preparation_preserves_heartbeat(
    execution, rich_capture, monkeypatch
):
    import asyncio
    import threading
    from pathlib import Path

    from tychora.adapters.docling.parser import DoclingParser

    started, release = threading.Event(), threading.Event()
    parser = DoclingParser(
        execution.owner, execution.registration, Path("/missing-models")
    )

    def held(*args):
        started.set()
        assert release.wait(5)
        raise ValueError("held preflight complete")

    monkeypatch.setattr(parser, "_prepare", held)
    task = asyncio.create_task(
        parser.aextract(rich_capture, execution.config, execution.ctx)
    )
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        await asyncio.sleep(0.01)
    finally:
        release.set()
        with pytest.raises(ValueError, match="held preflight"):
            await task


def test_document_registration_media_hint_does_not_change_physical_identity(
    retained_case,
):
    from tychora.contracts.documents import DocumentRegistration

    artifact, _, _ = retained_case
    registration = artifact.registration
    entry = registration.capture.entries[0]
    hinted = entry.artifact_ref.model_copy(update={"media_type": "application/pdf"})
    capture = registration.capture.model_copy(
        update={"entries": (entry.model_copy(update={"artifact_ref": hinted}),)}
    )
    candidate = registration.model_copy(update={"capture": capture})
    assert (
        DocumentRegistration.model_validate(candidate.model_dump()).capture.original_ref
        == registration.capture.original_ref
    )


def test_document_table_grid_cannot_be_silently_omitted():
    from tychora.contracts.documents import validate_document_units

    export, original = mapping_case()
    export["texts"] = []
    export["body"]["children"] = [{"$ref": "#/tables/0"}]
    export["tables"] = [
        {
            "self_ref": "#/tables/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "label": "table",
            "prov": [],
            "data": {
                "num_rows": 1,
                "num_cols": 1,
                "table_cells": [
                    {
                        "text": "Observed value",
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 1,
                        "start_col_offset_idx": 0,
                        "end_col_offset_idx": 1,
                    }
                ],
            },
        }
    ]
    mapped = map_document(export, original)
    omitted = mapped.units[0].model_copy(
        update={
            "cells": (),
            "text": "",
            "locators": (
                mapped.units[0].locators[0].model_copy(update={"char_end": 0}),
            ),
        }
    )
    with pytest.raises(ValueError, match="grid"):
        validate_document_units(mapped.document_json, (omitted,))


def test_unsupported_normalizer_fails_before_model_preflight(execution, rich_capture):
    from pathlib import Path

    from tychora.adapters.docling.parser import DoclingParser
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.errors import TychoraError

    pack = execution.owner.path.parent / "document-pack/kinds/manual.toml"
    pack.write_text(
        pack.read_text().replace(
            'normalizer = "structured_sections@1"',
            'normalizer = "numbered_provisions@1"',
        )
    )
    config = resolve_config(execution.owner.path.parent / "workspace.toml", "test", {})
    parser = DoclingParser(
        execution.owner, execution.registration, Path("/missing-models")
    )
    with pytest.raises(TychoraError, match="normalizer is not qualified") as exc:
        parser.extract(rich_capture, config, execution.ctx)
    assert exc.value.code == "CAPABILITY_MISSING"


def test_unsupported_native_objects_remain_retained_with_diagnostic():
    export, original = mapping_case()
    export["texts"][0]["label"] = "future_annotation"
    export["texts"][0]["annotations"] = [
        {"target": "native-region", "note": "Synthetic annotation"}
    ]
    mapped = map_document(export, original)
    assert mapped.units[0].raw_item["annotations"]
    assert any(d["code"] == "UNSUPPORTED_NATIVE_ITEM" for d in mapped.diagnostics)


def test_rich_extraction_rejects_unmapped_structured_record_payload(retained_case):
    from tychora.contracts.extraction import ExtractionArtifact, extraction_document
    from tychora.contracts.identity import canonical_bytes

    artifact, _, _ = retained_case
    data = artifact.model_dump(mode="json", exclude={"raw_document_ref"})
    data["records"] = [
        {
            "native_ref": artifact.units[0].native_ref,
            "value": {"claim": "unmapped record"},
        }
    ]
    payload = extraction_document(
        {key: value for key, value in data.items() if key != "document_json"}
    )
    hashed = sha256(payload).hexdigest()
    data["raw_document_ref"] = {
        "sha256": hashed,
        "storage_key": hashed,
        "size_bytes": len(payload),
        "media_type": "application/json",
    }
    with pytest.raises(ValueError, match="document extraction domains"):
        ExtractionArtifact.model_validate_json(canonical_bytes(data))


def test_document_validation_rejects_duplicate_retained_objects():
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.documents import validate_document_units

    export, original = mapping_case()
    mapped = map_document(export, original)
    export["texts"].append(export["texts"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_document_units(FrozenJsonMap(export), mapped.units)


def test_docx_rejects_unrendered_page_metadata(retained_case):
    from tychora.contracts.extraction import ExtractionArtifact, extraction_document
    from tychora.contracts.identity import canonical_bytes

    artifact, execution, _ = retained_case
    if execution.format == "pdf":
        assert artifact.document_json["pages"]
        return
    data = artifact.model_dump(mode="json", exclude={"raw_document_ref"})
    data["document_json"]["pages"] = {
        "1": {"page_no": 1, "size": {"width": 612.0, "height": 792.0}}
    }
    raw = canonical_bytes(data["document_json"])
    raw_sha = sha256(raw).hexdigest()
    data["docling_json_ref"] = {
        "sha256": raw_sha,
        "storage_key": raw_sha,
        "size_bytes": len(raw),
        "media_type": "application/json",
    }
    root = extraction_document(
        {key: value for key, value in data.items() if key != "document_json"}
    )
    root_sha = sha256(root).hexdigest()
    data["raw_document_ref"] = {
        "sha256": root_sha,
        "storage_key": root_sha,
        "size_bytes": len(root),
        "media_type": "application/json",
    }
    with pytest.raises(ValueError, match="DOCX physical pages"):
        ExtractionArtifact.model_validate_json(canonical_bytes(data))


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("change", ["page_identity", "unknown_origin"])
def test_rehashed_replay_rejects_invalid_native_coordinate_semantics(
    retained_case, change
):
    from tychora.contracts.extraction import extraction_document
    from tychora.contracts.identity import canonical_bytes
    from tychora.ingestion.documents import replay_document

    artifact, _, _ = retained_case
    data = artifact.model_dump(mode="json", exclude={"raw_document_ref"})
    document = data.pop("document_json")
    if change == "page_identity":
        document["pages"]["2"]["page_no"] = 99
    else:
        box = document["texts"][0]["prov"][0]["bbox"]
        box.update(coord_origin="UNKNOWN", t=120.0, b=140.0)
        data["units"][0]["provenance"][0]["raw_bbox"] = box
        data["units"][0]["provenance"][0]["transform"] = "identity_top_left"
    raw = canonical_bytes(document)
    raw_sha = sha256(raw).hexdigest()
    data["docling_json_ref"] = {
        "sha256": raw_sha,
        "storage_key": raw_sha,
        "size_bytes": len(raw),
        "media_type": "application/json",
    }
    root = extraction_document(data)
    root_sha = sha256(root).hexdigest()
    root_ref = ArtifactRef(
        sha256=root_sha,
        storage_key=root_sha,
        size_bytes=len(root),
        media_type="application/json",
    )
    with pytest.raises(ValueError):
        map_document(document, artifact.original_ref)
    with pytest.raises(ValueError, match="page identity|coordinate origin"):
        replay_document(root, raw, root_ref)
