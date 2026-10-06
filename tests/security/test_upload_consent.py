"""Explicit collection upload consent, using synthetic policy and mock dispatch only."""

import pytest

from tychora import Tychora
from tychora.api import ProviderBindings
from tychora.configuration.resolve import resolve_config
from tychora.contracts.embeddings import QueryUploadAuthorization
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


def _config(tmp_path, *, enabled=True, offline=False):
    settings = tmp_path / "workspace.toml"
    profile = (
        'schema_version=1\n[profiles.local]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="local"\n'
        f"[configuration]\noffline={'true' if offline else 'false'}\n"
    )
    if enabled:
        profile += (
            '[configuration.providers.text]\nprovider="voyage"\n'
            'model="voyage-context-4"\n'
            'credential_ref="env:SYNTHETIC_UNUSED_KEY"\n'
            '[configuration.index.text]\nbackend="chroma"\n'
        )
    settings.write_text(profile)
    return resolve_config(settings, "local", {})


def _request(action, collection_id="default", provider="voyage"):
    return {
        "operation": "provider-upload-consent",
        "collection_id": collection_id,
        "provider": provider,
        "action": action,
    }


def _dispatch(kb, collection_id, scope=None, *, policy_epoch=None):
    owner = kb._workspace.owner
    sent = []
    epoch = kb._workspace.bridge.call(
        owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()[0],
            ExecutionContext(),
        )
    )
    authorization = QueryUploadAuthorization(
        payload_sha="a" * 64,
        input_shas=("b" * 64,),
        collection_ids=frozenset({str(collection_id)}),
        policy_epoch=epoch if policy_epoch is None else policy_epoch,
    )
    result = kb._workspace.bridge.call(
        owner.run_query_provider(
            "voyage",
            lambda: sent.append("mock") or "mock",
            ExecutionContext(),
            scope=scope or kb._workspace.scope,
            authorization=authorization,
        )
    )
    return result, sent


def test_default_owner_grant_is_durable_only_after_reopen_and_revoke_is_current(
    tmp_path,
):
    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        collection = kb.default_collection_id
        with pytest.raises(TychoraError) as unavailable:
            kb.management.run(_request("grant", provider="anthropic"))
        assert unavailable.value.code == "INVALID_REQUEST"
        with pytest.raises(TychoraError) as denied:
            _dispatch(kb, collection)
        assert denied.value.code == "FORBIDDEN_SCOPE"
        epoch_before = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                    (kb._workspace.owner.workspace_id,),
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        granted = kb.management.run(_request("grant"))
        assert granted.upload_consent is not None
        assert granted.upload_consent.collection_upload_providers == ("voyage",)
        assert granted.upload_consent.owner_upload_providers == ("voyage",)
        assert granted.upload_consent.default_owner_requires_reopen is True
        assert granted.upload_consent.policy_epoch == epoch_before + 1
        with pytest.raises(TychoraError):
            _dispatch(kb, collection)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        collection = kb.default_collection_id
        assert "voyage" in kb._workspace.scope.upload_providers
        stale_scope = kb._workspace.scope
        assert _dispatch(kb, collection) == ("mock", ["mock"])
        old_epoch = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                    (kb._workspace.owner.workspace_id,),
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        narrower = stale_scope.model_copy(update={"upload_providers": frozenset()})
        with pytest.raises(TychoraError):
            _dispatch(kb, collection, narrower)
        revoked = kb.management.run(_request("revoke"))
        assert revoked.upload_consent is not None
        assert revoked.upload_consent.collection_upload_providers == ()
        assert revoked.upload_consent.owner_upload_providers == ()
        assert revoked.upload_consent.default_owner_requires_reopen is False
        assert revoked.upload_consent.policy_epoch == old_epoch + 1
        with pytest.raises(TychoraError) as denied:
            _dispatch(kb, collection, stale_scope)
        assert denied.value.code in {"FORBIDDEN_SCOPE", "SCOPE_CHANGED"}
        with pytest.raises(TychoraError) as queued:
            _dispatch(kb, collection, stale_scope, policy_epoch=old_epoch)
        assert queued.value.code in {"FORBIDDEN_SCOPE", "SCOPE_CHANGED"}
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        assert kb._workspace.scope.upload_providers == frozenset()


