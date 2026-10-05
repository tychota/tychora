"""Gold files are version pinned and family separated."""

from pathlib import Path

import pytest

from tychora.evaluation.dataset import load_dataset, load_dataset_bytes

FIXTURE = Path(__file__).parent.parent / "fixtures" / "evaluation"


def test_load_versioned_immutable_dataset():
    dataset = load_dataset(FIXTURE)
    assert dataset.dataset_id == "synthetic-integrity-v1"
    assert {case.split for case in dataset.cases} == {"development", "test"}
    assert len(dataset.cases) >= 10
    with pytest.raises((AttributeError, TypeError, ValueError)):
        dataset.cases[0].family_key = "other"


def test_tampered_cases_fail_version_check(tmp_path):
    (tmp_path / "manifest.json").write_bytes((FIXTURE / "manifest.json").read_bytes())
    (tmp_path / "cases.jsonl").write_bytes(
        (FIXTURE / "cases.jsonl").read_bytes() + b"{}\n"
    )
    with pytest.raises(ValueError, match="digest"):
        load_dataset(tmp_path)


def test_bytes_registration_uses_same_digest_validation():
    dataset = load_dataset_bytes(
        (FIXTURE / "manifest.json").read_bytes(),
        (FIXTURE / "cases.jsonl").read_bytes(),
    )
    assert dataset.dataset_id == "synthetic-integrity-v1"
