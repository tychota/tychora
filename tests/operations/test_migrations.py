"""Explicit v2 schema compatibility and unsupported migration targets."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from tychora.adapters.sqlite.migrations import supported_migration_target
from tychora.contracts.errors import TychoraError


def test_v2_is_the_only_packaged_migration_target():
    assert supported_migration_target(2) == 2
    for unsupported in (1, 3):
        with pytest.raises(TychoraError) as error:
            supported_migration_target(unsupported)
        assert error.value.code == "PROFILE_INCOMPATIBLE"


def test_ready_native_file_guard_precedes_constructor_and_skips_pending(tmp_path):
    from tychora.application.default_bindings import _missing_ready_native_storage

    class Db:
        ready = False

        def execute(self, _query, params):
            return SimpleNamespace(
                fetchone=lambda: (1,) if self.ready and params[1] == "text" else None
            )

    db = Db()
    owner = SimpleNamespace(
        path=tmp_path,
        db=db,
        store=SimpleNamespace(active_snapshot_id=lambda: "snapshot"),
    )
    assert _missing_ready_native_storage(cast(Any, owner)) == {}
    assert not (tmp_path / "indexes").exists()
    db.ready = True
    assert _missing_ready_native_storage(cast(Any, owner)) == {
        "text": "Ready text native storage is missing"
    }
    assert not (tmp_path / "indexes").exists()


def test_public_maintenance_health_and_unsupported_migration(tmp_path):
    from tychora import Tychora

    with Tychora.open(tmp_path / "workspace") as kb:
        health = kb.management.run({"operation": "health", "verify_payloads": True})
        assert health.recovery is not None
        assert health.recovery.deletion_suspended is False
        native = kb.management.run({"operation": "health", "verify_native": True})
        assert native.recovery is not None
        assert native.recovery.native_verified_records == 0
        assert native.recovery.native_verified_generations == 0
        assert native.recovery.native_verified_snapshot_id is None
        with pytest.raises(TychoraError) as error:
            kb.management.run({"operation": "migrate", "target_version": "3"})
        assert error.value.code == "PROFILE_INCOMPATIBLE"
        rebuilt = kb.management.run({"operation": "rebuild", "channels": ["text"]})
        assert rebuilt.recovery is not None
        assert rebuilt.recovery.native_pending_batches == 0


def test_internal_maintenance_health_and_unsupported_migration(tmp_path):
    from tychora import Tychora
    from tychora.contracts.operations import MaintenanceRequest
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace") as kb:
        ws = kb._workspace
        health = ws.bridge.call(
            ws.services.maintenance.run(
                MaintenanceRequest.from_mapping(
                    {"operation": "health", "verify_payloads": True}
                ),
                kb.scope,
                None,
                ExecutionContext(),
            )
        )
        assert health.recovery is not None
        assert health.recovery.deletion_suspended is False
        with pytest.raises(TychoraError) as error:
            ws.bridge.call(
                ws.services.maintenance.run(
                    MaintenanceRequest.from_mapping(
                        {"operation": "migrate", "target_version": "3"}
                    ),
                    kb.scope,
                    None,
                    ExecutionContext(),
                )
            )
        assert error.value.code == "PROFILE_INCOMPATIBLE"
        completed = ws.bridge.call(
            ws.services.maintenance.run(
                MaintenanceRequest.from_mapping(
                    {"operation": "migrate", "target_version": "2"}
                ),
                kb.scope,
                "same-version",
                ExecutionContext(),
            )
        )
        assert completed.job is not None
        assert completed.job.state == "succeeded"


@pytest.mark.asyncio
async def test_async_public_maintenance_health_and_rebuild(tmp_path):
    from tychora import AsyncTychora

    async with AsyncTychora.open(tmp_path / "workspace") as kb:
        health = await kb.management.run(
            {"operation": "health", "verify_payloads": True}
        )
        assert health.recovery is not None
        rebuilt = await kb.management.run(
            {"operation": "rebuild", "channels": ["text"]}
        )
        assert rebuilt.recovery is not None
        assert rebuilt.recovery.native_pending_batches == 0
