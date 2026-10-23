"""Boundary keys and section paths emitted by the normalizers."""

import json
from pathlib import Path

import pytest

from tests.fixtures.docling import picture_item, retain_export
from tests.ingestion.test_docling_mapping import mapping_case
from tychora.adapters.docling.mapping import map_document
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.identity import digest
from tychora.contracts.sections import (
    BOUNDARY_SCHEMES,
    ROOT_SECTION_KEY,
    HeadingLevel,
    normalizer_definition,
    section_path_of,
)
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit
from tychora.ingestion.document_records import (
    _Group,
    _group_placements,
    document_normalizer_fingerprint,
)
from tychora.ingestion.native import native_normalizer_fingerprint
from tychora.ingestion.normalize import normalize
from tychora.ingestion.sections import (
    document_placements,
    figure_placement,
    view_placement,
)
from tychora.retrieval.segments import _same_section

FIXTURES = Path(__file__).parents[1] / "fixtures/documents"


def _bundle(native_case, text, **options):
    parsed, _, registry = native_case(text, **options)
    return normalize(parsed, parsed.registration.revision.revision_id, registry)


def _units(native_case, text, **options):
    bundle = _bundle(native_case, text, **options)
    for unit in bundle.units:
        assert_path_invariants(unit)
    return {unit.text.strip(): unit for unit in bundle.units}


def _own(unit, scheme="section"):
    return f"{scheme}:{unit.native_ref}"


def _path(unit):
    return [
        (item.key, item.level, item.depth)
        for item in section_path_of(unit.attributes, unit.boundary_key)
    ]


def _starting(bundle, prefix):
    return next(unit for unit in bundle.units if unit.text.lstrip().startswith(prefix))


def assert_path_invariants(unit):
    """The path starts at the root and each depth is its index."""
    path = section_path_of(unit.attributes, unit.boundary_key)
    assert path[0].depth == 0
    assert path[0].key == unit.attributes["section_path"][0]["key"]
    assert [item.depth for item in path] == list(range(len(path)))


def assert_boundary_contract(bundle):
    """A unit keyed by its own native_ref is the root of what the key names."""
    for unit in bundle.units:
        assert_path_invariants(unit)
        scheme, _, ref = unit.boundary_key.partition(":")
        assert scheme in BOUNDARY_SCHEMES, unit.boundary_key
        path = section_path_of(unit.attributes, unit.boundary_key)
        assert [item.depth for item in path] == list(range(len(path)))
        if ref == unit.native_ref:
            assert scheme in {"table", "figure"} or path[-1].key == unit.boundary_key, (
                unit.unit_type,
                unit.boundary_key,
            )


def test_markdown_paragraphs_share_enclosing_heading_key(native_case):
    units = _units(
        native_case,
        "# Pumps\n\nCheck the housing.\n\nInspect seals.\n\n# Valves\n\nReplace valves.\n",
    )
    pumps, valves = units["# Pumps"], units["# Valves"]
    assert pumps.boundary_key == _own(pumps)
    assert units["Check the housing."].boundary_key == _own(pumps)
    assert units["Inspect seals."].boundary_key == _own(pumps)
    assert units["Replace valves."].boundary_key == _own(valves)
    assert _path(units["Inspect seals."]) == [
        (ROOT_SECTION_KEY, None, 0),
        (_own(pumps), None, 1),
    ]


def test_text_before_first_heading_uses_root(native_case):
    units = _units(native_case, "Preamble text.\n\n# First\n\nBody.\n")
    assert units["Preamble text."].boundary_key == ROOT_SECTION_KEY
    assert _path(units["Preamble text."]) == [(ROOT_SECTION_KEY, None, 0)]
    assert units["Body."].boundary_key == _own(units["# First"])
    plain = _units(native_case, "One paragraph.\n\nAnother paragraph.\n")
    assert {unit.boundary_key for unit in plain.values()} == {ROOT_SECTION_KEY}


