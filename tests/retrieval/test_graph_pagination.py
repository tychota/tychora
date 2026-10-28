"""Deterministic native traversal pages and shared work accounting."""

import time
from threading import Event, Thread

import pytest

from tests.retrieval.graph_fixture import assertion, sealed_graph, unit
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    AssertionQuery,
    GraphPredicatePolicy,
    GraphTraversalPolicy,
)
from tychora.ports.execution import ExecutionContext


def test_duplicate_first_page_does_not_hide_later_unique_target(tmp_path):
    observations = tuple(
        assertion("a", "b", key=f"duplicate-{index}", confidence=1.0)
        for index in range(1000)
    )
    later = assertion("a", "c", confidence=0.5)
    case = sealed_graph(
        tmp_path, tuple(unit(uid) for uid in ("a", "b", "c")), (*observations, later)
    )
    try:
        page = case.graph.expand(
            case.view(context={"a", "b", "c"}, candidates={"a", "b", "c"}),
            ("a",),
            case.budget(),
            case.ctx,
        )
        assert [candidate.unit_id for candidate in page.candidates] == ["b", "c"]
        assert len(page.candidates[0].paths) == 8
        assert page.candidates[0].omitted_path_count == 992
        assert page.inspected_rows == 1001
        assert page.exhausted
    finally:
        case.graph.close()


def test_incoming_direction_and_current_blocked_overlay(tmp_path):
    relation = assertion("a", "b")
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (relation,))
    try:
        open_view = case.view(context={"a", "b"}, candidates={"a", "b"})
        assert [
            c.unit_id
            for c in case.graph.expand(
                open_view, ("b",), case.budget(), case.ctx
            ).candidates
        ] == ["a"]
        blocked_view = case.view(
            context={"a", "b"}, candidates={"a", "b"}, blocked={relation.assertion_id}
        )
        assert (
            case.graph.expand(blocked_view, ("b",), case.budget(), case.ctx).candidates
            == ()
        )
    finally:
        case.graph.close()


def test_shared_work_cap_counts_duplicate_assertion_occurrences(tmp_path):
    observations = tuple(
        assertion("a", "b", key=f"duplicate-{index}") for index in range(4)
    )
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), observations)
    try:
        budget = case.budget(max_rows=2)
        page = case.graph.expand(
            case.view(context={"a", "b"}, candidates={"a", "b"}),
            ("a",),
            budget,
            case.ctx,
        )
        assert page.inspected_rows == 2
        assert budget.inspected_rows == 2
        assert page.truncation == "edge_work_limit"
        assert not page.exhausted
    finally:
        case.graph.close()


def test_explicit_inspection_returns_bounded_assertion_page(tmp_path):
    relation = assertion("a", "b")
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (relation,))
    try:
        view = case.view(context={"a", "b"}, candidates={"a", "b"})
        budget = case.budget(max_inspection_edges=1)
        page = case.graph.get_assertions(
            view,
            AssertionQuery(selected_unit_ids=("a",), predicates=("references@1",)),
            budget,
            case.ctx,
        )
        assert [path.assertion_ids[0] for path in page.assertions] == [
            relation.assertion_id
        ]
        assert budget.inspected_edges == 1
        blocked = case.view(
            context={"a", "b"}, candidates={"a", "b"}, blocked={relation.assertion_id}
        )
        assert (
            case.graph.get_assertions(
                blocked,
                AssertionQuery(selected_unit_ids=("a",), predicates=("references@1",)),
                case.budget(),
                case.ctx,
            ).assertions
            == ()
        )
    finally:
        case.graph.close()


