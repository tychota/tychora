"""Declared synthetic anchors drive a real ingest/query/evaluation integrity run."""

from tychora.contracts.operations import MaintenanceRequest
from tychora.evaluation.dataset import load_dataset


def test_declared_synthetic_corpus_runs_integrity_cases(executable_integrity_corpus):
    kb, gold, case_ids = executable_integrity_corpus
    dataset = load_dataset(gold)
    exact = next(
        case for case in dataset.cases if case.case_id == "synthetic-exact-rule"
    )
    assert exact.query.exact_only
    assert exact.query.exact_identifiers == ("rule:STD-17",)
    restricted = next(
        case for case in dataset.cases if case.case_id == "synthetic-cobalt-allowed"
    )
    assert restricted.query.revision_ids == tuple(restricted.expected_revision_ids)
    unrestricted = kb.search({"question": "cobalt", "modalities": ["lexical"]})
    unrestricted_sources = {segment.source_id for segment in unrestricted.segments}
    assert restricted.expected_source_ids <= unrestricted_sources
    assert restricted.forbidden_source_ids & unrestricted_sources
    kb.evaluations.register_dataset(gold)
    completed = kb.management.run(
        MaintenanceRequest.from_mapping(
            {
                "operation": "evaluate",
                "dataset_id": "synthetic-executable-v1",
                "split": "test",
            }
        )
    )
    assert completed.job.state == "succeeded"
    report = kb.evaluations.get(completed.job.id)
    assert set(report["cases"]) == set(case_ids)
    for case_id in case_ids:
        scored = {
            item["name"]: item
            for item in report["cases"][case_id]["runs"][0]["metrics"]["metrics"]
        }
        assert scored["source_recall"]["value"] == 1.0
        assert scored["revision_recall"]["value"] == 1.0
        assert scored["unit_recall"]["value"] == 1.0
        assert scored["forbidden_leakage"]["value"] == 0.0
        assert scored["locator_validity"]["value"] == 1.0
