"""Retrieval scores with explicit judged denominators."""

from __future__ import annotations

import math

from tychora.contracts.base import FrozenJsonValue
from tychora.contracts.evidence import (
    CodeLocator,
    DocumentLocator,
    QueryResult,
    TextLocator,
)
from tychora.contracts.operations import MetricSet, MetricValue
from tychora.evaluation.dataset import GoldCase


def recall(expected: set[str], returned: set[str]) -> float | None:
    return len(expected & returned) / len(expected) if expected else None


def mrr(expected: set[str], ranked: list[str]) -> float | None:
    if not expected:
        return None
    return next(
        (1 / rank for rank, key in enumerate(ranked, 1) if key in expected), 0.0
    )


def ndcg(grades: list[int], ideal_grades: list[int]) -> float | None:
    def gain(values: list[int]) -> float:
        return sum(
            (2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(values)
        )

    ideal = gain(ideal_grades)
    return gain(grades) / ideal if ideal else None


def span_coverage(
    expected: set[tuple[str, int, int]], returned: set[tuple[str, int, int]]
) -> float | None:
    return len(expected & returned) / len(expected) if expected else None


def _value(name: str, numerator: int, denominator: int) -> MetricValue:
    return MetricValue(
        name=name,
        value=numerator / denominator if denominator else None,
        sample_count=denominator,
        denominator=denominator,
        not_applicable_reason=None if denominator else "no judged items",
    )


def _scored(name: str, value: float | None, denominator: int) -> MetricValue:
    return MetricValue(
        name=name,
        value=value,
        sample_count=denominator,
        denominator=denominator,
        not_applicable_reason=None if value is not None else "no judged items",
    )


def _grade(value: FrozenJsonValue) -> int:
    if type(value) is not int:
        raise ValueError("Relevance grade is not an integer")
    return value


def metrics(case: GoldCase, result: QueryResult) -> MetricSet:
    """Score only actually selected evidence; context excerpts are excluded."""
    if not isinstance(case, GoldCase):
        raise TypeError("Expected a labelled gold case")
    selected = [segment for segment in result.segments if segment.matched_unit_ids]
    source_ids = {segment.source_id for segment in selected} | {
        visual.source_id for visual in result.visuals
    }
    revision_ids = {segment.revision_id for segment in selected} | {
        visual.revision_id for visual in result.visuals
    }
    document_ids = {
        f"{segment.source_id}:{segment.revision_id}" for segment in selected
    } | {f"{visual.source_id}:{visual.revision_id}" for visual in result.visuals}
    unit_ids = {unit for segment in selected for unit in segment.matched_unit_ids}
    asset_ids = {visual.asset_id for visual in result.visuals}
    file_rank: list[str] = []
    symbol_rank: list[str] = []
    spans = {
        (excerpt.unit_id, excerpt.char_start, excerpt.char_end)
        for segment in selected
        for excerpt in segment.excerpts
        if excerpt.unit_id in segment.matched_unit_ids
    }
    valid_locators = 0
    locator_count = 0
    judged_locators: set[tuple[str, int, int]] = set()
    for segment in selected:
        for citation in segment.citations:
            for locator in citation.locators:
                locator_count += 1
                attached = citation.unit_id in (
                    set(segment.matched_unit_ids) | set(segment.context_only_unit_ids)
                )
                valid_locators += int(
                    attached
                    and (
                        locator.char_end >= locator.char_start
                        if isinstance(
                            locator, (TextLocator, DocumentLocator, CodeLocator)
                        )
                        else True
                    )
                )
                if (
                    attached
                    and citation.unit_id in segment.matched_unit_ids
                    and isinstance(locator, (TextLocator, DocumentLocator, CodeLocator))
                ):
                    judged_locators.add(
                        (citation.unit_id, locator.char_start, locator.char_end)
                    )
                if (
                    isinstance(locator, CodeLocator)
                    and citation.unit_id in segment.matched_unit_ids
                ):
                    key = f"{segment.source_id}:{segment.revision_id}:{locator.relative_path}"
                    if key not in file_rank:
                        file_rank.append(key)
        for record in segment.structured_records:
            if (
                record.record_type == "code_symbol"
                and record.unit_id in segment.matched_unit_ids
            ):
                key = record.value.get("definition_key")
                if isinstance(key, str) and key not in symbol_rank:
                    symbol_rank.append(key)
    returned_paths = {
        tuple(path.assertion_ids) for path in result.relations if path.assertion_ids
    }
    expected_paths = {tuple(path) for path in case.expected_paths}
    returned_edges = {
        assertion for path in result.relations for assertion in path.assertion_ids
    }
    forbidden = (
        len(case.forbidden_source_ids & source_ids)
        + len(case.forbidden_revision_ids & revision_ids)
        + len(case.forbidden_unit_ids & unit_ids)
        + len(case.forbidden_asset_ids & asset_ids)
        + len(case.forbidden_definition_ids & set(symbol_rank))
        + len(case.forbidden_file_ids & set(file_rank))
    )
    expected_spans = {
        (span.unit_id, span.char_start, span.char_end) for span in case.expected_spans
    }
    grades = [_grade(case.graded_relevance.get(key, 0)) for key in file_rank]
    ideal = sorted(
        (_grade(case.graded_relevance.get(key, 0)) for key in case.expected_file_ids),
        reverse=True,
    )
    image_grades = [
        _grade(case.graded_relevance.get(visual.asset_id, 0))
        for visual in result.visuals
    ]
    image_ideal = sorted(
        (
            _grade(case.graded_relevance.get(asset_id, 0))
            for asset_id in case.expected_asset_ids
        ),
        reverse=True,
    )
    return MetricSet(
        metrics=(
            _value(
                "source_recall",
                len(case.expected_source_ids & source_ids),
                len(case.expected_source_ids),
            ),
            _value(
                "document_recall",
                len(case.expected_document_ids & document_ids),
                len(case.expected_document_ids),
            ),
            _value(
                "revision_recall",
                len(case.expected_revision_ids & revision_ids),
                len(case.expected_revision_ids),
            ),
            _value(
                "version_precision",
                len(case.expected_revision_ids & revision_ids),
                len(revision_ids) if case.expected_revision_ids else 0,
            ),
            _value(
                "unit_recall",
                len(case.expected_unit_ids & unit_ids),
                len(case.expected_unit_ids),
            ),
            _value(
                "asset_recall",
                len(case.expected_asset_ids & asset_ids),
                len(case.expected_asset_ids),
            ),
            _value(
                "file_recall",
                len(case.expected_file_ids & set(file_rank)),
                len(case.expected_file_ids),
            ),
            _value(
                "definition_recall",
                len(case.expected_definition_ids & set(symbol_rank)),
                len(case.expected_definition_ids),
            ),
            _scored(
                "file_mrr",
                mrr(set(case.expected_file_ids), file_rank),
                len(case.expected_file_ids),
            ),
            _scored(
                "definition_mrr",
                mrr(set(case.expected_definition_ids), symbol_rank),
                len(case.expected_definition_ids),
            ),
            _scored("file_ndcg", ndcg(grades, ideal), len(ideal)),
            _scored("image_ndcg", ndcg(image_grades, image_ideal), len(image_ideal)),
            _value(
                "span_completeness", len(expected_spans & spans), len(expected_spans)
            ),
            _value(
                "locator_precision",
                len(expected_spans & judged_locators),
                len(judged_locators) if expected_spans else 0,
            ),
            _value(
                "path_recall", len(expected_paths & returned_paths), len(expected_paths)
            ),
            _value(
                "path_precision",
                len(expected_paths & returned_paths),
                len(returned_paths),
            ),
            _value(
                "judged_edge_precision",
                len(case.expected_edge_ids & returned_edges),
                len(returned_edges) if case.expected_edge_ids else 0,
            ),
            MetricValue(
                name="resolution_rate",
                value=None,
                sample_count=0,
                denominator=0,
                not_applicable_reason="extracted references are not exposed in QueryResult",
            ),
            _value("locator_validity", valid_locators, locator_count),
            _value(
                "forbidden_leakage",
                forbidden,
                len(source_ids)
                + len(revision_ids)
                + len(unit_ids)
                + len(asset_ids)
                + len(file_rank)
                + len(symbol_rank),
            ),
        )
    )
