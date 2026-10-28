"""Policy-filtered native graph traversal over a sealed generation."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    AssertionPage,
    AssertionQuery,
    GraphBudget,
    GraphCandidate,
    GraphCandidates,
    GraphManifest,
    GraphPath,
    GraphScope,
    GraphTraversalPolicy,
    graph_manifest_digest,
)
from tychora.contracts.identity import digest
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from .generation import LadybugGraphStore

_PAGE_SIZE = 128
_MAX_OFFSET = 2000


@dataclass(frozen=True)
class _NativePath:
    source: str
    target: str
    assertion_id: str
    supports: frozenset[str]
    predicate: str
    status: str
    confidence: float
    pk: str

    @property
    def order(self) -> tuple[int, float, str, str]:
        return (
            0 if self.status == "confirmed" else 1,
            -self.confidence,
            self.target,
            self.pk,
        )

    def contract(self) -> GraphPath:
        return GraphPath(
            assertion_ids=(self.assertion_id,),
            required_support_ids=self.supports,
            predicate=self.predicate,
            status=cast(
                Literal["recorded", "proposed", "confirmed", "rejected"], self.status
            ),
            confidence=self.confidence,
            source_unit_id=self.source,
            target_unit_id=self.target,
        )


class _PageStream:
    def __init__(
        self,
        traversal: GraphTraversal,
        view: GraphScope,
        seed: str,
        direction: str,
        predicates: tuple[str, ...],
        statuses: tuple[str, ...],
        ctx: ExecutionContext,
    ):
        self.traversal = traversal
        self.view = view
        self.seed = seed
        self.direction = direction
        self.predicates = predicates
        self.statuses = statuses
        self.ctx = ctx
        self.offset = 0
        self.buffer: list[_NativePath] = []
        self.exhausted = False

    def peek(self) -> _NativePath | None:
        if not self.buffer and not self.exhausted:
            self.ctx.checkpoint()
            rows = self.traversal._fetch(
                self.view,
                self.seed,
                self.direction,
                self.predicates,
                self.statuses,
                self.offset,
                self.ctx,
            )
            self.buffer = rows[:_PAGE_SIZE]
            self.offset += len(self.buffer)
            self.exhausted = len(rows) <= _PAGE_SIZE
        return self.buffer[0] if self.buffer else None

    def pop(self) -> _NativePath | None:
        head = self.peek()
        if head is not None:
            self.buffer.pop(0)
        return head


class GraphTraversal:
    def __init__(self, store: LadybugGraphStore, policy: GraphTraversalPolicy):
        self.store = store
        self.policy = policy

    @staticmethod
    def _bind_budget(view: GraphScope, limits: GraphBudget) -> None:
        identity = digest(
            "graph-eligibility-view-v1",
            view.snapshot_id,
            view.policy_epoch,
            view.scope_digest,
            view.eligibility_policy_digest,
            view.eligibility_request_digest,
            sorted(view.selected_batch_ids),
            sorted(view.revision_ids),
            sorted(view.source_unit_ids),
            sorted(view.context_unit_ids),
            sorted(view.structural_unit_ids),
            sorted(view.blocked_assertion_ids),
            view.graph_generation_id,
            view.graph_family_id,
            view.graph_config_sha,
            view.graph_decision_watermark,
            view.graph_manifest_digest,
        )
        if limits.view_digest is None:
            limits.view_digest = identity
        elif limits.view_digest != identity:
            raise TychoraError(
                "SCOPE_CHANGED", "Graph eligibility changed within one request"
            )

    def _candidate_allocation(self, limits: GraphBudget, strategy: str) -> int:
        other = "structural" if strategy == "semantic" else "semantic"
        other_enabled = (
            self.policy.structural == "code_context@1"
            if other == "structural"
            else self.policy.semantic
        )
        if other_enabled and other not in limits.completed_strategies:
            reserved = min(16, limits.max_new_units // 2)
            return min(
                limits.max_new_units,
                len(limits.new_unit_ids)
                + max(0, reserved - limits.strategy_counts.get(strategy, 0)),
            )
        return limits.max_new_units

    def _ready(self, view: GraphScope) -> str:
        gid = view.graph_generation_id
        state = self.store._read_state(gid)
        receipt = state.get("receipt") if state and not state.get("dropping") else None
        if not receipt or any(
            (
                receipt["generation_id"] != gid,
                receipt["family_id"] != view.graph_family_id,
                receipt["config_sha"] != view.graph_config_sha,
                receipt["decision_watermark"] != view.graph_decision_watermark,
                receipt["manifest_digest"] != view.graph_manifest_digest,
            )
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE", "Pinned graph receipt is unavailable"
            )
        return gid

    def _structural_order(
        self, view: GraphScope
    ) -> dict[str, tuple[tuple[str, str, str | None], int]]:
        build = self.store._build
        if build is None or build.spec.generation_id != view.graph_generation_id:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Canonical structural source order is unavailable"
            )
        manifest = GraphManifest.from_build(build)
        if graph_manifest_digest(manifest) != view.graph_manifest_digest:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Structural order differs from sealed generation"
            )
        groups: dict[tuple[str, str, str | None], list[tuple[int, str]]] = {}
        for binding in build.units:
            unit = binding.unit
            if unit.unit_type != "symbol":
                continue
            key = (binding.revision_id, unit.boundary_key, unit.parent_id)
            groups.setdefault(key, []).append((unit.ordinal, unit.unit_id))
        order = {}
        for key, entries in groups.items():
            if len({ordinal for ordinal, _ in entries}) != len(entries):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Structural source order is ambiguous"
                )
            for index, (_, uid) in enumerate(sorted(entries)):
                order[uid] = (key, index)
        return order

    @staticmethod
    def _distance(
        order: dict[str, tuple[tuple[str, str, str | None], int]],
        source: str,
        target: str,
    ) -> int | None:
        left = order.get(source)
        right = order.get(target)
        if left is None or right is None or left[0] != right[0]:
            return None
        return abs(left[1] - right[1])

    def _adjacent_structural(
        self,
        view: GraphScope,
        source: str,
        direction: str,
        order: dict[str, tuple[tuple[str, str, str | None], int]],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> tuple[_NativePath | None, bool]:
        selected = None
        for path in self._iter_edges(
            view,
            source,
            direction,
            ("precedes@1",),
            ("recorded", "confirmed"),
            ctx,
            candidate_ids=view.graph_eligible_unit_ids,
        ):
            if limits.inspected_rows >= limits.max_rows:
                return None, True
            limits.inspected_rows += 1
            if self._distance(order, source, path.target) != 1:
                continue
            before = order[path.target][1] < order[source][1]
            if before != (direction == "incoming"):
                continue
            if selected is None:
                selected = path
            elif path.target != selected.target:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Precedes has multiple immediate neighbors"
                )
        return selected, False

    def _sibling_path(
        self,
        view: GraphScope,
        seed: str,
        target: str,
        order: dict[str, tuple[tuple[str, str, str | None], int]],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> tuple[GraphPath | None, bool]:
        canonical_parent = order[seed][0][2]
        for parent_edge in self._iter_edges(
            view,
            seed,
            "incoming",
            ("contains@1",),
            ("recorded", "confirmed"),
            ctx,
            candidate_ids=view.graph_eligible_unit_ids,
        ):
            if limits.inspected_rows >= limits.max_rows:
                return None, True
            limits.inspected_rows += 1
            if canonical_parent is not None and parent_edge.target != canonical_parent:
                continue
            for child_edge in self._iter_edges(
                view,
                parent_edge.target,
                "outgoing",
                ("contains@1",),
                ("recorded", "confirmed"),
                ctx,
                candidate_ids=view.graph_eligible_unit_ids,
                target_id=target,
            ):
                if limits.inspected_rows >= limits.max_rows:
                    return None, True
                limits.inspected_rows += 1
                return GraphPath(
                    assertion_ids=(parent_edge.assertion_id, child_edge.assertion_id),
                    required_support_ids=parent_edge.supports | child_edge.supports,
                    predicate="contains@1",
                    status="confirmed"
                    if parent_edge.status == child_edge.status == "confirmed"
                    else "recorded",
                    confidence=min(parent_edge.confidence, child_edge.confidence),
                    source_unit_id=seed,
                    target_unit_id=target,
                    assertion_occurrences=2,
                ), False
        return None, False

    def _fetch(
        self,
        view: GraphScope,
        seed: str,
        direction: str,
        predicates: tuple[str, ...],
        statuses: tuple[str, ...],
        offset: int,
        ctx: ExecutionContext,
        *,
        candidate_ids: frozenset[str] | None = None,
        target_id: str | None = None,
    ) -> list[_NativePath]:
        if type(offset) is not int or not 0 <= offset <= _MAX_OFFSET:
            raise ValueError("Graph page offset is outside the request work bound")
        page_size = _PAGE_SIZE + 1
        source_edge, target_edge = (
            ("Subject", "Object") if direction == "outgoing" else ("Object", "Subject")
        )
        target_clause = (
            "AND ve.evidence_id = $target_id" if target_id is not None else ""
        )
        query = f"""
