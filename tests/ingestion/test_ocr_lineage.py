"""Exact OCR-to-SDK lineage; synthetic cells, never private documents."""

import importlib
from types import SimpleNamespace

import pytest


def lineage_module():
    from tychora.adapters.docling import ocr_evidence

    assert hasattr(ocr_evidence, "validate_ordered_ocr_lineage"), (
        "Explicit PDF origin needs ordered cell lineage, not global substring search"
    )
    return importlib.import_module("tychora.adapters.docling.ocr_lineage")


def evidence(texts):
    return {
        1: SimpleNamespace(
            cells=tuple(
                {"index": i, "text": text, "from_ocr": True}
                for i, text in enumerate(texts)
            )
        )
    }


@pytest.mark.parametrize(
    ("texts", "ids", "expected"),
    [
        (["left top", "right top", "left bottom"], [0, 2], "left top left bottom"),
        (["alpha", "beta"], [1, 0], "beta alpha"),
        (["algo-", "rithms"], [0, 1], "algorithms"),
        (["reader’s ﬁ eld"], [0], "reader's field"),
    ],
)
def test_ordered_cells_replay_exact_pinned_assembly(texts, ids, expected):
    module = lineage_module()
    assert (
        module.segment_text(
            {"page": 1, "cells": ids, "join": "sanitize"}, evidence(texts)
        )
        == expected
    )


def test_lineage_rejects_invented_cross_cell_substring():
    module = lineage_module()
    unit = SimpleNamespace(
        native_ref="#/texts/0",
        text_origin="ocr",
        provenance=(SimpleNamespace(physical_page=1),),
        raw_item={"text": "habe", "orig": "habe", "label": "text"},
    )
    proof = {
        "native_ref": unit.native_ref,
        "kind": "text",
        "projection": "text",
        "segments": [{"page": 1, "cells": [0, 1], "join": "sanitize"}],
    }
    with pytest.raises(ValueError, match="lineage"):
        module.validate_lineage((unit,), evidence(["alpha", "beta"]), (proof,))


def test_policy_proof_version_changes_identity_without_changing_old_serialization():
    from tychora.application.pdf_processing import policy_sha256
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    values = {
        "mode": "local_ocr",
        "native_pages": (),
        "ocr_pages": (1,),
        "blank_pages": (),
        "backend_identity": {},
        "runtime_inventory": {},
        "model_inventory_sha256": "a" * 64,
        "options": {},
        "limits": {},
    }
    old = PdfProcessingPolicy(**values)
    assert "origin_proof" not in old.model_dump(mode="json")
    new = PdfProcessingPolicy(**values, origin_proof="ordered-ocr-cells@1")
    assert policy_sha256(new) != policy_sha256(old)


def test_old_captured_policy_is_not_silently_upgraded_on_retry(monkeypatch):
    from tychora.application import pdf_processing
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    old = PdfProcessingPolicy(
        mode="native",
        native_pages=(1,),
        ocr_pages=(),
        blank_pages=(),
        backend_identity={},
        runtime_inventory={},
        model_inventory_sha256="a" * 64,
        options={},
        limits={"max_retained_bytes": 1024},
    )
    before = canonical_bytes(old.model_dump(mode="json"))
    current = old.model_copy(update={"origin_proof": "ordered-ocr-cells@1"})
    monkeypatch.setattr(
        pdf_processing, "resolve_pdf_policy", lambda *args, **kwargs: (current, [])
    )
    with pytest.raises(TychoraError) as error:
        pdf_processing.revalidate_pdf_policy(
            SimpleNamespace(pdf_intakes=SimpleNamespace(max_retained_bytes=1024)),
            None,
            None,
            None,
            old,
        )
    assert error.value.code == "SCOPE_CHANGED"
    assert canonical_bytes(old.model_dump(mode="json")) == before


