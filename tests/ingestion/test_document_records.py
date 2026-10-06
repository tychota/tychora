"""Pure semantic source-part and grammar regression tests."""

from copy import deepcopy
from hashlib import sha256

import pytest

from tests.qualification import test_docling_records as qualification
from tychora.adapters.docling.mapping import map_document
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.extraction import ExtractionArtifact, extraction_document
from tychora.contracts.identity import canonical_bytes
from tychora.ingestion.normalize import normalize

document_record_case = qualification.document_record_case


@pytest.fixture(scope="module")
def source_case(document_record_case):
    return document_record_case("standard@1", "docx")


def export_case(source_case, paragraphs):
    export = {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "name": "Synthetic",
        "body": {"self_ref": "#/body", "children": []},
        "furniture": {"self_ref": "#/furniture", "children": []},
        "texts": [],
        "groups": [],
        "tables": [],
        "pictures": [],
        "pages": {},
    }
    for index, (level, text) in enumerate(paragraphs):
        ref = f"#/texts/{index}"
        export["body"]["children"].append({"$ref": ref})
        export["texts"].append(
            {
                "self_ref": ref,
                "parent": {"$ref": "#/body"},
                "children": [],
                "label": "section_header" if level else "text",
                **({"level": level} if level else {}),
                "text": text,
                "orig": text,
                "prov": [],
            }
        )
    return export


def artifact_from_export(source_case, export):
    parsed = source_case.parsed
    mapped = map_document(export, parsed.original_ref)
    data = parsed.model_dump(mode="json", exclude={"raw_document_ref"})
    raw = canonical_bytes(export)
    sha = sha256(raw).hexdigest()
    data.update(
        document_json=export,
        units=[u.model_dump(mode="json") for u in mapped.units],
        assets=[],
        docling_json_ref={
            "sha256": sha,
            "storage_key": sha,
            "size_bytes": len(raw),
            "media_type": "application/json",
        },
    )
    return rebind(data)


def rebind(data):
    root = extraction_document({k: v for k, v in data.items() if k != "document_json"})
    sha = sha256(root).hexdigest()
    return ExtractionArtifact.model_validate_json(
        canonical_bytes(
            {
                **data,
                "raw_document_ref": {
                    "sha256": sha,
                    "storage_key": sha,
                    "size_bytes": len(root),
                    "media_type": "application/json",
                },
            }
        )
    )


def normalized(source_case, export):
    parsed = artifact_from_export(source_case, export)
    return normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    )


def test_empty_original_text_retains_its_source_without_empty_index_parts(source_case):
    bundle = normalized(source_case, export_case(source_case, [(0, "")]))
    unit = next(
        unit
        for unit in bundle.units
        if unit.attributes.get("document_representation") == "docling-source-parts@1"
    )
    assert unit.text == ""
    assert tuple(unit.attributes["source_refs"]) == ("#/texts/0",)
    assert tuple(unit.attributes["source_parts"]) == ()


def test_empty_table_cell_adds_only_structure_not_indexed_cell_text(source_case):
    from tychora.ingestion.document_parts import _Budget, represent_document_parts

    item = source_case.parsed.units[0].model_copy(
        update={
            "unit_type": "table",
            "raw_item": FrozenJsonMap(
                {
                    "data": {
                        "table_cells": [
                            {
                                "text": "",
                                "start_row_offset_idx": 0,
                                "start_col_offset_idx": 0,
                            },
                            {
                                "text": "Value",
                                "start_row_offset_idx": 0,
                                "start_col_offset_idx": 1,
                            },
                        ]
                    }
                }
            ),
        }
    )
    text, parts, separators, _ = represent_document_parts(
        (item,),
        "revision",
        "extraction",
        "source.docx",
        _Budget(),
        source_case.parsed.original_ref.sha256,
    )
    assert text == "\tValue"
    assert [
        (part.cell_index, text[part.unit_start : part.unit_end]) for part in parts
    ] == [(1, "Value")]
    assert [(separator.unit_start, separator.unit_end) for separator in separators] == [
        (0, 1)
    ]


