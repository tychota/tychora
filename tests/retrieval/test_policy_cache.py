"""Cache isolation for compiled policy eligibility."""

from tychora.adapters.files.cas import LocalArtifacts
from tychora.configuration.registry import Registry
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.retrieval.compiler import EligibilityCompiler, eligible_cache_key


def _view(policy_digest: str) -> EligibilityView:
    return EligibilityView(
        snapshot_id="snapshot",
        policy_epoch=7,
        scope_digest=digest("scope"),
        eligibility_policy_digest=policy_digest,
        eligibility_request_digest=digest("request"),
        revisions={},
    )


def test_audit_and_compose_do_not_share_eligibility(tmp_path):
    from tests.retrieval.test_segments import config as resolved

    config = resolved(tmp_path)
    compiler = EligibilityCompiler(
        Registry.from_config(config), LocalArtifacts(tmp_path / "artifacts")
    )
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({"collection"}),
        uses=frozenset({"audit", "compose"}),
    )
    audit, compose = (
        compiler.mode(
            config, QueryRequest.from_mapping({"question": "q", "mode": mode}), scope
        )
        for mode in ("audit", "compose")
    )
    views = _view(audit.digest), _view(compose.digest)
    assert views[0].snapshot_id == views[1].snapshot_id
    assert views[0].scope_digest == views[1].scope_digest
    assert eligible_cache_key(views[0]) != eligible_cache_key(views[1])