def test_explicit_caller_and_http_token_scopes_never_widen(tmp_path):
    from tychora.interfaces.http.auth import LocalAuth

    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        kb.management.run(_request("grant"))
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        owner = kb._workspace.owner
        collection_id = str(kb.default_collection_id)
        token_scope = owner.scope.model_copy(update={"upload_providers": frozenset()})
        token = LocalAuth({"synthetic-token": token_scope}, authority="127.0.0.1:8765")
        narrower = token.authenticate("Bearer synthetic-token", owner.scope)
        assert narrower.upload_providers == frozenset()
        with pytest.raises(TychoraError):
            _dispatch(kb, kb.default_collection_id, narrower)
    explicit = RequestScope(
        principal_id="local-owner",
        collection_ids=frozenset({collection_id}),
        access_ceiling=0,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage"}),
    )
    with Tychora.open(
        workspace, config=config, providers=ProviderBindings(), scope=explicit
    ) as kb:
        assert kb._workspace.scope.upload_providers == frozenset()
        with pytest.raises(TychoraError):
            _dispatch(kb, kb.default_collection_id)


def test_explicit_provider_scope_uses_collection_policy_without_owner_set(tmp_path):
    from tychora.contracts.identity import canonical_bytes

    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        collection_id = str(kb.default_collection_id)
        kb.management.run(_request("grant"))
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "UPDATE collections SET policy_json=? WHERE collection_id=?",
                    (
                        canonical_bytes(
                            {
                                "schema_version": 1,
                                "upload_providers": ["voyage"],
                                "owner_upload_providers": [],
                            }
                        ).decode(),
                        collection_id,
                    ),
                ),
                ExecutionContext(),
            )
        )
    explicit = RequestScope(
        principal_id="local-owner",
        collection_ids=frozenset({collection_id}),
        access_ceiling=0,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage"}),
        upload_providers=frozenset({"voyage"}),
    )
    with Tychora.open(
        workspace, config=config, providers=ProviderBindings(), scope=explicit
    ) as kb:
        assert _dispatch(kb, collection_id) == ("mock", ["mock"])


def test_disabled_or_unregistered_provider_cannot_be_granted(tmp_path):
    workspace = tmp_path / "knowledge"
    disabled = _config(tmp_path, enabled=False)
    with Tychora.open(workspace, config=disabled, providers=ProviderBindings()) as kb:
        for provider in ("voyage", "anthropic", "unknown"):
            with pytest.raises(TychoraError) as error:
                kb.management.run(_request("grant", provider=provider))
            assert error.value.code == "INVALID_REQUEST"
        with pytest.raises(TychoraError) as absent:
            kb.management.run(_request("revoke"))
        assert absent.value.code == "INVALID_REQUEST"


def test_disabled_profile_can_revoke_dormant_registered_grant(tmp_path):
    workspace = tmp_path / "knowledge"
    enabled = _config(tmp_path)
    with Tychora.open(workspace, config=enabled, providers=ProviderBindings()) as kb:
        granted = kb.management.run(_request("grant"))
        grant_epoch = granted.upload_consent.policy_epoch
    disabled = _config(tmp_path, enabled=False)
    with Tychora.open(workspace, config=disabled, providers=ProviderBindings()) as kb:
        assert kb._workspace.scope.upload_providers == frozenset()
        revoked = kb.management.run(_request("revoke"))
        assert revoked.upload_consent.collection_upload_providers == ()
        assert revoked.upload_consent.owner_upload_providers == ()
        assert revoked.upload_consent.policy_epoch == grant_epoch + 1
    reenabled = _config(tmp_path)
    with Tychora.open(workspace, config=reenabled, providers=ProviderBindings()) as kb:
        assert kb._workspace.scope.upload_providers == frozenset()
        with pytest.raises(TychoraError) as denied:
            _dispatch(kb, kb.default_collection_id)
        assert denied.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_public_revoke_blocks_actual_queued_provider_admission(tmp_path):
    import asyncio
    import threading

    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        kb.management.run(_request("grant"))
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        owner = kb._workspace.owner
        scope = kb._workspace.scope
        epoch = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()[0],
            ExecutionContext(),
        )
        authorization = QueryUploadAuthorization(
            payload_sha="a" * 64,
            input_shas=("b" * 64,),
            collection_ids=frozenset({str(kb.default_collection_id)}),
            policy_epoch=epoch,
        )
        release = threading.Event()
        lock = threading.Lock()
        started = []

        def mock_dispatch():
            with lock:
                started.append(True)
            assert release.wait(5)
            return "mock"

        tasks = [
            asyncio.create_task(
                owner.run_query_provider(
                    "voyage",
                    mock_dispatch,
                    ExecutionContext(),
                    scope=scope,
                    authorization=authorization,
                )
            )
            for _ in range(3)
        ]
        try:
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(started) == 2
            revoked = await asyncio.to_thread(kb.management.run, _request("revoke"))
            assert revoked.upload_consent.owner_upload_providers == ()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert results[:2] == ["mock", "mock"]
            assert isinstance(results[2], TychoraError)
            assert results[2].code == "SCOPE_CHANGED"
            assert len(started) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


