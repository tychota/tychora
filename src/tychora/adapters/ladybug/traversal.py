"""Policy-filtered native graph traversal over a sealed generation."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    AssertionPage,
    EligibilityView,
    GraphBudget,
    GraphCandidate,
    GraphCandidates,
    GraphManifest,
    GraphPath,
    GraphTraversalPolicy,
)
from tychora.contracts.identity import digest
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.configuration.models import ResolvedConfig
    from tychora.configuration.registry import Registry

    from .generation import LadybugGraphStore

_PAGE_SIZE = 128
_MAX_OFFSET = 2000


def resolve_graph_traversal_policy(
    config: ResolvedConfig, registry: Registry
) -> GraphTraversalPolicy:
    """Bind a validated profile and registry to native traversal permissions."""
    from tychora.contracts.graph import GraphPredicatePolicy

    if config.registry_digest != registry.registry_digest:
        raise TychoraError(
            "INVALID_REQUEST", "Graph registry differs from resolved profile"
        )
    settings = config.values.retrieval.graph
    continuation = config.values.retrieval.table_continuations
    predicates = []
    for predicate in settings.predicates:
        definition = registry.relations.get(predicate)
        if not isinstance(definition, FrozenJsonMap):
            raise TychoraError("INVALID_REQUEST", "Graph predicate is not registered")
        search_directions = cast(
            tuple[str, ...], definition.get("search_directions", ())
        )
        directions = tuple(settings.directions)
        if not directions or set(directions) - set(search_directions):
            raise TychoraError(
                "INVALID_REQUEST", "Graph predicate has no permitted search direction"
            )
        statuses = tuple(
            status
            for status in settings.statuses
            if status in ("recorded", "confirmed")
        )
        if len(statuses) != len(settings.statuses):
            raise TychoraError("INVALID_REQUEST", "Graph status is not searchable")
        if not settings.semantic:
            continue
        if predicate == "continues_table@1":
            if continuation == "none":
                continue
            if continuation == "confirmed_only":
                statuses = tuple(status for status in statuses if status == "confirmed")
        if statuses:
            predicates.append(
                GraphPredicatePolicy(
                    predicate=predicate, directions=directions, statuses=statuses
                )
            )
    if settings.structural == "code_context@1":
        for predicate in ("contains@1", "precedes@1"):
            definition = registry.relations.get(predicate)
            if not isinstance(definition, FrozenJsonMap) or set(
                cast(tuple[str, ...], definition.get("search_directions", ()))
            ) != {"outgoing", "incoming"}:
                raise TychoraError(
                    "INVALID_REQUEST", "Code structural predicate is unavailable"
                )
    inspection = []
    for predicate, definition in sorted(registry.relations.items()):
        if not isinstance(definition, FrozenJsonMap):
            continue
        directions = cast(tuple[str, ...], definition.get("inspection_directions", ()))
        if (
            not directions
            or predicate == "continues_table@1"
            and continuation == "none"
        ):
            continue
        statuses = ("recorded", "proposed", "confirmed", "rejected")
        if predicate == "continues_table@1" and continuation == "confirmed_only":
            statuses = ("confirmed",)
        inspection.append(
            GraphPredicatePolicy(
                predicate=predicate,
                directions=cast(
                    tuple[Literal["outgoing", "incoming"], ...], directions
                ),
                statuses=statuses,
            )
        )
    return GraphTraversalPolicy(
        predicates=tuple(predicates),
        inspection_predicates=tuple(inspection),
        structural=settings.structural,
        semantic=settings.semantic,
        table_continuations=continuation,
    )


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
        view: EligibilityView,
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
    def _bind_budget(view: EligibilityView, limits: GraphBudget) -> None:
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

    def _ready(self, view: EligibilityView) -> str:
        gid = view.graph_generation_id
        if gid is None:
            raise TychoraError("INDEX_INCOMPLETE", "Graph generation is not selected")
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
        self, view: EligibilityView
    ) -> dict[str, tuple[tuple[str, str, str | None], int]]:
        build = self.store._build
        if build is None or build.spec.generation_id != view.graph_generation_id:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Canonical structural source order is unavailable"
            )
        manifest = GraphManifest.from_build(build)
        if (
            digest("graph-manifest-v1", manifest.model_dump(mode="json"))
            != view.graph_manifest_digest
        ):
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

    def _fetch(
        self,
        view: EligibilityView,
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
        view: EligibilityView,
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

    def expand(
        self,
        view: EligibilityView,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        with self.store._lock:
            self._ready(view)
            self._bind_budget(view, limits)
            if not self.policy.semantic:
                limits.completed_strategies.add("semantic")
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
        view: EligibilityView,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        with self.store._lock:
            self._ready(view)
            self._bind_budget(view, limits)
            if self.policy.structural != "code_context@1":
                limits.completed_strategies.add("structural")
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
                if seed not in view.graph_eligible_unit_ids:
                    continue
                immediate: dict[str, _NativePath] = {}
                for direction, reason in (
                    ("incoming", "preceding_definition"),
                    ("outgoing", "following_definition"),
                ):
                    paths = self._iter_edges(
                        view,
                        seed,
                        direction,
                        ("precedes@1",),
                        ("recorded", "confirmed"),
                        ctx,
                        candidate_ids=view.graph_eligible_unit_ids,
                    )
                    selected = None
                    for path in paths:
                        if limits.inspected_rows >= limits.max_rows:
                            truncation = "edge_work_limit"
                            break
                        limits.inspected_rows += 1
                        if selected is None:
                            selected = path
                        elif path.target != selected.target:
                            raise TychoraError(
                                "INTEGRITY_ERROR",
                                "Precedes has multiple immediate neighbors",
                            )
                    if truncation:
                        break
                    if selected is None:
                        continue
                    distance = self._distance(order, seed, selected.target)
                    if distance is None or distance == 0:
                        continue
                    if (
                        direction == "incoming"
                        and order[selected.target][1] >= order[seed][1]
                    ):
                        continue
                    if (
                        direction == "outgoing"
                        and order[selected.target][1] <= order[seed][1]
                    ):
                        continue
                    immediate[direction] = selected
                    if (
                        selected.target not in view.source_unit_ids
                        or selected.target in seeds
                    ):
                        continue
                    if selected.target in limits.new_unit_ids:
                        if selected.target not in strategy_seen:
                            strategy_seen.add(selected.target)
                            enrichments.append(
                                GraphCandidate(
                                    unit_id=selected.target,
                                    paths=(selected.contract(),),
                                    reasons=(reason,),
                                    strategy="structural",
                                    source_order_distance=distance,
                                )
                            )
                        continue
                    if len(limits.new_unit_ids) >= allocation:
                        truncation = "candidate_limit"
                        break
                    limits.new_unit_ids.add(selected.target)
                    strategy_seen.add(selected.target)
                    limits.strategy_counts["structural"] = (
                        limits.strategy_counts.get("structural", 0) + 1
                    )
                    candidates.append(
                        GraphCandidate(
                            unit_id=selected.target,
                            paths=(selected.contract(),),
                            reasons=(reason,),
                            strategy="structural",
                            source_order_distance=distance,
                        )
                    )
                if truncation:
                    break
                extras: list[tuple[int, int, str, _NativePath]] = []
                for direction in ("incoming", "outgoing"):
                    neighbor = immediate.get(direction)
                    if neighbor is None:
                        continue
                    paths = self._iter_edges(
                        view,
                        neighbor.target,
                        direction,
                        ("precedes@1",),
                        ("recorded", "confirmed"),
                        ctx,
                        candidate_ids=view.graph_eligible_unit_ids,
                    )
                    selected = None
                    for path in paths:
                        if limits.inspected_rows >= limits.max_rows:
                            truncation = "edge_work_limit"
                            break
                        limits.inspected_rows += 1
                        if selected is None:
                            selected = path
                        elif path.target != selected.target:
                            raise TychoraError(
                                "INTEGRITY_ERROR",
                                "Precedes has multiple immediate neighbors",
                            )
                    if truncation:
                        break
                    if (
                        selected
                        and selected.target != seed
                        and selected.target
                        not in {p.target for p in immediate.values()}
                    ):
                        distance = self._distance(order, seed, selected.target)
                        if distance is not None and distance > 0:
                            extras.append(
                                (
                                    distance,
                                    0 if direction == "incoming" else 1,
                                    selected.target,
                                    selected,
                                )
                            )
                if truncation:
                    break
                for distance, _, target, _ in sorted(extras)[:2]:
                    if target not in view.source_unit_ids or target in seeds:
                        continue
                    parent_edges = self._iter_edges(
                        view,
                        seed,
                        "incoming",
                        ("contains@1",),
                        ("recorded", "confirmed"),
                        ctx,
                        candidate_ids=view.graph_eligible_unit_ids,
                    )
                    matched = None
                    for parent_edge in parent_edges:
                        if limits.inspected_rows >= limits.max_rows:
                            truncation = "edge_work_limit"
                            break
                        limits.inspected_rows += 1
                        child_edges = self._iter_edges(
                            view,
                            parent_edge.target,
                            "outgoing",
                            ("contains@1",),
                            ("recorded", "confirmed"),
                            ctx,
                            candidate_ids=view.graph_eligible_unit_ids,
                            target_id=target,
                        )
                        for child_edge in child_edges:
                            if limits.inspected_rows >= limits.max_rows:
                                truncation = "edge_work_limit"
                                break
                            limits.inspected_rows += 1
                            matched = (parent_edge, child_edge)
                            break
                        if matched or truncation:
                            break
                    if truncation:
                        break
                    if matched is None:
                        continue
                    first, second = matched
                    path = GraphPath(
                        assertion_ids=(first.assertion_id, second.assertion_id),
                        required_support_ids=first.supports | second.supports,
                        predicate="contains@1",
                        status="confirmed"
                        if first.status == second.status == "confirmed"
                        else "recorded",
                        confidence=min(first.confidence, second.confidence),
                        source_unit_id=seed,
                        target_unit_id=target,
                        assertion_occurrences=2,
                    )
                    if target in limits.new_unit_ids:
                        if target not in strategy_seen:
                            strategy_seen.add(target)
                            enrichments.append(
                                GraphCandidate(
                                    unit_id=target,
                                    paths=(path,),
                                    reasons=("same_parent",),
                                    strategy="structural",
                                    source_order_distance=distance,
                                )
                            )
                        continue
                    if len(limits.new_unit_ids) >= allocation:
                        truncation = "candidate_limit"
                        break
                    limits.new_unit_ids.add(target)
                    strategy_seen.add(target)
                    limits.strategy_counts["structural"] = (
                        limits.strategy_counts.get("structural", 0) + 1
                    )
                    candidates.append(
                        GraphCandidate(
                            unit_id=target,
                            paths=(path,),
                            reasons=("same_parent",),
                            strategy="structural",
                            source_order_distance=distance,
                        )
                    )
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
        view: EligibilityView,
        selected_unit_ids: tuple[str, ...],
        predicates: tuple[str, ...],
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
            if set(predicates) - permitted:
                raise TychoraError(
                    "INVALID_REQUEST", "Unregistered inspection predicate"
                )
            if selected_unit_ids and limits.inspected_rows >= limits.max_rows:
                return AssertionPage(truncation="edge_work_limit", exhausted=False)
            frontier = set(selected_unit_ids) & view.graph_eligible_unit_ids
            visited_units = set(frontier)
            seen_assertions: set[str] = set()
            assertions: list[GraphPath] = []
            truncation = None
            for _ in range(limits.max_inspection_hops):
                next_frontier: set[str] = set()
                for seed in sorted(frontier):
                    for predicate in predicates:
                        rule = policies[predicate]
                        for direction in rule.directions:
                            rows = self._iter_edges(
                                view,
                                seed,
                                direction,
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
