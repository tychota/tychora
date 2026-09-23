import pytest

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


from types import SimpleNamespace

from tychora.contracts.graph import GraphCandidate, GraphCandidates
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.graph import augment


class _ResumingGraph:
    def __init__(self):
        self.calls = []

    def structural_context(self, view, seeds, budget, ctx):
        self.calls.append(("structural", seeds, budget))
        if len(self.calls) == 1:
            budget.new_unit_ids.add("target")
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
        budget.completed_strategies.add("structural")
        return GraphCandidates(exhausted=True)

    def expand(self, view, seeds, budget, ctx):
        self.calls.append(("semantic", seeds, budget))
        budget.completed_strategies.add("semantic")
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


def test_graph_coordinator_reuses_fixed_seeds_budget_and_merges_enrichment():
    graph = _ResumingGraph()
    config = SimpleNamespace(
        values=SimpleNamespace(
            limits=SimpleNamespace(
                graph_seed_limit=5, graph_candidate_limit=32, graph_work_limit=2000
            ),
            retrieval=SimpleNamespace(
                graph=SimpleNamespace(structural="code_context@1", semantic=True)
            ),
        )
    )
    view = SimpleNamespace(source_unit_ids={"a", "b"}, graph_generation_id="generation")
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
    config = SimpleNamespace(
        values=SimpleNamespace(
            limits=SimpleNamespace(
                graph_seed_limit=5, graph_candidate_limit=32, graph_work_limit=2000
            ),
            retrieval=SimpleNamespace(
                graph=SimpleNamespace(structural="none", semantic=True)
            ),
        )
    )
    view = case.view(context={"a", "b"}, candidates={"a", "b"})
    result = augment(view, (_candidate("a", 1),), config, case.graph, case.ctx)
    assert [candidate.unit_id for candidate in result.candidates] == ["b"]
    assert result.inspected_rows == 1
    assert result.exhausted


class _OrderedGraph:
    def __init__(self):
        self.calls = []

    def structural_context(self, view, seeds, budget, ctx):
        self.calls.append("structural")
        budget.completed_strategies.add("structural")
        budget.new_unit_ids.update({"s1", "s2"})
        return GraphCandidates(
            candidates=tuple(
                GraphCandidate(
                    unit_id=uid, paths=(), reasons=("neighbor",), strategy="structural"
                )
                for uid in ("s1", "s2")
            )
        )

    def expand(self, view, seeds, budget, ctx):
        self.calls.append("semantic")
        budget.completed_strategies.add("semantic")
        budget.new_unit_ids.update({"m1", "m2"})
        return GraphCandidates(
            candidates=tuple(
                GraphCandidate(
                    unit_id=uid, paths=(), reasons=("reference",), strategy="semantic"
                )
                for uid in ("m1", "m2")
            )
        )


def test_graph_targets_interleave_structural_first():
    graph = _OrderedGraph()
    config = SimpleNamespace(
        values=SimpleNamespace(
            limits=SimpleNamespace(
                graph_seed_limit=5, graph_candidate_limit=32, graph_work_limit=2000
            ),
            retrieval=SimpleNamespace(
                graph=SimpleNamespace(structural="code_context@1", semantic=True)
            ),
        )
    )
    view = SimpleNamespace(source_unit_ids={"a"}, graph_generation_id="generation")
    result = augment(view, (_candidate("a", 1),), config, graph, ExecutionContext())
    assert [candidate.unit_id for candidate in result.candidates] == [
        "s1",
        "m1",
        "s2",
        "m2",
    ]
    assert graph.calls == ["structural", "semantic"]


def test_zero_graph_candidate_allowance_does_not_traverse():
    graph = _OrderedGraph()
    config = SimpleNamespace(
        values=SimpleNamespace(
            limits=SimpleNamespace(
                graph_seed_limit=5, graph_candidate_limit=0, graph_work_limit=2000
            ),
            retrieval=SimpleNamespace(
                graph=SimpleNamespace(structural="code_context@1", semantic=True)
            ),
        )
    )
    view = SimpleNamespace(source_unit_ids={"a"}, graph_generation_id="generation")
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
