"""Complete synthetic eligibility views for retrieval unit tests."""

from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.identity import digest


def eligibility_view(
    *,
    revision_ids: frozenset[str] = frozenset(),
    context_unit_ids: frozenset[str] = frozenset(),
    structural_unit_ids: frozenset[str] = frozenset(),
    chunk_ids: frozenset[str] = frozenset(),
    graph_generation_id: str | None = None,
) -> LegacyEligibilityView:
    graph_bound = graph_generation_id is not None
    return LegacyEligibilityView(
        snapshot_id="synthetic-snapshot",
        policy_epoch=0,
        scope_digest=digest("synthetic-scope"),
        eligibility_policy_digest=digest("synthetic-policy"),
        eligibility_request_digest=digest("synthetic-request"),
        selected_batch_ids=(graph_generation_id,) if graph_bound else (),
        revision_ids=revision_ids,
        source_unit_ids=frozenset(),
        context_unit_ids=context_unit_ids,
        structural_unit_ids=structural_unit_ids,
        proxy_unit_ids=frozenset(),
        chunk_ids=chunk_ids,
        asset_ids=frozenset(),
        vector_record_ids=frozenset(),
        blocked_assertion_ids=frozenset(),
        graph_generation_id=graph_generation_id,
        graph_family_id=digest("synthetic-graph-family") if graph_bound else None,
        graph_config_sha=digest("synthetic-graph-config") if graph_bound else None,
        graph_decision_watermark=0 if graph_bound else None,
        graph_manifest_digest=digest("synthetic-graph-manifest")
        if graph_bound
        else None,
    )
