"""Bounded graph inspection is separate from rank expansion."""

from tests.helpers.eligibility import synthetic_graph_scope
from tests.retrieval.test_segments import config
from tychora.configuration.registry import Registry
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import (
    AssertionPage,
    GraphBatchReceipt,
    GraphBudget,
    GraphCandidates,
    GraphGenerationSpec,
    GraphManifest,
    GraphPath,
    GraphScope,
)
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.response import inspect_relations


class Graph:
    def begin_generation(self, spec: GraphGenerationSpec) -> str:
        raise AssertionError("inspection must not begin a graph generation")

    def sealed_receipt(self, generation_id: str) -> GraphBatchReceipt | None:
        raise AssertionError("inspection must not read a seal receipt")

    def upsert_batch(
        self,
        generation_id: str,
        assertions: tuple[RelationshipAssertion, ...],
        units: tuple[EvidenceUnit, ...],
        ctx: ExecutionContext,
    ) -> None:
        raise AssertionError("inspection must not upsert a graph batch")

    def seal_generation(
        self, generation_id: str, expected: GraphManifest, ctx: ExecutionContext
    ) -> GraphBatchReceipt:
        raise AssertionError("inspection must not seal a graph generation")

    def drop_generation(self, generation_id: str, ctx: ExecutionContext) -> None:
        raise AssertionError("inspection must not drop a graph generation")

    def expand(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        raise AssertionError("inspection must not expand semantic graph paths")

    def structural_context(
        self,
        view: GraphScope,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        raise AssertionError("inspection must not expand structural graph paths")

    def get_assertions(
        self,
        view: GraphScope,
        selected_unit_ids: tuple[str, ...],
        predicates: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
        *,
        direction: str | None = None,
        relation_view: str | None = None,
    ) -> AssertionPage:
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


def _view(eligible: frozenset[str]) -> GraphScope:
    return synthetic_graph_scope(
        source=eligible, context=eligible, revisions=(), generation_id="generation-1"
    )


def test_inspection_carries_expansion_work_and_keeps_eligible_conflict(tmp_path):
    view = _view(frozenset({"unit-1", "unit-2"}))
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
    view = _view(frozenset({"unit-1"}))
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
    view = _view(frozenset({"unit-1", "unit-2"}))

    class NamedGraph(Graph):
        def __init__(self, predicate):
            self.predicate = predicate

        def get_assertions(
            self,
            view,
            selected_unit_ids,
            predicates,
            limits,
            ctx,
            *,
            direction=None,
            relation_view=None,
        ):
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