def sdk_case(texts):
    pytest.importorskip("docling")
    from docling.datamodel.base_models import Cluster, TextElement
    from docling_core.types.doc import BoundingBox, DocItemLabel, DoclingDocument, Size
    from docling_core.types.doc.page import BoundingRectangle, TextCell

    from tychora.adapters.docling.ocr_lineage_capture import LineageCapture

    bbox = BoundingBox(l=0, t=0, r=100, b=100)
    cells = [
        TextCell(
            index=i,
            text=text,
            orig=text,
            from_ocr=True,
            confidence=0.9,
            rect=BoundingRectangle.from_bounding_box(bbox),
        )
        for i, text in enumerate(texts)
    ]
    saved = [
        {
            "index": c.index,
            "text": c.text,
            "from_ocr": c.from_ocr,
            "confidence": c.confidence,
            "rect": c.rect.model_dump(mode="json"),
        }
        for c in cells
    ]
    capture = LineageCapture({1: {"cells": saved}})
    doc = DoclingDocument(name="Synthetic")
    doc.add_page(page_no=1, size=Size(width=200, height=200))

    def element(ids, label=DocItemLabel.TEXT, cluster_id=0):
        from docling.models.stages.page_assemble.page_assemble_model import (
            PageAssembleModel,
            PageAssembleOptions,
        )

        selected = [cells[i] for i in ids]
        cluster = Cluster(
            id=cluster_id, label=label, bbox=bbox, confidence=0.9, cells=selected
        )
        text = PageAssembleModel(PageAssembleOptions()).sanitize_text(
            [c.text.replace("\x02", "-").strip() for c in selected if c.text.strip()]
        )
        return TextElement(
            label=label, id=cluster_id, text=text, page_no=1, cluster=cluster
        )

    return capture, doc, cells, element


@pytest.mark.parametrize(
    "label",
    ["text", "list_item", "formula", "code", "caption", "footnote", "section_header"],
)
def test_real_sdk_emission_binds_exact_cells_and_fields(label):
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel

    from tychora.adapters.docling.ocr_lineage import validate_lineage
    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, cells, element = sdk_case(
        ["1. reader’s algo-", "interruption", "rithms ﬁ eld"]
    )
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    elem = element([0, 2], DocItemLabel(label))
    if label == "code":
        item = model._add_code_element(elem, doc)
    elif label in ("caption", "footnote"):
        item = model._add_caption_or_footnote(elem, doc, None, 200)
    else:
        item, _ = model._handle_text_element(
            elem, out_doc=doc, current_list=None, parent=None, page_height=200
        )
    assert capture.records[item.self_ref]["segments"][0]["cells"] == [0, 2]
    unit = SimpleNamespace(
        native_ref=item.self_ref,
        text_origin="ocr",
        provenance=(SimpleNamespace(physical_page=1),),
        raw_item=item.model_dump(mode="json"),
    )
    validate_lineage((unit,), capture.evidence, tuple(capture.records.values()))
    assert [c.text for c in cells] == [
        "1. reader’s algo-",
        "interruption",
        "rithms ﬁ eld",
    ]


@pytest.mark.parametrize(
    ("texts", "marker", "enumerated", "text", "original"),
    [
        (["Neutral sentence"], "", False, "Neutral sentence", "Neutral sentence"),
        (["-"], "", False, "-", "-"),
        (["1."], "", False, "1.", "1."),
        (["• Neutral bullet"], "·", False, "Neutral bullet", "· Neutral bullet"),
        (["1. Neutral number"], "1.", True, "Neutral number", "1. Neutral number"),
        (
            ["1.2 Neutral outline"],
            "1.2",
            True,
            "Neutral outline",
            "1.2 Neutral outline",
        ),
        (
            ["Neutral first\nNeutral second"],
            "",
            False,
            "Neutral first\nNeutral second",
            "Neutral first\nNeutral second",
        ),
        (
            ["- Neutral first\nNeutral second"],
            "-",
            False,
            "Neutral first\nNeutral second",
            "- Neutral first\nNeutral second",
        ),
        (
            ["2. Neutral first\nNeutral second"],
            "2.",
            True,
            "Neutral first\nNeutral second",
            "2. Neutral first\nNeutral second",
        ),
        (
            ["Neutral first", "Neutral second"],
            "",
            False,
            "Neutral first Neutral second",
            "Neutral first Neutral second",
        ),
    ],
)
def test_actual_list_emission_replays_exact_fields_and_preserves_cells(
    texts, marker, enumerated, text, original
):
    from copy import deepcopy

    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel

    from tychora.adapters.docling.ocr_lineage import text_fields, validate_lineage
    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, cells, element = sdk_case(texts)
    before = [cell.model_dump(mode="json") for cell in cells]
    saved = deepcopy(capture.pages)
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    item, _ = model._handle_text_element(
        element(list(range(len(cells))), DocItemLabel.LIST_ITEM),
        out_doc=doc,
        current_list=None,
        parent=None,
        page_height=200,
    )
    expected = {
        "marker": marker,
        "enumerated": enumerated,
        "text": text,
        "orig": original,
    }
    assert {key: getattr(item, key) for key in expected} == expected
    assert text_fields(capture.records[item.self_ref], capture.evidence) == expected
    unit = SimpleNamespace(
        native_ref=item.self_ref,
        text_origin="ocr",
        provenance=(SimpleNamespace(physical_page=1),),
        raw_item=item.model_dump(mode="json"),
    )
    validate_lineage((unit,), capture.evidence, tuple(capture.records.values()))
    assert [cell.model_dump(mode="json") for cell in cells] == before
    assert capture.pages == saved


