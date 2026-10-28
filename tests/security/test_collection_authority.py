"""Explicit trusted composition, immutable static grants, and policy preservation."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.security.test_upload_consent import _config, _dispatch, _request
from tychora import ProviderBindings, Tychora
from tychora.contracts.collections import CollectionEdit
from tychora.contracts.errors import TychoraError
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


def test_invalid_and_duplicate_managed_binding_rejected(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=OutputCounter()) as kb:
        owner = kb._workspace.owner
        services = kb._workspace.services
        partial = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
        with pytest.raises(TychoraError):
            create_app(
                owner,
                services,
                LocalAuth(
                    {"x": partial}, authority="127.0.0.1:8765", managed_owner_token="x"
                ),
            )
        auth = LocalAuth(
            {"x": kb.scope}, authority="127.0.0.1:8765", managed_owner_token="x"
        )
        create_app(owner, services, auth)
        with pytest.raises(TychoraError):
            create_app(owner, services, auth)
        with pytest.raises(TychoraError):
            create_app(
                owner,
                services,
                LocalAuth(
                    {"y": kb.scope}, authority="127.0.0.1:8765", managed_owner_token="y"
                ),
            )


@pytest.mark.asyncio
async def test_consent_preserves_lifecycle_default_and_refreshes_only_managed_slot(
    tmp_path,
):
    with Tychora.open(
        tmp_path / "workspace",
        config=_config(tmp_path),
        providers=ProviderBindings(),
        token_counter=OutputCounter(),
    ) as kb:
        owner = kb._workspace.owner
        original = kb.scope
        auth = LocalAuth(
            {"managed": original, "static": original},
            authority="127.0.0.1:8765",
            managed_owner_token="managed",
        )
        create_app(owner, kb._workspace.services, auth)
        service = kb._workspace.services.collections
        cid = str(kb.default_collection_id)
        await service.edit(
            cid,
            CollectionEdit(title="Library", slug="default", default_kind_id="manual@1"),
            kb.scope,
            ExecutionContext(),
        )
        archived = await service.set_archived(cid, True, kb.scope, ExecutionContext())
        result = kb.management.run(_request("grant"))
        assert result.upload_consent is not None
        assert (
            "voyage"
            in auth.authenticate("Bearer managed", owner.scope).upload_providers
        )
        assert not auth.authenticate("Bearer static", owner.scope).upload_providers
        retained = await service.get(cid, kb.scope, ExecutionContext())
        assert (
            retained.archived_at == archived.archived_at
            and retained.default_kind_id == "manual@1"
        )
        with pytest.raises(TychoraError):
            _dispatch(kb, kb.default_collection_id)
        same = kb.management.run(_request("grant"))
        assert same.upload_consent.policy_epoch == result.upload_consent.policy_epoch
        kb.management.run(_request("revoke"))
        assert not auth.authenticate("Bearer managed", owner.scope).upload_providers
        assert original.upload_providers == frozenset()
