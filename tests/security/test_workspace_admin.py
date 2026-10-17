"""Workspace operations require canonical full collection/access/use authority."""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.contracts.collections import CollectionCreate
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import SettingsActivation
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_full_static_manager_and_narrow_tokens_across_global_routes(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        narrow = kb.scope
        await kb._workspace.services.collections.create(
            CollectionCreate(title="Second", slug="second"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
        full = kb.scope
        reader = full.model_copy(update={"capabilities": frozenset({"read"})})
        limited_uses = full.model_copy(update={"uses": frozenset({"lookup"})})
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {
                    "full": full,
                    "narrow": narrow,
                    "reader": reader,
                    "limited": limited_uses,
                },
                authority="127.0.0.1:8765",
            ),
        )
        original = (owner.path / "workspace.toml").read_text()
        patch = {
            "base_workspace_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "changes": [{"path": "limits.limit", "action": "set", "value": 8}],
        }
        cases = [
            ("PUT", "/v1/settings", {"profile": "local", "workspace_toml": original}),
            ("PATCH", "/v1/settings", patch),
            (
                "POST",
                "/v1/jobs",
                {"operation": "setup", "assets": ["unknown"], "offline": True},
            ),
            (
                "POST",
                "/v1/jobs",
                {"operation": "export", "destination": str(tmp_path / "backup")},
            ),
            (
                "POST",
                "/v1/jobs",
                {
                    "operation": "import",
                    "archive_path": str(tmp_path / "backup"),
                    "destination": str(tmp_path / "restored"),
                },
            ),
            ("POST", "/v1/jobs", {"operation": "rebuild", "channels": ["text"]}),
            ("POST", "/v1/jobs", {"operation": "migrate", "target_version": "3"}),
        ]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            for token in ("narrow", "reader", "limited"):
                for method, url, body in cases:
                    response = await client.request(
                        method,
                        url,
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert response.status_code == 403, (token, body, response.text)
                    assert response.json()["code"] == "FORBIDDEN_SCOPE"
            for url in (
                "/v1/settings/fields",
                "/v1/models",
                "/v1/models/readiness?selection=output-tokenizer",
            ):
                assert (
                    await client.get(url, headers={"Authorization": "Bearer narrow"})
                ).status_code == 403
            expected = [
                (200, None),
                (200, None),
                (400, "INVALID_REQUEST"),
                (200, None),
                (200, None),
                (200, None),
                (409, "PROFILE_INCOMPATIBLE"),
            ]
            for (method, url, body), (status, code) in zip(
                cases, expected, strict=True
            ):
                # Refresh digest after any successful settings write.
                if method == "PATCH":
                    body["base_workspace_sha256"] = hashlib.sha256(
                        (owner.path / "workspace.toml").read_bytes()
                    ).hexdigest()
                response = await client.request(
                    method, url, json=body, headers={"Authorization": "Bearer full"}
                )
                assert response.status_code == status, (body, response.text)
                if code is not None:
                    assert response.json()["code"] == code
                elif method == "PUT":
                    assert response.json()["profile"] == "local"
                elif method == "PATCH":
                    assert response.json()["active_profile"] == "local"
                elif body["operation"] in {"export", "import"}:
                    assert response.json()["receipt"]["operation"] == body["operation"]
                elif body["operation"] == "rebuild":
                    assert (
                        response.json()["recovery"]["workspace_id"]
                        == owner.workspace_id
                    )
            selected = await client.put(
                "/v1/settings",
                json={"profile": "local", "workspace_toml": original},
                headers={
                    "Authorization": "Bearer full",
                    "X-Tychora-Scope": narrow.model_dump_json(),
                },
            )
            assert selected.status_code == 403
            health = await client.post(
                "/v1/jobs",
                json={"operation": "health"},
                headers={"Authorization": "Bearer reader"},
            )
            assert health.status_code == 200
            health = await client.post(
                "/v1/jobs",
                json={"operation": "health"},
                headers={"Authorization": "Bearer limited"},
            )
            assert health.status_code == 403
            health = await client.post(
                "/v1/jobs",
                json={"operation": "health", "resume_pending_deletions": True},
                headers={"Authorization": "Bearer reader"},
            )
            assert health.status_code == 403
        with pytest.raises(TychoraError, match="outside|whole-workspace"):
            await app.state.settings.activate(
                SettingsActivation(profile="local", workspace_toml=original), narrow
            )


@pytest.mark.asyncio
async def test_direct_settings_admission_and_scope_are_not_http_only(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        app = create_app(
            kb._workspace.owner,
            kb._workspace.services,
            LocalAuth({"full": kb.scope}, authority="127.0.0.1:8765"),
        )
        original = (kb._workspace.owner.path / "workspace.toml").read_text()
        request = SettingsActivation(profile="local", workspace_toml=original)
        with app.state.runtime.operation():
            with pytest.raises(TychoraError) as error:
                await app.state.settings.activate(request, kb.scope)
            assert error.value.code == "WORKSPACE_BUSY"
        assert (await app.state.settings.activate(request, kb.scope)).profile == "local"
