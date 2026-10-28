"""Complete synthetic eligibility views and attempts for retrieval unit tests."""

from collections.abc import Mapping
from types import MappingProxyType, SimpleNamespace
from typing import cast

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView, RevisionGrant
from tychora.contracts.graph import GraphBatchReceipt, GraphScope
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.publication import SnapshotView
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import MemberRow
from tychora.retrieval.attempt import AttemptOwner, EligibilityAttempt
from tychora.retrieval.compiler import CompiledEligibility


def revision_view(
    grants: Mapping[str, str] | None = None,
    *,
    excluded_units=(),
    excluded_assets=(),
    unit_filter=None,
    all_members=True,
    batches=None,
    batch_extractions=None,
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
        batch_extractions={
            slot: frozenset(items) for slot, items in (batch_extractions or {}).items()
        },
        all_members_eligible=all_members,
    )


def revision_attempt(
    view: EligibilityView,
    *,
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] | None = None,
    owner: AttemptOwner | None = None,
    ctx: ExecutionContext | None = None,
) -> EligibilityAttempt:
    """An attempt that reads ``view`` and has no unit-level view."""
    compiled = CompiledEligibility(
        view,
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
        compiled=compiled, snapshot=snapshot, owner=owner, ctx=ctx
    )


def sets_transaction(sets: Mapping[str, frozenset[str]]) -> SimpleNamespace:
    """A read transaction double whose representations know only these persisted sets."""
    return SimpleNamespace(
        representations=SimpleNamespace(
            dependencies=lambda ids: {i: sets[i] for i in ids if i in sets}
        )
    )


def member_row(revision_id: str, definition_hash: str, **changes) -> MemberRow:
    """An accepted, readable ``manual@1`` member of ``collection`` in ``workspace``."""
    row = MemberRow(
        source_id="source",
        revision_id=revision_id,
        extraction_id=revision_id + "-extraction",
        is_default=True,
        kind_id="manual@1",
        definition_hash=definition_hash,
        collection_id="collection",
        collection_policy_json='{"schema_version":1,"upload_providers":[]}',
        collection_floor=0,
        source_floor=0,
        withdrawn=False,
        allowed_uses_override=None,
        access_rank=0,
        allowed_uses_json='["audit"]',
        review_state="accepted",
        published_on=None,
        effective_from=None,
        effective_until=None,
        edition_key=revision_id,
        workspace_id="workspace",
    )
    return row._replace(**changes)


def members_transaction(
    rows: tuple[MemberRow, ...], envelopes: Mapping[str, Mapping[str, object]]
) -> SimpleNamespace:
    """A read transaction double serving members and kind envelopes, with no reviews."""

    def members(snapshot_id: str, extraction_ids: tuple[str, ...] = ()):
        return tuple(
            row
            for row in rows
            if not extraction_ids or row.extraction_id in extraction_ids
        )

    def kind_definitions(kind_ids: tuple[str, ...]):
        return tuple(
            (
                kind,
                str(envelopes[kind]["envelope_digest"]),
                canonical_bytes(cast(JsonValue, envelopes[kind])).decode(),
            )
            for kind in kind_ids
        )

    return SimpleNamespace(
        workspace=SimpleNamespace(policy_epoch=lambda: 0),
        eligibility=SimpleNamespace(
            members=members,
            kind_definitions=kind_definitions,
            review_rows=lambda extractions, states: (),
            dependents=lambda owner_ids: {},
            batches=lambda snapshot_id: (),
        ),
    )


def synthetic_graph_scope(
    *,
    source=(),
    context=(),
    structural=(),
    blocked=(),
    revisions=("revision",),
    generation_id: str = "generation",
    family_id: str | None = None,
    config_sha: str | None = None,
    decision_watermark: int = 0,
    manifest_digest: str | None = None,
) -> GraphScope:
    """A graph scope over one pinned generation; ``source`` defaults to none."""
    return GraphScope(
        snapshot_id="snapshot",
        policy_epoch=0,
        scope_digest=digest("scope"),
        eligibility_policy_digest=digest("policy"),
        eligibility_request_digest=digest("request"),
        receipt=GraphBatchReceipt(
            generation_id=generation_id,
            family_id=family_id or digest("family"),
            config_sha=config_sha or digest("config"),
            decision_watermark=decision_watermark,
            manifest_digest=manifest_digest or digest("manifest"),
            entity_count=0,
            evidence_count=0,
            assertion_count=0,
        ),
        revision_ids=frozenset(revisions),
        selected_batch_ids=(generation_id,),
        source_unit_ids=frozenset(source),
        context_unit_ids=frozenset(context),
        structural_unit_ids=frozenset(structural),
        blocked_assertion_ids=frozenset(blocked),
    )