def test_skipped_and_rising_heading_levels(native_case):
    text = (
        "# Guide\n\n#### Deep\n\nDeep body.\n\n### Middle\n\nMiddle body.\n\n"
        "## Upper\n\nUpper body.\n\n# Next\n\nNext body.\n"
    )
    units = _units(native_case, text)
    guide = units["# Guide"]
    root = (ROOT_SECTION_KEY, None, 0)
    for heading, body in (
        ("#### Deep", "Deep body."),
        ("### Middle", "Middle body."),
        ("## Upper", "Upper body."),
    ):
        assert units[body].boundary_key == _own(units[heading])
        assert _path(units[body]) == [
            root,
            (_own(guide), None, 1),
            (_own(units[heading]), None, 2),
        ]
    assert _path(units["Next body."]) == [root, (_own(units["# Next"]), None, 1)]
    assert units["Deep body."].heading_path == ("Guide", "Deep")


def test_markdown_table_uses_table_scheme(native_case):
    units = _units(
        native_case,
        "# Results\n\n| Pump | Hours |\n| --- | --- |\n| A | 3 |\n\nAfter the table.\n",
    )
    table = next(unit for unit in units.values() if unit.unit_type == "table")
    results = units["# Results"]
    assert table.boundary_key == _own(table, "table")
    assert _path(table)[-1][0] == _own(results)
    assert units["After the table."].boundary_key == _own(results)


def test_text_without_headings_uses_file_scheme(native_case):
    units = _units(native_case, "# not a heading\nline two\n\nnext\n", format="text")
    assert {unit.boundary_key for unit in units.values()} == {"file:synthetic.text"}
    assert all(
        _path(unit) == [("file:synthetic.text", None, 0)] for unit in units.values()
    )


def test_native_normalizer_fingerprint_is_v2(native_case):
    parsed, _, registry = native_case("# Manual\n\nBody.\n")
    kind = registry.kinds["manual@1"]
    assert parsed.normalizer_fingerprint == native_normalizer_fingerprint(
        kind, registry
    )
    assert parsed.normalizer_fingerprint == digest(
        "native-normalizer-v2", normalizer_definition(kind), registry.schema_digests
    )
    assert parsed.normalizer_fingerprint != digest(
        "native-normalizer-v1", kind, registry.schema_digests
    )


def test_numbered_provisions_are_articles(native_case):
    text = (
        "# Policy\n\n## Article 4\n\nIntro.\n\n1. First numbered paragraph.\n"
        "   1. Nested paragraph.\n\n## Article 5\n\nNext article.\n\n## Annex\n\nAnnex text.\n"
    )
    bundle = _bundle(native_case, text, kind="law@1")
    policy = _starting(bundle, "# Policy")
    article = _starting(bundle, "## Article 4")
    first = _starting(bundle, "1. First")
    nested = next(unit for unit in bundle.units if unit.text.startswith("   1. Nested"))
    annex_text = _starting(bundle, "Annex text.")
    root = (ROOT_SECTION_KEY, None, 0)
    section = (_own(policy), None, 1)
    assert article.boundary_key == _own(article, "article")
    assert _path(article) == [root, section, (_own(article, "article"), "article", 2)]
    assert first.boundary_key == _own(first, "article")
    assert _path(first)[-1] == (_own(first, "article"), "clause", 3)
    assert _path(nested)[-2:] == [
        (_own(first, "article"), "clause", 3),
        (_own(nested, "article"), "clause", 4),
    ]
    assert annex_text.boundary_key == _own(_starting(bundle, "## Annex"))
    assert_boundary_contract(bundle)


def test_rule_records_are_articles(native_case):
    bundle = _bundle(
        native_case, (FIXTURES / "standards.md").read_text(), kind="standard@1"
    )
    rules = [unit for unit in bundle.units if unit.unit_type == "rule"]
    assert len(rules) == 2
    for rule in rules:
        assert rule.boundary_key == _own(rule, "article")
        assert _path(rule)[-1] == (rule.boundary_key, "article", 2)