def test_cancelled_native_read_releases_generation_for_drop(tmp_path, monkeypatch):
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (assertion("a", "b"),))
    entered = Event()
    release = Event()
    dropped = Event()
    cancelled = Event()
    failures = []
    native_rows = case.graph._rows

    def held_read(query, parameters=None):
        rows = native_rows(query, parameters)
        if "MATCH (u:Entity)" in query:
            entered.set()
            assert release.wait(5)
        return rows

    monkeypatch.setattr(case.graph, "_rows", held_read)
    view = case.view(context={"a", "b"}, candidates={"a", "b"})

    def read():
        try:
            case.graph.expand(
                view, ("a",), case.budget(), ExecutionContext(cancelled=cancelled)
            )
        except TychoraError as error:
            failures.append(error)

    def drop():
        case.graph.drop_generation(case.spec.generation_id, ExecutionContext())
        dropped.set()

    reader = Thread(target=read)
    remover = Thread(target=drop)
    try:
        reader.start()
        assert entered.wait(5)
        remover.start()
        assert not dropped.wait(0.05)
        cancelled.set()
        release.set()
        reader.join(5)
        remover.join(5)
        assert len(failures) == 1 and isinstance(failures[0], TychoraError)
        assert failures[0].code == "CANCELLED"
        assert dropped.is_set()
        assert case.graph._read_state(case.spec.generation_id) is None
    finally:
        release.set()
        reader.join(5)
        remover.join(5)
        case.graph.close()


def test_expired_deadline_does_not_hold_generation_lock(tmp_path):
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (assertion("a", "b"),))
    try:
        view = case.view(context={"a", "b"}, candidates={"a", "b"})
        with pytest.raises(TychoraError) as error:
            case.graph.expand(
                view,
                ("a",),
                case.budget(),
                ExecutionContext(deadline=time.monotonic() - 1),
            )
        assert error.value.code == "DEADLINE_EXCEEDED"
        case.graph.drop_generation(case.spec.generation_id, ExecutionContext())
        assert case.graph._read_state(case.spec.generation_id) is None
    finally:
        case.graph.close()


def test_deadline_after_native_rows_unwinds_at_next_checkpoint(tmp_path, monkeypatch):
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (assertion("a", "b"),))
    entered, release, finished = Event(), Event(), Event()
    failures = []
    native_rows = case.graph._rows

    def held_read(query, parameters=None):
        rows = native_rows(query, parameters)
        if "MATCH (u:Entity)" in query:
            entered.set()
            assert release.wait(5)
        return rows

    monkeypatch.setattr(case.graph, "_rows", held_read)
    view = case.view(context={"a", "b"}, candidates={"a", "b"})
    deadline = time.monotonic() + 0.15

    def read():
        try:
            case.graph.expand(
                view,
                ("a",),
                case.budget(),
                ExecutionContext(deadline=deadline),
            )
        except TychoraError as error:
            failures.append(error)
        finally:
            finished.set()

    worker = Thread(target=read)
    try:
        worker.start()
        assert entered.wait(5)
        assert not finished.wait(max(0, deadline - time.monotonic()) + 0.05)
        release.set()
        worker.join(5)
        assert finished.is_set()
        assert len(failures) == 1
        assert failures[0].code == "DEADLINE_EXCEEDED"
        case.graph.drop_generation(case.spec.generation_id, ExecutionContext())
        assert case.graph._read_state(case.spec.generation_id) is None
    finally:
        release.set()
        worker.join(5)
        case.graph.close()


def test_shared_budget_rejects_overlay_change_between_strategy_calls(tmp_path):
    relation = assertion("a", "b")
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (relation,))
    try:
        budget = case.budget()
        open_view = case.view(context={"a", "b"}, candidates={"a", "b"})
        case.graph.expand(open_view, ("a",), budget, case.ctx)
        blocked = case.view(
            context={"a", "b"}, candidates={"a", "b"}, blocked={relation.assertion_id}
        )
        with pytest.raises(TychoraError) as error:
            case.graph.get_assertions(
                blocked,
                AssertionQuery(selected_unit_ids=("a",), predicates=("references@1",)),
                budget,
                case.ctx,
            )
        assert error.value.code == "SCOPE_CHANGED"
    finally:
        case.graph.close()


def test_direction_streams_merge_by_status_before_confidence(tmp_path):
    outgoing = assertion("a", "b", status="recorded", confidence=1.0)
    incoming = assertion("c", "a", status="confirmed", confidence=0.1)
    case = sealed_graph(
        tmp_path, tuple(unit(uid) for uid in ("a", "b", "c")), (outgoing, incoming)
    )
    try:
        view = case.view(context={"a", "b", "c"}, candidates={"a", "b", "c"})
        page = case.graph.expand(view, ("a",), case.budget(), case.ctx)
        assert [c.unit_id for c in page.candidates] == ["c", "b"]
    finally:
        case.graph.close()


