"""Boundary keys and section paths emitted by the normalizers."""

import json
from pathlib import Path

import pytest

from tychora.contracts.identity import digest
from tychora.contracts.sections import (
    BOUNDARY_SCHEMES,
    ROOT_SECTION_KEY,
    normalizer_definition,
    section_path_of,
)
from tychora.ingestion.native import native_normalizer_fingerprint
from tychora.ingestion.normalize import normalize

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