def test_field_split_across_items_preserves_literal_markup(source_case):
    export = export_case(
        source_case,
        [
            (1, "STD-8 Sample"),
            (0, "Requirement:"),
            (0, "<source>Keep **literal** text.</source>"),
            (0, "Expected evidence: <b>source evidence</b>"),
        ],
    )
    record = normalized(source_case, export).records[0].data_json
    assert record["requirement"] == "<source>Keep **literal** text.</source>"
    assert record["proof"] == "<b>source evidence</b>"


def test_retained_internal_line_boundaries_are_literal(source_case):
    export = export_case(
        source_case,
        [
            (1, "STD-8 Sample"),
            (
                0,
                "Requirement: Keep.\r\n\r\nKeep uncertainty.\r\nExpected evidence: Original.\nControl: Inspect.",
            ),
        ],
    )
    value = normalized(source_case, export).records[0].data_json
    assert value["requirement"] == "Keep.\r\n\r\nKeep uncertainty."
    assert value["proof"] == "Original."
    assert value["control"] == "Inspect."


def test_semantic_expansion_work_limit_is_inclusive(source_case, monkeypatch):
    from tychora.contracts.errors import TychoraError
    from tychora.ingestion import document_parts

    export = export_case(source_case, [(1, "STD-8 Sample"), (0, "Requirement: Keep.")])
    monkeypatch.setattr(document_parts, "MAX_SEMANTIC_WORK", 10)
    assert len(normalized(source_case, export).records) == 1
    monkeypatch.setattr(document_parts, "MAX_SEMANTIC_WORK", 9)
    with pytest.raises(TychoraError, match="SEMANTIC_EXPANSION_LIMIT") as exc:
        normalized(source_case, export)
    assert exc.value.code == "INVALID_REQUEST"


def test_semantic_expansion_counts_actual_canonical_bytes(source_case, monkeypatch):
    from tychora.contracts.errors import TychoraError
    from tychora.ingestion import document_parts

    export = export_case(source_case, [(1, "STD-8 Sample"), (0, "Requirement: Keep.")])
    bundle = normalized(source_case, export)
    size = len(canonical_bytes(bundle.model_dump(mode="json")))
    monkeypatch.setattr(document_parts, "MAX_SEMANTIC_BYTES", size)
    assert normalized(source_case, export) == bundle
    monkeypatch.setattr(document_parts, "MAX_SEMANTIC_BYTES", size - 1)
    with pytest.raises(TychoraError, match="SEMANTIC_EXPANSION_LIMIT"):
        normalized(source_case, export)


def test_semantic_expansion_bounds_lines_within_one_sdk_item(source_case, monkeypatch):
    from tychora.contracts.errors import TychoraError
    from tychora.ingestion import document_parts

    export = export_case(
        source_case,
        [(1, "STD-8 Sample"), (0, "Requirement: Keep.\n" + "Continuation.\n" * 20)],
    )
    monkeypatch.setattr(document_parts, "MAX_SEMANTIC_WORK", 20)
    with pytest.raises(TychoraError, match="SEMANTIC_EXPANSION_LIMIT"):
        normalized(source_case, export)


@pytest.mark.parametrize(
    "change",
    ["missing_marker", "missing_owners", "unknown_owner", "missing_source_part"],
)
def test_semantic_representation_requires_complete_ownership(source_case, change):
    from tychora.ingestion.document_parts import validate_document_ownership

    unit = source_case.bundle.units[0]
    attrs = dict(unit.attributes)
    if change == "missing_marker":
        attrs.pop("document_representation", None)
    elif change == "missing_owners":
        attrs.pop("overlapping_owner_ids")
    elif change == "unknown_owner":
        attrs["overlapping_owner_ids"] = [unit.unit_id, "forged-owner"]
    else:
        attrs["source_parts"] = attrs["source_parts"][:-1]
    with pytest.raises(ValueError):
        validate_document_ownership(
            (unit.model_copy(update={"attributes": FrozenJsonMap(attrs)}),),
            source_case.parsed,
        )