@pytest.mark.parametrize(
    "text,kind,source_format",
    [
        ((FIXTURES / "standards.md").read_text(), "standard@1", "markdown"),
        ((FIXTURES / "template.md").read_text(), "template@1", "markdown"),
        (
            "# Manual\n\nIntro.\n\n## Part\n\n- item one\n- item two\n\n| A | B |\n| - | - |\n| 1 | 2 |\n",
            "manual@1",
            "markdown",
        ),
        (
            "## Article 4\n\nIntroductory text.\n\n1. First.\n   1. Nested.\n\n## Article 5\n\nNext.\n",
            "law@1",
            "markdown",
        ),
        ("Plain line.\n\nSecond paragraph.\n", "manual@1", "text"),
    ],
)
def test_boundary_key_is_never_a_paragraph_ref(native_case, text, kind, source_format):
    assert_boundary_contract(
        _bundle(native_case, text, kind=kind, format=source_format)
    )


def test_json_records_are_placed_under_the_file(native_case):
    record = {
        "claim": "The sample measured 2 units.",
        "target": "Synthetic sample",
        "provenance": ["Operator supplied reference"],
        "status": "recorded",
        "foundation_ids": [],
        "foundation_refs": ["EXT-2"],
    }
    bundle = _bundle(
        native_case, json.dumps([record, record]), kind="evidence@1", format="json"
    )
    assert len(bundle.units) == 2
    for unit in bundle.units:
        assert unit.boundary_key == "file:synthetic.json"
        assert _path(unit) == [("file:synthetic.json", None, 0)]
    assert_boundary_contract(bundle)


def _text(ref, label, text, level=None):
    item = {
        "self_ref": ref,
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": label,
        "text": text,
        "orig": text,
        "prov": [],
    }
    if level is not None:
        item["level"] = level
    return item


def _table(ref, rows):
    return {
        "self_ref": ref,
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "table",
        "prov": [],
        "data": {
            "num_rows": len(rows),
            "num_cols": len(rows[0]),
            "table_cells": [
                {
                    "text": value,
                    "start_row_offset_idx": row,
                    "end_row_offset_idx": row + 1,
                    "start_col_offset_idx": column,
                    "end_col_offset_idx": column + 1,
                }
                for row, values in enumerate(rows)
                for column, value in enumerate(values)
            ],
        },
    }


def _export(texts, tables=(), order=None):
    """A synthetic Docling export whose body lists the items in reading order."""
    export, original = mapping_case()
    refs = order or [item["self_ref"] for item in (*texts, *tables)]
    export["texts"] = list(texts)
    export["tables"] = list(tables)
    export["body"]["children"] = [{"$ref": ref} for ref in refs]
    return export, original


SECTIONED = (
    _text("#/texts/0", "title", "Pump manual"),
    _text("#/texts/1", "text", "Read this first."),
    _text("#/texts/2", "section_header", "Seals", level=1),
    _text("#/texts/3", "text", "Inspect seals quarterly."),
    _text("#/texts/4", "section_header", "Seal rings", level=2),
    _text("#/texts/5", "text", "Replace worn rings."),
    _text("#/texts/6", "section_header", "Valves", level=1),
    _text("#/texts/7", "caption", "Table 1 Valve hours"),
)


def test_docling_units_use_enclosing_section_header():
    export, original = _export(SECTIONED)
    placements = document_placements(map_document(export, original).units)
    assert placements["#/texts/1"].boundary_key == "section:#/texts/0"
    assert placements["#/texts/3"].boundary_key == "section:#/texts/2"
    assert placements["#/texts/5"].boundary_key == "section:#/texts/4"
    assert [entry.key for entry in placements["#/texts/5"].section_path] == [
        "section:#root",
        "section:#/texts/2",
        "section:#/texts/4",
    ]
    assert placements["#/texts/6"].boundary_key == "section:#/texts/6"
    assert [entry.depth for entry in placements["#/texts/6"].section_path] == [0, 1]
    assert placements["#/texts/7"].boundary_key == "section:#/texts/6"


