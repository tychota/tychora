"""Retrieval metrics count selected evidence and expose empty denominators."""

import pytest

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import (
    Citation,
    CodeLocator,
    Coverage,
    Excerpt,
    ImageLocator,
    QueryResult,
    RelationPath,
    Segment,
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.evaluation.dataset import GoldCase
from tychora.evaluation.metrics import metrics, ndcg, recall


def test_empty_denominator_is_not_perfect_quality():
    assert recall(set(), set()) is None
    assert recall({"a", "b"}, {"a"}) == 0.5
    assert ndcg([3, 1], [3, 1]) == pytest.approx(1.0)


def test_ndcg_without_judgements_is_not_applicable():
    assert ndcg([], []) is None


def test_context_only_unit_and_code_file_do_not_count_as_selected():
    case = GoldCase(
        case_id="selected-only",
        family_key="code",
        split="development",
        query=QueryRequest.from_mapping({"question": "Find entry"}),
        expected_unit_ids=frozenset({"selected", "context"}),
        expected_file_ids=frozenset({"source:revision:src/entry.py"}),
        forbidden_unit_ids=frozenset({"forbidden"}),
    )
    locator = CodeLocator(
        artifact_ref="artifact",
        native_ref="line",
        relative_path="src/entry.py",
        char_start=0,
        char_end=4,
        byte_start=0,
        byte_end=4,
        line_start=1,
        line_end=1,
    )
    result = QueryResult(
        run_id="run",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="user"),
        coverage=Coverage(status="complete"),
        segments=(
            Segment(
                segment_id="segment",
                source_id="source",
                revision_id="revision",
                extraction_id="extraction",
                kind="code",
                role="match",
                lane="lexical",
                text="match",
                matched_unit_ids=("selected",),
                context_only_unit_ids=("context", "forbidden"),
                excerpts=(
                    Excerpt(unit_id="selected", char_start=0, char_end=4, text="match"),
                ),
                citations=(Citation(unit_id="context", locators=(locator,)),),
            ),
        ),
    )
    scored = {item.name: item for item in metrics(case, result).metrics}
    assert scored["unit_recall"].value == 0.5
    assert scored["unit_recall"].sample_count == 2
    assert scored["file_recall"].value == 0.0
    assert scored["forbidden_leakage"].value is not None
    assert scored["forbidden_leakage"].value > 0.0
    assert scored["file_mrr"].value == 0.0
    assert scored["document_recall"].value is None
    assert scored["version_precision"].value is None
    assert scored["resolution_rate"].value is None
    assert scored["judged_edge_precision"].value is None


def test_symbol_rank_uses_retained_definition_key_of_selected_unit():
    case = GoldCase(
        case_id="overload",
        family_key="code",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find overload"}),
        expected_definition_ids=frozenset({"definition-two"}),
        forbidden_definition_ids=frozenset({"definition-one"}),
    )
    result = QueryResult(
        run_id="run",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="user"),
        coverage=Coverage(status="complete"),
        segments=(
            Segment(
                segment_id="segment",
                source_id="source",
                revision_id="revision",
                extraction_id="extraction",
                kind="code",
                role="match",
                lane="lexical",
                text="overload",
                matched_unit_ids=("selected",),
                context_only_unit_ids=("context",),
                excerpts=(
                    Excerpt(
                        unit_id="selected", char_start=0, char_end=8, text="overload"
                    ),
                ),
                structured_records=(
                    StructuredRecord(
                        record_id="record-two",
                        unit_id="selected",
                        record_type="code_symbol",
                        schema_id="symbol",
                        value=FrozenJsonMap({"definition_key": "definition-two"}),
                    ),
                    StructuredRecord(
                        record_id="record-one",
                        unit_id="context",
                        record_type="code_symbol",
                        schema_id="symbol",
                        value=FrozenJsonMap({"definition_key": "definition-one"}),
                    ),
                ),
            ),
        ),
    )
    scored = {item.name: item for item in metrics(case, result).metrics}
    assert scored["definition_recall"].value == 1.0
    assert scored["definition_mrr"].value == 1.0
    assert scored["forbidden_leakage"].value is not None
    assert scored["forbidden_leakage"].value > 0.0