def test_forged_group_mapping_cannot_leave_normalization(source_case, monkeypatch):
    from tychora.ingestion import document_records

    real = document_records.represent_document_parts

    def corrupt(*args, **kwargs):
        text, parts, separators, locators = real(*args, **kwargs)
        return (
            text,
            (parts[0].model_copy(update={"source_domain": "forged"}), *parts[1:]),
            separators,
            locators,
        )

    monkeypatch.setattr(document_records, "represent_document_parts", corrupt)
    with pytest.raises(ValueError, match="domain"):
        normalize(
            source_case.parsed,
            source_case.bundle.revision.revision_id,
            source_case.registry,
        )


def test_numbered_sdk_children_keep_nearest_canonical_parent(document_record_case):
    result = document_record_case("law@1", "docx")
    export = export_case(
        result,
        [(1, "Article 4"), (0, "1. Parent sample."), (0, "2. Child observation.")],
    )
    export["body"]["children"].pop()
    export["texts"][1]["children"] = [{"$ref": "#/texts/2"}]
    export["texts"][2]["parent"] = {"$ref": "#/texts/1"}
    bundle = normalized(result, export)
    article, parent, child = bundle.units
    assert child.parent_id == parent.unit_id
    assert parent.parent_id == article.unit_id
    assert parent.text == "1. Parent sample.\n\n2. Child observation."
    assert set(child.attributes["overlapping_owner_ids"]) == {
        article.unit_id,
        parent.unit_id,
        child.unit_id,
    }


@pytest.mark.parametrize(
    "paragraphs",
    [
        [
            (1, "STD-8 Sample"),
            (0, "Requirement: Keep."),
            (0, "Requirement: Different."),
        ],
        [(1, "STD-8 Sample"), (0, "Control: Inspect.")],
        [
            (1, "STD-8 Sample"),
            (0, "Requirement: Keep."),
            (0, "Unknown field: explicit boundary"),
        ],
        [
            (1, "STD-8 Sample"),
            (0, "Requirement: Keep. Expected evidence: merged label"),
        ],
    ],
)
def test_ambiguous_or_missing_fields_have_explicit_diagnostic(source_case, paragraphs):
    bundle = normalized(source_case, export_case(source_case, paragraphs))
    assert not bundle.records
    assert any(
        d["code"] == "ambiguous_record"
        for u in bundle.units
        for d in u.attributes["semantic_diagnostics"]
    )


def test_sibling_headings_never_supply_missing_record_fields(source_case):
    bundle = normalized(
        source_case,
        export_case(
            source_case,
            [
                (1, "STD-8 Sample"),
                (0, "Requirement: Keep."),
                (1, "Unrelated"),
                (0, "Expected evidence: Elsewhere."),
            ],
        ),
    )
    assert bundle.records[0].data_json["proof"] is None
    record = next(u for u in bundle.units if u.unit_id == bundle.records[0].unit_id)
    assert "Elsewhere" not in record.text


def test_unsupported_custom_heading_grammar_is_not_a_silent_fallback(source_case):
    bundle = normalized(
        source_case,
        export_case(source_case, [(1, "Rule eight"), (0, "Requirement: Keep.")]),
    )
    assert not bundle.records
    assert any(
        d["code"] == "unsupported_record_structure"
        for u in bundle.units
        for d in u.attributes["semantic_diagnostics"]
    )


def test_foundations_resolve_only_unique_original_identifiers(source_case):
    paragraphs = [
        (1, "STD-1 Sample"),
        (0, "Requirement: Keep."),
        (0, "Foundation: STD-2, STD-3, EXT-8"),
        (1, "STD-2 One"),
        (0, "Requirement: First."),
        (1, "STD-2 Duplicate"),
        (0, "Requirement: Second."),
        (1, "STD-3 Unique"),
        (0, "Requirement: Third."),
    ]
    bundle = normalized(source_case, export_case(source_case, paragraphs))
    first = bundle.records[0].data_json
    assert first["foundation_refs"] == ("STD-2", "STD-3", "EXT-8")
    assert first["foundation_ids"] == (bundle.records[-1].unit_id,)
    diagnostics = [
        d["code"] for u in bundle.units for d in u.attributes["semantic_diagnostics"]
    ]
    assert (
        "ambiguous_foundation" in diagnostics and "unresolved_foundation" in diagnostics
    )
    assert "duplicate_identifier" in diagnostics