@pytest.mark.parametrize("text", ["Neutral sentence", "1. Neutral sentence"])
@pytest.mark.parametrize("field", ["marker", "enumerated", "text", "orig"])
def test_list_lineage_rejects_altered_emitted_fields(text, field):
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel

    from tychora.adapters.docling.ocr_lineage import validate_lineage
    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, _, element = sdk_case([text])
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    item, _ = model._handle_text_element(
        element([0], DocItemLabel.LIST_ITEM),
        out_doc=doc,
        current_list=None,
        parent=None,
        page_height=200,
    )
    unit = SimpleNamespace(
        native_ref=item.self_ref,
        text_origin="ocr",
        provenance=(SimpleNamespace(physical_page=1),),
        raw_item=item.model_dump(mode="json"),
    )
    records = tuple(capture.records.values())
    validate_lineage((unit,), capture.evidence, records)
    unit.raw_item[field] = (
        not unit.raw_item[field] if field == "enumerated" else "substitution"
    )
    with pytest.raises(ValueError, match="lineage"):
        validate_lineage((unit,), capture.evidence, records)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("algo-", "rithms", "algorithms"),
        ("algo\u00ad", "Rithms", "algoRithms"),
        ("left-", "Upper", "left- Upper"),
    ],
)
def test_actual_sdk_reordered_cluster_merge_and_distinct_hyphen_rule(
    left, right, expected
):
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )

    from tychora.adapters.docling.ocr_lineage import text_fields
    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, _, element = sdk_case([right, left])
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    first, second = element([1]), element([0], cluster_id=1)
    item, _ = model._handle_text_element(
        first, out_doc=doc, current_list=None, parent=None, page_height=200
    )
    model._merge_elements(first, second, item, 200)
    record = capture.records[item.self_ref]
    assert [s["cells"] for s in record["segments"]] == [[1], [0]]
    assert item.text == expected == text_fields(record, capture.evidence)["text"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "page",
        "cell",
        "extra",
        "recipe",
        "projection",
        "text",
        "orig",
        "duplicate",
    ],
)
def test_exact_lineage_tampering_fails(mutation):
    from copy import deepcopy

    from tychora.adapters.docling.ocr_lineage import validate_lineage

    unit = SimpleNamespace(
        native_ref="#/texts/0",
        text_origin="ocr",
        provenance=(SimpleNamespace(physical_page=1),),
        raw_item={"text": "alpha beta", "orig": "alpha beta", "label": "text"},
    )
    record = {
        "native_ref": unit.native_ref,
        "kind": "text",
        "projection": "text",
        "segments": [{"page": 1, "cells": [0, 1], "join": "sanitize"}],
    }
    records = [deepcopy(record)]
    if mutation == "missing":
        records = []
    elif mutation == "page":
        records[0]["segments"][0]["page"] = 2
    elif mutation == "cell":
        records[0]["segments"][0]["cells"] = [0, 9]
    elif mutation == "extra":
        records.append({**record, "native_ref": "#/texts/1"})
    elif mutation == "recipe":
        records[0]["segments"][0]["join"] = "fuzzy"
    elif mutation == "projection":
        records[0]["projection"] = "formula"
    elif mutation in ("text", "orig"):
        unit.raw_item[mutation] = "invented"
    elif mutation == "duplicate":
        records.append(deepcopy(record))
    with pytest.raises(ValueError, match="lineage"):
        validate_lineage((unit,), evidence(["alpha", "beta"]), records)


