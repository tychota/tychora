"""Gold files are version pinned and family separated."""

import json
from pathlib import Path

import pytest

from tychora.evaluation.dataset import load_dataset, load_dataset_bytes


def test_scenario_catalog_is_explicitly_unqualified_and_not_gold():
    fixture_dir = Path(__file__).parent.parent / "fixtures" / "evaluation"
    catalog = json.loads((fixture_dir / "scenario-catalog.json").read_text())
    assert catalog["format"] == "tychora-synthetic-scenarios@1"
    assert catalog["registerable_gold"] is False
    assert catalog["qualification"] == "partial"
    assert len(catalog["scenarios"]) == 13
    assert {
        scenario["id"]
        for scenario in catalog["scenarios"]
        if "mechanical_evidence" in scenario
    } == {
        "exact-id",
        "historic-cutoff",
        "related-edition",
        "mode-global",
        "unknown-review",
        "long-end-match",
    }
    assert not (fixture_dir / "manifest.json").exists()
    assert not (fixture_dir / "cases.jsonl").exists()


def test_load_versioned_immutable_dataset(executable_integrity_corpus):
    _, gold, _ = executable_integrity_corpus
    dataset = load_dataset(gold)
    assert dataset.dataset_id == "synthetic-executable-v1"
    assert {case.split for case in dataset.cases} == {"test"}
    assert len(dataset.cases) == 6
    with pytest.raises((AttributeError, TypeError, ValueError)):
        dataset.cases[0].family_key = "other"


def test_tampered_cases_fail_version_check(tmp_path, executable_integrity_corpus):
    _, gold, _ = executable_integrity_corpus
    (tmp_path / "manifest.json").write_bytes((gold / "manifest.json").read_bytes())
    (tmp_path / "cases.jsonl").write_bytes(
        (gold / "cases.jsonl").read_bytes() + b"{}\n"
    )
    with pytest.raises(ValueError, match="digest"):
        load_dataset(tmp_path)


def test_bytes_registration_uses_same_digest_validation(executable_integrity_corpus):
    _, gold, _ = executable_integrity_corpus
    dataset = load_dataset_bytes(
        (gold / "manifest.json").read_bytes(),
        (gold / "cases.jsonl").read_bytes(),
    )
    assert dataset.dataset_id == "synthetic-executable-v1"
