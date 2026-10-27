"""Complete synthetic eligibility views and attempts for retrieval unit tests."""

from collections.abc import Mapping
from types import MappingProxyType

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView, RevisionGrant
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.identity import digest
from tychora.contracts.publication import SnapshotView
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.attempt import AttemptOwner, EligibilityAttempt
from tychora.retrieval.compiler import CompiledEligibility


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


def revision_view(
    grants: Mapping[str, str] | None = None,
    *,
    excluded_units=(),
    excluded_assets=(),
    unit_filter=None,
    all_members=True,
    batches=None,
    snapshot_id="snapshot",
    lane="standard",
) -> EligibilityView:
    """A revision-level view granting each ``revision -> extraction`` pair."""
    pairs = {"revision": "extraction"} if grants is None else grants
    return EligibilityView(
        snapshot_id=snapshot_id,
        policy_epoch=0,
        scope_digest=digest("synthetic-scope"),
        eligibility_policy_digest=digest("synthetic-policy"),
        eligibility_request_digest=digest("synthetic-request"),
        revisions={
            revision: RevisionGrant(
                extraction_id=extraction,
                source_id=f"source-{revision}",
                kind_id="manual@1",
                lane=lane,
                collection_id="collection",
            )
            for revision, extraction in pairs.items()
        },
        excluded_unit_ids=frozenset(excluded_units),
        excluded_asset_ids=frozenset(excluded_assets),
        unit_filter=None if unit_filter is None else frozenset(unit_filter),
        selected_batches=batches or {},
        all_members_eligible=all_members,
    )


def legacy_attempt(
    view: LegacyEligibilityView,
    *,
    dependencies: Mapping[str, frozenset[str]] | None = None,
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] | None = None,
    owner: AttemptOwner | None = None,
    ctx: ExecutionContext | None = None,
) -> EligibilityAttempt:
    """An attempt whose legacy readers see ``view``.

    Its revision-level view carries only ``view``'s snapshot, epoch and
    digests, so epoch rechecks compare against the same policy epoch.
    """
    compiled = CompiledEligibility(
        EligibilityView(
            snapshot_id=view.snapshot_id,
            policy_epoch=view.policy_epoch,
            scope_digest=view.scope_digest,
            eligibility_policy_digest=view.eligibility_policy_digest,
            eligibility_request_digest=view.eligibility_request_digest,
            revisions={},
        ),
        historical_kind_envelopes=MappingProxyType(
            dict(historical_kind_envelopes or {})
        ),
    )
    snapshot = SnapshotView(
        snapshot_id=view.snapshot_id,
        workspace_id="synthetic-workspace",
        config_sha=digest("synthetic-config"),
        membership_sha=digest("synthetic-membership"),
        member_count=0,
        page_ids=(),
        families=(),
    )
    return EligibilityAttempt(
        compiled=compiled,
        snapshot=snapshot,
        legacy=view,
        legacy_dependencies=MappingProxyType(dict(dependencies or {})),
        owner=owner,
        ctx=ctx,
    )