def test_real_table_token_assignment_keeps_duplicate_text_ids(monkeypatch):
    from docling_core.types.doc import TableCell
    from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor

    from tychora.adapters.docling.ocr_lineage_capture import LineageTableStage

    capture, _, _, _ = sdk_case(["same", "unrelated", "same"])
    responses = [
        {
            "cell_id": index,
            "bbox": {"l": 0, "t": 0, "r": 20, "b": 20},
            "row_span": 1,
            "col_span": 1,
            "start_row_offset_idx": 0,
            "end_row_offset_idx": 1,
            "start_col_offset_idx": 0,
            "end_col_offset_idx": 1,
            "indentation_level": 0,
            "column_header": False,
            "row_header": False,
            "row_section": False,
        }
        for index in (0, 2)
    ]
    tokens = [
        {"id": i, "text": text, "bbox": [0, 0, 20, 20]}
        for i, text in enumerate(["same", "unrelated", "same"])
    ]
    predictor = object.__new__(TFPredictor)
    calls = []

    class TableModel:
        tf_predictor = predictor

        def __call__(self, result, pages):
            for page in pages:
                calls.append(page.page_no)
                outputs = self.tf_predictor._merge_tf_output(responses, tokens)
                table = SimpleNamespace(
                    cluster=SimpleNamespace(id=7),
                    table_cells=[TableCell.model_validate(out) for out in outputs],
                )
                page.predictions.tablestructure = SimpleNamespace(table_map={7: table})
                yield page

    stage = LineageTableStage(TableModel(), capture)
    page = SimpleNamespace(page_no=1, predictions=SimpleNamespace())
    assert list(stage(None, [page])) == [page]
    assert calls == [1]
    assert (
        page.predictions.tablestructure.table_map[7].table_cells[0].text == "same same"
    )
    assert capture.tables[(1, 7)] == [{"page": 1, "cells": [0, 2], "join": "table"}]
    tokens[0]["text"] = "substitution"
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError) as error:
        list(stage(None, [page]))
    assert error.value.code == "INTEGRITY_ERROR"


def test_lineage_merge_remaps_only_output_refs_not_physical_cells():
    from tychora.adapters.docling.ocr_lineage import merge_lineage

    exports = [{"texts": [{"self_ref": "#/texts/0"}]}] * 2
    proofs = [
        [
            {
                "native_ref": "#/texts/0",
                "kind": "text",
                "projection": "text",
                "segments": [{"page": page, "cells": [0], "join": "sanitize"}],
            }
        ]
        for page in (1, 17)
    ]
    merged = merge_lineage(exports, proofs)
    assert [record["native_ref"] for record in merged] == ["#/texts/0", "#/texts/1"]
    assert [record["segments"][0]["page"] for record in merged] == [1, 17]


@pytest.mark.parametrize(
    "native_ref",
    [
        None,
        17,
        True,
        1.5,
        [],
        {},
        "",
        "#/texts",
        "#/unknown/0",
        "#/texts/-1",
        "#/texts/00",
        "#/texts/9",
    ],
)
def test_parent_pdf_finish_rejects_malformed_lineage_ref_as_integrity_error(native_ref):
    from tychora.adapters.docling.parser import DoclingParser
    from tychora.contracts.errors import TychoraError

    # Exercise the real parent boundary before any owner/storage/model is needed.
    parser = object.__new__(DoclingParser)
    parser.policy = SimpleNamespace(origin_proof="ordered-ocr-cells@1")
    parser._exports = [{"texts": [{"self_ref": "#/texts/0"}]}]
    parser._lineages = [[{"native_ref": native_ref}]]
    parser._lineage = None
    with pytest.raises(TychoraError) as error:
        parser._finish_pdf(None, None, None, None, None)
    assert error.value.code == "INTEGRITY_ERROR"
    assert parser._lineage is None


