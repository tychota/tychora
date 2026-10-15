import json
from copy import deepcopy

import pytest

from tests.ingestion.test_docling_mapping import mapping_case
from tychora.adapters.docling.mapping import map_document
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.documents import validate_document_units
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes


def test_merge_keeps_physical_pages_and_remaps_local_references():
    from tychora.adapters.docling.pdf_batches import merge_pdf_page_exports

    first, original = mapping_case()
    second = deepcopy(first)
    second["pages"] = {"20": {**second["pages"]["2"], "page_no": 20}}
    second["texts"][0]["prov"][0]["page_no"] = 20
    merged = merge_pdf_page_exports((FrozenJsonMap(first), FrozenJsonMap(second)))
    mapped = map_document(merged, original)
    validate_document_units(merged, mapped.units)
    assert [u.native_ref for u in mapped.units] == ["#/texts/0", "#/texts/1"]
    assert [u.locators[0].physical_page for u in mapped.units] == [2, 20]


@pytest.mark.parametrize("change", ["duplicate_page", "unknown_ref", "disconnected"])
def test_merge_rejects_incomplete_or_ambiguous_exports(change):
    from tychora.adapters.docling.pdf_batches import merge_pdf_page_exports

    first, _ = mapping_case()
    second = deepcopy(first)
    if change == "unknown_ref":
        second["texts"][0]["annotations"] = [{"$ref": "#/unknown/0"}]
    if change == "disconnected":
        second["body"]["children"] = []
    with pytest.raises(TychoraError) as error:
        merge_pdf_page_exports((FrozenJsonMap(first), FrozenJsonMap(second)))
    assert error.value.code == "INTEGRITY_ERROR"


def test_merge_nested_caption_and_table_cell_references():
    from tychora.adapters.docling.pdf_batches import merge_pdf_page_exports

    first, original = mapping_case()
    first["groups"] = [
        {
            "self_ref": "#/groups/0",
            "parent": {"$ref": "#/body"},
            "children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}],
            "label": "list",
        }
    ]
    first["body"]["children"] = [{"$ref": "#/groups/0"}]
    first["texts"][0]["parent"] = {"$ref": "#/groups/0"}
    first["tables"] = [
        {
            "self_ref": "#/tables/0",
            "parent": {"$ref": "#/groups/0"},
            "children": [],
            "label": "table",
            "prov": [],
            "captions": [{"$ref": "#/texts/0"}],
            "data": {
                "num_rows": 1,
                "num_cols": 1,
                "table_cells": [
                    {
                        "text": "cell",
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 1,
                        "start_col_offset_idx": 0,
                        "end_col_offset_idx": 1,
                        "provenance": {"page_no": 2, "source": {"$ref": "#/texts/0"}},
                    }
                ],
            },
        }
    ]
    second = deepcopy(first)
    second["pages"] = {"20": {**second["pages"]["2"], "page_no": 20}}
    second["texts"][0]["prov"][0]["page_no"] = 20
    second["tables"][0]["data"]["table_cells"][0]["provenance"]["page_no"] = 20
    merged = merge_pdf_page_exports((FrozenJsonMap(first), FrozenJsonMap(second)))
    retained = json.loads(canonical_bytes(merged))
    assert retained["tables"][1]["captions"][0]["$ref"] == "#/texts/1"
    assert (
        retained["tables"][1]["data"]["table_cells"][0]["provenance"]["page_no"] == 20
    )
    assert (
        retained["tables"][1]["data"]["table_cells"][0]["provenance"]["source"]["$ref"]
        == "#/texts/1"
    )
    assert retained["groups"][1]["children"][1]["$ref"] == "#/tables/1"
    validate_document_units(merged, map_document(merged, original).units)


@pytest.mark.parametrize(
    "failure",
    ["partial", "errors", "timeout", "missing", "duplicate", "export_missing"],
)
def test_sdk_success_requires_exact_processed_and_exported_coverage(failure):
    from types import SimpleNamespace

    from docling.datamodel.base_models import ConversionStatus

    from tychora.adapters.docling.pdf_batches import validate_conversion

    converted = SimpleNamespace(
        status=ConversionStatus.SUCCESS,
        errors=[],
        has_timeout_errors=lambda: False,
        pages=[SimpleNamespace(page_no=1), SimpleNamespace(page_no=2)],
        document=SimpleNamespace(pages={1: {}, 2: {}}),
    )
    if failure == "partial":
        converted.status = ConversionStatus.PARTIAL_SUCCESS
    elif failure == "errors":
        converted.errors = [object()]
    elif failure == "timeout":
        converted.has_timeout_errors = lambda: True
    elif failure == "missing":
        converted.pages.pop()
    elif failure == "duplicate":
        converted.pages[1].page_no = 1
    else:
        converted.document.pages.pop(2)
    with pytest.raises(TychoraError) as error:
        validate_conversion(converted, (1, 2))
    assert error.value.code == "INDEX_INCOMPLETE"
