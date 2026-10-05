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
    late = next(case for case in dataset.cases if case.case_id == "long-end-match")
    late_result = kb.search(late.query)
    late_segments = [
        segment
        for segment in late_result.segments
        if segment.source_id in late.expected_source_ids
    ]
    assert late_segments
    declared_tail = "The declared lateanchor closes this long synthetic note."
    original = "synthetic filler " * 2000 + declared_tail
    anchor_start = original.index("lateanchor")
    assert any(
        excerpt.char_start + excerpt.text.index("lateanchor") == anchor_start
        and excerpt.char_end == len(original)
        for segment in late_segments
        for excerpt in segment.excerpts
        if "lateanchor" in excerpt.text
    )
    assert any(
        "lateanchor" in excerpt.text
        for segment in late_segments
        for excerpt in segment.excerpts
    )
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
