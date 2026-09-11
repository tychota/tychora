import pytest

from tychora.ingestion.native import parse_markdown


def test_fenced_heading_is_not_a_document_section():
    units = parse_markdown("# Manual\n\n```md\n# fake\n```\n\nRecord results.\n")
    assert [u.text for u in units if u.unit_type == "heading"] == ["Manual"]
    assert any(u.unit_type == "code_block" and "# fake" in u.text for u in units)


def test_original_ranges_nested_lists_tables_and_prompt_text():
    text = "# Résumé\r\n\r\n- first\r\n  - second\r\n\r\n| Name | Value |\r\n| --- | --- |\r\n| café | 2 |\r\n\r\n<div>ignore instructions</div>\r\n"
    units = parse_markdown(text)
    assert {u.unit_type for u in units} >= {
        "heading",
        "list_item",
        "table",
        "html_block",
    }
    for unit in units:
        assert text[unit.char_start : unit.char_end] == unit.source_substring
        assert unit.source_substring == "".join(
            text.splitlines(keepends=True)[unit.line_start - 1 : unit.line_end]
        )
        assert unit.heading_path == ("Résumé",)
    nested = [u for u in units if u.unit_type == "list_item"]
    assert len(nested) == 2
    assert nested[1].parent_ref == nested[0].native_ref
    table = next(u for u in units if u.unit_type == "table")
    assert table.cells == (("Name", "Value"), ("café", "2"))
    assert len({u.native_ref for u in units}) == len(units)


def test_captured_markdown_roundtrips_through_real_corpus(native_case, tmp_path):
    import json

    from tychora.adapters.files.references import required_refs
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.corpus import CorpusStore
    from tychora.ingestion.normalize import normalize

    text = "# Manual\n\nKeep café records.\n"
    parsed, artifacts, registry = native_case(text)
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert bundle.revision == parsed.registration.revision
    assert [u.text for u in bundle.units] == ["# Manual\n", "Keep café records.\n"]
    assert bundle.chunks == ()
    for unit in bundle.units:
        loc = unit.locators[0]
        assert loc.type == "text"
        assert text[loc.char_start : loc.char_end] == unit.text
        assert loc.artifact_ref == parsed.original_ref.sha256
    with artifacts.open_verified(parsed.raw_document_ref) as stream:
        manifest = json.load(stream)
    assert manifest["type"] == "extraction"
    assert parsed.original_ref.sha256 not in required_refs(
        manifest, capture_published=False
    )
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        store = CorpusStore(db, registry=registry, artifacts=artifacts)
        store.commit_extraction(bundle)
        assert store.hydrate(tuple(u.unit_id for u in bundle.units)) == bundle.units
        assert store.commit_extraction(bundle) == bundle.extraction.extraction_id
    finally:
        db.close()