def test_legacy_evidence_list_is_unchanged_and_new_envelope_is_versioned():
    from tychora.adapters.docling.ocr_lineage import evidence_document

    page = SimpleNamespace(model_dump=lambda **kwargs: {"transcript": "original"})
    assert evidence_document((page,), None, None) == [{"transcript": "original"}]
    assert evidence_document((page,), (), "ordered-ocr-cells@1") == {
        "version": "ordered-ocr-cells@1",
        "pages": [{"transcript": "original"}],
        "lineage": (),
    }


def test_actual_picture_child_uses_plain_join_not_page_sanitizer():
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel

    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, _, element = sdk_case(["child’s", "algo-", "rithms"])
    parent = element([], DocItemLabel.PICTURE)
    parent.cluster.children = [element([0, 1, 2]).cluster]
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    picture = doc.add_picture()
    model._add_child_elements(parent, picture, doc)
    assert doc.texts[0].text == "child’s algo- rithms"
    assert capture.records[doc.texts[0].self_ref]["segments"][0]["join"] == "plain"


def test_pinned_normalizations_match_actual_sdk_without_deleting_literal_hyphen():
    from docling.models.stages.page_assemble.page_assemble_model import (
        PageAssembleModel,
        PageAssembleOptions,
    )

    from tychora.adapters.docling.ocr_lineage import sanitize_text

    model = PageAssembleModel(PageAssembleOptions())
    for lines in (
        ["literal -", "token"],
        ["’‘“”⁄• ﬀ ﬁ eld ﬂ ﬃ ﬄ ﬅ ﬆ Ĳ is ĳ is x\uf0a0 y"],
        ["x-", "!"],
        [],
        ["one"],
    ):
        assert sanitize_text(lines) == model.sanitize_text(list(lines))


def test_lineage_budget_rejects_excess_records(monkeypatch):
    from tychora.adapters.docling import ocr_lineage

    monkeypatch.setattr(ocr_lineage, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="lineage"):
        ocr_lineage.validate_lineage((), {}, ({}, {}))


def test_actual_rich_table_fallback_binds_empty_cell_and_child_text():
    from docling.datamodel.base_models import Table
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel

    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, _, element = sdk_case(["reader’s child"])
    cluster = element([], DocItemLabel.TABLE).cluster
    cluster.children = [element([0]).cluster]
    table = Table(
        label=DocItemLabel.TABLE,
        id=0,
        page_no=1,
        cluster=cluster,
        text="",
        otsl_seq=[],
        table_cells=[],
    )
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    item = model._add_table_element(table, doc)
    model._add_table_children(table, doc, item)
    assert item.data.table_cells[0].text == ""
    assert capture.records[item.self_ref]["cells"] == [
        {"page": 1, "cells": [], "join": "table"}
    ]
    assert doc.texts[0].text == "reader’s child"
    assert capture.records[doc.texts[0].self_ref]["segments"][0]["cells"] == [0]


def test_actual_rich_table_picture_keeps_table_token_lineage():
    from docling.datamodel.base_models import FigureElement
    from docling.models.stages.reading_order.readingorder_model import (
        ReadingOrderModel,
        ReadingOrderOptions,
    )
    from docling_core.types.doc import DocItemLabel, TableCell, TableData

    from tychora.adapters.docling.ocr_lineage_capture import instrument_reading_order

    capture, doc, _, element = sdk_case(["table text"])
    cluster = element([], DocItemLabel.PICTURE).cluster
    picture = FigureElement(
        label=DocItemLabel.PICTURE, id=1, page_no=1, cluster=cluster, text="", data=None
    )
    cell = TableCell(
        start_row_offset_idx=0,
        end_row_offset_idx=1,
        start_col_offset_idx=0,
        end_col_offset_idx=1,
        text="table text",
    )
    item = doc.add_table(data=TableData(num_rows=1, num_cols=1, table_cells=[cell]))
    segment = {"page": 1, "cells": [0], "join": "table"}
    capture.records[item.self_ref] = {
        "native_ref": item.self_ref,
        "kind": "table",
        "cells": [segment],
    }
    model = instrument_reading_order(ReadingOrderModel(ReadingOrderOptions()), capture)
    model._add_rich_table_pictures(
        table_item=item, pictures_by_cell={0: [picture]}, doc=doc, page_height=200
    )
    assert doc.texts[0].text == "table text"
    assert capture.records[doc.texts[0].self_ref]["segments"] == [segment]
