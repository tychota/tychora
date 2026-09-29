"""Bounded graph inspection is separate from rank expansion."""

from tests.retrieval.test_segments import config
from tychora.configuration.registry import Registry
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


def test_inspection_carries_expansion_work_and_keeps_eligible_conflict(tmp_path):
    view = type(
        "View",
        (),
        {
            "graph_generation_id": "generation-1",
            "graph_eligible_unit_ids": frozenset({"unit-1", "unit-2"}),
            "blocked_assertion_ids": frozenset(),
        },
    )()
    registry = Registry.from_config(config(tmp_path))
    checks = inspect_relations(
        Graph(),
        view,
        ("unit-1",),
        ("contradicts@1",),
        ExecutionContext(),
        expansion_inspected_rows=1999,
        registry=registry,
    )
    assert checks.status == "complete"
    assert checks.assertion_ids == ("a" * 64,)
    assert checks.decisions[0].unit_ids == ("unit-1", "unit-2")


def test_exhausted_shared_work_is_partial_and_disabled_is_not_evaluated(tmp_path):
    registry = Registry.from_config(config(tmp_path))
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
            registry=registry,
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
            registry=registry,
            enabled=False,
        ).status
        == "not_evaluated"
    )


def test_configured_decision_flag_overrides_predicate_name(tmp_path):
    from tests.configuration.test_registry import changed_config
    from tests.retrieval.test_segments import config
    from tychora.configuration.registry import Registry

    base = config(tmp_path)
    configured = changed_config(
        base,
        lambda data: (
            data["relations"]["supports@1"].update({"requires_decision": True}),
            data["relations"]["contradicts@1"].update({"requires_decision": False}),
        ),
    )
    registry = Registry.from_config(configured)
    view = type(
        "View",
        (),
        {
            "graph_generation_id": "generation-1",
            "graph_eligible_unit_ids": frozenset({"unit-1", "unit-2"}),
            "blocked_assertion_ids": frozenset(),
        },
    )()

    class NamedGraph:
        def __init__(self, predicate):
            self.predicate = predicate

        def get_assertions(self, view, selected_unit_ids, predicates, limits, ctx):
            return AssertionPage(
                assertions=(
                    GraphPath(
                        assertion_ids=("b" * 64,),
                        required_support_ids=frozenset({"unit-1", "unit-2"}),
                        predicate=self.predicate,
                        status="recorded",
                        confidence=1.0,
                        source_unit_id="unit-1",
                        target_unit_id="unit-2",
                    ),
                ),
                inspected_edges=1,
                exhausted=True,
            )

    true_check = inspect_relations(
        NamedGraph("supports@1"),
        view,
        ("unit-1",),
        ("supports@1",),
        ExecutionContext(),
        expansion_inspected_rows=0,
        registry=registry,
    )
    false_check = inspect_relations(
        NamedGraph("contradicts@1"),
        view,
        ("unit-1",),
        ("contradicts@1",),
        ExecutionContext(),
        expansion_inspected_rows=0,
        registry=registry,
    )
    assert true_check.decisions[0].assertion_ids == ("b" * 64,)
    assert false_check.decisions == ()
