import pytest

from tests.helpers.eligibility import eligibility_view
from tests.retrieval.test_segments import config as base_config
from tychora.configuration.models import ResolvedConfig
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import (
    AssertionPage,
    EligibilityView,
    GraphBatchReceipt,
    GraphBudget,
    GraphGenerationSpec,
    GraphManifest,
)
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.admission import LanePolicy, admit
from tychora.retrieval.fusion import FusedCandidate


def _candidate(uid, rank):
    return FusedCandidate(("unit", uid), rank, 1 / rank, {"text": rank})


def test_graph_only_target_gets_assessment_slot_after_strong_text_hits():
    candidates = tuple(_candidate(f"text-{i:02}", i + 1) for i in range(64)) + (
        _candidate("graph", 65),
    )
    lanes = {item.key: "general" for item in candidates}
    result = admit(
        candidates, (), {"semantic": (("unit", "graph"),)}, LanePolicy(), lanes=lanes
    )
    assert ("unit", "graph") in [item.key for item in result.selected]
    assert len(result.selected) == 64
    assert result.reservation_coverage["graph"] == 1
    assert len(result.discarded) == 1


def test_reservation_saturation_and_missing_required_lane_are_explicit():
    candidates = tuple(_candidate(f"text-{i:02}", i + 1) for i in range(64)) + (
        _candidate("special", 65),
    )
    lanes = {item.key: "general" for item in candidates}
    lanes[("unit", "special")] = "special"
    policy = LanePolicy(
        mode="one_per_required_lane",
        order=("general", "special", "missing"),
        required=("special", "missing"),
        known=("general", "special", "missing"),
    )
    result = admit(candidates, (), {}, policy, lanes=lanes)
    assert ("unit", "special") in [item.key for item in result.selected]
    assert result.missing_lanes == ("missing",)
    assert result.coverage["special"] == 1
    assert result.unused_slots == 0


def test_missing_canonical_lane_fails_closed():
    with pytest.raises(ValueError, match="Canonical lane"):
        admit((_candidate("a", 1),), (), {}, LanePolicy(), lanes={})


from tychora.contracts.graph import GraphCandidate, GraphCandidates
from tychora.retrieval.graph import augment


def _config(
    tmp_path, *, structural: str, semantic: bool, candidates: int = 32
) -> ResolvedConfig:
    base = base_config(tmp_path)
    values = base.values
    return base.model_copy(
        update={
            "values": values.model_copy(
                update={
                    "limits": values.limits.model_copy(
                        update={"graph_candidate_limit": candidates}
                    ),
                    "retrieval": values.retrieval.model_copy(
                        update={
                            "graph": values.retrieval.graph.model_copy(
                                update={"structural": structural, "semantic": semantic}
                            )
                        }
                    ),
                }
            )
        }
    )


def _view(*seeds: str) -> EligibilityView:
    view = eligibility_view(
        context_unit_ids=frozenset(seeds), graph_generation_id="generation"
    )
    return view.model_copy(update={"source_unit_ids": frozenset(seeds)})


class _TraversalGraph:
    def begin_generation(self, spec: GraphGenerationSpec) -> str:
        raise AssertionError("query traversal must not begin generation")

    def sealed_receipt(self, generation_id: str) -> GraphBatchReceipt | None:
        raise AssertionError("query traversal must not read seal receipt")

    def upsert_batch(
        self,
        generation_id: str,
        assertions: tuple[RelationshipAssertion, ...],
        units: tuple[EvidenceUnit, ...],
        ctx: ExecutionContext,
    ) -> None:
        raise AssertionError("query traversal must not upsert")

    def seal_generation(
        self, generation_id: str, expected: GraphManifest, ctx: ExecutionContext
    ) -> GraphBatchReceipt:
        raise AssertionError("query traversal must not seal")

    def drop_generation(self, generation_id: str, ctx: ExecutionContext) -> None:
        raise AssertionError("query traversal must not drop")

    def get_assertions(
        self,
        view: EligibilityView,
        selected_unit_ids: tuple[str, ...],
        predicates: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
        *,
        direction: str | None = None,
        relation_view: str | None = None,
    ) -> AssertionPage:
        raise AssertionError("query traversal must not inspect assertions")


