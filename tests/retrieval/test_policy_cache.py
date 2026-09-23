"""Cache isolation for compiled policy eligibility."""

from types import SimpleNamespace

from tychora.contracts.graph import EligibilityView
from tychora.contracts.identity import digest
from tychora.retrieval.eligibility import eligible_cache_key


def _view(policy: str) -> EligibilityView:
    return EligibilityView(
        snapshot_id="snapshot",
        policy_epoch=7,
        scope_digest=digest("scope"),
        eligibility_policy_digest=digest("policy", policy),
        eligibility_request_digest=digest("request"),
        selected_batch_ids=("lexical-batch",),
        revision_ids=frozenset(),
        source_unit_ids=frozenset(),
        context_unit_ids=frozenset(),
        structural_unit_ids=frozenset(),
        proxy_unit_ids=frozenset(),
        chunk_ids=frozenset(),
        asset_ids=frozenset(),
        vector_record_ids=frozenset(),
        blocked_assertion_ids=frozenset(),
    )


def test_audit_and_compose_do_not_share_eligibility():
    views = SimpleNamespace(audit=_view("audit"), compose=_view("compose"))
    assert views.audit.snapshot_id == views.compose.snapshot_id
    assert views.audit.scope_digest == views.compose.scope_digest
    assert eligible_cache_key(views.audit) != eligible_cache_key(views.compose)