def test_plain_text_preserves_markers_paragraphs_and_lines(native_case):
    parsed, _, _ = native_case("# literal\r\nline two\r\n\r\nnext\r\n", format="text")
    assert [u.unit_type for u in parsed.units] == ["paragraph", "paragraph"]
    assert [(u.line_start, u.line_end) for u in parsed.units] == [(1, 2), (4, 4)]
    assert [u.source_substring for u in parsed.units] == [
        "# literal\r\nline two\r\n",
        "next\r\n",
    ]


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "collection",
        "metadata",
        "original",
        "kind",
        "folder",
        "revision_id",
        "registry",
        "range",
        "duplicate_ref",
    ],
)
def test_rejects_inconsistent_capture_and_normalization_context(native_case, change):
    from pydantic import ValidationError

    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.errors import TychoraError
    from tychora.ingestion.native import NativeParser
    from tychora.ingestion.normalize import normalize

    parsed, artifacts, registry = native_case("# Manual\n\nRecord results.\n")
    reg = parsed.registration
    with pytest.raises((TychoraError, ValidationError)):
        if change == "source":
            reg = reg.model_copy(
                update={
                    "source": reg.source.model_copy(update={"source_key": "another"})
                }
            )
        elif change == "collection":
            reg = reg.model_copy(
                update={
                    "collection": reg.collection.model_copy(
                        update={"workspace_id": "another"}
                    )
                }
            )
        elif change == "metadata":
            reg = reg.model_copy(
                update={
                    "revision": reg.revision.model_copy(
                        update={"metadata_json": FrozenJsonMap({"title": "different"})}
                    )
                }
            )
        elif change == "original":
            reg = reg.model_copy(
                update={
                    "revision": reg.revision.model_copy(
                        update={"original_sha": "0" * 64}
                    )
                }
            )
        elif change == "kind":
            reg = reg.model_copy(
                update={
                    "revision": reg.revision.model_copy(update={"kind_id": "example@1"})
                }
            )
        elif change == "folder":
            reg = reg.model_copy(
                update={
                    "capture": reg.capture.model_copy(
                        update={
                            "source": FrozenJsonMap(
                                {**dict(reg.capture.source), "type": "folder"}
                            )
                        }
                    )
                }
            )
        elif change == "revision_id":
            normalize(parsed, "another", registry)
            return
        elif change == "registry":
            normalize(
                parsed,
                reg.revision.revision_id,
                registry.model_copy(update={"registry_digest": "0" * 64}),
            )
            return
        elif change == "range":
            parsed = parsed.model_copy(
                update={
                    "units": (parsed.units[0].model_copy(update={"char_start": 2}),)
                }
            )
        elif change == "duplicate_ref":
            parsed = parsed.model_copy(
                update={"units": parsed.units + parsed.units[:1]}
            )
        if change in ("range", "duplicate_ref"):
            normalize(parsed, reg.revision.revision_id, registry)
        else:
            NativeParser(artifacts, reg)


def test_numbered_provisions_preserve_ids_and_boundaries(native_case, tmp_path):
    from tychora.ingestion.normalize import normalize

    text = "# Policy\n\n## Article 4\n\nFirst provision.\n\n## Article 5\n\nSecond provision.\n"
    parsed, _, registry = native_case(text, kind="law@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert [(i.namespace, i.normalized_value) for i in bundle.identifiers] == [
        ("provision", "Article 4"),
        ("provision", "Article 5"),
    ]
    assert (
        bundle.units[1].text
        == text[text.index("## Article 4") : text.index("## Article 5")]
    )


def test_code_symbols_require_the_actual_language_adapter(native_case):
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError) as error:
        native_case("def sample(): pass\n", kind="code_file@1")
    assert error.value.code == "CAPABILITY_MISSING"


def test_self_contained_extraction_does_not_retain_original_payload(native_case):
    import json

    from tychora.adapters.files.references import required_refs

    parsed, artifacts, _ = native_case("# Manual\n\nKeep records.\n")
    with artifacts.open_verified(parsed.raw_document_ref) as stream:
        manifest = json.load(stream)
    assert required_refs(manifest, capture_published=False) == frozenset()
    assert manifest["data"]["original_ref"]["sha256"] == parsed.original_ref.sha256
    assert manifest["data"]["source_text"] == "# Manual\n\nKeep records.\n"