class _ResumingGraph(_TraversalGraph):
    def __init__(self):
        self.calls = []

    def structural_context(self, view, seed_unit_ids, limits, ctx):
        self.calls.append(("structural", seed_unit_ids, limits))
        if len(self.calls) == 1:
            limits.new_unit_ids.add("target")
            return GraphCandidates(
                candidates=(
                    GraphCandidate(
                        unit_id="target",
                        paths=(),
                        reasons=("neighbor",),
                        strategy="structural",
                    ),
                ),
                truncation="candidate_limit",
                exhausted=False,
            )
        limits.completed_strategies.add("structural")
        return GraphCandidates(exhausted=True)

    def expand(self, view, seed_unit_ids, limits, ctx):
        self.calls.append(("semantic", seed_unit_ids, limits))
        limits.completed_strategies.add("semantic")
        return GraphCandidates(
            enrichments=(
                GraphCandidate(
                    unit_id="target",
                    paths=(),
                    reasons=("reference",),
                    strategy="semantic",
                ),
            ),
            exhausted=True,
        )


def test_graph_coordinator_reuses_fixed_seeds_budget_and_merges_enrichment(tmp_path):
    graph = _ResumingGraph()
    config = _config(tmp_path, structural="code_context@1", semantic=True)
    view = _view("a", "b")
    result = augment(
        view,
        (_candidate("a", 1), _candidate("b", 2)),
        config,
        graph,
        ExecutionContext(),
    )
    assert [call[0] for call in graph.calls] == ["structural", "semantic", "structural"]
    assert all(call[1] == ("a", "b") for call in graph.calls)
    assert len({id(call[2]) for call in graph.calls}) == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].reasons == ("neighbor", "reference")
    assert result.exhausted


@pytest.mark.parametrize("strategy", ["structural", "semantic"])
def test_graph_ablation_calls_only_selected_strategy(strategy, tmp_path):
    graph = _ResumingGraph()
    config = _config(tmp_path, structural="code_context@1", semantic=True)
    view = _view("a")
    first = augment(
        view,
        (_candidate("a", 1),),
        config,
        graph,
        ExecutionContext(),
        frozenset({strategy}),
    )
    assert [call[0] for call in graph.calls] == [strategy]
    replay_graph = _ResumingGraph()
    second = augment(
        view,
        (_candidate("a", 1),),
        config,
        replay_graph,
        ExecutionContext(),
        frozenset({strategy}),
    )
    assert [item.unit_id for item in first.candidates] == [
        item.unit_id for item in second.candidates
    ]


def test_exact_and_graph_only_total_caps_survive_fused_fill():
    candidates = tuple(_candidate(f"exact-{i:02}", i + 1) for i in range(40)) + tuple(
        _candidate(f"graph-{i:02}", i + 41) for i in range(12)
    )
    exact = tuple(item.key for item in candidates[:40])
    graph = {"semantic": tuple(item.key for item in candidates[40:])}
    result = admit(
        candidates,
        exact,
        graph,
        LanePolicy(),
        lanes={item.key: "general" for item in candidates},
    )
    assert result.reservation_coverage["exact"] == 32
    assert result.reservation_coverage["graph"] == 8
    assert result.unused_slots == 24
    assert {
        item.reason for item in result.discarded if item.candidate.key in exact
    } == {"exact_reservation_cap"}
    assert {
        item.reason
        for item in result.discarded
        if item.candidate.key in graph["semantic"]
    } == {"graph_reservation_cap"}


