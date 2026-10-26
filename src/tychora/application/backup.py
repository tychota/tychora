"""Owner-only, typed canonical workspace backup."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from tychora.adapters.files import references
from tychora.adapters.files.archive import ArchiveWriter
from tychora.adapters.files.references import (
    MAX_MANIFEST_BYTES,
    BackupInventory,
    from_backup,
    required_refs,
)
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.owner import WorkspaceOwner
from tychora.application.removal import RemovalService
from tychora.configuration.resolve import exportable_resolved_config
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

# One backup manifest lists every required artifact in a single typed header,
# so its reference and byte limits cap what one export can hold.
MAX_BACKUP_MANIFEST_BYTES = MAX_MANIFEST_BYTES


def _manifest_limit(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="backup.manifest_limit")


@dataclass(frozen=True)
class BackupManifest:
    workspace_id: str
    artifacts: tuple[ArtifactRef, ...]
    payload_states: tuple[tuple[str, str], ...]
    policy_epoch: int
    lexical_statistics_epoch: int
    active_snapshot_id: str | None
    snapshot_ids: tuple[str, ...]
    tombstones: tuple[tuple[str, str], ...]
    config_shas: tuple[str, ...]
    resolved_config_sha256: str | None = None
    omitted_channels: tuple[str, ...] = ("text", "image", "graph")
    warning: str = "Present tombstoned bytes may be included at the backup cut."

    def as_header(self) -> dict:
        data = {
            "workspace_id": self.workspace_id,
            "payload_states": [list(row) for row in self.payload_states],
            "policy_epoch": self.policy_epoch,
            "lexical_statistics_epoch": self.lexical_statistics_epoch,
            "active_snapshot_id": self.active_snapshot_id,
            "snapshot_ids": list(self.snapshot_ids),
            "tombstones": [list(row) for row in self.tombstones],
            "config_shas": list(self.config_shas),
            "omitted_channels": list(self.omitted_channels),
            "warning": self.warning,
            "projection_rebuild_required": list(self.omitted_channels),
        }
        if self.resolved_config_sha256 is not None:
            data["resolved_config_sha256"] = self.resolved_config_sha256
        return {
            "type": "backup",
            "schema_version": 1,
            "required_artifacts": [ref.model_dump() for ref in self.artifacts],
            "data": data,
        }


@dataclass(frozen=True)
class BackupReceipt:
    manifest: BackupManifest
    sqlite_sha256: str
    manifest_sha256: str
    destination: Path


class BackupService:
    def __init__(self, owner: WorkspaceOwner) -> None:
        self.owner = owner

    def export(
        self,
        destination: Path,
        scope: RequestScope,
        *,
        mode: str = "canonical",
        ctx: ExecutionContext,
    ) -> BackupReceipt:
        if mode != "canonical":
            raise TychoraError(
                "CAPABILITY_MISSING", "Exact native replay backup is unsupported"
            )
        destination = destination.absolute()
        if destination.resolve(strict=False).is_relative_to(self.owner.path):
            raise TychoraError(
                "INVALID_REQUEST", "Backup destination must be outside the workspace"
            )
        return self.owner.run_write_sync(
            lambda: self._export_on_owner(destination, scope, ctx), ctx
        )

    def _authorize(self, scope: RequestScope) -> None:
        from .workspace_authority import authorize_workspace

        authorize_workspace(self.owner, scope)

    def _export_on_owner(
        self, destination: Path, scope: RequestScope, ctx: ExecutionContext
    ) -> BackupReceipt:
        self._authorize(scope)
        ctx.checkpoint()
        with ArchiveWriter(destination) as archive:
            copy_path = archive.stage / "corpus.sqlite3"
            with self.owner.maintenance_barrier():
                repository = JobRepository(
                    self.owner.db,
                    artifacts=self.owner.artifacts,
                    workspace_id=self.owner.workspace_id,
                )
                RemovalService(self.owner, repository).reconcile_pending_deletions()
                copied = sqlite3.connect(copy_path)
                try:
                    self.owner.db.backup(copied)
                    os.chmod(copy_path, 0o600, follow_symlinks=False)
                finally:
                    copied.close()
                verified = open_corpus(copy_path)
                try:
                    inventory = from_backup(verified, artifacts=self.owner.artifacts)
                finally:
                    verified.close()
                lease = self.owner.artifacts.pin_payloads(inventory.required_shas)
                lease.__enter__()
            try:
                return self._copy_under_lease(archive, copy_path, inventory, ctx)
            finally:
                lease.__exit__(None, None, None)

    def _copy_under_lease(
        self,
        archive: ArchiveWriter,
        copy_path: Path,
        inventory: BackupInventory,
        ctx: ExecutionContext,
    ) -> BackupReceipt:
        selected_config = {
            "type": "resolved-config",
            "schema_version": 1,
            "data": exportable_resolved_config(self.owner.config),
        }
        if len(canonical_bytes(selected_config)) > 16 * 1024 * 1024:
            raise TychoraError(
                "INTEGRITY_ERROR", "Selected configuration exceeds backup limit"
            )
        if len(inventory.artifacts) > references.MAX_REFERENCES:
            raise _manifest_limit("Backup inventory exceeds manifest reference limit")
        config_sha = archive.write_json("resolved-config.json", selected_config)
        for ref in inventory.artifacts:
            ctx.checkpoint()
            archive.copy_artifact(self.owner.artifacts, ref, ctx)
        with copy_path.open("rb") as stream:
            digest = sha256()
            while block := stream.read(1024 * 1024):
                digest.update(block)
            sqlite_sha = digest.hexdigest()
            os.fsync(stream.fileno())
        manifest = BackupManifest(
            workspace_id=inventory.workspace_id,
            artifacts=inventory.artifacts,
            payload_states=inventory.payload_states,
            policy_epoch=inventory.policy_epoch,
            lexical_statistics_epoch=inventory.lexical_statistics_epoch,
            active_snapshot_id=inventory.active_snapshot_id,
            snapshot_ids=inventory.snapshot_ids,
            tombstones=inventory.tombstones,
            config_shas=inventory.config_shas,
            resolved_config_sha256=config_sha,
        )
        header = manifest.as_header()
        if len(canonical_bytes(header)) > MAX_BACKUP_MANIFEST_BYTES:
            raise _manifest_limit("Backup manifest exceeds byte limit")
        required_refs(header, capture_published=False)
        manifest_sha = archive.write_json("backup-manifest.json", header)
        archive.write_json(
            "backup-receipt.json",
            {
                "type": "backup-receipt",
                "schema_version": 1,
                "workspace_id": inventory.workspace_id,
                "sqlite_sha256": sqlite_sha,
                "manifest_sha256": manifest_sha,
                "complete": True,
            },
        )
        ctx.checkpoint()
        archive.finish()
        return BackupReceipt(manifest, sqlite_sha, manifest_sha, archive.destination)
