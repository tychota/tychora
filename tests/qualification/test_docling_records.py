import json
from itertools import pairwise

import pytest

from tychora.contracts import identity
from tychora.ingestion.normalize import normalize

pytestmark = pytest.mark.assets("TYCHORA_TEST_DOCLING_MODELS")


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_standard_document_records_use_source_fields(document_record_case, format):
    result = document_record_case("standard@1", format)
    record = next(row for row in result.bundle.records if row.record_type == "rule@1")
    assert (
        "Record the sample before changing the instrument."
        in record.data_json["requirement"]
    )
    assert "Keep the original label." in record.data_json["requirement"]
    assert record.data_json["proof"] == "The sample identifier is quotable."
    assert record.data_json["control"] == "Inspect the source."
    assert record.data_json["severity"] == "warning"
    assert record.data_json["foundation_ids"] == ()
    assert record.data_json["foundation_refs"] == ("REF-MISSING",)


def assert_source_parts(result):
    from tychora.contracts.documents import DocumentSourcePart

    raw = {u.native_ref: u for u in result.parsed.units}
    for unit in result.bundle.units:
        assert unit.attributes["source_refs"]
        ranges = []
        for entry in unit.attributes["source_parts"]:
            part = DocumentSourcePart.model_validate_json(
                identity.canonical_bytes(entry)
            )
            item = raw[part.native_ref]
            source = (
                item.raw_item[part.field]
                if part.field != "table_cell"
                else item.raw_item["data"]["table_cells"][part.cell_index]["text"]
            )
            assert (
                unit.text[part.unit_start : part.unit_end]
                == source[part.source_start : part.source_end]
            )
            assert part.provenance == item.provenance
            ranges.append((part.unit_start, part.unit_end))
        for sep in unit.attributes["structural_separators"]:
            assert unit.text[sep["unit_start"] : sep["unit_end"]] == sep["text"]
            ranges.append((sep["unit_start"], sep["unit_end"]))
        ranges.sort()
        assert ranges[0][0] == 0 and ranges[-1][1] == len(unit.text)
        assert all(a[1] == b[0] for a, b in pairwise(ranges))
    assert (
        result.owner.run_blocking_sync(
            lambda: result.store.hydrate(tuple(u.unit_id for u in result.bundle.units)),
            result.ctx,
        )
        == result.bundle.units
    )


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
@pytest.mark.parametrize("kind", ["template@1", "feedback@1", "feedback-explicit@1"])
def test_literal_template_and_feedback(document_record_case, format, kind):
    result = document_record_case(kind, format)
    values = [row.data_json for row in result.bundle.records]
    if kind == "template@1":
        assert [(v["slot"], v["instruction"]) for v in values] == [
            ("Summary", "State the observed result.\n\nKeep the uncertainty explicit."),
            ("Limits", "State the supported range."),
        ]
        assert {r.record_type for r in result.bundle.records} == {"template-slot@1"}
    else:
        assert len(values) == 1
        assert result.bundle.records[0].record_type == "feedback@1"
        value = values[0]
        assert value["comment"] == "The baseline is missing."
        assert value["target"] == "paragraph-3"
        assert [value[k] for k in ("author", "decision", "observation", "status")] == (
            [
                "Source reviewer",
                "Request source comparison",
                "Source supplied observation",
                "proposed",
            ]
            if "explicit" in kind
            else [None] * 4
        )
    assert all(u.review_state == "unreviewed" for u in result.bundle.units)
    assert_source_parts(result)


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_numbered_descendants_persist_all_overlap_owners(document_record_case, format):
    result = document_record_case("law@1", format)
    assert [row.original_value for row in result.bundle.identifiers] == [
        "Article 4",
        "1.",
        "2.",
        "4.1",
        "1.",
        "Article 5",
        "1.",
    ]
    by_id = {u.unit_id: u for u in result.bundle.units}
    article, first, second, sub, child, sibling, last = [
        by_id[r.unit_id] for r in result.bundle.identifiers
    ]
    assert first.parent_id == second.parent_id == sub.parent_id == article.unit_id
    assert child.parent_id == sub.unit_id
    assert last.parent_id == sibling.unit_id
    assert (
        article.text
        == "Article 4\n\n1. Record the sample.\n\n2. Preserve the label.\n\n4.1 Observations\n\n1. Retain the observation."
    )
    assert "Article 5" not in article.text
    assert set(child.attributes["overlapping_owner_ids"]) == {
        article.unit_id,
        sub.unit_id,
        child.unit_id,
    }
    assert child.unit_id in article.attributes["overlapping_owner_ids"]
    assert_source_parts(result)
    rows = result.owner.run_blocking_sync(
        lambda: result.owner.db.execute(
            "SELECT u.attributes_json FROM identifiers i JOIN evidence_units u ON u.unit_id=i.unit_id WHERE i.namespace='provision' AND i.normalized_value='4.1'"
        ).fetchall(),
        result.ctx,
    )
    assert len(rows) == 1
    stored = json.loads(rows[0][0])
    assert child.unit_id in stored["overlapping_owner_ids"]
    assert stored["source_parts"]
    from tychora.ingestion.documents import replay_document

    def replay():
        with result.owner.artifacts.open_verified(
            result.parsed.raw_document_ref
        ) as stream:
            manifest = stream.read()
        with result.owner.artifacts.open_verified(
            result.parsed.docling_json_ref
        ) as stream:
            sdk = stream.read()
        return replay_document(manifest, sdk, result.parsed.raw_document_ref)

    replayed = result.owner.run_blocking_sync(replay, result.ctx)
    assert (
        normalize(replayed, result.bundle.revision.revision_id, result.registry)
        == result.bundle
    )


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_grouped_source_replay_and_retry(document_record_case, format):
    from tychora.ingestion.documents import replay_document

    result = document_record_case("standard@1", format, page_break_at=3)
    assert_source_parts(result)
    unit = next(
        u for u in result.bundle.units if u.unit_id == result.bundle.records[0].unit_id
    )
    assert {loc.physical_page for loc in unit.locators} == (
        {1, 2} if format == "pdf" else {None}
    )

    def replay():
        with result.owner.artifacts.open_verified(
            result.parsed.raw_document_ref
        ) as stream:
            manifest = stream.read()
        with result.owner.artifacts.open_verified(
            result.parsed.docling_json_ref
        ) as stream:
            sdk = stream.read()
        return replay_document(manifest, sdk, result.parsed.raw_document_ref)

    result.source.unlink()
    replayed = result.owner.run_blocking_sync(replay, result.ctx)
    bundle = normalize(replayed, result.bundle.revision.revision_id, result.registry)
    assert bundle == result.bundle
    result.owner.run_blocking_sync(
        lambda: result.store.commit_extraction(bundle), result.ctx
    )
    assert bundle.revision == result.bundle.revision


@pytest.mark.qualification
@pytest.mark.parametrize("format", ["docx", "pdf"])
def test_custom_pack_matches_native_semantic_values(document_record_case, format):
    native = document_record_case("standard@1", "markdown", custom=True)
    rich = document_record_case("standard@1", format, custom=True)
    for result in (native, rich):
        assert result.bundle.revision.kind_id == "standard@1"
        assert result.registry.kinds["standard@1"]["default_lane"] == "standard"
        assert result.bundle.records[0].record_type == "rule@1"
        unit = next(
            u
            for u in result.bundle.units
            if u.unit_id == result.bundle.records[0].unit_id
        )
        assert (
            "Exigence: Record the sample before changing the instrument." in unit.text
        )
        assert "Preuve attendue: The sample identifier is quotable." in unit.text
        assert result.bundle.identifiers[0].original_value == "LOCAL-01"
        assert result.bundle.records[0].data_json["foundation_refs"] == ("REF-MISSING",)
    semantic = lambda r: {
        k: v for k, v in r.bundle.records[0].data_json.items() if k != "provenance"
    }
    assert semantic(native) == semantic(rich)
