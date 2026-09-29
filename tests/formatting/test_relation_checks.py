"""Bounded graph inspection is separate from rank expansion."""

from tychora.contracts.graph import AssertionPage, GraphPath
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.response import inspect_relations


class Graph:
    def get_assertions(self, view, selected_unit_ids, predicates, limits, ctx):
        assert limits.inspected_rows == 1999
        limits.inspected_rows += 1
        return AssertionPage(
            assertions=(
                GraphPath(
                    assertion_ids=("a" * 64,),
                    required_support_ids=frozenset({"unit-1", "unit-2"}),
                    predicate="contradicts@1",
                    status="recorded",
                    confidence=1.0,
                    source_unit_id="unit-1",
                    target_unit_id="unit-2",
                ),
            ),
            inspected_edges=1,
            exhausted=True,
        )


def test_inspection_carries_expansion_work_and_keeps_eligible_conflict():
    view = type(
        "View",
        (),
        {
            "graph_generation_id": "generation-1",
            "graph_eligible_unit_ids": frozenset({"unit-1", "unit-2"}),
            "blocked_assertion_ids": frozenset(),
        },
    )()
    checks = inspect_relations(
        Graph(),
        view,
        ("unit-1",),
        ("contradicts@1",),
        ExecutionContext(),
        expansion_inspected_rows=1999,
    )
    assert checks.status == "complete"
    assert checks.assertion_ids == ("a" * 64,)
    assert checks.decisions[0].unit_ids == ("unit-1", "unit-2")


def test_exhausted_shared_work_is_partial_and_disabled_is_not_evaluated():
    view = type(
        "View",
        (),
        {
            "graph_generation_id": "generation-1",
            "graph_eligible_unit_ids": frozenset({"unit-1"}),
            "blocked_assertion_ids": frozenset(),
        },
    )()
    assert (
        inspect_relations(
            Graph(),
            view,
            ("unit-1",),
            ("contradicts@1",),
            ExecutionContext(),
            expansion_inspected_rows=2000,
        ).status
        == "partial"
    )
    assert (
        inspect_relations(
            Graph(),
            view,
            ("unit-1",),
            ("contradicts@1",),
            ExecutionContext(),
            expansion_inspected_rows=2000,
            enabled=False,
        ).status
        == "not_evaluated"
    )
