"""Explicit v1 schema compatibility and unsupported migration targets."""

import pytest

from tychora.adapters.sqlite.migrations import supported_migration_target
from tychora.contracts.errors import TychoraError


def test_v1_is_the_only_packaged_migration_target():
    assert supported_migration_target(1) == 1
    with pytest.raises(TychoraError) as error:
        supported_migration_target(2)
    assert error.value.code == "PROFILE_INCOMPATIBLE"


def test_public_maintenance_health_and_unsupported_migration(tmp_path):
    from tychora import Tychora

    with Tychora.open(tmp_path / "workspace") as kb:
        health = kb.management.run({"operation": "health", "verify_payloads": True})
        assert health.recovery is not None
        assert health.recovery.deletion_suspended is False
        with pytest.raises(TychoraError) as error:
            kb.management.run({"operation": "migrate", "target_version": "2"})
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
                        {"operation": "migrate", "target_version": "2"}
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
                    {"operation": "migrate", "target_version": "1"}
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
