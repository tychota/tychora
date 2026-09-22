"""Code structural context uses eligible native adjacency and parent paths."""

import pytest

from tests.retrieval.graph_fixture import assertion, sealed_graph, unit
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphPredicatePolicy, GraphTraversalPolicy


def _case(tmp_path):
    units = tuple(
        unit(uid, ordinal) for ordinal, uid in enumerate(("parent", "a", "b", "c", "d"))
    )
    relations = tuple(
        assertion("parent", uid, predicate="contains@1") for uid in ("a", "b", "c", "d")
    )
    adjacency = tuple(
        assertion(left, right, predicate="precedes@1")
        for left, right in (("a", "b"), ("b", "c"), ("c", "d"))
    )
    policy = GraphTraversalPolicy(
        predicates=(
            GraphPredicatePolicy(
                predicate="contains@1", directions=("outgoing", "incoming")
            ),
            GraphPredicatePolicy(
                predicate="precedes@1", directions=("outgoing", "incoming")
            ),
        ),
        structural="code_context@1",
        semantic=False,
    )
    return sealed_graph(tmp_path, units, (*relations, *adjacency), policy)


def test_adjacent_then_nearest_sibling_with_structural_parent(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(
            context={"a", "b", "c", "d"},
            candidates={"a", "b", "c", "d"},
            structural={"parent"},
        )
        page = case.graph.structural_context(view, ("b",), case.budget(), case.ctx)
        assert [c.unit_id for c in page.candidates] == ["a", "c", "d"]
        assert [c.reasons[0] for c in page.candidates] == [
            "preceding_definition",
            "following_definition",
            "same_parent",
        ]
        assert all(c.unit_id != "parent" for c in page.candidates)
        assert page.candidates[2].paths[0].assertion_occurrences == 2
    finally:
        case.graph.close()


def test_hidden_immediate_neighbor_does_not_jump_to_later_sibling(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(
            context={"a", "b", "d"}, candidates={"a", "b", "d"}, structural={"parent"}
        )
        page = case.graph.structural_context(view, ("b",), case.budget(), case.ctx)
        assert [c.unit_id for c in page.candidates] == ["a"]
    finally:
        case.graph.close()


def test_hidden_parent_blocks_sibling_pattern(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(context={"a", "b", "c", "d"}, candidates={"a", "b", "c", "d"})
        page = case.graph.structural_context(view, ("b",), case.budget(), case.ctx)
        assert [c.unit_id for c in page.candidates] == ["a", "c"]
    finally:
        case.graph.close()


def test_candidate_only_facet_allows_context_intermediate(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(
            context={"a", "b", "c", "d"}, candidates={"b", "d"}, structural={"parent"}
        )
        page = case.graph.structural_context(view, ("b",), case.budget(), case.ctx)
        assert [c.unit_id for c in page.candidates] == ["d"]
        assert page.candidates[0].reasons == ("same_parent",)
    finally:
        case.graph.close()


def test_sibling_pair_requires_two_remaining_work_units(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(
            context={"a", "b", "c", "d"},
            candidates={"a", "b", "c", "d"},
            structural={"parent"},
        )
        budget = case.budget(max_rows=4)
        page = case.graph.structural_context(view, ("b",), budget, case.ctx)
        assert [c.unit_id for c in page.candidates] == ["a", "c"]
        assert budget.inspected_rows == 4
        assert page.truncation == "edge_work_limit"
    finally:
        case.graph.close()


def test_reopened_structural_order_requires_matching_canonical_build(tmp_path):
    case = _case(tmp_path)
    view = case.view(
        context={"a", "b", "c", "d"},
        candidates={"a", "b", "c", "d"},
        structural={"parent"},
    )
    build = case.graph._build
    policy = case.graph._traversal.policy
    case.graph.close()
    reopened = LadybugGraphStore(
        tmp_path / "graph", build=build, traversal_policy=policy
    )
    assert [
        c.unit_id
        for c in reopened.structural_context(
            view, ("b",), case.budget(), case.ctx
        ).candidates
    ] == ["a", "c", "d"]
    reopened.close()
    missing = LadybugGraphStore(tmp_path / "graph", traversal_policy=policy)
    with pytest.raises(TychoraError) as error:
        missing.structural_context(view, ("b",), case.budget(), case.ctx)
    assert error.value.code == "INDEX_INCOMPLETE"
    missing.close()
    changed = build.model_copy(
        update={
            "units": (
                build.units[0],
                build.units[1].model_copy(
                    update={
                        "unit": build.units[1].unit.model_copy(update={"ordinal": 99})
                    }
                ),
                *build.units[2:],
            )
        }
    )
    altered = LadybugGraphStore(
        tmp_path / "graph", build=changed, traversal_policy=policy
    )
    with pytest.raises(TychoraError) as error:
        altered.structural_context(view, ("b",), case.budget(), case.ctx)
    assert error.value.code == "INDEX_INCOMPLETE"
    altered.close()


@pytest.mark.parametrize("first", ("semantic", "structural"))
def test_mixed_strategy_reservations_return_unused_slots(tmp_path, first):
    names = ("parent", "a", "b", "c", *(f"s{index:02}" for index in range(20)))
    units = tuple(unit(uid, index) for index, uid in enumerate(names))
    relations = (
        *(assertion("parent", uid, predicate="contains@1") for uid in ("a", "b", "c")),
        assertion("a", "b", predicate="precedes@1"),
        assertion("b", "c", predicate="precedes@1"),
        *(
            assertion("b", f"s{index:02}", predicate="references@1")
            for index in range(20)
        ),
    )
    policy = GraphTraversalPolicy(
        predicates=(
            GraphPredicatePolicy(predicate="references@1", directions=("outgoing",)),
        ),
        structural="code_context@1",
        semantic=True,
    )
    case = sealed_graph(tmp_path, units, relations, policy)
    try:
        eligible = set(names) - {"parent"}
        view = case.view(context=eligible, candidates=eligible, structural={"parent"})
        budget = case.budget()
        if first == "semantic":
            initial = case.graph.expand(view, ("b",), budget, case.ctx)
            assert len(initial.candidates) == 16
            assert initial.truncation == "candidate_limit"
            structural = case.graph.structural_context(view, ("b",), budget, case.ctx)
            resumed = case.graph.expand(view, ("b",), budget, case.ctx)
            assert len(resumed.candidates) == 4
            assert resumed.exhausted
            found = {
                c.unit_id
                for c in (
                    *initial.candidates,
                    *resumed.candidates,
                    *structural.candidates,
                )
            }
        else:
            structural = case.graph.structural_context(view, ("b",), budget, case.ctx)
            semantic = case.graph.expand(view, ("b",), budget, case.ctx)
            assert len(semantic.candidates) == 20
            assert semantic.exhausted
            found = {c.unit_id for c in (*semantic.candidates, *structural.candidates)}
        assert {c.unit_id for c in structural.candidates} == {"a", "c"}
        assert found == {"a", "c", *(f"s{index:02}" for index in range(20))}
        assert len(budget.new_unit_ids) == 22
    finally:
        case.graph.close()


def test_zero_candidate_allowance_truncates_without_admission(tmp_path):
    case = _case(tmp_path)
    try:
        view = case.view(
            context={"a", "b", "c", "d"},
            candidates={"a", "b", "c", "d"},
            structural={"parent"},
        )
        page = case.graph.structural_context(
            view, ("b",), case.budget(max_new_units=0), case.ctx
        )
        assert page.candidates == ()
        assert page.truncation == "candidate_limit"
    finally:
        case.graph.close()


@pytest.mark.parametrize("first", ("semantic", "structural"))
def test_shared_target_retains_both_strategy_reasons_without_second_vote(
    tmp_path, first
):
    units = tuple(unit(uid, index) for index, uid in enumerate(("parent", "b", "c")))
    relations = (
        assertion("parent", "b", predicate="contains@1"),
        assertion("parent", "c", predicate="contains@1"),
        assertion("b", "c", predicate="precedes@1"),
        assertion("b", "c", predicate="references@1"),
    )
    policy = GraphTraversalPolicy(
        predicates=(
            GraphPredicatePolicy(predicate="references@1", directions=("outgoing",)),
        ),
        structural="code_context@1",
        semantic=True,
    )
    case = sealed_graph(tmp_path, units, relations, policy)
    try:
        view = case.view(
            context={"b", "c"}, candidates={"b", "c"}, structural={"parent"}
        )
        budget = case.budget()
        if first == "semantic":
            initial = case.graph.expand(view, ("b",), budget, case.ctx)
            second = case.graph.structural_context(view, ("b",), budget, case.ctx)
        else:
            initial = case.graph.structural_context(view, ("b",), budget, case.ctx)
            second = case.graph.expand(view, ("b",), budget, case.ctx)
        assert [candidate.unit_id for candidate in initial.candidates] == ["c"]
        assert second.candidates == ()
        assert [candidate.unit_id for candidate in second.enrichments] == ["c"]
        assert len(budget.new_unit_ids) == 1
        assert {initial.candidates[0].strategy, second.enrichments[0].strategy} == {
            "semantic",
            "structural",
        }
    finally:
        case.graph.close()
