"""Coverage of one query: unavailable channels make it partial, bounds do not."""

from collections.abc import Iterable

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.evidence import Coverage


def build_coverage(
    *,
    completed: Iterable[str],
    unavailable: Iterable[str],
    truncated: Iterable[str],
    rerank_failed: bool,
    graph_exhausted: bool,
) -> Coverage:
    """Partial only when a channel could not run; bounded scans are truncated."""
    missing = (*unavailable, *(("rerank",) if rerank_failed else ()))
    bounded = (*truncated, *(() if graph_exhausted else ("graph",)))
    return Coverage(
        status="partial" if missing else "complete",
        completed_channels=tuple(completed),
        unavailable_channels=tuple(dict.fromkeys(missing)),
        truncated_channels=tuple(dict.fromkeys(bounded)),
    )


def missing_batch_slots(view: EligibilityView, slots: Iterable[str]) -> tuple[str, ...]:
    """Slots whose selected batches do not serve every eligible extraction."""
    return tuple(
        slot
        for slot in slots
        if view.extraction_ids - view.batch_extractions.get(slot, frozenset())
    )