def test_split_table_fragments_keep_their_own_table_keys():
    texts = (_text("#/texts/0", "section_header", "Hours", level=1),)
    tables = (
        _table("#/tables/0", [["Pump", "Hours"], ["A", "3"]]),
        _table("#/tables/1", [["Pump", "Hours"], ["B", "5"]]),
    )
    export, original = _export(texts, tables)
    placements = document_placements(map_document(export, original).units)
    first, second = placements["#/tables/0"], placements["#/tables/1"]
    assert first.boundary_key == "table:#/tables/0"
    assert second.boundary_key == "table:#/tables/1"
    assert first.section_path[-1].key == "section:#/texts/0"
    for kind in ("table", "table_row"):
        assert view_placement(first, kind, "#/tables/0").boundary_key == (
            "table:#/tables/0"
        )
        assert view_placement(second, kind, "#/tables/1").boundary_key == (
            "table:#/tables/1"
        )
    caption = placements["#/texts/0"]
    assert view_placement(caption, "caption", "#/texts/0") == caption


def test_standalone_image_uses_figure_scheme():
    placement = figure_placement("#/original-image")
    assert placement.boundary_key == "figure:#/original-image"
    assert [(e.key, e.depth) for e in placement.section_path] == [
        ("figure:#/original-image", 0)
    ]


def test_docling_normalizer_fingerprints_are_v3(native_case):
    _, _, registry = native_case("# Manual\n")
    structured, records = registry.kinds["manual@1"], registry.kinds["standard@1"]
    for kind, family in (
        (structured, "docling-normalizer"),
        (records, "docling-record-normalizer"),
    ):
        assert document_normalizer_fingerprint(kind, registry) == digest(
            f"{family}-v3", normalizer_definition(kind), registry.schema_digests
        )
        assert document_normalizer_fingerprint(
            kind, registry, legacy_v1=True
        ) == digest(f"{family}-v1", kind, registry.schema_digests)


def _group(ref, level, text, identifier=None, parent=None):
    unit = map_document(*_export((_text(ref, "section_header", text, level),))).units[0]
    return _Group(
        originals=(unit,),
        native_ref=ref,
        parent_ref=parent,
        unit_type="section_header",
        identifier=identifier,
    )


def test_record_groups_are_placed_like_native_records():
    groups = [
        _group("#/texts/0", 1, "Rules"),
        _group("#/texts/1", 2, "STD-1 Seals", identifier="STD-1"),
        _group("#/texts/2", 2, "Notes"),
    ]
    articles = _group_placements(groups, "numbered_provisions@1")
    assert articles[1].boundary_key == "article:#/texts/1"
    assert [entry.key for entry in articles[1].section_path] == [
        "section:#root",
        "section:#/texts/0",
        "article:#/texts/1",
    ]
    # A record heading opens an article, not a section: what follows nests under
    # the enclosing section, as it does for native records.
    assert [entry.key for entry in articles[2].section_path][:2] == [
        "section:#root",
        "section:#/texts/0",
    ]
    assert articles[2].boundary_key == "section:#/texts/2"
    plain = _group_placements(groups, "feedback_records@1")
    assert plain[1].boundary_key == "section:#/texts/1"


@pytest.fixture
def docling_bundle(execution, rich_capture):
    """The registry and bundle of a retained synthetic export with sections, a picture and a table."""
    texts = (*SECTIONED[:7], _text("#/texts/7", "caption", "Figure 1 Seal ring"))
    order = [item["self_ref"] for item in texts[:4]] + ["#/pictures/0", "#/texts/7"]
    order += [item["self_ref"] for item in texts[4:7]] + ["#/tables/0"]
    table = _table("#/tables/0", [["Valve", "Hours"], ["V1", "7"]])
    export, _ = _export(texts, (table,), order)
    export["pictures"] = [picture_item(captions=("#/texts/7",))]
    artifact, registry = retain_export(execution, rich_capture, export)
    return registry, normalize(
        artifact, artifact.registration.revision.revision_id, registry
    )


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("document_kind", ["manual@1"], indirect=True)
def test_picture_and_caption_use_enclosing_section(docling_bundle):
    registry, bundle = docling_bundle
    units = {unit.native_ref: unit for unit in bundle.units}
    assert units["#/texts/3"].boundary_key == "section:#/texts/2"
    assert units["#/pictures/0"].boundary_key == "section:#/texts/2"
    assert units["#/texts/7"].boundary_key == "section:#/texts/2"
    table = units["#/tables/0"]
    assert table.boundary_key == "table:#/tables/0"
    path = section_path_of(table.attributes, table.boundary_key)
    assert path[-1].key == "section:#/texts/6"
    assert bundle.extraction.normalizer_fingerprint == document_normalizer_fingerprint(
        registry.kinds["manual@1"], registry
    )


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("document_kind", ["manual@1"], indirect=True)
def test_neighbour_scope_follows_the_docling_section(docling_bundle):
    units = {unit.native_ref: unit for unit in docling_bundle[1].units}
    paragraph, caption = units["#/texts/3"], units["#/texts/7"]
    assert paragraph.boundary_key == caption.boundary_key
    assert _same_section(paragraph, caption)
    assert not _same_section(paragraph, units["#/texts/5"])
    assert not _same_section(paragraph, units["#/tables/0"])


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("document_kind", ["manual@1"], indirect=True)
def test_docling_boundary_key_is_never_a_paragraph_ref(docling_bundle):
    assert_boundary_contract(docling_bundle[1])


