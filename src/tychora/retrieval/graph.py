"""Coordinate native graph strategies under one pinned request budget."""

from collections.abc import Sequence

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.graph import (
    GraphBudget,
    GraphCandidate,
    GraphCandidates,
    GraphScope,
)
from tychora.ports.execution import ExecutionContext
from tychora.ports.graph import GraphStore
from tychora.retrieval.fusion import FusedCandidate


def _merge(previous: GraphCandidate, addition: GraphCandidate) -> GraphCandidate:
    paths = {path.assertion_ids: path for path in previous.paths}
    for path in addition.paths:
        paths.setdefault(path.assertion_ids, path)
    return GraphCandidate(
        unit_id=previous.unit_id,
        paths=tuple(paths.values())[:8],
        omitted_path_count=max(
            previous.omitted_path_count, addition.omitted_path_count
        ),
        reasons=tuple(dict.fromkeys((*previous.reasons, *addition.reasons))),
        strategy=previous.strategy,
        source_order_distance=previous.source_order_distance
        if previous.source_order_distance is not None
        else addition.source_order_distance,
    )


def augment(
    view: GraphScope,
    fused: Sequence[FusedCandidate],
    config: ResolvedConfig,
    graph: GraphStore,
    ctx: ExecutionContext,
    strategies: frozenset[str] | None = None,
) -> GraphCandidates:
    """Use one fixed seed list, budget and target set across strategies and replay."""
    settings = config.values.retrieval.graph
    if not (settings.semantic or settings.structural == "code_context@1"):
        return GraphCandidates()
    limits = config.values.limits
    seeds = tuple(
        dict.fromkeys(
            item.key[1]
            for item in sorted(fused, key=lambda item: (-item.score, item.key))
            if item.key[0] == "unit" and item.key[1] in view.source_unit_ids
        )
    )[: min(5, limits.graph_seed_limit)]
    if not seeds:
        return GraphCandidates()
    if limits.graph_candidate_limit == 0:
        return GraphCandidates(truncation="candidate_limit", exhausted=False)
    if limits.graph_work_limit == 0:
        return GraphCandidates(truncation="edge_work_limit", exhausted=False)
    budget = GraphBudget(
        max_seeds=limits.graph_seed_limit,
        max_new_units=limits.graph_candidate_limit,
        max_rows=limits.graph_work_limit,
    )
    enabled = tuple(
        name
        for name, active in (
            ("structural", settings.structural == "code_context@1"),
            ("semantic", settings.semantic),
        )
        if active and (strategies is None or name in strategies)
    )
    results: dict[str, GraphCandidate] = {}
    strategy_order: dict[str, list[str]] = {name: [] for name in enabled}
    pending: set[str] = set()
    truncation = None

    def run(name: str) -> GraphCandidates:
        return (
            graph.structural_context(view, seeds, budget, ctx)
            if name == "structural"
            else graph.expand(view, seeds, budget, ctx)
        )

    def collect(name: str) -> None:
        nonlocal truncation
        page = run(name)
        for candidate in (*page.candidates, *page.enrichments):
            if candidate.unit_id not in results:
                strategy_order[name].append(candidate.unit_id)
            if candidate.unit_id in results:
                results[candidate.unit_id] = _merge(
                    results[candidate.unit_id], candidate
                )
            else:
                results[candidate.unit_id] = candidate
        if page.exhausted:
            pending.discard(name)
        else:
            pending.add(name)
            truncation = page.truncation

    for name in enabled:
        if budget.inspected_rows >= budget.max_rows and budget.max_rows > 0:
            pending.add(name)
            truncation = "edge_work_limit"
            continue
        collect(name)
    # A strategy held at its initial share can use released places after its peer
    # exhausts. Replayed native rows remain charged to this same budget.
    for name in enabled:
        if (
            name in pending
            and any(
                other != name and other in budget.completed_strategies
                for other in enabled
            )
            and len(budget.new_unit_ids) < budget.max_new_units
            and budget.inspected_rows < budget.max_rows
        ):
            collect(name)
    exhausted = not pending
    ordered_ids: list[str] = []
    positions = {name: 0 for name in enabled}
    while any(positions[name] < len(strategy_order[name]) for name in enabled):
        for name in enabled:
            if positions[name] < len(strategy_order[name]):
                ordered_ids.append(strategy_order[name][positions[name]])
                positions[name] += 1
    return GraphCandidates(
        candidates=tuple(results[uid] for uid in ordered_ids),
        inspected_rows=budget.inspected_rows,
        truncation=None if exhausted else truncation,
        exhausted=exhausted,
    )
