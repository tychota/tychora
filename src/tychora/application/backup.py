"""Owner-only, typed canonical workspace backup."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from tychora.adapters.files.archive import ArchiveWriter
from tychora.adapters.files.references import (
    BackupInventory,
    from_backup,
    required_refs,
)
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.owner import WorkspaceOwner
from tychora.application.removal import RemovalService
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


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
    omitted_channels: tuple[str, ...] = ("text", "image", "graph")
    warning: str = "Present tombstoned bytes may be included at the backup cut."

    def as_header(self) -> dict:
        return {
            "type": "backup",
            "schema_version": 1,
            "required_artifacts": [ref.model_dump() for ref in self.artifacts],
            "data": {
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
            },
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
        return self.owner.run_blocking_sync(
            lambda: self._export_on_owner(destination, scope, ctx), ctx
        )

    def _authorize(self, scope: RequestScope) -> None:
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Workspace backup requires management grant"
            )
        collections = self.owner.db.execute(
            "SELECT collection_id,access_floor FROM collections WHERE workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchall()
        source_floor = self.owner.db.execute(
            "SELECT MAX(s.access_floor) FROM sources s JOIN collections c USING(collection_id) "
            "WHERE c.workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchone()[0]
        revision_rank = self.owner.db.execute(
            "SELECT MAX(r.access_rank) FROM revisions r JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchone()[0]
        if (
            grant.access_ceiling != self.owner.scope.access_ceiling
            or grant.uses != self.owner.scope.uses
            or any(
                collection_id not in grant.collection_ids
                or floor > grant.access_ceiling
                for collection_id, floor in collections
            )
            or (source_floor is not None and source_floor > grant.access_ceiling)
            or (revision_rank is not None and revision_rank > grant.access_ceiling)
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Backup requires whole-workspace authority"
            )

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
        )
        header = manifest.as_header()
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