def test_relation_support_and_context_excerpt_expose_forbidden_units():
    case = GoldCase(
        case_id="support",
        family_key="support",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find support"}),
        expected_unit_ids=frozenset({"match"}),
        forbidden_unit_ids=frozenset({"hidden-context", "hidden-support"}),
    )
    result = QueryResult(
        run_id="run",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="user"),
        coverage=Coverage(status="complete"),
        segments=(
            Segment(
                segment_id="segment",
                source_id="source",
                revision_id="revision",
                extraction_id="extraction",
                kind="text",
                role="match",
                lane="lexical",
                text="match",
                matched_unit_ids=("match",),
                excerpts=(),
                context_excerpts=(
                    Excerpt(
                        unit_id="hidden-context",
                        char_start=0,
                        char_end=6,
                        text="hidden",
                    ),
                ),
            ),
        ),
        relations=(
            RelationPath(
                unit_ids=("match",),
                assertion_ids=("assertion",),
                predicates=("supports",),
                support_unit_ids=frozenset({"hidden-support"}),
            ),
        ),
    )
    scored = {item.name: item for item in metrics(case, result).metrics}
    assert scored["unit_recall"].value == 1.0
    assert scored["forbidden_leakage"].value is not None
    assert scored["forbidden_leakage"].value > 0.0


def test_file_ndcg_uses_all_judged_relevance_at_query_cutoff():
    case = GoldCase(
        case_id="graded-files",
        family_key="graded",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find files", "limit": 2}),
        expected_file_ids=frozenset({"source:revision:one.py"}),
        file_relevance=FrozenJsonMap(
            {
                "source:revision:one.py": 3,
                "source:revision:two.py": 3,
            }
        ),
    )

    def segment(index, path):
        return Segment(
            segment_id=str(index),
            source_id="source",
            revision_id="revision",
            extraction_id="extraction",
            kind="code",
            role="match",
            lane="lexical",
            text="code",
            matched_unit_ids=(f"unit-{index}",),
            excerpts=(),
            citations=(
                Citation(
                    unit_id=f"unit-{index}",
                    locators=(
                        CodeLocator(
                            artifact_ref="artifact",
                            native_ref=path,
                            relative_path=path,
                            char_start=0,
                            char_end=4,
                            byte_start=0,
                            byte_end=4,
                            line_start=1,
                            line_end=1,
                        ),
                    ),
                ),
            ),
        )

    def result(paths):
        return QueryResult(
            run_id="run",
            snapshot_id="snapshot",
            applied_scope=RequestScope(principal_id="user"),
            coverage=Coverage(status="complete"),
            segments=tuple(segment(index, path) for index, path in enumerate(paths)),
        )

    both = {
        item.name: item for item in metrics(case, result(("one.py", "two.py"))).metrics
    }
    assert both["file_ndcg"].value == pytest.approx(1.0)
    unjudged = {
        item.name: item
        for item in metrics(case, result(("other.py", "one.py"))).metrics
    }
    unjudged_ndcg = unjudged["file_ndcg"].value
    assert isinstance(unjudged_ndcg, float)
    assert 0.0 < unjudged_ndcg < 1.0
    empty = {item.name: item for item in metrics(case, result(())).metrics}
    assert empty["file_ndcg"].value == 0.0