def test_high_degree_node_reports_candidate_cap(tmp_path):
    targets = tuple(f"t{index:02}" for index in range(40))
    case = sealed_graph(
        tmp_path,
        tuple(unit(uid) for uid in ("a", *targets)),
        tuple(assertion("a", uid) for uid in targets),
    )
    try:
        eligible = {"a", *targets}
        page = case.graph.expand(
            case.view(context=eligible, candidates=eligible),
            ("a",),
            case.budget(),
            case.ctx,
        )
        assert len({c.unit_id for c in page.candidates}) == 32
        assert page.truncation == "candidate_limit"
        assert not page.exhausted
    finally:
        case.graph.close()


def test_explicit_inspection_labels_proposed_assertion_without_expansion(tmp_path):
    proposal = assertion("a", "b", status="proposed")
    policy = GraphTraversalPolicy(
        predicates=(),
        semantic=False,
        inspection_predicates=(
            GraphPredicatePolicy(
                predicate="references@1",
                directions=("outgoing",),
                statuses=("proposed",),
            ),
        ),
    )
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (proposal,), policy)
    try:
        view = case.view(context={"a", "b"}, candidates={"a", "b"})
        page = case.graph.get_assertions(
            view,
            AssertionQuery(selected_unit_ids=("a",), predicates=("references@1",)),
            case.budget(),
            case.ctx,
        )
        assert [(path.assertion_ids[0], path.status) for path in page.assertions] == [
            (proposal.assertion_id, "proposed")
        ]
        assert case.graph.expand(view, ("a",), case.budget(), case.ctx).candidates == ()
    finally:
        case.graph.close()


def test_distinct_support_paths_to_one_target_remain_inspectable(tmp_path):
    first = assertion("a", "b", key="observation-one")
    second = assertion("a", "b", supports={"a", "b", "c"}, key="observation-two")
    case = sealed_graph(tmp_path, (unit("a"), unit("b"), unit("c")), (first, second))
    try:
        view = case.view(context={"a", "b", "c"}, candidates={"a", "b"})
        page = case.graph.expand(view, ("a",), case.budget(), case.ctx)
        assert [candidate.unit_id for candidate in page.candidates] == ["b"]
        assert {path.assertion_ids[0] for path in page.candidates[0].paths} == {
            first.assertion_id,
            second.assertion_id,
        }
    finally:
        case.graph.close()


def test_zero_assertion_work_does_not_issue_traversal_query(tmp_path, monkeypatch):
    case = sealed_graph(tmp_path, (unit("a"), unit("b")), (assertion("a", "b"),))
    try:
        view = case.view(context={"a", "b"}, candidates={"a", "b"})

        def forbidden_read(query, parameters=None):
            raise AssertionError("No native graph rows may be inspected")

        monkeypatch.setattr(case.graph, "_rows", forbidden_read)
        page = case.graph.expand(view, ("a",), case.budget(max_rows=0), case.ctx)
        assert page.candidates == ()
        assert page.truncation == "edge_work_limit"
    finally:
        case.graph.close()


def test_blocked_high_ranked_assertions_are_filtered_before_page_limit(tmp_path):
    blocked = tuple(
        assertion("a", "b", key=f"blocked-{index}", status="confirmed")
        for index in range(130)
    )
    valid = assertion("a", "c", confidence=0.2)
    case = sealed_graph(
        tmp_path, tuple(unit(uid) for uid in ("a", "b", "c")), (*blocked, valid)
    )
    try:
        view = case.view(
            context={"a", "b", "c"},
            candidates={"a", "b", "c"},
            blocked={item.assertion_id for item in blocked},
        )
        page = case.graph.expand(view, ("a",), case.budget(max_rows=1), case.ctx)
        assert [candidate.unit_id for candidate in page.candidates] == ["c"]
        assert page.inspected_rows == 1
        assert page.exhausted
    finally:
        case.graph.close()