def test_offline_profile_keeps_mock_provider_blocked_after_grant(tmp_path):
    config = _config(tmp_path, offline=True)
    with Tychora.open(
        tmp_path / "knowledge", config=config, providers=ProviderBindings()
    ) as kb:
        kb.management.run(_request("grant"))
    with (
        Tychora.open(
            tmp_path / "knowledge", config=config, providers=ProviderBindings()
        ) as kb,
        pytest.raises(TychoraError),
    ):
        _dispatch(kb, kb.default_collection_id)


def test_owner_union_from_grant_a_does_not_authorize_collection_b(tmp_path):
    from tychora.contracts.identity import canonical_bytes

    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    collection_b = "00000000-0000-4000-8000-000000000099"
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        kb.management.run(_request("grant"))
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        owner = kb._workspace.owner
        collection_a = str(kb.default_collection_id)

        def add_b():
            owner.db.execute(
                "INSERT INTO collections "
                "(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,0,?)",
                (
                    collection_b,
                    owner.workspace_id,
                    "synthetic-b",
                    "Synthetic B",
                    canonical_bytes(
                        {
                            "schema_version": 1,
                            "upload_providers": ["voyage"],
                            "owner_upload_providers": [],
                        }
                    ).decode(),
                ),
            )
            owner.scope = owner.scope.model_copy(
                update={"collection_ids": frozenset({collection_a, collection_b})}
            )
            return owner.scope

        kb._workspace.scope = kb._workspace.bridge.call(
            owner.run_blocking(add_b, ExecutionContext())
        )
        assert _dispatch(kb, collection_a) == ("mock", ["mock"])
        with pytest.raises(TychoraError) as denied:
            _dispatch(kb, collection_b)
        assert denied.value.code == "FORBIDDEN_SCOPE"
        changed = kb.management.run(_request("grant", collection_b))
        assert changed.upload_consent.owner_upload_providers == ("voyage",)
        assert _dispatch(kb, collection_b) == ("mock", ["mock"])


@pytest.mark.asyncio
async def test_http_management_receipt_does_not_widen_existing_token(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    config = _config(tmp_path)
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        owner = kb._workspace.owner
        token = LocalAuth({"manager": owner.scope}, authority="127.0.0.1:8765")
        app = create_app(owner, kb._workspace.services, token)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer manager"},
        ) as client:
            granted = await client.post("/v1/jobs", json=_request("grant"))
            assert granted.status_code == 200, granted.text
            assert granted.json()["upload_consent"]["owner_upload_providers"] == [
                "voyage"
            ]
            assert token.authenticate(
                "Bearer manager", owner.scope
            ).upload_providers == (frozenset())
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        assert (
            token.authenticate(
                "Bearer manager", kb._workspace.owner.scope
            ).upload_providers
            == frozenset()
        )
        app = create_app(kb._workspace.owner, kb._workspace.services, token)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer manager"},
        ) as client:
            revoked = await client.post("/v1/jobs", json=_request("revoke"))
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["upload_consent"]["owner_upload_providers"] == []


def test_schema_v1_policy_extension_rejects_unknown_or_duplicate_grants():
    from tychora.application.consent import parse_upload_policy

    assert parse_upload_policy('{"schema_version":1,"upload_providers":[]}') == (
        frozenset(),
        frozenset(),
    )
    for raw in (
        '{"schema_version":1,"upload_providers":[],"unknown":[]}',
        '{"schema_version":1,"upload_providers":["voyage","voyage"]}',
        '{"schema_version":1,"upload_providers":[],"owner_upload_providers":["unregistered"]}',
        '{"schema_version":true,"upload_providers":[]}',
        '{"schema_version":1,"upload_providers":[],"upload_providers":[]}',
    ):
        with pytest.raises(TychoraError) as error:
            parse_upload_policy(raw)
        assert error.value.code == "INTEGRITY_ERROR"


