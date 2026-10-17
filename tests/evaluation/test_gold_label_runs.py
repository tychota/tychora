"""A label-based gold set runs end to end and reports unresolved cases."""

from hashlib import sha256

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import MaintenanceRequest

TEXT = "Alpha beta zx00001q gamma."


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def _case(case_id: str, source_key: str) -> dict:
    start = TEXT.index("zx00001q")
    return {
        "case_id": case_id,
        "family_key": case_id,
        "split": "test",
        "query": {"question": "zx00001q", "modalities": ["lexical"]},
        "gold_labels": [
            {
                "collection": "default",
                "source_key": source_key,
                "anchor": {"char_start": start, "char_end": start + 8},
            }
        ],
    }


def _register(kb, root, dataset_id: str, cases: tuple[dict, ...]) -> None:
    gold = root / dataset_id
    gold.mkdir()
    payload = b"".join(canonical_bytes(case) + b"\n" for case in cases)
    (gold / "cases.jsonl").write_bytes(payload)
    (gold / "manifest.json").write_bytes(
        canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": dataset_id,
                "name": "Label-based gold",
                "version_sha": sha256(payload).hexdigest(),
                "case_count": len(cases),
            }
        )
    )
    kb.evaluations.register_dataset(gold)


def _evaluate(kb, dataset_id: str):
    return kb.management.run(
        MaintenanceRequest.from_mapping(
            {"operation": "evaluate", "dataset_id": dataset_id, "split": "test"}
        )
    )


def _open_with_guide(tmp_path):
    kb = Tychora.open(tmp_path / "ws", token_counter=Counter())
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": "guide",
            "collection_id": "default",
            "kind": "document@1",
            "format": "text",
            "text": TEXT,
            "metadata": {"title": "Guide"},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"
    return kb


def test_label_based_dataset_scores_and_reports_failures(tmp_path):
    with _open_with_guide(tmp_path) as kb:
        _register(
            kb, tmp_path, "labels-v1", (_case("good", "guide"), _case("bad", "absent"))
        )
        completed = _evaluate(kb, "labels-v1")
        assert completed.job.state == "succeeded"
        stored = kb.evaluations.get(completed.job.id)
    assert set(stored["cases"]) == {"good"}
    assert [
        (item["case_id"], item["reason"]) for item in stored["report"]["label_failures"]
    ] == [("bad", "label.missing")]
    scored = {
        item["name"]: item["value"]
        for item in stored["cases"]["good"]["runs"][0]["metrics"]["metrics"]
    }
    assert scored["unit_recall"] == 1.0


def test_run_without_a_resolvable_case_is_rejected_with_the_label_reason(tmp_path):
    with _open_with_guide(tmp_path) as kb:
        _register(kb, tmp_path, "labels-none", (_case("bad", "absent"),))
        with pytest.raises(TychoraError) as error:
            _evaluate(kb, "labels-none")
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.reason == "label.missing"
