from pathlib import Path

import pytest

from tests.helpers.corpus import registry
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.ingestion.normalize import normalize


@pytest.mark.parametrize(
    "kind,fields",
    [
        (
            "standard@1",
            {
                "requirement": "Keep records.",
                "proof": None,
                "control": None,
                "severity": None,
            },
        ),
        (
            "feedback@1",
            {
                "comment": "Explain the result.",
                "author": None,
                "observation": None,
                "decision": None,
            },
        ),
        ("template@1", {"slot": "Summary", "instruction": "State the result."}),
    ],
)
def test_record_schemas_preserve_unknowns_and_literal_foundations(kind, fields):
    record = {
        **fields,
        "target": None,
        "status": None,
        "provenance": ["supplied source reference"],
        "foundation_ids": [],
        "foundation_refs": ["REF-8"],
    }
    assert registry().validate_record(kind, record) == record


FIXTURES = Path(__file__).parents[1] / "fixtures/documents"


def commit_and_hydrate(tmp_path, bundle, artifacts, registry):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        store = CorpusStore(db, registry=registry, artifacts=artifacts)
        store.commit_extraction(bundle)
        assert store.hydrate(tuple(u.unit_id for u in bundle.units)) == bundle.units
        return [
            tuple(row)
            for row in db.execute(
                "SELECT namespace,normalized_value,unit_id FROM identifiers"
            )
        ]
    finally:
        db.close()


