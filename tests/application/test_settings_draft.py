"""Guided edits preserve the workspace document and activation guarantees."""

import hashlib
import tomllib

import pytest
from httpx import ASGITransport, AsyncClient

from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_ast_patch_preview_apply_inherit_and_stale(tmp_path):
    root = tmp_path / "workspace"
    with Tychora.open(root, token_counter=Counter()) as kb:
        path = root / "workspace.toml"
        original = (
            path.read_text()
            + '\n# keep café "quoted"\n[profiles.alternate]\nbuiltin="documents@1"\n[configuration.providers.text]\ncredential_ref="env:UNCHANGED_REFERENCE"\n'
        )
        path.write_text(original)
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            fields = await client.get("/v1/settings/fields")
            assert fields.status_code == 200, fields.text
            patch = {
                "base_workspace_sha256": fields.json()["base_workspace_sha256"],
                "changes": [{"path": "limits.limit", "action": "set", "value": 12}],
            }
            before = owner.config
            epoch = await owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT policy_epoch FROM workspaces"
                ).fetchone()[0],
                ExecutionContext(),
            )
            preview = await client.post("/v1/settings/preview", json=patch)
            assert preview.status_code == 200, preview.text
            assert preview.json()["activation_allowed"] is True
            assert preview.json()["rebuild_queued"] is False
            assert path.read_text() == original and owner.config is before
            assert (
                await owner.run_blocking(
                    lambda: owner.db.execute(
                        "SELECT policy_epoch FROM workspaces"
                    ).fetchone()[0],
                    ExecutionContext(),
                )
                == epoch
            )
            applied = await client.patch("/v1/settings", json=patch)
            assert applied.status_code == 200, applied.text
            revised = path.read_text()
            assert '# keep café "quoted"' in revised
            assert (
                tomllib.loads(revised)["profiles"]
                == tomllib.loads(original)["profiles"]
            )
            assert (
                tomllib.loads(revised)["configuration"]["providers"]["text"][
                    "credential_ref"
                ]
                == "env:UNCHANGED_REFERENCE"
            )
            assert owner.config.values.limits.limit == 12
            stale = await client.patch("/v1/settings", json=patch)
            assert stale.json()["code"] == "IDEMPOTENCY_CONFLICT"
            patch["base_workspace_sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            patch["changes"] = [{"path": "limits.limit", "action": "inherit"}]
            inherited = await client.patch("/v1/settings", json=patch)
            assert inherited.status_code == 200, inherited.text
            field = next(
                f for f in inherited.json()["fields"] if f["path"] == "limits.limit"
            )
            assert field["overridden"] is False and field["effective_value"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        [{"path": "profiles.local.builtin", "action": "set", "value": "other"}],
        [
            {
                "path": "providers.text.credential_ref",
                "action": "set",
                "value": "sk-secret",
            }
        ],
        [
            {
                "path": "providers.text.credential_ref",
                "action": "set",
                "value": 'env:é"name',
            }
        ],
        [{"path": "limits.limit", "action": "set", "value": None}],
        [{"path": "limits.limit", "action": "inherit", "value": None}],
        [{"path": "limits.limit", "action": "set", "value": True}],
        [{"path": "providers.rerank.model", "action": "set", "value": "rerank-3"}],
        [{"path": "limits.limit", "action": "set", "value": 8}] * 2,
    ],
)
async def test_guided_patch_rejects_non_allowlisted_or_invalid_values(
    tmp_path, changes
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            digest = hashlib.sha256(
                (kb._workspace.owner.path / "workspace.toml").read_bytes()
            ).hexdigest()
            response = await client.patch(
                "/v1/settings",
                json={"base_workspace_sha256": digest, "changes": changes},
            )
            assert response.status_code == 400, response.text
            assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_draft_failed_runtime_preparation_rolls_back(tmp_path, monkeypatch):
    from tychora.contracts.errors import TychoraError

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        path = owner.path / "workspace.toml"
        original = path.read_bytes()
        old = owner.config

        def fail(*args, **kwargs):
            raise TychoraError("CAPABILITY_MISSING", "Synthetic preparation failure.")

        monkeypatch.setattr("tychora.application.settings.build_services", fail)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            patch = {
                "base_workspace_sha256": hashlib.sha256(original).hexdigest(),
                "changes": [{"path": "limits.limit", "action": "set", "value": 12}],
            }
            preview = await client.post("/v1/settings/preview", json=patch)
            assert preview.json()["activation_allowed"]
            failed = await client.patch("/v1/settings", json=patch)
            assert failed.json()["code"] == "CAPABILITY_MISSING"
            assert path.read_bytes() == original and owner.config is old
            assert app.state.runtime.services is kb._workspace.services


@pytest.mark.asyncio
async def test_references_are_manager_only_and_preview_redacts_values(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner": kb.scope, "reader": reader}, authority="127.0.0.1:8765"
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            fields = (await client.get("/v1/settings/fields")).json()
            patch = {
                "base_workspace_sha256": fields["base_workspace_sha256"],
                "changes": [
                    {
                        "path": "providers.text.credential_ref",
                        "action": "set",
                        "value": "secret:Workspace.VOYAGE-key",
                    }
                ],
            }
            preview = await client.post("/v1/settings/preview", json=patch)
            assert "Workspace.VOYAGE-key" not in preview.text
            applied = await client.patch("/v1/settings", json=patch)
            assert applied.status_code == 200, applied.text
            assert "secret:Workspace.VOYAGE-key" in applied.text
            assert (
                "secret:Workspace.VOYAGE-key"
                not in (await client.get("/v1/settings")).text
            )
            denied = await client.get(
                "/v1/settings/fields", headers={"Authorization": "Bearer reader"}
            )
            assert denied.status_code == 403


@pytest.mark.asyncio
async def test_activation_refreshes_only_managed_future_upload_authority(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        auth = LocalAuth(
            {"managed": kb.scope, "static": kb.scope},
            authority="127.0.0.1:8765",
            managed_owner_token="managed",
        )
        app = create_app(owner, kb._workspace.services, auth)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer managed"},
        ) as client:
            fields = (await client.get("/v1/settings/fields")).json()
            patch = {
                "base_workspace_sha256": fields["base_workspace_sha256"],
                "changes": [
                    {
                        "path": "providers.text.provider",
                        "action": "set",
                        "value": "voyage",
                    },
                    {
                        "path": "providers.text.model",
                        "action": "set",
                        "value": "voyage-context-4",
                    },
                ],
            }
            assert (await client.patch("/v1/settings", json=patch)).status_code == 200
            consent = await client.post(
                "/v1/jobs",
                json={
                    "operation": "provider-upload-consent",
                    "collection_id": "default",
                    "provider": "voyage",
                    "action": "grant",
                },
            )
            assert consent.status_code == 200, consent.text
            assert "voyage" in kb.scope.upload_providers
            captured = kb.scope
            fields = (await client.get("/v1/settings/fields")).json()
            patch = {
                "base_workspace_sha256": fields["base_workspace_sha256"],
                "changes": [
                    {
                        "path": "providers.text.provider",
                        "action": "set",
                        "value": "disabled",
                    }
                ],
            }
            assert (await client.patch("/v1/settings", json=patch)).status_code == 200
            managed = (await client.get("/v1/handshake")).json()
            static = (
                await client.get(
                    "/v1/handshake", headers={"Authorization": "Bearer static"}
                )
            ).json()
            assert "voyage" not in managed["scope"]["upload_providers"]
            assert "voyage" not in static["scope"]["upload_providers"]
            assert "voyage" not in kb.scope.upload_providers
            assert "voyage" in captured.upload_providers
            patch["base_workspace_sha256"] = (
                await client.get("/v1/settings/fields")
            ).json()["base_workspace_sha256"]
            patch["changes"][0]["value"] = "voyage"
            assert (await client.patch("/v1/settings", json=patch)).status_code == 200
            assert (
                "voyage"
                in (await client.get("/v1/handshake")).json()["scope"][
                    "upload_providers"
                ]
            )
            assert (
                "voyage"
                not in (
                    await client.get(
                        "/v1/handshake", headers={"Authorization": "Bearer static"}
                    )
                ).json()["scope"]["upload_providers"]
            )


@pytest.mark.asyncio
async def test_fresh_remote_settings_apply_and_inherit_without_provider_io(
    tmp_path, monkeypatch
):
    from tychora.application.setup import SetupService

    cache = tmp_path / "missing-cache"
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(cache))
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner

        def forbidden(*args, **kwargs):
            pytest.fail(
                "Settings inspection/activation must not resolve secrets or dispatch/download"
            )

        monkeypatch.setattr(
            "tychora.application.default_bindings._credential", forbidden
        )
        monkeypatch.setattr(
            "tychora.application.default_bindings.HttpxVoyageTransport", forbidden
        )
        monkeypatch.setattr(SetupService, "_download", forbidden)
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            fields = (await client.get("/v1/settings/fields")).json()
            indexed = {field["path"]: field for field in fields["fields"]}
            assert indexed["offline"]["value_schema"]["type"] == "boolean"
            for path in ("index.text.backend", "index.image.backend"):
                assert indexed[path]["choices"] == ["disabled", "chroma"]
            patch = {
                "base_workspace_sha256": fields["base_workspace_sha256"],
                "changes": [
                    {"path": "offline", "action": "set", "value": False},
                    {
                        "path": "providers.text.provider",
                        "action": "set",
                        "value": "voyage",
                    },
                    {
                        "path": "providers.text.model",
                        "action": "set",
                        "value": "voyage-context-4",
                    },
                    {
                        "path": "providers.text.credential_ref",
                        "action": "set",
                        "value": "env:SYNTHETIC_NOT_RESOLVED",
                    },
                    {
                        "path": "providers.rerank.provider",
                        "action": "set",
                        "value": "voyage",
                    },
                    {
                        "path": "providers.rerank.model",
                        "action": "set",
                        "value": "rerank-2.5",
                    },
                    {
                        "path": "providers.rerank.credential_ref",
                        "action": "set",
                        "value": "env:SYNTHETIC_NOT_RESOLVED",
                    },
                    {"path": "index.text.backend", "action": "set", "value": "chroma"},
                    {
                        "path": "index.image.backend",
                        "action": "set",
                        "value": "disabled",
                    },
                ],
            }
            original = (owner.path / "workspace.toml").read_bytes()
            previous = owner.config
            preview = await client.post("/v1/settings/preview", json=patch)
            assert preview.status_code == 200, preview.text
            assert preview.json()["activation_allowed"]
            assert (owner.path / "workspace.toml").read_bytes() == original
            assert owner.config is previous and not (owner.path / "indexes").exists()
            applied = await client.patch("/v1/settings", json=patch)
            assert applied.status_code == 200, applied.text
            assert not owner.config.values.offline
            assert owner.config.values.index.text.backend == "chroma"
            bindings = app.state.runtime.services.provider_bindings
            assert bindings.text_query is not None and bindings.reranker is not None
            assert bindings.text_index is not None
            assert not cache.exists()
            assert not kb.scope.upload_providers
            assert not (
                await client.get(
                    "/v1/collections/" + str(kb.default_collection_id) + "/consent"
                )
            ).json()["collection_upload_providers"]
            patch = {
                "base_workspace_sha256": applied.json()["base_workspace_sha256"],
                "changes": [
                    {"path": path, "action": "inherit"}
                    for path in ("offline", "index.text.backend", "index.image.backend")
                ],
            }
            inherited = await client.patch("/v1/settings", json=patch)
            assert inherited.status_code == 200, inherited.text
            fields = {field["path"]: field for field in inherited.json()["fields"]}
            assert fields["offline"]["effective_value"] is True
            for path in ("offline", "index.text.backend", "index.image.backend"):
                assert not fields[path]["overridden"]
            assert fields["index.text.backend"]["effective_value"] == "disabled"
            assert fields["index.image.backend"]["effective_value"] == "disabled"

            added = await client.post(
                "/v1/sources",
                json={
                    "type": "text",
                    "source_key": "populated",
                    "collection_id": "default",
                    "kind": "document@1",
                    "format": "markdown",
                    "text": "Synthetic retained evidence.",
                    "metadata": {"title": "Retained evidence"},
                },
            )
            assert added.status_code == 202, added.text
            job = await app.state.runtime.services.jobs_for_scope(owner.scope).wait(
                added.json()["id"], ExecutionContext(), timeout=5
            )
            assert job.state == "succeeded"
            incompatible = {
                "base_workspace_sha256": inherited.json()["base_workspace_sha256"],
                "changes": [
                    {"path": "offline", "action": "set", "value": False},
                    {"path": "index.text.backend", "action": "set", "value": "chroma"},
                ],
            }
            preview = await client.post("/v1/settings/preview", json=incompatible)
            assert preview.status_code == 200, preview.text
            assert preview.json()["blockers"] == ["PROFILE_INCOMPATIBLE"]
            assert not preview.json()["activation_allowed"]
            rejected = await client.patch("/v1/settings", json=incompatible)
            assert (
                rejected.status_code == 409
                and rejected.json()["code"] == "PROFILE_INCOMPATIBLE"
            )
            assert (
                owner.config.values.offline
                and owner.config.values.index.text.backend == "disabled"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("offline", "false"),
        ("offline", 0),
        ("index.text.backend", "other"),
        ("index.image.backend", True),
    ],
)
async def test_remote_prerequisite_types_are_strict(tmp_path, path, value):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner": kb.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner"},
        ) as client:
            fields = (await client.get("/v1/settings/fields")).json()
            assert path in {field["path"] for field in fields["fields"]}
            patch = {
                "base_workspace_sha256": fields["base_workspace_sha256"],
                "changes": [{"path": path, "action": "set", "value": value}],
            }
            result = await client.patch("/v1/settings", json=patch)
            assert (
                result.status_code == 400 and result.json()["code"] == "INVALID_REQUEST"
            )
