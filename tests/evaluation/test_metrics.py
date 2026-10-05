"""Retrieval metrics count selected evidence and expose empty denominators."""

import pytest

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import (
    Citation,
    CodeLocator,
    Coverage,
    Excerpt,
    QueryResult,
    Segment,
    StructuredRecord,
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
    assert scored["forbidden_leakage"].value == 0.0
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
    assert scored["forbidden_leakage"].value == 0.0