def test_native_graph_composes_seed_and_candidate_eligibility(tmp_path):
    from tests.retrieval.graph_fixture import assertion, sealed_graph, unit

    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (assertion("a", "b"),))
    config = _config(tmp_path, structural="none", semantic=True)
    view = case.view(context={"a", "b"}, candidates={"a", "b"})
    result = augment(view, (_candidate("a", 1),), config, case.graph, case.ctx)
    assert [candidate.unit_id for candidate in result.candidates] == ["b"]
    assert result.inspected_rows == 1
    assert result.exhausted


class _OrderedGraph(_TraversalGraph):
    def __init__(self):
        self.calls = []

    def structural_context(self, view, seed_unit_ids, limits, ctx):
        self.calls.append("structural")
        limits.completed_strategies.add("structural")
        limits.new_unit_ids.update({"s1", "s2"})
        return GraphCandidates(
            candidates=tuple(
                GraphCandidate(
                    unit_id=uid, paths=(), reasons=("neighbor",), strategy="structural"
                )
                for uid in ("s1", "s2")
            )
        )

    def expand(self, view, seed_unit_ids, limits, ctx):
        self.calls.append("semantic")
        limits.completed_strategies.add("semantic")
        limits.new_unit_ids.update({"m1", "m2"})
        return GraphCandidates(
            candidates=tuple(
                GraphCandidate(
                    unit_id=uid, paths=(), reasons=("reference",), strategy="semantic"
                )
                for uid in ("m1", "m2")
            )
        )


def test_graph_targets_interleave_structural_first(tmp_path):
    graph = _OrderedGraph()
    config = _config(tmp_path, structural="code_context@1", semantic=True)
    view = _view("a")
    result = augment(view, (_candidate("a", 1),), config, graph, ExecutionContext())
    assert [candidate.unit_id for candidate in result.candidates] == [
        "s1",
        "m1",
        "s2",
        "m2",
    ]
    assert graph.calls == ["structural", "semantic"]


def test_zero_graph_candidate_allowance_does_not_traverse(tmp_path):
    graph = _OrderedGraph()
    config = _config(tmp_path, structural="code_context@1", semantic=True, candidates=0)
    view = _view("a")
    result = augment(view, (_candidate("a", 1),), config, graph, ExecutionContext())
    assert graph.calls == []
    assert result.candidates == ()
    assert result.truncation == "candidate_limit"


def test_duplicate_owner_is_selected_or_discarded_once():
    candidate = _candidate("a", 1)
    result = admit(
        (candidate, candidate),
        (),
        {},
        LanePolicy(),
        limit=0,
        lanes={candidate.key: "general"},
    )
    assert len(result.discarded) == 1


def test_lane_named_exact_does_not_collide_with_reservation_coverage():
    candidate = _candidate("a", 1)
    result = admit(
        (candidate,),
        (),
        {},
        LanePolicy(known=("exact",), order=("exact",)),
        lanes={candidate.key: "exact"},
    )
    assert result.coverage["exact"] == 1
    assert result.reservation_coverage["exact"] == 0


def test_missing_required_lane_reports_exact_cap():
    candidates = tuple(_candidate(f"a-{i:02}", i + 1) for i in range(32)) + (
        _candidate("special", 33),
    )
    lanes = {item.key: "general" for item in candidates}
    lanes[("unit", "special")] = "special"
    policy = LanePolicy(
        mode="one_per_required_lane",
        known=("general", "special"),
        order=("special",),
        required=("special",),
    )
    result = admit(
        candidates, tuple(item.key for item in candidates), {}, policy, lanes=lanes
    )
    assert result.missing_lane_reasons == {"special": "exact_reservation_cap"}


def test_required_lane_tries_later_eligible_candidate_after_capped_exact():
    candidates = tuple(_candidate(f"exact-{i:02}", i + 1) for i in range(32)) + (
        _candidate("capped-exact", 33),
        _candidate("ordinary", 34),
    )
    exact = tuple(item.key for item in candidates[:33])
    lanes = {item.key: "general" for item in candidates}
    lanes[("unit", "capped-exact")] = "special"
    lanes[("unit", "ordinary")] = "special"
    policy = LanePolicy(
        mode="one_per_required_lane",
        order=("special",),
        required=("special",),
        known=("general", "special"),
    )
    result = admit(candidates, exact, {}, policy, lanes=lanes)
    assert ("unit", "ordinary") in [item.key for item in result.selected]
    assert result.coverage["special"] == 1
    assert result.missing_lanes == ()
    assert result.missing_lane_reasons == {}