WITH $eligible AS allowed_supports
MATCH (u:Entity)-[:GroundedIn]->(ue:Evidence),
      (a:RelationAssertion)-[:{source_edge}]->(u),
      (a)-[:{target_edge}]->(v:Entity),
      (v)-[:GroundedIn]->(ve:Evidence)
WHERE ue.evidence_id = $seed
  AND u.generation_id = $gid AND v.generation_id = $gid
  AND a.generation_id = $gid AND ue.generation_id = $gid AND ve.generation_id = $gid
  AND ue.evidence_id IN allowed_supports AND ve.evidence_id IN allowed_supports
  AND ve.evidence_id IN $candidate_eligible
  {target_clause}
  AND a.origin_evidence_id IN allowed_supports
  AND a.status IN $statuses AND a.predicate IN $predicates
  AND NOT (a.assertion_id IN $blocked_assertion_ids)
  AND size(a.required_support_ids) > 0
  AND all(eid IN a.required_support_ids WHERE eid IN allowed_supports)
RETURN ue.evidence_id, ve.evidence_id, a.assertion_id,
       a.required_support_ids, a.predicate, a.status, a.confidence, a.pk
ORDER BY CASE a.status WHEN 'confirmed' THEN 0 ELSE 1 END,
         a.confidence DESC, ve.evidence_id, a.pk, u.pk, v.pk
