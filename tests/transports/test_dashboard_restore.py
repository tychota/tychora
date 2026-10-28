"""Restore failures remain typed and cannot reveal path state before authorization."""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from tests.transports.test_http import Counter
from tychora import Tychora
from tychora.contracts.collections import CollectionCreate
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_missing_restore_archive_is_typed_and_authorized_before_path_lookup(
    tmp_path,
):
    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        narrow = kb.scope
        await kb._workspace.services.collections.create(
            CollectionCreate(title="Second", slug="second"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
        owner = kb._workspace.owner
        original = (owner.path / "workspace.toml").read_bytes()

        async def identity():
            return await owner.run_write(
                lambda: (
                    owner.policy_epoch(),
                    owner.store.active_snapshot_id(),
                ),
                ExecutionContext(),
            )

        epoch, snapshot = await identity()
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"full": kb.scope, "narrow": narrow},
                authority="127.0.0.1:8765",
            ),
        )
        destination = tmp_path / "restored"
        payload = {
            "operation": "import",
            "archive_path": str(tmp_path / "absent"),
            "destination": str(destination),
        }
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1:8765",
        ) as client:
            denied = await client.post(
                "/v1/jobs", json=payload, headers={"Authorization": "Bearer narrow"}
            )
            assert denied.status_code == 403
            missing = await client.post(
                "/v1/jobs", json=payload, headers={"Authorization": "Bearer full"}
            )
            assert missing.status_code == 404, missing.text
            assert missing.json()["code"] == "NOT_FOUND"
            assert str(tmp_path) not in missing.text
        assert not destination.exists()
        assert not list(tmp_path.glob(".*restore*"))
        assert (
            hashlib.sha256((owner.path / "workspace.toml").read_bytes()).digest()
            == hashlib.sha256(original).digest()
        )
        assert await identity() == (epoch, snapshot)
