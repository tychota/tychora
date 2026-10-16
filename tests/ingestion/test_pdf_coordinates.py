"""Actual pinned SDK assembly coordinates, using only synthetic cells."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from tests.ingestion import test_docling_mapping as mapping_fixtures
from tychora.adapters.docling.mapping import map_document
from tychora.adapters.docling.ocr_lineage_capture import (
    LineageCapture,
    instrument_reading_order,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.documents import validate_document_units

ORIGINAL = ArtifactRef(
    sha256="0" * 64, storage_key="0" * 64, size_bytes=0, media_type="application/pdf"
)
document_kind = mapping_fixtures.document_kind


def sdk_document(label, segments, *, from_ocr=True, page_start=1):
    from docling.datamodel.base_models import Cluster, TextElement
    from docling.models.stages.page_assemble.page_assemble_model import (
        PageAssembleModel,
        PageAssembleOptions,
    )
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import BoundingBox, DocItemLabel, DoclingDocument, Size
    from docling_core.types.doc.page import BoundingRectangle, TextCell

    document = DoclingDocument(name="Synthetic coordinates")
    saved: dict[int, Any] = {}
    elements = []
    assembler = PageAssembleModel(PageAssembleOptions())
    for page_no, texts in enumerate(segments, page_start):
        document.add_page(page_no=page_no, size=Size(width=200, height=200))
        bbox = BoundingBox(l=10, t=10, r=100, b=50)
        cells = [
            TextCell(
                index=i,
                text=t,
                orig=t,
                from_ocr=from_ocr,
                confidence=0.9,
                rect=BoundingRectangle.from_bounding_box(bbox),
            )
            for i, t in enumerate(texts)
        ]
        saved[page_no] = {
            "cells": [
                {
                    "index": c.index,
                    "text": c.text,
                    "from_ocr": c.from_ocr,
                    "confidence": c.confidence,
                    "rect": c.rect.model_dump(mode="json"),
                }
                for c in cells
            ]
        }
        cluster = Cluster(
            id=page_no,
            label=DocItemLabel(label),
            bbox=bbox,
            confidence=0.9,
            cells=cells,
        )
        text = assembler.sanitize_text(
            [c.text.replace("\x02", "-").strip() for c in cells if c.text.strip()]
        )
        elements.append(
            TextElement(
                label=DocItemLabel(label),
                id=page_no,
                text=text,
                page_no=page_no,
                cluster=cluster,
            )
        )
    capture = LineageCapture(saved, from_ocr=from_ocr)
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    item, _ = model._handle_text_element(
        elements[0], out_doc=document, current_list=None, parent=None, page_height=200
    )
    for element in elements[1:]:
        model._merge_elements(elements[0], element, item, 200)
    raw = document.export_to_dict()
    DoclingDocument.model_validate(raw)
    return raw, saved, list(capture.records.values())


def proof(saved, records, *, from_ocr=True):
    from tychora.adapters.docling.text_coordinates import coordinate_proof

    return coordinate_proof(
        "docling-assembly@1",
        {number: SimpleNamespace(cells=page["cells"]) for number, page in saved.items()}
        if from_ocr
        else {},
        records if from_ocr else [],
        None
        if from_ocr
        else {
            "pages": [
                {"physical_page": number, **page} for number, page in saved.items()
            ],
            "lineage": records,
        },
    )


@pytest.mark.parametrize("from_ocr", [True, False])
@pytest.mark.parametrize(
    ("label", "segments", "expected"),
    [
        ("text", [["alpha"]], [(0, 5, "alpha")]),
        (
            "text",
            [["reader’s ﬁ eld", "algo-", "rithms"]],
            [(0, 25, "reader's field algorithms")],
        ),
        ("formula", [["x"]], [(0, 0, "")]),
        ("text", [["alpha"], ["beta"]], [(0, 5, "alpha"), (6, 10, "beta")]),
        ("text", [["a-"], ["b"]], [(0, 1, "a"), (1, 2, "b")]),
        ("text", [["a\u00ad"], ["B"]], [(0, 1, "a"), (1, 2, "B")]),
        ("list_item", [["1. alpha"], ["beta"]], [(0, 5, "alpha"), (6, 10, "beta")]),
        ("list_item", [["1. a-"], ["b"]], [(0, 1, "a"), (1, 2, "b")]),
        ("text", [["a-"], ["b"], ["c"]], [(0, 1, "a"), (1, 2, "b"), (3, 4, "c")]),
        ("formula", [["x"], ["y"]], [(0, 0, ""), (1, 2, "y")]),
    ],
)
def test_exact_sdk_coordinate_replay(label, segments, expected, from_ocr):
    raw, saved, records = sdk_document(label, segments, from_ocr=from_ocr)
    coordinates = proof(saved, records, from_ocr=from_ocr)
    original = deepcopy(raw)
    mapped = map_document(raw, ORIGINAL, coordinates=coordinates)
    validate_document_units(FrozenJsonMap(raw), mapped.units, coordinates=coordinates)
    unit = mapped.units[0]
    assert [
        (p.char_start, p.char_end, unit.text[p.char_start : p.char_end])
        for p in unit.locators
    ] == expected
    assert [p.physical_page for p in unit.locators] == list(range(1, len(segments) + 1))
    assert raw == original
    assert [p.charspan for p in unit.provenance] == [
        tuple(p["charspan"]) for p in raw["texts"][0]["prov"]
    ]


def test_actual_307_character_shape_and_batch_reference_remapping():
    from tychora.adapters.docling.ocr_lineage import merge_lineage
    from tychora.adapters.docling.pdf_batches import merge_pdf_page_exports

    first, s1, r1 = sdk_document("text", [["prefix"]])
    second, s2, r2 = sdk_document(
        "text", [["a" * 60, "a" * 67 + "-"], ["b" * 59] * 3], page_start=25
    )
    contexts = (proof(s1, r1), proof(s2, r2))
    exports = (FrozenJsonMap(first), FrozenJsonMap(second))
    merged = merge_pdf_page_exports(exports, coordinates=contexts)
    coordinates = proof(s1 | s2, merge_lineage(exports, (r1, r2)))
    mapped = map_document(merged, ORIGINAL, coordinates=coordinates)
    validate_document_units(merged, mapped.units, coordinates=coordinates)
    unit = mapped.units[1]
    assert len(unit.text) == 307
    assert [(p.physical_page, p.char_start, p.char_end) for p in unit.locators] == [
        (25, 0, 128),
        (26, 128, 307),
    ]
    assert unit.native_ref == "#/texts/1"


@pytest.mark.parametrize(
    "mutation",
    ["span", "page", "origin", "bbox", "parent", "reference", "cell", "field", "proof"],
)
def test_coordinate_proof_rejects_exact_mutations(mutation):
    raw, saved, records = sdk_document("list_item", [["1. a-"], ["b"], ["c"]])
    item = raw["texts"][0]
    if mutation == "span":
        item["prov"][1]["charspan"][1] -= 1
    elif mutation == "page":
        item["prov"][1]["page_no"] = 1
    elif mutation == "origin":
        item["prov"][0]["bbox"]["coord_origin"] = "UNKNOWN"
    elif mutation == "bbox":
        item["prov"][0]["bbox"]["r"] = 201
    elif mutation == "parent":
        item["parent"]["$ref"] = "#/furniture"
    elif mutation == "reference":
        records[0]["native_ref"] = "#/texts/99"
    elif mutation == "cell":
        saved[2]["cells"][0]["text"] = "x"
    elif mutation == "field":
        item["orig"] += "x"
    else:
        records[0]["segments"].reverse()
    with pytest.raises(ValueError):
        map_document(raw, ORIGINAL, coordinates=proof(saved, records))


def test_new_native_coordinates_require_native_evidence():
    raw, saved, records = sdk_document("text", [["a-"], ["b"]], from_ocr=False)
    with pytest.raises(ValueError):
        map_document(raw, ORIGINAL, coordinates=proof({}, []))
    with pytest.raises(ValueError):
        map_document(raw, ORIGINAL, coordinates=proof(saved, records))


@pytest.mark.parametrize(
    "native",
    [
        {"pages": 1, "lineage": []},
        {"pages": [{"physical_page": 1}], "lineage": []},
        {"pages": [{"physical_page": 1, "cells": [0]}], "lineage": []},
        {"pages": [], "lineage": 1},
    ],
)
@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
def test_malformed_native_proof_has_a_validation_error(native, execution, rich_capture):
    import io

    from tychora.adapters.docling.parser import DoclingParser
    from tychora.adapters.docling.text_coordinates import coordinate_proof
    from tychora.application.owner import StagedOutput
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    with pytest.raises(ValueError, match="assembly"):
        coordinate_proof("docling-assembly@1", {}, [], native)
    policy = PdfProcessingPolicy(
        mode="native",
        native_pages=(1,),
        ocr_pages=(),
        blank_pages=(),
        origin_proof="ordered-ocr-cells@1",
        text_coordinates="docling-assembly@1",
        backend_identity=FrozenJsonMap({}),
        runtime_inventory=FrozenJsonMap({}),
        model_inventory_sha256="a" * 64,
        options=FrozenJsonMap({}),
        limits=FrozenJsonMap({"max_retained_bytes": 268435456}),
    )
    accepted = []
    parser = DoclingParser(
        execution.owner,
        execution.registration,
        execution.owner.path,
        policy=policy,
        progress=accepted.extend,
    )
    raw, _, _ = sdk_document("text", [["native text"]], from_ocr=False)
    payload = canonical_bytes(
        {
            "document": raw,
            "evidence": [],
            "lineage": [],
            "native_text_evidence": native,
        }
    )
    with pytest.raises(TychoraError) as rejected:
        parser._accept_batch(
            (
                (
                    StagedOutput("batch.json", "application/json", len(payload)),
                    io.BytesIO(payload),
                ),
            ),
            (1,),
        )
    assert rejected.value.code == "INTEGRITY_ERROR"
    assert accepted == []


def test_marker_source_part_belongs_only_to_initial_segment():
    from tychora.contracts.documents import DocumentSourcePart
    from tychora.ingestion.document_parts import _part_locators

    raw, saved, records = sdk_document("list_item", [["1. alpha"], ["beta"]])
    unit = map_document(raw, ORIGINAL, coordinates=proof(saved, records)).units[0]
    marker = DocumentSourcePart(
        native_ref=unit.native_ref,
        field="marker",
        source_start=0,
        source_end=2,
        unit_start=0,
        unit_end=2,
        source_domain="docling_marker",
        provenance=unit.provenance,
    )
    locators = _part_locators(unit, marker, "synthetic", ORIGINAL.sha256)
    assert [(p.physical_page, p.char_start, p.char_end) for p in locators] == [
        (1, 0, 2)
    ]


def test_coordinate_policy_preserves_legacy_bytes_and_requires_fresh_analysis(
    monkeypatch,
):
    from tychora.application import pdf_processing
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    values = {
        "version": "pdf-processing@1",
        "mode": "native",
        "native_pages": [1],
        "ocr_pages": [],
        "blank_pages": [],
        "backend_identity": {},
        "runtime_inventory": {},
        "model_inventory_sha256": "a" * 64,
        "options": {},
        "limits": {"max_retained_bytes": 1024},
        "origin_proof": "ordered-ocr-cells@1",
    }
    legacy = PdfProcessingPolicy.model_validate_json(canonical_bytes(values))
    before = canonical_bytes(values)
    assert canonical_bytes(legacy.model_dump(mode="json")) == before
    current = PdfProcessingPolicy.model_validate_json(
        canonical_bytes({**values, "text_coordinates": "docling-assembly@1"})
    )
    assert pdf_processing.policy_sha256(current) != pdf_processing.policy_sha256(legacy)
    monkeypatch.setattr(
        pdf_processing, "resolve_pdf_policy", lambda *args, **kwargs: (current, [])
    )
    with pytest.raises(TychoraError) as rejected:
        pdf_processing.revalidate_pdf_policy(
            SimpleNamespace(pdf_intakes=SimpleNamespace(max_retained_bytes=1024)),
            None,
            None,
            None,
            legacy,
        )
    assert rejected.value.code == "SCOPE_CHANGED"
    assert canonical_bytes(legacy.model_dump(mode="json")) == before


def test_legacy_mapping_keeps_recorded_coordinates_without_new_qualification():
    raw, saved, records = sdk_document("list_item", [["1. alpha"], ["beta"]])
    legacy = map_document(raw, ORIGINAL)
    before = legacy.model_dump(mode="json")
    validate_document_units(FrozenJsonMap(raw), legacy.units)
    assert before == map_document(raw, ORIGINAL).model_dump(mode="json")
    assert [(p.char_start, p.char_end) for p in legacy.units[0].locators] == [
        (0, 5),
        (3, 7),
    ]
    current = map_document(raw, ORIGINAL, coordinates=proof(saved, records))
    assert current.units[0].locators[1].char_start == 6
    with pytest.raises(ValueError):
        validate_document_units(
            FrozenJsonMap(raw), legacy.units, coordinates=proof(saved, records)
        )