@pytest.mark.parametrize(
    ("required", "order"),
    [(("special",), ()), (("special", "special"), ("special",))],
)
def test_required_lane_policy_rejects_missing_order_or_duplicate(required, order):
    with pytest.raises(ValueError, match="required"):
        LanePolicy(
            mode="one_per_required_lane",
            known=("special",),
            required=required,
            order=order,
        )


def test_native_augment_resumes_structural_after_semantic_releases_slots(tmp_path):
    from tests.retrieval.graph_fixture import assertion, sealed_graph, unit
    from tychora.contracts.graph import GraphPredicatePolicy, GraphTraversalPolicy

    units = []
    relations = []
    seeds = []
    for group in range(5):
        parent = f"parent-{group}"
        names = tuple(f"g{group}-{index}" for index in range(5))
        units.append(unit(parent, group))
        units.extend(
            unit(name, index, parent_id=parent) for index, name in enumerate(names)
        )
        relations.extend(
            assertion(parent, name, predicate="contains@1") for name in names
        )
        relations.extend(
            assertion(names[index], names[index + 1], predicate="precedes@1")
            for index in range(4)
        )
        seeds.append(names[2])
    relations.append(assertion(seeds[0], "g0-1", predicate="references@1"))
    policy = GraphTraversalPolicy(
        predicates=(
            GraphPredicatePolicy(predicate="references@1", directions=("outgoing",)),
        ),
        structural="code_context@1",
        semantic=True,
    )
    case = sealed_graph(tmp_path, tuple(units), tuple(relations), policy)

    class TracingGraph(_TraversalGraph):
        def __init__(self, native):
            self.native = native
            self.calls = []

        def structural_context(self, view, seed_unit_ids, limits, ctx):
            page = self.native.structural_context(view, seed_unit_ids, limits, ctx)
            self.calls.append(
                ("structural", seed_unit_ids, id(limits), limits.inspected_rows, page)
            )
            return page

        def expand(self, view, seed_unit_ids, limits, ctx):
            page = self.native.expand(view, seed_unit_ids, limits, ctx)
            self.calls.append(
                ("semantic", seed_unit_ids, id(limits), limits.inspected_rows, page)
            )
            return page

    try:
        eligible = {
            uid for uid in (u.unit_id for u in units) if not uid.startswith("parent-")
        }
        parents = {f"parent-{group}" for group in range(5)}
        view = case.view(context=eligible, candidates=eligible, structural=parents)
        config = _config(tmp_path, structural="code_context@1", semantic=True)
        graph = TracingGraph(case.graph)
        result = augment(
            view,
            tuple(_candidate(seed, rank) for rank, seed in enumerate(seeds, 1)),
            config,
            graph,
            case.ctx,
        )
        assert [call[0] for call in graph.calls] == [
            "structural",
            "semantic",
            "structural",
        ]
        assert all(call[1] == tuple(seeds) for call in graph.calls)
        assert len({call[2] for call in graph.calls}) == 1
        assert len(graph.calls[0][4].candidates) == 16
        assert graph.calls[0][4].truncation == "candidate_limit"
        assert graph.calls[1][4].exhausted
        assert len(graph.calls[2][4].candidates) == 4
        assert graph.calls[2][3] > graph.calls[0][3]
        assert len(result.candidates) == 20
        assert len({candidate.unit_id for candidate in result.candidates}) == 20
        assert result.inspected_rows == graph.calls[-1][3]
        assert result.exhausted
        shared = next(
            candidate for candidate in result.candidates if candidate.unit_id == "g0-1"
        )
        assert "preceding_definition" in shared.reasons
        assert "references@1" in shared.reasons
    finally:
        case.graph.close()
