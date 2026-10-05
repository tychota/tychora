"""Owner-coordinated integrity checks and explicit restored-deletion resume."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from tychora.adapters.files.references import from_backup
from tychora.adapters.sqlite.graph import read_graph_build
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.removal import RemovalService
from tychora.application.restore import RESTORE_SUSPENSION, deletion_suspended
from tychora.configuration.resolve import validate_retained_config
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, GraphManifest
from tychora.contracts.identity import canonical_bytes, digest, family_id, space_id
from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
from tychora.contracts.publication import IndexFamily, PublicationBatch
from tychora.contracts.requests import RequestScope
from tychora.contracts.vectors import BatchReceipt, VectorBatch
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext


@dataclass(frozen=True)
class RecoveryReport:
    workspace_id: str
    verified_payloads: int
    native_pending_batches: int
    interrupted_jobs: int
    deletion_suspended: bool


@dataclass(frozen=True)
class NativeGcReport:
    workspace_id: str
    pruned_batches: tuple[str, ...]
    retained_batches: int
    deletion_suspended: bool
    admission_busy: bool


def verified_vector_records(
    family: IndexFamily, batch: PublicationBatch, artifacts
) -> VectorBatch:
    """Rebuild only exact retained float32 bytes bound to the sealed vector receipt."""
    receipt = batch.receipt
    try:
        spec = FamilySpec.model_validate_json(
            canonical_bytes(family.config_json["spec"])
        )
    except (ValueError, KeyError, TypeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Vector family specification differs"
        ) from None
    if (
        family.channel not in {"text", "image"}
        or family.backend != "chroma"
        or spec.backend_space != "ip"
        or spec.normalization != "unit-l2"
        or spec.family_id != family.family_id
        or spec.family_id
        != family_id(spec.workspace_id, spec.config_sha, spec.creation_uuid)
        or spec.dimensions != family.dimension
        or spec.provider != family.provider
        or spec.model != family.model
        or spec.backend_namespace != family.backend_namespace
        or not isinstance(receipt, BatchReceipt)
        or family.config_json.get("namespace_hash") != receipt.namespace_hash
        or receipt.family_id != family.family_id
        or receipt.config_sha != family.config_sha
        or receipt.count != len(batch.vector_records)
        or batch.expected_count != receipt.count
    ):
        raise TychoraError("INTEGRITY_ERROR", "Vector family or receipt differs")
    descriptors = tuple(
        IndexRecordDigest(
            record_id=row.record_id, input_sha=row.input_sha, value_sha=row.value_sha
        )
        for row in sorted(batch.vector_records, key=lambda item: item.record_id)
    )
    if receipt.records != descriptors or receipt.digest != receipt_digest(descriptors):
        raise TychoraError("INTEGRITY_ERROR", "Vector record binding differs")
    expected_space = space_id(
        spec.modality,
        spec.provider,
        spec.model,
        spec.dimensions,
        spec.metric,
        spec.representation_fingerprint,
    )
    records = []
    for row in sorted(batch.vector_records, key=lambda item: item.record_id):
        if not artifacts.stat(row.embedding_cache).available:
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Retained vector cache is absent; create a new family and snapshot for authorized recomputation",
            )
        with artifacts.open_verified(row.embedding_cache) as stream:
            raw = stream.read(row.embedding_cache.size_bytes + 1)
        if (
            len(raw) != spec.dimensions * struct.calcsize("<f")
            or sha256(raw).hexdigest() != row.value_sha
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Vector cache differs from sealed float32 bytes"
            )
        try:
            records.append(
                EmbeddingRecord.from_normalized_bytes(
                    record_id=row.record_id,
                    input_sha=row.input_sha,
                    space_id=expected_space,
                    stored=raw,
                )
            )
        except ValueError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained vector is not normalized float32"
            ) from None
    return VectorBatch(family_id=family.family_id, records=tuple(records))


def verify_native_vector(
    family: IndexFamily,
    batch: PublicationBatch,
    artifacts,
    index,
    ctx: ExecutionContext,
) -> BatchReceipt:
    """Write exact retained bytes and require backend proof against the old seal."""
    receipt = batch.receipt
    if not isinstance(receipt, BatchReceipt):
        raise TychoraError("INTEGRITY_ERROR", "Vector receipt type differs")
    try:
        spec = FamilySpec.model_validate_json(
            canonical_bytes(family.config_json["spec"])
        )
    except (ValueError, KeyError, TypeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Vector family specification differs"
        ) from None
    handle = index.create_family(spec)
    if (
        handle.spec != spec
        or handle.namespace_hash != receipt.namespace_hash
        or handle.namespace_hash != family.config_json.get("namespace_hash")
        or handle.config_sha != family.config_sha
        or handle.config_sha != receipt.config_sha
    ):
        raise TychoraError("INTEGRITY_ERROR", "Native vector family identity differs")
    if batch.vector_records:
        records = verified_vector_records(family, batch, artifacts)
        if index.upsert_batch(records, ctx) != receipt:
            raise TychoraError("INTEGRITY_ERROR", "Native vector seal differs")
    elif (
        receipt.count != 0
        or receipt.digest != receipt_digest(())
        or batch.expected_count != 0
    ):
        raise TychoraError("INTEGRITY_ERROR", "Empty vector receipt differs")
    verification = index.verify_batch(receipt)
    if (
        not verification.ready
        or not verification.config_matches
        or verification.missing_record_ids
        or verification.mismatched_record_ids
    ):
        raise TychoraError("INTEGRITY_ERROR", "Native vector bytes differ")
    return receipt


class RecoveryService:
    def __init__(self, owner) -> None:
        self.owner = owner

    def reconcile(self, ctx: ExecutionContext) -> RecoveryReport:
        return self.owner.run_blocking_sync(lambda: self._reconcile_on_owner(ctx), ctx)

    def prune_native(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> NativeGcReport:
        """Explicit conservative GC; canonical receipts and CAS are never removed."""
        return self.owner.run_blocking_sync(
            lambda: self._prune_native_on_owner(scope, ctx), ctx
        )

    def _prune_native_on_owner(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> NativeGcReport:
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Native GC requires management grant")
        db = self.owner.db
        collections = {
            row[0] for row in db.execute("SELECT collection_id FROM collections")
        }
        if not collections.issubset(grant.collection_ids):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Native GC requires all collection grants"
            )
        with (
            self.owner.maintenance_barrier(),
            self.owner.leases.exclusive_if_idle() as no_leases,
        ):
            health = self._reconcile_on_owner(ctx)
            batches = tuple(
                row
                for row in db.execute(
                    "SELECT b.batch_id,b.family_id,f.channel,b.receipt_json "
                    "FROM index_batches b JOIN index_families f USING(family_id) "
                    "WHERE f.channel IN ('text','image','graph') AND b.state='ready' "
                    "ORDER BY b.batch_id"
                )
            )
            busy = (
                not no_leases
                or db.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('running','cancelling') LIMIT 1"
                ).fetchone()
                is not None
            )
            if health.deletion_suspended or busy:
                return NativeGcReport(
                    health.workspace_id,
                    (),
                    len(batches),
                    health.deletion_suspended,
                    busy,
                )
            active = self.owner.store.active_snapshot_id()
            protected: set[str] = set()
            if active is not None:
                protected.add(active)
                previous = db.execute(
                    "SELECT parent_snapshot_id FROM snapshots WHERE snapshot_id=?",
                    (active,),
                ).fetchone()
                if previous is None:
                    raise TychoraError("INTEGRITY_ERROR", "Active snapshot is missing")
                if previous[0] is not None:
                    protected.add(previous[0])
            protected_batches = {
                batch_id
                for snapshot_id, batch_id in db.execute(
                    "SELECT snapshot_id,batch_id FROM snapshot_batches"
                )
                if snapshot_id in protected
            }
            pruned: list[str] = []
            for batch_id, family_id, channel, receipt_json in batches:
                ctx.checkpoint()
                if batch_id in protected_batches:
                    continue
                if channel == "graph":
                    if receipt_json is None:
                        continue
                    receipt = GraphBatchReceipt.model_validate_json(receipt_json)
                    index = self.owner.store.graph_indexes.get(receipt.generation_id)
                    if index is None:
                        continue
                else:
                    index = self.owner.store.vector_indexes.get(family_id)
                    if index is None:
                        continue
                    record_ids = tuple(
                        row[0]
                        for row in db.execute(
                            "SELECT record_id FROM vector_records "
                            "WHERE batch_id=? ORDER BY record_id",
                            (batch_id,),
                        )
                    )
                # Make absence visible before touching a nontransactional native store.
                with self.owner.store.immediate_transaction():
                    db.execute(
                        "UPDATE index_batches SET state='pending' "
                        "WHERE batch_id=? AND state='ready'",
                        (batch_id,),
                    )
                if channel == "graph":
                    index.drop_generation(receipt.generation_id, ctx)
                elif record_ids:
                    index.drop_records(record_ids, ctx)
                pruned.append(batch_id)
            return NativeGcReport(
                health.workspace_id,
                tuple(pruned),
                len(batches) - len(pruned),
                False,
                False,
            )

    def rebuild_native(
        self,
        scope: RequestScope,
        ctx: ExecutionContext,
        *,
        channels: tuple[str, ...] = ("text", "image", "graph"),
        snapshot_id: str | None = None,
    ) -> RecoveryReport:
        """Rebuild retained native projections under the canonical owner."""
        return self.owner.run_blocking_sync(
            lambda: self._rebuild_native_on_owner(scope, ctx, channels, snapshot_id),
            ctx,
        )

    def _rebuild_native_on_owner(
        self,
        scope: RequestScope,
        ctx: ExecutionContext,
        channels: tuple[str, ...] = ("text", "image", "graph"),
        snapshot_id: str | None = None,
    ) -> RecoveryReport:
        if not channels or set(channels) - {"text", "image", "graph"}:
            raise TychoraError(
                "INVALID_REQUEST",
                "Native recovery channels must be text, image or graph",
            )
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Native recovery requires management grant"
            )
        self._reconcile_on_owner(ctx)
        store = self.owner.store
        active_snapshot_id = store.active_snapshot_id()
        if active_snapshot_id is None:
            return self._reconcile_on_owner(ctx)
        with store.pin_snapshot(snapshot_id) as snapshot:
            collection_ids = {
                row[0]
                for row in self.owner.db.execute(
                    "SELECT DISTINCT s.collection_id FROM snapshot_members m "
                    "JOIN sources s USING(source_id) WHERE m.snapshot_id=?",
                    (snapshot.snapshot_id,),
                )
            }
            if not collection_ids.issubset(grant.collection_ids):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Native recovery requires snapshot collection grants",
                )
            validate_retained_config(store.retained_config(snapshot))
            for slot in snapshot.slots:
                if slot.slot not in channels:
                    continue
                if (
                    slot.slot in {"text", "image"}
                    and slot.family.config_json.get("effective_config_sha")
                    != snapshot.config_sha
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR",
                        "Retained vector semantic configuration differs",
                    )
                for batch in slot.batches:
                    row = self.owner.db.execute(
                        "SELECT state FROM index_batches WHERE batch_id=? AND family_id=?",
                        (batch.batch_id, slot.family.family_id),
                    ).fetchone()
                    if row is None or row[0] not in {"pending", "ready"}:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Native batch state differs"
                        )
                    if row[0] == "ready":
                        continue
                    with self.owner.artifacts.open_verified(batch.input_manifest):
                        pass
                    if slot.slot in {"text", "image"}:
                        index = store.vector_indexes.get(slot.family.family_id)
                        if index is None:
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native vector index is unavailable",
                            )
                        receipt = verify_native_vector(
                            slot.family, batch, self.owner.artifacts, index, ctx
                        )
                        verified = index.verify_batch(receipt)
                        if (
                            not verified.ready
                            or not verified.config_matches
                            or verified.missing_record_ids
                            or verified.mismatched_record_ids
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Native vector proof differs"
                            )
                        physical = {
                            "backend": "chroma-ip",
                            "entries_digest": receipt.digest,
                            "value_digests": [
                                item.value_sha for item in receipt.records
                            ],
                        }
                    else:
                        receipt = batch.receipt
                        manifest = batch.graph_manifest
                        if (
                            not isinstance(receipt, GraphBatchReceipt)
                            or manifest is None
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Graph receipt type differs"
                            )
                        build = read_graph_build(store, manifest.spec, ctx)
                        if GraphManifest.from_build(build) != manifest:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Graph canonical input differs"
                            )
                        graph = store.graph_indexes.get(receipt.generation_id)
                        if graph is None or not hasattr(graph, "prepare_build"):
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native graph builder is unavailable",
                            )
                        graph.prepare_build(build)
                        if (
                            graph.begin_generation(manifest.spec)
                            != receipt.generation_id
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Graph generation identity differs"
                            )
                        graph.upsert_batch(
                            receipt.generation_id,
                            tuple(item.assertion for item in build.assertions),
                            tuple(item.unit for item in build.units),
                            ctx,
                        )
                        if (
                            graph.seal_generation(receipt.generation_id, manifest, ctx)
                            != receipt
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Native graph receipt differs"
                            )
                        physical = {
                            "backend": "ladybug",
                            "manifest_digest": receipt.manifest_digest,
                            "decision_watermark": receipt.decision_watermark,
                        }
                    self._write_physical_proof(
                        batch.batch_id,
                        {
                            "type": "physical-verification",
                            "schema_version": 1,
                            "workspace_id": self.owner.workspace_id,
                            "snapshot_id": snapshot.snapshot_id,
                            "family_id": slot.family.family_id,
                            "batch_id": batch.batch_id,
                            "input_manifest_sha256": batch.input_manifest.sha256,
                            "historical_receipt_digest": digest(
                                "historical-native-receipt-v1",
                                receipt.model_dump(mode="json"),
                            ),
                            "physical": physical,
                        },
                    )
                    ctx.checkpoint()
                    with store.immediate_transaction():
                        if store.active_snapshot_id() != active_snapshot_id:
                            raise TychoraError(
                                "WORKSPACE_BUSY",
                                "Active snapshot changed during native recovery",
                            )
                        if (
                            self.owner.db.execute(
                                "UPDATE index_batches SET state='ready' WHERE batch_id=? AND family_id=? AND state='pending'",
                                (batch.batch_id, slot.family.family_id),
                            ).rowcount
                            != 1
                        ):
                            raise TychoraError(
                                "WORKSPACE_BUSY", "Native batch changed during recovery"
                            )
        return self._reconcile_on_owner(ctx)

    def _write_physical_proof(self, batch_id: str, proof: dict) -> None:
        raw = canonical_bytes(proof)
        if len(raw) > 4096:
            raise TychoraError("INTEGRITY_ERROR", "Physical proof exceeds limit")
        directory = self.owner.path / ".physical-verification"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise TychoraError("INTEGRITY_ERROR", "Physical proof directory is unsafe")
        target = directory / f"{sha256(batch_id.encode()).hexdigest()}.json"
        temporary = directory / f".{uuid4()}.pending"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _reconcile_on_owner(self, ctx: ExecutionContext) -> RecoveryReport:
        ctx.checkpoint()
        db = self.owner.db
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise TychoraError(
                "INTEGRITY_ERROR", "Canonical database failed integrity check"
            )
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Canonical database has broken foreign keys"
            )
        inventory = from_backup(db, artifacts=self.owner.artifacts)
        if inventory.workspace_id != self.owner.workspace_id:
            raise TychoraError("INTEGRITY_ERROR", "Recovery workspace identity differs")
        pending = db.execute(
            "SELECT COUNT(*) FROM index_batches b JOIN index_families f USING(family_id) "
            "WHERE f.channel IN ('text','image','graph') AND b.state='pending'"
        ).fetchone()[0]
        interrupted = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE state='failed' AND error_code='JOB_INTERRUPTED'"
        ).fetchone()[0]
        suspended = deletion_suspended(self.owner.path, self.owner.workspace_id)
        if suspended != self.owner.restore_deletion_suspended:
            raise TychoraError(
                "INTEGRITY_ERROR", "Deletion suspension changed outside owner"
            )
        ctx.checkpoint()
        return RecoveryReport(
            workspace_id=self.owner.workspace_id,
            verified_payloads=len(inventory.artifacts),
            native_pending_batches=pending,
            interrupted_jobs=interrupted,
            deletion_suspended=suspended,
        )

    def resume_pending_deletions(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> RecoveryReport:
        return self.owner.run_blocking_sync(
            lambda: self._resume_on_owner(scope, ctx), ctx
        )

    def _resume_on_owner(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> RecoveryReport:
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Deletion resume requires management grant"
            )
        checked = self._reconcile_on_owner(ctx)
        if not checked.deletion_suspended:
            return checked
        marker = self.owner.path / RESTORE_SUSPENSION
        os.unlink(marker)
        directory = os.open(
            self.owner.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.owner.restore_deletion_suspended = False
        repository = JobRepository(
            self.owner.db,
            artifacts=self.owner.artifacts,
            workspace_id=self.owner.workspace_id,
        )
        RemovalService(self.owner, repository).reconcile_pending_deletions()
        return self._reconcile_on_owner(ctx)