def test_legacy_collection_only_policy_does_not_create_owner_authority(tmp_path):
    from tychora.contracts.identity import canonical_bytes

    workspace = tmp_path / "knowledge"
    config = _config(tmp_path)
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        collection_id = str(kb.default_collection_id)
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "UPDATE collections SET policy_json=? WHERE collection_id=?",
                    (
                        canonical_bytes(
                            {"schema_version": 1, "upload_providers": ["voyage"]}
                        ).decode(),
                        collection_id,
                    ),
                ),
                ExecutionContext(),
            )
        )
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        assert kb._workspace.scope.upload_providers == frozenset()
        with pytest.raises(TychoraError):
            _dispatch(kb, collection_id)


@pytest.mark.asyncio
async def test_async_management_uses_same_consent_receipt(tmp_path):
    from tychora import AsyncTychora

    config = _config(tmp_path)
    async with AsyncTychora.open(
        tmp_path / "knowledge", config=config, providers=ProviderBindings()
    ) as kb:
        granted = await kb.management.run(_request("grant"))
        assert granted.upload_consent.owner_upload_providers == ("voyage",)
        revoked = await kb.management.run(_request("revoke"))
        assert revoked.upload_consent.owner_upload_providers == ()


def test_cli_management_grant_and_revoke_receipts_are_explicit(tmp_path):
    import json

    from typer.testing import CliRunner

    from tychora.interfaces.cli import app

    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    settings = workspace / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="local"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\n'
        'credential_ref="env:SYNTHETIC_UNUSED_KEY"\n'
    )
    with Tychora.open(
        workspace,
        config=resolve_config(settings, "local", {}),
        providers=ProviderBindings(),
    ):
        pass
    runner = CliRunner()
    for action, expected in (("grant", ["voyage"]), ("revoke", [])):
        result = runner.invoke(
            app,
            [
                "--workspace",
                str(workspace),
                "manage",
                "provider-upload-consent",
                "default",
                "voyage",
                action,
                "--json",
            ],
        )
        assert result.exit_code == 0, result.stderr
        assert json.loads(result.stdout)["upload_consent"][
            "owner_upload_providers"
        ] == (expected)


@pytest.mark.asyncio
async def test_retained_source_job_public_retry_after_revoke_never_dispatches(tmp_path):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
    from tychora.adapters.chroma.index import ChromaVectorIndex
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    _config(tmp_path)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        settings.read_text().replace(
            'model="voyage-context-4"\n', 'model="voyage-context-4"\ndimensions=2\n'
        )
    )
    config = resolve_config(settings, "local", {})
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, providers=ProviderBindings()) as kb:
        kb.management.run(_request("grant"))

    counter = RecordingCounter()
    embedder = RecordingEmbedder(counter)

    class FailOnce:
        capabilities = embedder.capabilities

        def __init__(self):
            self.calls = 0

        def embed_groups(self, groups, ctx):
            self.calls += 1
            if self.calls == 1:
                raise TychoraError(
                    "PROVIDER_FAILURE",
                    "Synthetic transport interrupted",
                    retryable=True,
                )
            return embedder.embed_groups(groups, ctx)

        def embed_queries(self, queries, ctx):
            return embedder.embed_queries(queries, ctx)

        def probe_queries(self, queries, ctx):
            return embedder.probe_queries(queries, ctx)

    flaky = FailOnce()
    bindings = ProviderBindings(
        text_embedder=flaky,
        text_counter=counter,
        text_index=ChromaVectorIndex(tmp_path / "vectors"),
    )
    with Tychora.open(
        workspace, config=config, providers=bindings, token_counter=counter
    ) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic consent retry evidence.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "consent-retry",
                "collection_id": "default",
                "metadata": {
                    "title": "Consent retry",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            },
            idempotency_key="retained-consent-retry",
        )
        failed = kb.jobs.wait(job.id)
        assert failed.state == "failed" and failed.error_code == "PROVIDER_FAILURE"
        assert failed.captured_inputs is not None and flaky.calls == 1
        revoked = kb.management.run(_request("revoke"))
        assert revoked.upload_consent.collection_upload_providers == ()
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"writer": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer writer"},
        ) as client:
            response = await client.post(f"/v1/jobs/{job.id}/retry")
        assert response.status_code == 202, response.text
        retried = await asyncio.to_thread(kb.jobs.wait, job.id)
        assert retried.state == "failed"
        assert retried.error_code in {"SCOPE_CHANGED", "FORBIDDEN_SCOPE"}
        assert flaky.calls == 1