def test_image_ndcg_uses_additional_judged_assets():
    case = GoldCase(
        case_id="graded-images",
        family_key="images",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find images", "limit": 2}),
        expected_asset_ids=frozenset({"asset-one"}),
        asset_relevance=FrozenJsonMap({"asset-one": 3, "asset-two": 3}),
    )
    result = QueryResult(
        run_id="run",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="user"),
        coverage=Coverage(status="complete"),
        visuals=tuple(
            VisualEvidence(
                asset_id=asset_id,
                source_id="source",
                revision_id="revision",
                asset_ref="artifact",
                locator=ImageLocator(artifact_ref="artifact", native_ref=asset_id),
            )
            for asset_id in ("asset-one", "asset-two")
        ),
    )
    scored = {item.name: item for item in metrics(case, result).metrics}
    assert scored["image_ndcg"].value == pytest.approx(1.0)
    duplicated = result.model_copy(
        update={
            "visuals": (
                result.visuals[0],
                result.visuals[0],
                result.visuals[1],
            )
        }
    )
    duplicate_score = {item.name: item for item in metrics(case, duplicated).metrics}
    duplicate_ndcg = duplicate_score["image_ndcg"].value
    assert isinstance(duplicate_ndcg, float)
    assert duplicate_ndcg <= 1.0
    only_one_judged = case.model_copy(
        update={"asset_relevance": FrozenJsonMap({"asset-one": 3})}
    )
    repeated_only = result.model_copy(
        update={
            "visuals": (
                result.visuals[0],
                result.visuals[0],
            )
        }
    )
    repeated_score = {
        item.name: item for item in metrics(only_one_judged, repeated_only).metrics
    }
    assert repeated_score["image_ndcg"].value == 1.0


def test_graded_relevance_keeps_file_and_asset_universes_separate():
    case = GoldCase(
        case_id="mixed",
        family_key="mixed",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find files and images"}),
        expected_file_ids=frozenset({"file-one"}),
        expected_asset_ids=frozenset({"asset-one"}),
        graded_relevance=FrozenJsonMap({"file-one": 3, "asset-one": 1}),
        file_relevance=FrozenJsonMap({"file-two": 2}),
        asset_relevance=FrozenJsonMap({"asset-two": 3}),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        GoldCase(
            case_id="bad",
            family_key="bad",
            split="test",
            query=case.query,
            graded_relevance=FrozenJsonMap({"untyped-extra": 3}),
        )
    assert case.file_relevance["file-two"] == 2
    assert case.asset_relevance["asset-two"] == 3
    result = QueryResult(
        run_id="run",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="user"),
        coverage=Coverage(status="complete"),
        segments=(
            Segment(
                segment_id="file",
                source_id="source",
                revision_id="revision",
                extraction_id="extraction",
                kind="code",
                role="match",
                lane="lexical",
                text="code",
                matched_unit_ids=("unit",),
                excerpts=(),
                citations=(
                    Citation(
                        unit_id="unit",
                        locators=(
                            CodeLocator(
                                artifact_ref="artifact",
                                native_ref="file-one",
                                relative_path="file-one",
                                char_start=0,
                                char_end=4,
                                byte_start=0,
                                byte_end=4,
                                line_start=1,
                                line_end=1,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        visuals=(
            VisualEvidence(
                asset_id="asset-one",
                source_id="source",
                revision_id="revision",
                asset_ref="image",
                locator=ImageLocator(artifact_ref="image", native_ref="asset-one"),
            ),
        ),
    )
    # The file judgement uses canonical source:revision:path identity.
    typed = case.model_copy(
        update={
            "expected_file_ids": frozenset({"source:revision:file-one"}),
            "graded_relevance": FrozenJsonMap(
                {"source:revision:file-one": 3, "asset-one": 1}
            ),
            "file_relevance": FrozenJsonMap({"source:revision:file-two": 2}),
        }
    )
    scored = {item.name: item for item in metrics(typed, result).metrics}
    file_ndcg = scored["file_ndcg"].value
    image_ndcg = scored["image_ndcg"].value
    assert isinstance(file_ndcg, float)
    assert isinstance(image_ndcg, float)
    assert 0.0 < file_ndcg < 1.0
    assert 0.0 < image_ndcg < 1.0
