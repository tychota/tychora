"""Delivery rechecks every owner a prepared result names against the view."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.helpers.eligibility import revision_attempt, revision_view
from tests.helpers.legacy import OutputCounter
from tychora.application.query import QueryService, _delivered_eligibility
from tychora.application.query_plan import QueryPlan
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    ImageLocator,
    LabelledProxy,
    RelationChecks,
    RelationPath,
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.graph import GraphCandidates
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import OwnerFact
from tychora.retrieval.response import ResponseAssembler, ResponseContext
from tychora.retrieval.segments import Selection

_EXCLUDED = {
    "record": "record-dependency",
    "relation": "relation-unit",
    "visual": "proxy-dependency",
}


def _facts(owner_ids):
    def fact(owner_id):
        kind = "asset" if owner_id == "asset" else "unit"
        return OwnerFact(
            owner_id=owner_id,
            kind=kind,
            extraction_id="extraction",
            dependency_ids=frozenset({"revision", owner_id}),
        )

    return tuple(fact(owner_id) for owner_id in owner_ids)


def _visual(dependency):
    return VisualEvidence(
        asset_id="asset",
        source_id="source",
        revision_id="revision",
        asset_ref="artifact",
        locator=ImageLocator(artifact_ref="artifact", native_ref="pixels"),
        proxies=(
            LabelledProxy(
                proxy_id="ocr",
                label="ocr",
                text="Synthetic derived OCR",
                origin="ocr",
                review_state="accepted",
                dependencies=frozenset({"asset", dependency}),
            ),
        ),
    )


def _prepared(kind):
    excluded = _EXCLUDED[kind]
    record = StructuredRecord(
        record_id="record",
        unit_id="kept",
        record_type="feedback",
        schema_id="feedback@1",
        value=FrozenJsonMap({"quote": "verbatim"}),
        dependencies=frozenset({"kept", excluded if kind == "record" else "kept"}),
    )
    relation = RelationPath(
        unit_ids=("kept", excluded if kind == "relation" else "kept"),
        assertion_ids=("assertion",),
        predicates=("references@1",),
        support_unit_ids=frozenset({"kept"}),
    )
    visual = _visual(excluded if kind == "visual" else "kept")
    return SimpleNamespace(records=(record,), relations=(relation,), visuals=(visual,))


@pytest.mark.parametrize("kind", sorted(_EXCLUDED))
def test_delivery_drops_an_excluded_dependency_of_each_owner_kind(kind):
    view = revision_view({"revision": "extraction"}, excluded_units={_EXCLUDED[kind]})
    tx = SimpleNamespace(eligibility=SimpleNamespace(owner_facts=_facts))
    units, assets = _delivered_eligibility(
        cast(Any, tx), view, cast(Any, _prepared(kind))
    )
    assert units == {"kept"}
    assert assets == {"asset"}


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("relation", "Relation path eligibility differs"),
        ("visual", "Visual proxy dependencies differ"),
    ],
)
def test_delivery_refuses_an_excluded_relation_unit_or_visual_dependency(kind, message):
    view = revision_view({"revision": "extraction"}, excluded_units={_EXCLUDED[kind]})
    tx = SimpleNamespace(eligibility=SimpleNamespace(owner_facts=_facts))
    prepared = _prepared(kind)
    units, assets = _delivered_eligibility(cast(Any, tx), view, cast(Any, prepared))
    context = ResponseContext(
        run_id="run",
        snapshot_id="snapshot",
        scope=RequestScope(principal_id="synthetic"),
        counter=OutputCounter(),
        eligible_unit_ids=units,
        eligible_asset_ids=assets,
        relations=prepared.relations if kind == "relation" else (),
        visuals=prepared.visuals if kind == "visual" else (),
    )
    selection = Selection(
        segments=(),
        reservation_priority=(),
        omitted_anchors=(),
        lane_coverage=(),
        text_budget=6000,
        visual_reserved_tokens=0,
    )
    with pytest.raises(TychoraError, match=message) as error:
        ResponseAssembler().assemble(
            selection,
            (),
            RelationChecks(status="not_evaluated"),
            QueryRequest.from_mapping({"question": "synthetic"}),
            context,
        )
    assert error.value.code == "INTEGRITY_ERROR"


def _graph_plan(explicit: frozenset[str]) -> QueryPlan:
    return QueryPlan(
        lexical=True,
        text=False,
        image=False,
        graph_strategies=frozenset({"semantic"}),
        rerank=False,
        skipped=(),
        explicit=explicit,
    )


@pytest.mark.asyncio
async def test_planned_graph_without_a_pinned_generation_raises_graph_disabled():
    async def no_scope(attempt, ctx):
        return None

    service = QueryService.__new__(QueryService)
    vars(service).update(graph=object(), _graph_scope=no_scope)
    attempt = revision_attempt(revision_view())
    request = QueryRequest.from_mapping({"question": "synthetic"})
    # A graph only in the default plan is skipped, not refused.
    assert await service._augment_graph(
        request, attempt, (), _graph_plan(frozenset()), ExecutionContext()
    ) == (None, GraphCandidates(), ())
    with pytest.raises(TychoraError) as error:
        await service._augment_graph(
            request,
            attempt,
            (),
            _graph_plan(frozenset({"graph"})),
            ExecutionContext(),
        )
    assert (error.value.code, error.value.reason) == (
        "CAPABILITY_MISSING",
        "graph.disabled",
    )