def _documents_config(tmp_path, **kind_fields):
    """The documents@1 profile with fields added to manual@1."""
    path = tmp_path / "custom.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    data = config.model_dump(mode="json")["registry"]
    data["kinds"]["manual@1"].update(kind_fields)
    return config.model_copy(
        update={
            "registry": FrozenJsonMap(data),
            "registry_digest": digest("registry-v1", data),
        }
    )


ARTICLE_HEADINGS = [{"pattern": r"^Article \d+", "level": "article"}]


def test_kind_heading_pattern_names_level(native_case, tmp_path):
    text = "# Article 1 Scope\n\nBody.\n\n# Definitions\n\nTerms.\n"
    config = _documents_config(tmp_path, heading_levels=ARTICLE_HEADINGS)
    units = _units(native_case, text, config_override=config)
    assert _path(units["Body."])[-1] == (_own(units["# Article 1 Scope"]), "article", 1)
    assert _path(units["Terms."])[-1] == (_own(units["# Definitions"]), None, 1)


def test_heading_pattern_change_changes_normalizer_fingerprint(native_case, tmp_path):
    plain, _, _ = native_case("# Article 1\n\nBody.\n")
    patterned, _, _ = native_case(
        "# Article 1\n\nBody.\n",
        config_override=_documents_config(tmp_path, heading_levels=ARTICLE_HEADINGS),
    )
    assert plain.normalizer_fingerprint != patterned.normalizer_fingerprint


def test_docling_heading_pattern_names_level():
    export, original = _export(
        (
            _text("#/texts/0", "section_header", "Article 3 Duties", level=1),
            _text("#/texts/1", "text", "Keep records."),
        )
    )
    placements = document_placements(
        map_document(export, original).units,
        (HeadingLevel(pattern=r"^Article \d+", level="article"),),
    )
    assert placements["#/texts/1"].section_path[-1].level == "article"


class _Characters:
    model = "synthetic-codepoint-counter"
    tokenizer_id = "synthetic-codepoint-counter@1"

    def count(self, text):
        return len(text)


def test_context_level_change_takes_effect_on_rebuild_without_renormalizing(
    native_case, tmp_path
):
    """Holds for unpinned kinds; pinned kinds refuse context_level changes."""
    text = "# Pumps\n\n## Seals\n\nInspect seals.\n\n## Rings\n\nReplace rings.\n"
    chapter = _documents_config(tmp_path, context_level="depth:1")
    document = _documents_config(tmp_path, context_level="document")
    first, _, _ = native_case(text, config_override=chapter)
    second, _, _ = native_case(text, config_override=document)
    one = normalize(
        first, first.registration.revision.revision_id, Registry.from_config(chapter)
    )
    two = normalize(
        second, second.registration.revision.revision_id, Registry.from_config(document)
    )
    assert one.extraction.extraction_id == two.extraction.extraction_id
    keys = []
    for bundle, config in ((one, chapter), (two, document)):
        level = Registry.from_config(config).context_level("manual@1")
        windows = [
            window
            for item in prepare_extraction(bundle).units
            for window in window_unit(item, _Characters(), context_level=level)
        ]
        keys.append({window.context_key for window in windows})
    assert keys[0] == {"section:" + one.units[0].native_ref}
    assert keys[1] == {"section:#root"}