@pytest.mark.parametrize("change", ["text", "heading", "provenance"])
def test_forged_parsed_source_facts_are_rejected_even_with_rehashed_root(
    source_case, change
):
    data = source_case.parsed.model_dump(mode="json", exclude={"raw_document_ref"})
    if change == "text":
        data["units"][1]["text"] = "substituted source"
    elif change == "heading":
        data["units"][0]["heading_path"] = ["forged context"]
    else:
        data["units"][0]["locators"][0]["physical_page"] = 99
    count = source_case.owner.run_blocking_sync(
        lambda: source_case.owner.db.execute(
            "SELECT COUNT(*) FROM extractions"
        ).fetchone()[0],
        source_case.ctx,
    )
    with pytest.raises(ValueError):
        rebind(data)
    assert (
        source_case.owner.run_blocking_sync(
            lambda: source_case.owner.db.execute(
                "SELECT COUNT(*) FROM extractions"
            ).fetchone()[0],
            source_case.ctx,
        )
        == count
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing_ref",
        "duplicate",
        "conflicting",
        "reversed",
        "out_of_range",
        "mismatch",
        "missing_part",
        "separator_source",
        "wrong_domain",
        "wrong_provenance",
        "wrong_ancestry",
    ],
)
def test_group_source_part_validation_rejects_forgery(source_case, change):
    from tychora.ingestion.document_parts import validate_document_source_parts

    unit = source_case.bundle.units[0]
    attrs = deepcopy(
        unit.attributes.model_dump()
        if hasattr(unit.attributes, "model_dump")
        else dict(unit.attributes)
    )
    # Convert frozen containers into detached mutable JSON facts.
    import json

    attrs = json.loads(canonical_bytes(attrs))
    part = attrs["source_parts"][0]
    if change == "missing_ref":
        part["native_ref"] = "#/texts/999"
    elif change == "duplicate":
        attrs["source_parts"].append(dict(part))
    elif change == "conflicting":
        attrs["source_parts"][1]["unit_start"] = 0
    elif change == "reversed":
        part["source_start"] = part["source_end"] + 1
    elif change == "out_of_range":
        part["source_end"] = part["unit_end"] = 9999
    elif change == "mismatch":
        part["source_start"] = 1
        part["source_end"] += 1
    elif change == "missing_part":
        attrs["source_parts"].pop()
    elif change == "separator_source":
        part["unit_end"] += 2
        part["source_end"] += 2
    elif change == "wrong_domain":
        part["source_domain"] = "forged-domain"
    elif change == "wrong_provenance":
        part["provenance"] = [{"invented": True}]
    elif change == "wrong_ancestry":
        attrs["source_refs"] = list(reversed(attrs["source_refs"]))
    with pytest.raises(ValueError):
        validate_document_source_parts(
            unit.model_copy(update={"attributes": FrozenJsonMap(attrs)}),
            source_case.parsed,
        )


def test_table_cells_and_separate_list_markers_have_original_domains(source_case):
    export = export_case(
        source_case, [(1, "STD-8 Sample"), (0, "Requirement: Preserve this table.")]
    )
    export["tables"] = [
        {
            "self_ref": "#/tables/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "label": "table",
            "prov": [],
            "data": {
                "num_rows": 1,
                "num_cols": 2,
                "table_cells": [
                    {
                        "text": "Label",
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 1,
                        "start_col_offset_idx": 0,
                        "end_col_offset_idx": 1,
                    },
                    {
                        "text": "Exact value",
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 1,
                        "start_col_offset_idx": 1,
                        "end_col_offset_idx": 2,
                    },
                ],
            },
        }
    ]
    export["body"]["children"].append({"$ref": "#/tables/0"})
    export["texts"].append(
        {
            "self_ref": "#/texts/2",
            "parent": {"$ref": "#/body"},
            "children": [],
            "label": "list_item",
            "text": "Keep marker.",
            "orig": "Keep marker.",
            "marker": "7.",
            "enumerated": True,
            "prov": [],
        }
    )
    export["body"]["children"].append({"$ref": "#/texts/2"})
    bundle = normalized(source_case, export)
    unit = bundle.units[0]
    assert "Label\tExact value" in unit.text and "7. Keep marker." in unit.text
    assert "7. Keep marker." in bundle.records[0].data_json["requirement"]
    assert [
        (p["field"], p["cell_index"])
        for p in unit.attributes["source_parts"]
        if p["native_ref"] == "#/tables/0"
    ] == [("table_cell", 0), ("table_cell", 1)]
    assert any(p["field"] == "marker" for p in unit.attributes["source_parts"])