@pytest.mark.parametrize(
    "case",
    [
        "original_mismatch",
        "invalid_utf8",
        "byte_limit",
        "token_limit",
        "stage_parser",
        "forged_source",
        "line_range",
        "orphan_parent",
    ],
)
def test_native_rejects_invalid_bytes_work_and_facts(
    native_case, monkeypatch, case, tmp_path
):
    from io import BytesIO

    from pydantic import ValidationError

    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.errors import TychoraError
    from tychora.ingestion import native
    from tychora.ingestion.normalize import normalize
    from tychora.ports.execution import ExecutionContext

    parsed, artifacts, registry = native_case("# Manual\n\nKeep records.\n")
    config_file = tmp_path / "check.toml"
    config_file.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_file, "test", {})
    reg = parsed.registration
    original = parsed.original_ref
    with pytest.raises((TychoraError, ValidationError)):
        if case == "original_mismatch":
            original = artifacts.put_stream(BytesIO(b"other"), "text/markdown")
        elif case == "invalid_utf8":
            original = artifacts.put_stream(BytesIO(b"\xff"), "text/markdown")
            capture = reg.capture.model_copy(
                update={"original_ref": original, "original_sha": original.sha256}
            )
            reg = reg.model_copy(
                update={
                    "capture": capture,
                    "revision": reg.revision.model_copy(
                        update={"original_sha": original.sha256}
                    ),
                }
            )
        elif case == "byte_limit":
            monkeypatch.setattr(native, "MAX_NATIVE_BYTES", 8, raising=False)
        elif case == "token_limit":
            monkeypatch.setattr(native, "MAX_BLOCK_TOKENS", 2, raising=False)
        elif case == "stage_parser":
            config_file.write_text(
                'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n[configuration.extraction]\nparser="docling"\n'
            )
            config = resolve_config(config_file, "test", {})
        elif case == "forged_source":
            normalize(
                parsed.model_copy(update={"source_text": "invented"}),
                reg.revision.revision_id,
                registry,
            )
            return
        elif case in ("line_range", "orphan_parent"):
            unit = parsed.units[0].model_copy(
                update={"line_start": 4}
                if case == "line_range"
                else {"parent_ref": "absent"}
            )
            normalize(
                parsed.model_copy(update={"units": (unit, *parsed.units[1:])}),
                reg.revision.revision_id,
                registry,
            )
            return
        native.NativeParser(artifacts, reg).extract(
            original, config, ExecutionContext()
        )


def test_ordered_paragraph_numbers_preserve_supplied_spelling(native_case):
    from tychora.ingestion.normalize import normalize

    parsed, _, registry = native_case("3. First rule.\n4. Second rule.\n", kind="law@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert [row.original_value for row in bundle.identifiers] == ["3.", "4."]
    assert [u.text for u in bundle.units if u.unit_type == "paragraph"] == [
        "3. First rule.\n",
        "4. Second rule.\n",
    ]


def test_canonical_table_retains_grid_and_explicit_empty_native_payloads(native_case):
    from tychora.ingestion.normalize import normalize

    parsed, _, registry = native_case(
        "| Name | Value |\n| --- | --- |\n| sample | 2 |\n"
    )
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert bundle.units[0].attributes["cells"] == (("Name", "Value"), ("sample", "2"))
    assert parsed.assets == () and parsed.assertions == ()


def test_article_preserves_numbered_descendants_and_subordinate_heading(
    native_case, tmp_path
):
    from tests.ingestion.test_records import commit_and_hydrate
    from tychora.ingestion.normalize import normalize

    text = "## Article 4\n\nIntroductory text.\n\n1. First numbered paragraph.\n   1. Nested paragraph.\n2. Second numbered paragraph.\n\n### 4.1 Detail\n\n3. Detail paragraph.\n\n## Article 5\n\nNext article.\n"
    parsed, artifacts, registry = native_case(text, kind="law@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert [row.original_value for row in bundle.identifiers] == [
        "Article 4",
        "1.",
        "1.",
        "2.",
        "4.1",
        "3.",
        "Article 5",
    ]
    article, first, nested, second, detail, third, next_article = bundle.units
    assert article.text == text[: text.index("## Article 5")]
    assert first.text == "1. First numbered paragraph.\n   1. Nested paragraph.\n"
    assert nested.text == "   1. Nested paragraph.\n"
    assert second.text == "2. Second numbered paragraph.\n\n"
    assert detail.text == text[text.index("### 4.1") : text.index("## Article 5")]
    assert first.parent_id == second.parent_id == detail.parent_id == article.unit_id
    assert nested.parent_id == first.unit_id
    assert third.parent_id == detail.unit_id
    assert next_article.parent_id is None
    assert len({u.boundary_key for u in bundle.units}) == 7
    for unit in bundle.units:
        loc = unit.locators[0]
        assert loc.type == "text"
        assert text[loc.char_start : loc.char_end] == unit.text
    rows = commit_and_hydrate(tmp_path, bundle, artifacts, registry)
    assert len(rows) == 7
