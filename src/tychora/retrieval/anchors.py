"""Exact producer anchors and retained source-range permission checks."""

from __future__ import annotations

from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import AnchorRange
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.indexing import PreparedExtraction
from tychora.retrieval.rerank import RankedEvidenceItem, RerankMatch


def permitted_ranges(
    prepared: PreparedExtraction,
    unit_id: str,
    start: int,
    end: int,
    view: LegacyEligibilityView,
) -> tuple[tuple[int, int], ...]:
    """Intersect a source range with every canonical owner and current grant."""
    try:
        representation = prepared.representation(unit_id)
    except ValueError:
        raise TychoraError(
            "INTEGRITY_ERROR", "Source anchor has no retained unit."
        ) from None
    if start < 0 or end <= start or end > len(representation.unit.text):
        raise TychoraError("INTEGRITY_ERROR", "Source anchor exceeds retained text.")
    allowed = (
        view.revision_ids
        | view.context_unit_ids
        | view.structural_unit_ids
        | view.asset_ids
    )
    result: list[tuple[int, int]] = []
    cursor = start
    for interval in representation.ownership:
        if interval.char_end <= start or interval.char_start >= end:
            continue
        left, right = max(start, interval.char_start), min(end, interval.char_end)
        if left != cursor:
            raise TychoraError("INTEGRITY_ERROR", "Source ownership has a gap.")
        if interval.dependency_ids <= allowed:
            if result and result[-1][1] == left:
                result[-1] = (result[-1][0], right)
            else:
                result.append((left, right))
        cursor = right
    if cursor != end:
        raise TychoraError("INTEGRITY_ERROR", "Source ownership has a gap.")
    return tuple(result)


def source_anchors(item: RankedEvidenceItem) -> tuple[AnchorRange, ...]:
    """Order original producer matches without parsing model-rendered text."""
    if item.facts is None:
        return ()
    matches = item.facts.matches
    if not matches and item.facts.graph_support is not None:
        matches = (item.facts.graph_support,)
    if not matches:
        return item.anchors
    seen: set[tuple[str, int, int]] = set()
    anchors: list[AnchorRange] = []
    for match in sorted(matches, key=_order):
        key = (match.unit_id, match.char_start, match.char_end)
        if key in seen:
            continue
        seen.add(key)
        anchors.append(
            AnchorRange(
                unit_id=match.unit_id,
                char_start=match.char_start,
                char_end=match.char_end,
                origin=match.channel,
                indexed_window_id=match.window_id,
            )
        )
    return tuple(anchors)


def _order(match: RerankMatch) -> tuple[int, float, int, int, str, str, int, str]:
    return (
        match.rank,
        -match.weight,
        match.window_rank,
        match.char_start,
        match.unit_id,
        match.window_id or "",
        match.char_end,
        match.channel,
    )