@pytest.mark.parametrize(
    "span,want", [([0, 21], (0, 18)), ([3, 9], (0, 6)), ([0, 2], None)]
)
def test_list_provenance_uses_proven_original_domain(source_case, span, want):
    from tychora.contracts.documents import validate_document_units

    export = export_case(source_case, [(0, "Record the sample.")])
    export["texts"][0].update(
        label="list_item",
        marker="1.",
        orig="1. Record the sample.",
        prov=[
            {
                "page_no": 1,
                "charspan": span,
                "bbox": {
                    "l": 1.0,
                    "t": 10.0,
                    "r": 80.0,
                    "b": 20.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    )
    export["pages"] = {"1": {"page_no": 1, "size": {"width": 100.0, "height": 100.0}}}
    mapped = map_document(export, source_case.parsed.original_ref)
    unit = mapped.units[0]
    assert unit.text == "Record the sample."
    assert unit.provenance[0].char_domain == "docling_orig_text_item"
    assert unit.provenance[0].charspan == tuple(span)
    assert [(l.char_start, l.char_end) for l in unit.locators] == (
        [] if want is None else [want]
    )
    validate_document_units(mapped.document_json, mapped.units)


@pytest.mark.parametrize("change", ["orig", "marker", "text", "domain", "offset"])
def test_retained_list_original_domain_rejects_substitution(source_case, change):
    from tychora.contracts.documents import validate_document_units

    export = export_case(source_case, [(0, "Record the sample.")])
    export["texts"][0].update(
        label="list_item",
        marker="1.",
        orig="1. Record the sample.",
        prov=[
            {
                "page_no": 1,
                "charspan": [0, 21],
                "bbox": {
                    "l": 1.0,
                    "t": 10.0,
                    "r": 80.0,
                    "b": 20.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    )
    export["pages"] = {"1": {"page_no": 1, "size": {"width": 100.0, "height": 100.0}}}
    mapped = map_document(export, source_case.parsed.original_ref)
    item = mapped.units[0]
    if change in ("orig", "marker", "text"):
        export["texts"][0][change] = "forged original value"
    elif change == "domain":
        item = item.model_copy(
            update={
                "provenance": (
                    item.provenance[0].model_copy(
                        update={"char_domain": "docling_text_item"}
                    ),
                )
            }
        )
    else:
        item = item.model_copy(
            update={
                "locators": (item.locators[0].model_copy(update={"char_start": 1}),)
            }
        )
    with pytest.raises(ValueError):
        validate_document_units(FrozenJsonMap(export), (item,))


@pytest.mark.parametrize("change", ["orig", "marker", "offset"])
def test_rehashed_extraction_cannot_substitute_list_facts(document_record_case, change):
    result = document_record_case("law@1", "pdf")
    data = result.parsed.model_dump(mode="json", exclude={"raw_document_ref"})
    item = next(u for u in data["units"] if u["raw_item"].get("marker") == "1.")
    if change == "offset":
        item["locators"][0]["char_start"] += 1
    else:
        item["raw_item"][change] = "substituted source"
    with pytest.raises(ValueError):
        rebind(data)


def test_marker_only_provenance_retains_its_physical_locator(document_record_case):
    source_case = document_record_case("standard@1", "pdf")
    from tychora.ingestion.document_parts import _Budget, represent_document_parts

    export = export_case(source_case, [(0, "Record the sample.")])
    export["texts"][0].update(
        label="list_item",
        marker="1.",
        orig="1. Record the sample.",
        prov=[
            {
                "page_no": 1,
                "charspan": [0, 2],
                "bbox": {
                    "l": 1.0,
                    "t": 10.0,
                    "r": 80.0,
                    "b": 20.0,
                    "coord_origin": "TOPLEFT",
                },
            }
        ],
    )
    export["pages"] = {"1": {"page_no": 1, "size": {"width": 100.0, "height": 100.0}}}
    mapped = map_document(export, source_case.parsed.original_ref)
    text, parts, _, locators = represent_document_parts(
        mapped.units,
        "revision",
        "extraction",
        "source.pdf",
        _Budget(),
        source_case.parsed.original_ref.sha256,
    )
    assert text == "1. Record the sample."
    assert [(loc.char_start, loc.char_end, loc.physical_page) for loc in locators] == [
        (0, 2, 1)
    ]
    assert parts[0].field == "marker" and parts[0].provenance[0].charspan == (0, 2)

    from tychora.ingestion.document_parts import validate_document_source_parts

    parsed = artifact_from_export(source_case, export)
    unit = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    ).units[0]
    validate_document_source_parts(unit, parsed)
    assert [
        (loc.char_start, loc.char_end)
        for loc in unit.locators
        if loc.type == "document"
    ] == [(0, 2)]
    with pytest.raises(ValueError, match="locator"):
        validate_document_source_parts(unit.model_copy(update={"locators": ()}), parsed)


def test_exact_alias_collision_is_diagnostic(source_case, tmp_path):
    import shutil
    from pathlib import Path

    from tychora.configuration.registry import Registry
    from tychora.configuration.resolve import resolve_config
    from tychora.ingestion.document_records import document_normalizer_fingerprint

    pack = tmp_path / "pack"
    shutil.copytree(Path(__file__).parents[2] / "src/tychora/profiles/documents", pack)
    definition = pack / "kinds/standard.toml"
    definition.write_text(
        definition.read_text().replace(
            'proof_labels = ["Expected evidence"]', 'proof_labels = ["Requirement"]'
        )
    )
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\npath="pack"\n')
    registry = Registry.from_config(resolve_config(path, "test", {}))
    artifact = artifact_from_export(
        source_case,
        export_case(source_case, [(1, "STD-8 Sample"), (0, "Requirement: Ambiguous.")]),
    )
    data = artifact.model_dump(mode="json", exclude={"raw_document_ref"})
    data["registry_digest"] = registry.registry_digest
    kind = registry.kinds["standard@1"]
    assert isinstance(kind, FrozenJsonMap)
    data["normalizer_fingerprint"] = document_normalizer_fingerprint(kind, registry)
    bundle = normalize(
        rebind(data), artifact.registration.revision.revision_id, registry
    )
    assert not bundle.records
    diagnostics = bundle.units[0].attributes["semantic_diagnostics"]
    assert isinstance(diagnostics, tuple)
    assert isinstance(diagnostics[0], FrozenJsonMap)
    assert diagnostics[0]["code"] == "ambiguous_record"


def test_multiple_numbered_lines_in_one_sdk_item_fail_explicitly(document_record_case):
    from tychora.contracts.errors import TychoraError

    result = document_record_case("law@1", "docx")
    export = export_case(
        result,
        [(1, "Article 4"), (0, "1. Record the sample.\n2. Preserve the label.")],
    )
    with pytest.raises(TychoraError, match="numbered lines") as error:
        normalized(result, export)
    assert error.value.code == "CAPABILITY_MISSING"


def test_numbered_item_keeps_non_numbered_line_continuation(document_record_case):
    result = document_record_case("law@1", "docx")
    literal = "1. Record the sample.\nKeep the original label."
    export = export_case(result, [(1, "Article 4"), (0, literal)])
    bundle = normalized(result, export)
    child = next(unit for unit in bundle.units if unit.text == literal)
    assert any(
        row.unit_id == child.unit_id and row.normalized_value == "1."
        for row in bundle.identifiers
    )


@pytest.mark.parametrize(
    "change",
    ["page", "geometry", "range", "artifact", "source", "missing", "extra", "order"],
)
def test_canonical_locator_forgery_fails_normalization(
    source_case, monkeypatch, change
):
    from tychora.contracts.evidence import Geometry
    from tychora.ingestion import document_records

    real = document_records.represent_document_parts

    def corrupt(*args, **kwargs):
        text, parts, separators, locators = real(*args, **kwargs)
        first = locators[0]
        updates = {
            "page": {"physical_page": 99},
            "geometry": {
                "geometry": Geometry(domain="points", x0=0.0, y0=0.0, x1=1.0, y1=1.0)
            },
            "range": {"char_start": first.char_start + 1},
            "artifact": {"artifact_ref": "0" * 64},
            "source": {"native_ref": "another-source.docx"},
        }
        if change == "missing":
            locators = locators[1:]
        elif change == "extra":
            locators = (*locators, first)
        elif change == "order":
            locators = tuple(reversed(locators))
        else:
            locators = (first.model_copy(update=updates[change]), *locators[1:])
        return text, parts, separators, locators

    monkeypatch.setattr(document_records, "represent_document_parts", corrupt)
    with pytest.raises(ValueError, match="locator"):
        normalize(
            source_case.parsed,
            source_case.bundle.revision.revision_id,
            source_case.registry,
        )


def test_retained_unit_locator_substitution_fails_pure_validator(source_case):
    from tychora.ingestion.document_parts import validate_document_source_parts

    unit = source_case.bundle.units[0]
    validate_document_source_parts(unit, source_case.parsed)
    changed = unit.model_copy(
        update={
            "locators": (
                unit.locators[0].model_copy(update={"physical_page": 99}),
                *unit.locators[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="locator"):
        validate_document_source_parts(changed, source_case.parsed)


def table_order_export(source_case):
    export = export_case(source_case, [(1, "STD-8 Sample"), (0, "Requirement: Keep.")])
    # SDK array order is deliberately not row-major: ordinal fields are authoritative.
    cells = [
        {
            "text": text,
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
        }
        for text, row, col in (("Down", 1, 0), ("Left", 0, 0), ("Rght", 0, 1))
    ]
    export["tables"] = [
        {
            "self_ref": "#/tables/0",
            "parent": {"$ref": "#/body"},
            "children": [],
            "label": "table",
            "prov": [],
            "data": {"num_rows": 2, "num_cols": 2, "table_cells": cells},
        }
    ]
    export["body"]["children"].append({"$ref": "#/tables/0"})
    return export


def test_table_cell_order_uses_source_rows_not_sdk_array_order(source_case):
    import json

    from tychora.ingestion.document_parts import validate_document_source_parts

    parsed = artifact_from_export(source_case, table_order_export(source_case))
    bundle = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    )
    unit = bundle.units[0]
    assert unit.text.endswith("Left\tRght\nDown")
    assert [
        p["cell_index"]
        for p in json.loads(canonical_bytes(unit.attributes))["source_parts"]
        if p["field"] == "table_cell"
    ] == [1, 2, 0]
    validate_document_source_parts(unit, parsed)


@pytest.mark.parametrize("preserve_source_order", [False, True])
def test_consistent_table_cell_reordering_fails_pure_validator(
    source_case, preserve_source_order
):
    import json

    from tychora.ingestion.document_parts import validate_document_source_parts

    parsed = artifact_from_export(source_case, table_order_export(source_case))
    bundle = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    )
    unit = bundle.units[0]
    attrs = json.loads(canonical_bytes(unit.attributes))
    first, second, _ = [
        i
        for i, part in enumerate(attrs["source_parts"])
        if part["field"] == "table_cell"
    ]
    a, b = attrs["source_parts"][first], attrs["source_parts"][second]
    attrs["source_parts"][first] = {
        **b,
        "unit_start": a["unit_start"],
        "unit_end": a["unit_end"],
    }
    attrs["source_parts"][second] = {
        **a,
        "unit_start": b["unit_start"],
        "unit_end": b["unit_end"],
    }
    text = (
        unit.text[: a["unit_start"]]
        + "Rght"
        + unit.text[a["unit_end"] : b["unit_start"]]
        + "Left"
        + unit.text[b["unit_end"] :]
    )
    changed = unit.model_copy(
        update={
            "text": text,
            "text_sha": sha256(text.encode()).hexdigest(),
            "attributes": FrozenJsonMap(attrs),
        }
    )
    if preserve_source_order:
        attrs["source_parts"][first], attrs["source_parts"][second] = (
            attrs["source_parts"][second],
            attrs["source_parts"][first],
        )
        locators = list(unit.locators)
        locators[first] = locators[first].model_copy(
            update={"char_start": b["unit_start"], "char_end": b["unit_end"]}
        )
        locators[second] = locators[second].model_copy(
            update={"char_start": a["unit_start"], "char_end": a["unit_end"]}
        )
        changed = changed.model_copy(
            update={"attributes": FrozenJsonMap(attrs), "locators": tuple(locators)}
        )
    with pytest.raises(ValueError, match="order"):
        validate_document_source_parts(changed, parsed)


def test_nontext_source_locators_are_complete_and_ordered(document_record_case):
    source_case = document_record_case("standard@1", "pdf")
    import json

    from tychora.ingestion.document_parts import validate_document_source_parts

    export = export_case(source_case, [(1, "STD-8 Sample"), (0, "Requirement: Keep.")])
    export["pages"] = {"1": {"page_no": 1, "size": {"width": 100.0, "height": 100.0}}}
    for index in range(2):
        ref = f"#/pictures/{index}"
        export["pictures"].append(
            {
                "self_ref": ref,
                "parent": {"$ref": "#/body"},
                "children": [],
                "label": "picture",
                "prov": [
                    {
                        "page_no": 1,
                        "charspan": [0, 0],
                        "bbox": {
                            "l": 1.0,
                            "t": 10.0,
                            "r": 80.0,
                            "b": 20.0,
                            "coord_origin": "TOPLEFT",
                        },
                    }
                ],
            }
        )
        export["body"]["children"].append({"$ref": ref})
    parsed = artifact_from_export(source_case, export)
    unit = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    ).units[0]
    assert all(
        part["native_ref"] not in ("#/pictures/0", "#/pictures/1")
        for part in json.loads(canonical_bytes(unit.attributes))["source_parts"]
    )
    assert [
        (loc.char_start, loc.char_end)
        for loc in unit.locators[-2:]
        if loc.type == "document"
    ] == [
        (len(unit.text) - 2, len(unit.text) - 2),
        (len(unit.text), len(unit.text)),
    ]
    validate_document_source_parts(unit, parsed)
    for locators in (
        unit.locators[:-1],
        (*unit.locators[:-2], unit.locators[-1], unit.locators[-2]),
    ):
        with pytest.raises(ValueError, match="locator"):
            validate_document_source_parts(
                unit.model_copy(update={"locators": locators}), parsed
            )


def test_partial_source_span_retains_exact_locator_projection(source_case):
    import json

    from tychora.ingestion.document_parts import validate_document_source_parts

    parsed = artifact_from_export(
        source_case, export_case(source_case, [(0, "abcdef")])
    )
    unit = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    ).units[0]
    attrs = json.loads(canonical_bytes(unit.attributes))
    attrs["source_parts"][0].update(
        source_start=2, source_end=4, unit_start=0, unit_end=2
    )
    sliced = unit.model_copy(
        update={
            "text": "cd",
            "text_sha": sha256(b"cd").hexdigest(),
            "attributes": FrozenJsonMap(attrs),
            "locators": (unit.locators[0].model_copy(update={"char_end": 2}),),
        }
    )
    validate_document_source_parts(sliced, parsed)


def test_consistent_marker_text_reordering_fails_pure_validator(source_case):
    import json

    from tychora.ingestion.document_parts import validate_document_source_parts

    export = export_case(source_case, [(0, "Hi")])
    export["texts"][0].update(label="list_item", marker="1.", orig="1. Hi")
    parsed = artifact_from_export(source_case, export)
    unit = normalize(
        parsed, parsed.registration.revision.revision_id, source_case.registry
    ).units[0]
    attrs = json.loads(canonical_bytes(unit.attributes))
    a, b = attrs["source_parts"]
    attrs["source_parts"] = [
        {**b, "unit_start": a["unit_start"], "unit_end": a["unit_end"]},
        {**a, "unit_start": b["unit_start"], "unit_end": b["unit_end"]},
    ]
    text = "Hi 1."
    changed = unit.model_copy(
        update={
            "text": text,
            "text_sha": sha256(text.encode()).hexdigest(),
            "attributes": FrozenJsonMap(attrs),
        }
    )
    with pytest.raises(ValueError, match="order"):
        validate_document_source_parts(changed, parsed)
