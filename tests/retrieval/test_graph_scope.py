"""Graph traversal scopes come from the revision-level view, only when needed."""

import importlib.util
from types import SimpleNamespace

from tests.helpers.eligibility import revision_view
from tychora.contracts.eligibility import GraphBinding
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import digest
from tychora.ports.repositories import UnitFlags
from tychora.retrieval.graph_scope import graph_scope


def _receipt():
    return GraphBatchReceipt(
        generation_id="generation",
        family_id=digest("graph-family"),
        config_sha=digest("graph-config"),
        decision_watermark=4,
        manifest_digest=digest("manifest"),
        entity_count=0,
        evidence_count=0,
        assertion_count=0,
    )


def _tx(flags, blocked=frozenset()):
    calls = []

    def unit_flags(extractions):
        calls.append(extractions)
        return tuple(f for f in flags if f.extraction_id in extractions)

    eligibility = SimpleNamespace(
        unit_flags=unit_flags, blocked_assertions=lambda watermark: blocked
    )
    return SimpleNamespace(eligibility=eligibility), calls


def test_no_graph_binding_reads_nothing():
    tx, calls = _tx(())
    assert graph_scope(tx, revision_view()) is None
    assert calls == []


def test_scope_is_eligible_units_minus_exclusions():
    receipt = _receipt()
    view = revision_view(excluded_units=("excluded",), unit_filter=("source", "proxy"))
    view = view.model_copy(
        update={
            "graph": GraphBinding(
                family_id=receipt.family_id, all_eligible=False, receipt=receipt
            )
        }
    )
    flags = (
        UnitFlags("source", "extraction", True, False),
        UnitFlags("filtered", "extraction", True, False),
        UnitFlags("heading", "extraction", False, False),
        UnitFlags("proxy", "extraction", False, True),
        UnitFlags("excluded", "extraction", True, False),
        UnitFlags("other", "elsewhere", True, False),
    )
    tx, calls = _tx(flags, blocked=frozenset({digest("assertion")}))
    scope = graph_scope(tx, view)
    assert scope is not None
    assert calls == [("extraction",)]
    assert scope.source_unit_ids == {"source"}
    assert scope.context_unit_ids == {"source", "filtered", "proxy"}
    assert scope.structural_unit_ids == {"heading"}
    assert scope.graph_eligible_unit_ids == {"source", "filtered", "proxy", "heading"}
    assert scope.blocked_assertion_ids == {digest("assertion")}
    assert scope.graph_generation_id == "generation"
    assert "generation" in scope.selected_batch_ids


def test_legacy_names_are_gone():
    assert importlib.util.find_spec("tychora.retrieval.eligibility") is None
    assert importlib.util.find_spec("tychora.adapters.sqlite.eligibility") is None