SKIP {offset} LIMIT {page_size}
"""
        ctx.checkpoint()
        rows: list[list[Any]] = self.store._rows(
            query,
            {
                "eligible": sorted(view.graph_eligible_unit_ids),
                "seed": seed,
                "gid": self._ready(view),
                "candidate_eligible": sorted(
                    candidate_ids if candidate_ids is not None else view.source_unit_ids
                ),
                "statuses": list(statuses),
                "predicates": list(predicates),
                "blocked_assertion_ids": sorted(view.blocked_assertion_ids),
                **({"target_id": target_id} if target_id is not None else {}),
            },
        )
        ctx.checkpoint()
        return [
            _NativePath(
                row[0],
                row[1],
                row[2],
                frozenset(row[3]),
                row[4],
                row[5],
                row[6],
                row[7],
            )
            for row in rows
        ]

    def _iter_edges(
        self,
        view: GraphScope,
        seed: str,
        direction: str,
        predicates: tuple[str, ...],
        statuses: tuple[str, ...],
        ctx: ExecutionContext,
        *,
        candidate_ids: frozenset[str] | None = None,
        target_id: str | None = None,
    ):
        offset = 0
        while True:
            rows = self._fetch(
                view,
                seed,
                direction,
                predicates,
                statuses,
                offset,
                ctx,
                candidate_ids=candidate_ids,
                target_id=target_id,
            )
            yield from rows[:_PAGE_SIZE]
            if len(rows) <= _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    def _strategy_enabled(
        self,
        view: GraphScope,
        limits: GraphBudget,
        strategy: Literal["semantic", "structural"],
        enabled: bool,
    ) -> bool:
        """Check the generation and budget binding; a disabled strategy completes empty.

        Callers hold ``self.store._lock`` around this and the traversal it guards.
        """
        self._ready(view)
        self._bind_budget(view, limits)
        if not enabled:
            limits.completed_strategies.add(strategy)
        return enabled

    def expand(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        with self.store._lock:
            if not self._strategy_enabled(
                view, limits, "semantic", self.policy.semantic
            ):
                return GraphCandidates()
            seeds = tuple(dict.fromkeys(seed_unit_ids))
            if (
                len(seeds) > limits.max_seeds
                or len(limits.seed_unit_ids | set(seeds)) > limits.max_seeds
            ):
                raise TychoraError("BUDGET_TOO_SMALL", "Graph seed limit exceeded")
            limits.seed_unit_ids.update(seeds)
            if seeds and limits.inspected_rows >= limits.max_rows:
                return GraphCandidates(truncation="edge_work_limit", exhausted=False)
            streams = []
            for seed in seeds:
                if seed not in view.graph_eligible_unit_ids:
                    continue
                for direction in ("outgoing", "incoming"):
                    for predicate in self.policy.predicates:
                        if direction in predicate.directions:
                            streams.append(
                                _PageStream(
                                    self,
                                    view,
                                    seed,
                                    direction,
                                    (predicate.predicate,),
                                    predicate.statuses,
                                    ctx,
                                )
                            )
            heap: list[tuple[tuple[int, float, str, str], int, _NativePath]] = []
            for index, stream in enumerate(streams):
                if (head := stream.peek()) is not None:
                    heapq.heappush(heap, (head.order, index, head))
            candidate_paths: dict[str, list[GraphPath]] = {}
            enrichment_paths: dict[str, list[GraphPath]] = {}
            omitted_paths: dict[str, int] = {}
            strategy_seen = limits.strategy_unit_ids.setdefault("semantic", set())
            allocation = self._candidate_allocation(limits, "semantic")
            seen = set(seeds) | limits.new_unit_ids
            start_rows = limits.inspected_rows
            truncation = None
            while heap:
                ctx.checkpoint()
                _, index, path = heapq.heappop(heap)
                if limits.inspected_rows + 1 > limits.max_rows:
                    truncation = "edge_work_limit"
                    break
                limits.inspected_rows += 1
                streams[index].pop()
                if (head := streams[index].peek()) is not None:
                    heapq.heappush(heap, (head.order, index, head))
                if path.target in seen:
                    if path.target in candidate_paths:
                        if len(candidate_paths[path.target]) < 8:
                            candidate_paths[path.target].append(path.contract())
                        else:
                            omitted_paths[path.target] = (
                                omitted_paths.get(path.target, 0) + 1
                            )
                    elif (
                        path.target in limits.new_unit_ids
                        and path.target not in strategy_seen
                    ):
                        enrichment_paths[path.target] = [path.contract()]
                        strategy_seen.add(path.target)
                    elif (
                        path.target in enrichment_paths
                        and len(enrichment_paths[path.target]) < 8
                    ):
                        enrichment_paths[path.target].append(path.contract())
                    continue
                if len(limits.new_unit_ids) >= allocation:
                    truncation = "candidate_limit"
                    break
                seen.add(path.target)
                limits.new_unit_ids.add(path.target)
                strategy_seen.add(path.target)
                limits.strategy_counts["semantic"] = (
                    limits.strategy_counts.get("semantic", 0) + 1
                )
                candidate_paths[path.target] = [path.contract()]
            if truncation is None:
                limits.completed_strategies.add("semantic")
            candidates = tuple(
                GraphCandidate(
                    unit_id=uid,
                    paths=tuple(paths),
                    omitted_path_count=omitted_paths.get(uid, 0),
                    reasons=tuple(dict.fromkeys(path.predicate for path in paths)),
                    strategy="semantic",
                )
                for uid, paths in candidate_paths.items()
            )
            return GraphCandidates(
                candidates=candidates,
                enrichments=tuple(
                    GraphCandidate(
                        unit_id=uid,
                        paths=tuple(paths),
                        reasons=tuple(dict.fromkeys(path.predicate for path in paths)),
                        strategy="semantic",
                    )
                    for uid, paths in enrichment_paths.items()
                ),
                inspected_rows=limits.inspected_rows - start_rows,
                truncation=truncation,
                exhausted=truncation is None,
            )

    def structural_context(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        with self.store._lock:
            if not self._strategy_enabled(
                view, limits, "structural", self.policy.structural == "code_context@1"
            ):
                return GraphCandidates()
            order = self._structural_order(view)
            seeds = tuple(dict.fromkeys(seed_unit_ids))
            if len(limits.seed_unit_ids | set(seeds)) > limits.max_seeds:
                raise TychoraError("BUDGET_TOO_SMALL", "Graph seed limit exceeded")
            limits.seed_unit_ids.update(seeds)
            if seeds and limits.inspected_rows >= limits.max_rows:
                return GraphCandidates(truncation="edge_work_limit", exhausted=False)
            candidates: list[GraphCandidate] = []
            enrichments: list[GraphCandidate] = []
            strategy_seen = limits.strategy_unit_ids.setdefault("structural", set())
            start_rows = limits.inspected_rows
            truncation = None
            allocation = self._candidate_allocation(limits, "structural")
            for seed in seeds:
                if seed not in view.graph_eligible_unit_ids or seed not in order:
                    continue
                immediate: dict[str, _NativePath] = {}
                for direction, reason in (
                    ("incoming", "preceding_definition"),
                    ("outgoing", "following_definition"),
                ):
                    selected, work_limit = self._adjacent_structural(
                        view, seed, direction, order, limits, ctx
                    )
                    if work_limit:
                        truncation = "edge_work_limit"
                        break
                    if selected is None:
                        continue
                    immediate[direction] = selected
                    target = selected.target
                    if target not in view.source_unit_ids or target in seeds:
                        continue
                    candidate = GraphCandidate(
                        unit_id=target,
                        paths=(selected.contract(),),
                        reasons=(reason,),
                        strategy="structural",
                        source_order_distance=1,
                    )
                    if target in limits.new_unit_ids:
                        if target not in strategy_seen:
                            strategy_seen.add(target)
                            enrichments.append(candidate)
                        continue
                    if len(limits.new_unit_ids) >= allocation:
                        truncation = "candidate_limit"
                        break
                    limits.new_unit_ids.add(target)
                    strategy_seen.add(target)
                    limits.strategy_counts["structural"] = (
                        limits.strategy_counts.get("structural", 0) + 1
                    )
                    candidates.append(candidate)
                if truncation:
                    break

                frontier = {
                    direction: path.target for direction, path in immediate.items()
                }
                immediate_ids = set(frontier.values())
                additional = 0
                while frontier and additional < 2:
                    options: list[tuple[int, int, str]] = []
                    next_frontier: dict[str, str] = {}
                    for direction in ("incoming", "outgoing"):
                        source = frontier.get(direction)
                        if source is None:
                            continue
                        selected, work_limit = self._adjacent_structural(
                            view, source, direction, order, limits, ctx
                        )
                        if work_limit:
                            truncation = "edge_work_limit"
                            break
                        if selected is None:
                            continue
                        next_frontier[direction] = selected.target
                        distance = self._distance(order, seed, selected.target)
                        if distance is not None and distance > 1:
                            options.append(
                                (
                                    distance,
                                    0 if direction == "incoming" else 1,
                                    selected.target,
                                )
                            )
                    if truncation:
                        break
                    for distance, _, target in sorted(options):
                        if additional >= 2:
                            break
                        if (
                            target not in view.source_unit_ids
                            or target in seeds
                            or target in immediate_ids
                        ):
                            continue
                        sibling, work_limit = self._sibling_path(
                            view, seed, target, order, limits, ctx
                        )
                        if work_limit:
                            truncation = "edge_work_limit"
                            break
                        if sibling is None:
                            continue
                        candidate = GraphCandidate(
                            unit_id=target,
                            paths=(sibling,),
                            reasons=("same_parent",),
                            strategy="structural",
                            source_order_distance=distance,
                        )
                        if target in limits.new_unit_ids:
                            if target not in strategy_seen:
                                strategy_seen.add(target)
                                enrichments.append(candidate)
                            additional += 1
                            continue
                        if len(limits.new_unit_ids) >= allocation:
                            truncation = "candidate_limit"
                            break
                        limits.new_unit_ids.add(target)
                        strategy_seen.add(target)
                        limits.strategy_counts["structural"] = (
                            limits.strategy_counts.get("structural", 0) + 1
                        )
                        candidates.append(candidate)
                        additional += 1
                    if truncation:
                        break
                    frontier = next_frontier
                if truncation:
                    break
            if truncation is None:
                limits.completed_strategies.add("structural")
            return GraphCandidates(
                candidates=tuple(candidates),
                enrichments=tuple(enrichments),
                inspected_rows=limits.inspected_rows - start_rows,
                truncation=truncation,
                exhausted=truncation is None,
            )

    def get_assertions(
        self,
        view: GraphScope,
        query: AssertionQuery,
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> AssertionPage:
        with self.store._lock:
            self._ready(view)
            self._bind_budget(view, limits)
            policies = {
                item.predicate: item
                for item in (
                    *self.policy.predicates,
                    *self.policy.inspection_predicates,
                )
            }
            permitted = set(policies)
            if set(query.predicates) - permitted:
                raise TychoraError(
                    "INVALID_REQUEST", "Unregistered inspection predicate"
                )
            if query.relation_view is not None:
                policies = {
                    name: policy
                    for name, policy in policies.items()
                    if policy.view == query.relation_view
                }
            predicates = tuple(name for name in query.predicates if name in policies)
            if query.selected_unit_ids and limits.inspected_rows >= limits.max_rows:
                return AssertionPage(truncation="edge_work_limit", exhausted=False)
            frontier = set(query.selected_unit_ids) & view.graph_eligible_unit_ids
            visited_units = set(frontier)
            seen_assertions: set[str] = set()
            assertions: list[GraphPath] = []
            truncation = None
            for _ in range(limits.max_inspection_hops):
                next_frontier: set[str] = set()
                for seed in sorted(frontier):
                    for predicate in predicates:
                        rule = policies[predicate]
                        directions = (
                            rule.directions
                            if query.direction in (None, "both")
                            else tuple(
                                item
                                for item in rule.directions
                                if item == query.direction
                            )
                        )
                        for edge_direction in directions:
                            rows = self._iter_edges(
                                view,
                                seed,
                                edge_direction,
                                (predicate,),
                                rule.statuses,
                                ctx,
                                candidate_ids=view.graph_eligible_unit_ids,
                            )
                            for path in rows:
                                if limits.inspected_rows >= limits.max_rows:
                                    truncation = "edge_work_limit"
                                    break
                                limits.inspected_rows += 1
                                if path.assertion_id in seen_assertions:
                                    continue
                                if (
                                    limits.inspected_edges
                                    >= limits.max_inspection_edges
                                ):
                                    truncation = "edge_limit"
                                    break
                                seen_assertions.add(path.assertion_id)
                                limits.inspected_edges += 1
                                assertions.append(path.contract())
                                if path.target not in visited_units:
                                    next_frontier.add(path.target)
                            if truncation:
                                break
                        if truncation:
                            break
                    if truncation:
                        break
                if truncation or not next_frontier:
                    break
                visited_units.update(next_frontier)
                frontier = next_frontier
            return AssertionPage(
                assertions=tuple(
                    sorted(
                        assertions,
                        key=lambda path: (
                            0 if path.status == "confirmed" else 1,
                            -path.confidence,
                            path.target_unit_id,
                            path.assertion_ids,
                        ),
                    )
                ),
                inspected_edges=len(assertions),
                truncation=truncation,
                exhausted=truncation is None,
            )
