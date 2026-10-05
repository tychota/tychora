"""Shared public maintenance dispatch over existing owner handlers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from tychora.adapters.sqlite.migrations import (
    SCHEMA_VERSION,
    supported_migration_target,
)
from tychora.application.backup import BackupService
from tychora.application.recovery import RecoveryService
from tychora.application.restore import RestoreService
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    HealthMaintenance,
    ImportMaintenance,
    JobHandle,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceResult,
    MigrateMaintenance,
    OperationReceipt,
    PurgeOriginalsMaintenance,
    RebuildMaintenance,
    RecoveryStatus,
    RemoveMaintenance,
)
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


class MaintenanceService:
    def __init__(self, owner, repository) -> None:
        self.owner = owner
        self.repository = repository

    async def run(
        self,
        request: MaintenanceRequest,
        scope: RequestScope,
        key: str | None,
        ctx: ExecutionContext,
    ) -> MaintenanceResult:
        operation = request.root
        if operation.operation == "setup":
            from tychora.application.setup import run_public_setup

            return await run_public_setup(self.owner, self.repository, scope, operation)
        if isinstance(operation, (RemoveMaintenance, PurgeOriginalsMaintenance)):
            from tychora.application.removal import run_public_removal

            return await run_public_removal(
                self.owner, self.repository, scope, operation
            )
        if operation.operation == "provider-upload-consent":
            from tychora.application.consent import run_public_upload_consent

            return await run_public_upload_consent(self.owner, scope, operation)
        grant = self.owner.scope.narrow(scope)
        required = (
            "read"
            if isinstance(operation, HealthMaintenance)
            and not operation.resume_pending_deletions
            else "manage"
        )
        if required not in grant.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Maintenance is outside the grant")
        if isinstance(operation, HealthMaintenance):
            recovery = RecoveryService(self.owner)
            report = await self.owner.run_blocking(
                lambda: (
                    recovery._resume_on_owner(grant, ctx)
                    if operation.resume_pending_deletions
                    else recovery._reconcile_on_owner(ctx)
                ),
                ctx,
            )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="health",
                    target_ids=(report.workspace_id,),
                    actions=("verify canonical integrity and recovery state",),
                ),
                recovery=RecoveryStatus(**report.__dict__),
            )
        if isinstance(operation, RebuildMaintenance):
            if operation.revision_ids:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Selected revision rebuild is unavailable"
                )
            recovery = RecoveryService(self.owner)
            report = await self.owner.run_blocking(
                lambda: recovery._rebuild_native_on_owner(
                    grant, ctx, operation.channels
                ),
                ctx,
            )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="rebuild",
                    target_ids=(report.workspace_id,),
                    actions=(
                        "verify retained native inputs and recover selected channels",
                    ),
                ),
                recovery=RecoveryStatus(**report.__dict__),
            )
        if operation.operation == "export":
            if not operation.include_originals or operation.snapshot_ids:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Selected backup cuts are unsupported"
                )
            receipt = await asyncio.to_thread(
                BackupService(self.owner).export,
                Path(operation.destination),
                grant,
                ctx=ctx,
            )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="export",
                    target_ids=(receipt.manifest.workspace_id,),
                    actions=("copy verified canonical backup",),
                ),
                receipt=OperationReceipt(
                    operation="export",
                    receipt_id=receipt.manifest_sha256,
                    target_ids=(receipt.manifest.workspace_id,),
                    completed_at=datetime.now(UTC),
                    manifest_sha=receipt.manifest_sha256,
                ),
            )
        if isinstance(operation, ImportMaintenance):
            if operation.destination is None:
                raise TychoraError(
                    "INVALID_REQUEST", "Import requires a clean destination"
                )
            receipt = await asyncio.to_thread(
                RestoreService(config=self.owner.config).import_backup,
                Path(operation.archive_path),
                Path(operation.destination),
                grant,
                ctx,
            )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="import",
                    target_ids=(receipt.workspace_id,),
                    actions=("activate verified canonical restore",),
                ),
                receipt=OperationReceipt(
                    operation="import",
                    receipt_id=receipt.backup_manifest_sha256,
                    target_ids=(receipt.workspace_id,),
                    completed_at=datetime.now(UTC),
                    manifest_sha=receipt.backup_manifest_sha256,
                ),
            )
        if isinstance(operation, MigrateMaintenance):
            try:
                target = int(operation.target_version)
            except ValueError:
                raise TychoraError(
                    "INVALID_REQUEST", "Migration target must be an integer"
                ) from None
            handle = await self.owner.run_blocking(
                lambda: self._migrate_on_owner(target, grant, key, ctx), ctx
            )
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="migrate",
                    target_ids=(self.owner.workspace_id,),
                    actions=("verify packaged schema version",),
                ),
                job=handle,
            )
        raise TychoraError("CAPABILITY_MISSING", "Maintenance operation is unavailable")

    def migrate(self, target_version: int, scope: RequestScope, ctx: ExecutionContext):
        return self.owner.run_blocking_sync(
            lambda: self._migrate_on_owner(target_version, scope, None, ctx), ctx
        )

    def _migrate_on_owner(
        self, target: int, scope: RequestScope, key: str | None, ctx: ExecutionContext
    ):
        supported_migration_target(target)
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Migration requires management grant")
        checked = RecoveryService(self.owner)._reconcile_on_owner(ctx)
        if checked.workspace_id != self.owner.workspace_id or SCHEMA_VERSION != target:
            raise TychoraError("PROFILE_INCOMPATIBLE", "Migration preflight differs")
        config = self.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "registry",
                        "schema_version": "1",
                        "data": self.owner.config.model_dump(mode="json"),
                    }
                )
            ),
            "application/json",
        )
        handle = self.repository.accept(
            "migrate", grant, key, {"target_version": target}, config
        )
        if handle.state == "queued":
            self.repository.start(handle.id)
            self.repository.complete(handle.id)
        status = self.repository.get(handle.id)
        return JobHandle(id=status.id, operation="migrate", state=status.state)