def test_rule_is_one_original_unit_with_typed_fields_and_foundations(
    native_case, tmp_path
):
    text = (FIXTURES / "standards.md").read_text()
    parsed, artifacts, registry = native_case(text, kind="standard@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 2
    record = dict(bundle.records[0].data_json)
    assert record["requirement"] == "Keep the original result."
    assert record["proof"] == "The recorded measurement."
    assert record["control"] == "Check the timestamp."
    assert record["severity"] == "Source-declared major"
    assert record["foundation_refs"] == ("STD-13", "EXT-9")
    assert record["foundation_ids"] == (bundle.records[1].unit_id,)
    assert record["target"] is None and record["status"] is None
    unit = next(u for u in bundle.units if u.unit_id == bundle.records[0].unit_id)
    assert unit.text == text[text.index("## STD-12") : text.index("## STD-13")]
    assert unit.unit_type == "rule" and unit.review_state == "unreviewed"
    assert any(d.code == "unresolved_foundation" for d in parsed.diagnostics)
    identifiers = commit_and_hydrate(tmp_path, bundle, artifacts, registry)
    assert ("rule", "STD-12", unit.unit_id) in identifiers


def test_template_keeps_original_instruction(native_case, tmp_path):
    text = (FIXTURES / "template.md").read_text()
    parsed, artifacts, registry = native_case(text, kind="template@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 1
    value = bundle.records[0].data_json
    assert value["slot"] == "Summary"
    assert (
        value["instruction"]
        == "Describe the result exactly; keep <system> tags as source text."
    )
    assert value["foundation_ids"] == ()
    commit_and_hydrate(tmp_path, bundle, artifacts, registry)


def test_json_feedback_nulls_and_supplied_observation_remain_distinct(
    native_case, tmp_path
):
    text = (FIXTURES / "feedback.json").read_text()
    parsed, artifacts, registry = native_case(
        text,
        kind="feedback@1",
        format="json",
        metadata={
            "title": "Annotation set",
            "author": None,
            "target": None,
            "decision": None,
        },
    )
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 1
    record = bundle.records[0].data_json
    assert record["comment"] == "The chart needs a source."
    assert (
        record["observation"] == "Reviewer supplied interpretation, not the annotation."
    )
    assert (
        record["author"] is None
        and record["target"] is None
        and record["decision"] is None
    )
    assert record["foundation_refs"] == ("EXT-4",) and record["foundation_ids"] == ()
    assert bundle.units[0].text == text[text.index("{") : text.rindex("}") + 1]
    assert (
        bundle.units[0].text_origin == "source"
        and bundle.units[0].review_state == "unreviewed"
    )
    commit_and_hydrate(tmp_path, bundle, artifacts, registry)


@pytest.mark.parametrize(
    "kind,options,text,field,want",
    [
        (
            "standard@1",
            {"requirement_labels": ["Exigence"], "control_labels": ["Contrôle"]},
            "## STD-8\n\nExigence: Garder la preuve.\nContrôle: Vérifier.\n",
            "control",
            "Vérifier.",
        ),
        (
            "feedback@1",
            {"comment_labels": ["Commentaire"], "author_labels": ["Auteur"]},
            "## Note\n\nCommentaire: Clarifier.\nAuteur: Rédacteur\n",
            "author",
            "Rédacteur",
        ),
        (
            "template@1",
            {"slot_labels": ["Champ"], "instruction_labels": ["Consigne"]},
            "## Résumé\n\nChamp: Résumé\nConsigne: Décrire.\n",
            "instruction",
            "Décrire.",
        ),
    ],
)
def test_exact_registered_multilingual_aliases(
    native_case, kind, options, text, field, want, tmp_path
):
    import json

    from tychora.configuration.registry import Registry
    from tychora.configuration.resolve import resolve_config

    config_file = tmp_path / "aliases.toml"
    import shutil

    pack = tmp_path / "pack"
    shutil.copytree(Path(__file__).parents[2] / "src/tychora/profiles/documents", pack)
    kind_file = pack / "kinds" / (kind.split("@")[0] + ".toml")
    original = kind_file.read_text().split("[normalizer_options]")[0]
    kind_file.write_text(
        original
        + "\n[normalizer_options]\n"
        + "\n".join(
            key + "=" + json.dumps(value, ensure_ascii=False)
            for key, value in options.items()
        )
    )
    config_file.write_text('schema_version=1\n[profiles.test]\npath="pack"\n')
    config = resolve_config(config_file, "test", {})
    Registry.from_config(config)
    parsed, _, reg = native_case(text, kind=kind, config_override=config)
    bundle = normalize(parsed, parsed.registration.revision.revision_id, reg)
    assert len(bundle.records) == 1
    assert bundle.records[0].data_json[field] == want


def test_duplicate_rule_identifiers_preserve_ambiguous_lookup(native_case, tmp_path):
    text = "## STD-1\n\nRequirement: First.\n\n## STD-1\n\nRequirement: Second.\n\n## STD-2\n\nRequirement: Third.\nFoundations: STD-1\n"
    parsed, artifacts, registry = native_case(text, kind="standard@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 3
    assert bundle.records[2].data_json["foundation_ids"] == ()
    assert any(d.code == "ambiguous_foundation" for d in parsed.diagnostics)
    identifiers = commit_and_hydrate(tmp_path, bundle, artifacts, registry)
    assert len([row for row in identifiers if row[:2] == ("rule", "STD-1")]) == 2


@pytest.mark.parametrize(
    "body",
    [
        "Requirement: First.\nRequirement: Second.\n",
        "```text\nRequirement: Invented from a fence.\n```\n",
        "Unlabelled body.\n",
    ],
)
def test_ambiguous_or_unlabelled_rule_stays_original_with_diagnostic(native_case, body):
    parsed, _, _registry = native_case("## STD-8\n\n" + body, kind="standard@1")
    assert parsed.records == ()
    assert any(d.code == "ambiguous_record" for d in parsed.diagnostics)
    assert all(u.unit_type != "rule" for u in parsed.units)


def test_sibling_slots_have_separate_original_boundaries(native_case):
    text = "# Form\n\n## First\n\nSlot: First\nInstruction: Record one.\n\n## Second\n\nSlot: Second\nInstruction: Record two.\n"
    parsed, _, registry = native_case(text, kind="template@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 2
    slots = [u for u in bundle.units if u.unit_type == "slot"]
    assert slots[0].text == text[text.index("## First") : text.index("## Second")]
    assert slots[1].text == text[text.index("## Second") :]


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "duplicate_key",
        "trailing_data",
        "missing_close",
        "trailing_comma",
        "foundation_id",
    ],
)
def test_json_rejects_malformed_or_unsupported_records(native_case, change):
    import json

    from tychora.contracts.errors import TychoraError

    value = json.loads((FIXTURES / "feedback.json").read_text())[0]
    if change == "unknown":
        value["unregistered"] = True
    if change == "foundation_id":
        value["foundation_ids"] = ["not-a-canonical-unit"]
    text = json.dumps(value)
    if change == "duplicate_key":
        text = text.replace('"author": null', '"author": null, "author": "Second"')
    if change == "trailing_data":
        text += " false"
    if change == "missing_close":
        text = "[" + text
    if change == "trailing_comma":
        text = "[" + text + ",]"
    with pytest.raises(TychoraError):
        parsed, _, registry = native_case(text, kind="feedback@1", format="json")
        normalize(parsed, parsed.registration.revision.revision_id, registry)


def test_table_proof_and_heading_body_ranges_survive_rule_grouping(native_case):
    text = "## STD-1\n\nRequirement: Keep values.\n\nExpected evidence:\n\n| Name | Value |\n| --- | --- |\n| sample | 2 |\n"
    parsed, _, registry = native_case(text, kind="standard@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert (
        bundle.records[0].data_json["proof"]
        == "| Name | Value |\n| --- | --- |\n| sample | 2 |\n"
    )
    assert any(
        block.cells == (("Name", "Value"), ("sample", "2")) for block in parsed.blocks
    )
    rule = next(unit for unit in parsed.units if unit.unit_type == "rule")
    assert text[rule.char_start : rule.heading_char_end] == "## STD-1\n"
    assert text[rule.heading_char_end : rule.char_end] == text[len("## STD-1\n") :]


def test_generic_evidence_json_retains_literal_provenance_without_circular_ids(
    native_case, tmp_path
):
    import json

    value = {
        "claim": "The sample measured 2 units.",
        "target": "Synthetic sample",
        "provenance": ["Operator supplied reference"],
        "status": "recorded",
        "foundation_ids": [],
        "foundation_refs": ["EXT-2"],
    }
    parsed, artifacts, registry = native_case(
        json.dumps(value), kind="evidence@1", format="json"
    )
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    record = bundle.records[0].data_json
    assert record["claim"] == "The sample measured 2 units."
    assert record["provenance"] == ("Operator supplied reference",)
    assert record["foundation_ids"] == () and record["foundation_refs"] == ("EXT-2",)
    assert bundle.units[0].locators[0].artifact_ref == parsed.original_ref.sha256
    commit_and_hydrate(tmp_path, bundle, artifacts, registry)


def test_feedback_json_preserves_provenance_as_a_source_claim(native_case):
    text = (FIXTURES / "feedback.json").read_text()
    parsed, _, registry = native_case(text, kind="feedback@1", format="json")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert bundle.records[0].data_json["provenance"] == ("Supplied annotation record",)


@pytest.mark.parametrize(
    "kind,text",
    [
        ("standard@1", "Requirement: Keep results.\n"),
        ("template@1", "# Untyped form\n\nEnter something.\n"),
        ("feedback@1", "A comment without registered fields.\n"),
    ],
)
def test_unsupported_native_record_structure_is_diagnosed(native_case, kind, text):
    parsed, _, _ = native_case(text, kind=kind)
    assert parsed.records == ()
    assert parsed.diagnostics


def test_json_cannot_bypass_the_code_adapter(native_case):
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError) as error:
        native_case("{}", kind="code_file@1", format="json")
    assert error.value.code == "CAPABILITY_MISSING"


def test_json_carriage_return_lines_keep_actual_original_coordinates(native_case):
    text = (FIXTURES / "feedback.json").read_text().replace("\n", "\r")
    parsed, _, registry = native_case(text, kind="feedback@1", format="json")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert parsed.units[0].line_start == 2
    assert parsed.units[0].line_end == 12
    loc = bundle.units[0].locators[0]
    assert loc.type == "text"
    assert text[loc.char_start : loc.char_end] == bundle.units[0].text


def test_unicode_line_separator_remains_literal_inside_a_rule(native_case):
    text = "## STD-1\n\nRequirement: Keep\u2028the original wording.\n"
    parsed, _, registry = native_case(text, kind="standard@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert (
        bundle.records[0].data_json["requirement"] == "Keep\u2028the original wording."
    )
    assert next(u for u in parsed.units if u.unit_type == "rule").line_end == 3


@pytest.mark.parametrize(
    "change", ["raw_ref", "record", "normalizer", "rebound_normalizer"]
)
def test_retained_document_binding_rejects_substituted_facts_before_commit(
    native_case, tmp_path, change
):
    import json
    from io import BytesIO

    from pydantic import ValidationError

    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes

    parsed, artifacts, registry = native_case(
        "## STD-1\n\nRequirement: Keep the original requirement.\n", kind="standard@1"
    )
    db = open_corpus(tmp_path / "bound.sqlite3", initialize=True)
    try:
        store = CorpusStore(db, registry=registry, artifacts=artifacts)
        if change == "raw_ref":
            parsed = parsed.model_copy(update={"raw_document_ref": parsed.original_ref})
        elif change == "record":
            record = parsed.records[0]
            parsed = parsed.model_copy(
                update={
                    "records": (
                        record.model_copy(
                            update={
                                "value": FrozenJsonMap(
                                    {
                                        **dict(record.value),
                                        "requirement": "An unrelated invented requirement.",
                                    }
                                )
                            }
                        ),
                    )
                }
            )
        else:
            if change == "rebound_normalizer":
                with artifacts.open_verified(parsed.raw_document_ref) as stream:
                    manifest = json.load(stream)
                manifest["data"]["normalizer_fingerprint"] = "0" * 64
                ref = artifacts.put_stream(
                    BytesIO(canonical_bytes(manifest)), "application/json"
                )
                parsed = parsed.model_copy(update={"raw_document_ref": ref})
            parsed = parsed.model_copy(update={"normalizer_fingerprint": "0" * 64})
        with pytest.raises((TychoraError, ValidationError)):
            store.commit_extraction(
                normalize(parsed, parsed.registration.revision.revision_id, registry)
            )
        assert db.execute("SELECT count(*) FROM extractions").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "kind,label,field,prefix",
    [
        ("standard@1", "Requirement", "requirement", "## STD-1"),
        ("feedback@1", "Comment", "comment", "## Note"),
        ("template@1", "Instruction", "instruction", "## Summary\n\nSlot: Summary"),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_literal_fields_preserve_paragraph_and_physical_line_separators(
    native_case, kind, label, field, prefix, newline
):
    text = (
        prefix
        + "\n\n"
        + label
        + ": First paragraph.\n\nSecond paragraph.\nContinuation.\n"
    ).replace("\n", newline)
    parsed, _, registry = native_case(text, kind=kind)
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert (
        bundle.records[0].data_json[field]
        == "First paragraph."
        + newline
        + newline
        + "Second paragraph."
        + newline
        + "Continuation."
    )


@pytest.mark.parametrize(
    "label",
    [
        "**Requirement:**",
        "**Requirement**:",
        "**Requirement.**",
        "*Requirement:*",
        "*Requirement*:",
        "*Requirement.*",
        "__Requirement:__",
        "_Requirement._",
    ],
)
def test_initial_formatted_label_preserves_exact_literal_value(native_case, label):
    text = "## STD-1\n\n" + label + " Preserve **source** wording.\n"
    parsed, _, registry = native_case(text, kind="standard@1")
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert len(bundle.records) == 1
    assert bundle.records[0].data_json["requirement"] == "Preserve **source** wording."


def test_mixed_bare_and_bold_labels_are_separate_fields(native_case):
    parsed, _, registry = native_case(
        "## STD-1\n\nRequirement: Keep it.\r\n**Control:** Inspect it.\r\n",
        kind="standard@1",
    )
    bundle = normalize(parsed, parsed.registration.revision.revision_id, registry)
    assert bundle.records[0].data_json["requirement"] == "Keep it."
    assert bundle.records[0].data_json["control"] == "Inspect it."


def test_unsupported_formatted_field_continuation_is_diagnosed(native_case):
    parsed, _, _ = native_case(
        "## STD-1\n\nRequirement: Keep it.\n**Control** - Inspect it.\n",
        kind="standard@1",
    )
    assert parsed.records == ()
    assert any(d.code == "ambiguous_record" for d in parsed.diagnostics)


def test_retained_extraction_facts_replay_without_original_payload(
    native_case, tmp_path
):
    import json

    from tychora.contracts.extraction import ExtractionArtifact

    parsed, artifacts, registry = native_case(
        "## STD-1\n\nRequirement: Keep the evidence.\n", kind="standard@1"
    )
    original_bundle = normalize(
        parsed, parsed.registration.revision.revision_id, registry
    )
    commit_and_hydrate(tmp_path, original_bundle, artifacts, registry)
    # Controlled payload loss proves this pure replay boundary needs no original read;
    # owner-authorized purge lifecycle itself belongs to the later maintenance task.
    (artifacts.root / parsed.original_ref.storage_key).unlink()
    with artifacts.open_verified(parsed.raw_document_ref) as stream:
        manifest = json.load(stream)
    restored = ExtractionArtifact.model_validate_json(
        json.dumps(
            {
                **manifest["data"],
                "raw_document_ref": parsed.raw_document_ref.model_dump(mode="json"),
            }
        )
    )
    assert (
        normalize(restored, restored.registration.revision.revision_id, registry)
        == original_bundle
    )
