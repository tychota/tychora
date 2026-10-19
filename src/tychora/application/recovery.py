"""Owner-coordinated integrity checks and explicit restored-deletion resume."""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from tychora.adapters.files.references import from_backup
from tychora.adapters.sqlite.graph import read_graph_build
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.vector_owners import vector_owner_rows
from tychora.application.removal import RemovalService
from tychora.application.restore import RESTORE_SUSPENSION, deletion_suspended
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.resolve import validate_retained_config
from tychora.contracts.embeddings import EmbeddingCapabilities, EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, GraphManifest
from tychora.contracts.identity import canonical_bytes, digest, family_id, space_id
from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
from tychora.contracts.publication import IndexFamily, PublicationBatch
from tychora.contracts.requests import RequestScope
from tychora.contracts.vector_batches import decode_vector_batch, row_value_sha
from tychora.contracts.vectors import BatchReceipt, VectorBatch, VectorOwner
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext


@dataclass(frozen=True)
class RecoveryReport:
    workspace_id: str
    verified_payloads: int
    native_pending_batches: int
    interrupted_jobs: int
    deletion_suspended: bool
    native_verified_snapshot_id: str | None = None
    native_verified_records: int = 0
    native_verified_generations: int = 0
    native_skipped_pending_batches: int = 0


@dataclass(frozen=True)
class NativeGcReport:
    workspace_id: str
    pruned_batches: tuple[str, ...]
    retained_batches: int
    deletion_suspended: bool
    admission_busy: bool


def verify_semantic_vector_family(
    family: IndexFamily, config: ResolvedConfig, workspace_id: str
) -> None:
    """Recompute the original family digest from retained typed facts only."""
    channel = family.channel
    if channel not in {"text", "image"}:
        raise TychoraError("INTEGRITY_ERROR", "Vector channel differs")
    raw = family.config_json.get("capabilities")
    if raw is None:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Retained embedding capability proof is absent; create a new family and snapshot",
        )
    try:
        capabilities = EmbeddingCapabilities.model_validate_json(canonical_bytes(raw))
        spec = FamilySpec.model_validate_json(
            canonical_bytes(family.config_json["spec"])
        )
        retained_modes = json.loads(canonical_bytes(raw))["input_types"]
        if not isinstance(retained_modes, list) or sorted(retained_modes) != [
            "document",
            "query",
        ]:
            raise ValueError("Retained input modes differ")
    except (ValueError, TypeError, KeyError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained vector semantic proof differs"
        ) from None
    origin_sha = family.config_json.get("effective_config_sha")
    settings = getattr(config.values.providers, channel)
    expected = digest(
        f"{channel}-family-config-v1",
        origin_sha,
        settings.model_dump(mode="json"),
        raw,
    )
    if (
        not isinstance(origin_sha, str)
        or spec.workspace_id != workspace_id
        or spec.config_sha != expected
        or spec.family_id != family_id(workspace_id, expected, spec.creation_uuid)
        or spec.family_id != family.family_id
        or spec.modality != ("text" if channel == "text" else "visual")
        or spec.provider != capabilities.provider
        or spec.model != capabilities.model
        or spec.dimensions != capabilities.dimensions
        or spec.representation_fingerprint != capabilities.representation_fingerprint
        or spec.metric != capabilities.metric
        or capabilities.normalization != "l2"
    ):
        raise TychoraError("INTEGRITY_ERROR", "Retained vector semantic proof differs")


def verify_canonical_native_batch(
    store, family: IndexFamily, batch: PublicationBatch
) -> None:
    """Compare the pinned publication plan to every mutable SQL/native input row."""
    db = store.db
    family_row = db.execute(
        "SELECT family_id,channel,backend,provider,model,dimension,config_sha,"
        "config_json,backend_namespace,created_at FROM index_families WHERE family_id=?",
        (family.family_id,),
    ).fetchone()
    expected_family = (
        family.family_id,
        family.channel,
        family.backend,
        family.provider,
        family.model,
        family.dimension,
        family.config_sha,
        canonical_bytes(family.config_json).decode(),
        family.backend_namespace,
        family.created_at,
    )
    if family_row != expected_family:
        raise TychoraError(
            "INTEGRITY_ERROR", "Native family SQL differs from sealed plan"
        )
    entries = (
        tuple(row.record_id for row in batch.vector_records)
        if family.channel in {"text", "image"}
        else tuple(batch.graph_manifest.evidence_ids)
        if batch.graph_manifest is not None
        else ()
    )
    count = (
        batch.receipt.count
        if isinstance(batch.receipt, BatchReceipt)
        else batch.receipt.evidence_count
        if isinstance(batch.receipt, GraphBatchReceipt)
        else -1
    )
    batch_row = db.execute(
        "SELECT family_id,extraction_id,input_manifest_sha,expected_count,actual_count,"
        "entry_ids_sha,receipt_json,vectors_sha FROM index_batches WHERE batch_id=?",
        (batch.batch_id,),
    ).fetchone()
    expected_batch = (
        family.family_id,
        batch.extraction_id,
        batch.input_manifest.sha256,
        batch.expected_count,
        count,
        digest("index-entries-v1", entries),
        canonical_bytes(batch.receipt.model_dump(mode="json")).decode(),
        batch.vectors.sha256 if batch.vectors else None,
    )
    if (
        batch.family_id != family.family_id
        or batch.batch_id
        != digest(
            "index-batch-v1",
            family.family_id,
            batch.extraction_id,
            batch.input_manifest.sha256,
        )
        or batch_row != expected_batch
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Native batch SQL differs from sealed plan"
        )
    artifact_row = db.execute(
        "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
        (batch.input_manifest.sha256,),
    ).fetchone()
    if artifact_row != (
        batch.input_manifest.size_bytes,
        batch.input_manifest.media_type,
        batch.input_manifest.storage_key,
        "present",
    ):
        raise TychoraError("INTEGRITY_ERROR", "Native input artifact SQL differs")
    with store.artifacts.open_verified(batch.input_manifest) as stream:
        raw = stream.read(batch.input_manifest.size_bytes + 1)
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Native input manifest is malformed"
        ) from None
    expected_data = {"entries": list(entries)}
    if family.channel == "text":
        expected_data["representations"] = [
            {
                "record_id": row.record_id,
                "group_id": row.group_id,
                "dependency_ids": sorted(row.dependency_ids),
            }
            for row in batch.vector_records
        ]
    if (
        canonical_bytes(document) != raw
        or document.get("type") != "index_input"
        or document.get("schema_version") != "1"
        or document.get("data") != expected_data
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Native input manifest differs from sealed plan"
        )
    if family.channel in {"text", "image"}:
        _verify_native_vector_sql(db, batch)


def _verify_native_vector_sql(db, batch: PublicationBatch) -> None:
    """Check the batch's vector rows and its batch artifact against the sealed plan."""
    rows = db.execute(
        "SELECT record_id,batch_id,chunk_id,asset_id,input_sha,row_index "
        "FROM vector_records WHERE batch_id=? ORDER BY record_id",
        (batch.batch_id,),
    ).fetchall()
    expected_rows = sorted(
        (
            row.record_id,
            batch.batch_id,
            row.chunk_id,
            row.asset_id,
            row.input_sha,
            row.row_index,
        )
        for row in batch.vector_records
    )
    if rows != expected_rows:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Native vector SQL differs from sealed plan",
            reason="canonical.binding_mismatch",
        )
    vectors = batch.vectors
    artifact_row = db.execute(
        "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts "
        "WHERE artifact_sha=?",
        (vectors.sha256 if vectors else None,),
    ).fetchone()
    if vectors is not None and artifact_row != (
        vectors.size_bytes,
        vectors.media_type,
        vectors.storage_key,
        "present",
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Native vector batch artifact SQL differs",
            reason="canonical.binding_mismatch",
        )


def _vector_family_spec(family: IndexFamily, batch: PublicationBatch) -> FamilySpec:
    """Return the family's specification once family, receipt and batch agree."""
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
    return spec


def _receipt_records(batch: PublicationBatch) -> tuple[IndexRecordDigest, ...]:
    """The receipt's record digests; empty unless the receipt is a vector receipt."""
    receipt = batch.receipt
    return receipt.records if isinstance(receipt, BatchReceipt) else ()


def _row_digests(rows) -> tuple[IndexRecordDigest, ...]:
    """Digest each decoded row as a receipt entry."""
    return tuple(
        IndexRecordDigest(
            record_id=row.record_id,
            input_sha=row.input_sha,
            value_sha=row_value_sha(row.values),
        )
        for row in rows
    )


def _bindings_match_receipt(batch: PublicationBatch) -> bool:
    """True when the sealed receipt describes exactly the batch's vector bindings."""
    receipt = batch.receipt
    descriptors = tuple(
        IndexRecordDigest(
            record_id=row.record_id, input_sha=row.input_sha, value_sha=row.value_sha
        )
        for row in sorted(batch.vector_records, key=lambda item: item.record_id)
    )
    return (
        isinstance(receipt, BatchReceipt)
        and receipt.records == descriptors
        and receipt.digest == receipt_digest(descriptors)
    )


def _vector_owners(owner_rows: tuple[tuple, ...]) -> tuple[VectorOwner, ...]:
    """Owners of the records described by (record, revision, extraction, unit, asset) rows."""
    return tuple(
        VectorOwner(
            record_id=record_id,
            revision_id=revision_id,
            extraction_id=extraction_id,
            owner_kind="unit" if unit_id else "asset",
            owner_id=unit_id or asset_id,
        )
        for record_id, revision_id, extraction_id, unit_id, asset_id in owner_rows
    )


def vector_batch_records(
    family: IndexFamily,
    batch: PublicationBatch,
    artifacts,
    *,
    owner_rows: tuple[tuple, ...] = (),
) -> VectorBatch:
    """Rebuild the exact retained float32 rows bound to the sealed receipt.

    ``owner_rows`` restores the owner metadata that native backends keep next to
    each vector; without it the batch carries no owners.
    """
    spec = _vector_family_spec(family, batch)
    vectors = batch.vectors
    if not _bindings_match_receipt(batch):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Vector record binding differs",
            reason="canonical.binding_mismatch",
        )
    if vectors is None or not artifacts.stat(vectors).available:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Retained vector batch is absent; rebuild the index to recompute it",
            reason="vectors.cache_absent",
        )
    with artifacts.open_verified(vectors) as stream:
        decoded = decode_vector_batch(stream.read(vectors.size_bytes + 1))
    if (
        decoded.family_id != family.family_id
        or decoded.dimension != spec.dimensions
        or _row_digests(decoded.rows) != _receipt_records(batch)
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Vector batch artifact differs from its receipt",
            reason="canonical.binding_mismatch",
        )
    expected_space = space_id(
        spec.modality,
        spec.provider,
        spec.model,
        spec.dimensions,
        spec.metric,
        spec.representation_fingerprint,
    )
    try:
        records = tuple(
            EmbeddingRecord.from_normalized_bytes(
                record_id=row.record_id,
                input_sha=row.input_sha,
                space_id=expected_space,
                stored=struct.pack(f"<{len(row.values)}f", *row.values),
            )
            for row in decoded.rows
        )
    except ValueError:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Retained vector is not normalized float32",
            reason="canonical.row_invalid",
        ) from None
    return VectorBatch(
        family_id=family.family_id, records=records, owners=_vector_owners(owner_rows)
    )


def verify_native_vector(
    family: IndexFamily,
    batch: PublicationBatch,
    records: Callable[[], VectorBatch],
    index,
    ctx: ExecutionContext,
) -> BatchReceipt:
    """Write the rows ``records`` loads and require backend proof against the old seal."""
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
        if index.upsert_batch(records(), ctx) != receipt:
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

    def reconcile(
        self, ctx: ExecutionContext, *, verify_payloads: bool = False
    ) -> RecoveryReport:
        return self.owner.run_blocking_sync(
            lambda: self._reconcile_on_owner(ctx, verify_payloads=verify_payloads), ctx
        )

    def prune_native(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> NativeGcReport:
        """Explicit conservative GC; canonical receipts and CAS are never removed."""
        return self.owner.run_blocking_sync(
            lambda: self._prune_native_on_owner(scope, ctx), ctx
        )

    def _prune_native_on_owner(
        self, scope: RequestScope, ctx: ExecutionContext, *, admitted: bool = False
    ) -> NativeGcReport:
        if not admitted:
            with self.owner.comparison_admission.mutation_nowait():
                return self._prune_native_on_owner(scope, ctx, admitted=True)
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

    def _reverify_vector(
        self, family: IndexFamily, batch: PublicationBatch, ctx: ExecutionContext
    ) -> tuple[BatchReceipt, dict]:
        """Rewrite one pending vector batch natively; return its receipt and proof."""
        index = self.owner.store.vector_indexes.get(family.family_id)
        if index is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Native vector index is unavailable"
            )
        receipt = verify_native_vector(
            family,
            batch,
            lambda: vector_batch_records(
                family,
                batch,
                self.owner.artifacts,
                owner_rows=vector_owner_rows(self.owner.db, batch.batch_id),
            ),
            index,
            ctx,
        )
        verified = index.verify_batch(receipt)
        if (
            not verified.ready
            or not verified.config_matches
            or verified.missing_record_ids
            or verified.mismatched_record_ids
        ):
            raise TychoraError("INTEGRITY_ERROR", "Native vector proof differs")
        physical = {
            "backend": "chroma-ip",
            "entries_digest": receipt.digest,
            "value_digests": [item.value_sha for item in receipt.records],
        }
        return receipt, physical

    def _rebuild_native_on_owner(
        self,
        scope: RequestScope,
        ctx: ExecutionContext,
        channels: tuple[str, ...] = ("text", "image", "graph"),
        snapshot_id: str | None = None,
        *,
        admitted: bool = False,
    ) -> RecoveryReport:
        if not admitted:
            with self.owner.comparison_admission.mutation_nowait():
                return self._rebuild_native_on_owner(
                    scope, ctx, channels, snapshot_id, admitted=True
                )
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
            snapshot_config = validate_retained_config(store.retained_config(snapshot))
            for slot in snapshot.slots:
                if slot.slot not in channels:
                    continue
                if slot.slot in {"text", "image"}:
                    origin_sha = slot.family.config_json.get("effective_config_sha")
                    if not isinstance(origin_sha, str):
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Retained vector config binding differs"
                        )
                    origin_config = validate_retained_config(
                        store.retained_config_sha(origin_sha)
                    )
                    if not pinned_runtime_compatible(origin_config, snapshot_config):
                        raise TychoraError(
                            "PROFILE_INCOMPATIBLE", "Retained vector profile differs"
                        )
                    verify_semantic_vector_family(
                        slot.family, origin_config, snapshot.workspace_id
                    )
                for batch in slot.batches:
                    verify_canonical_native_batch(store, slot.family, batch)
            for slot in snapshot.slots:
                if slot.slot not in channels:
                    continue
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
                    if slot.slot in {"text", "image"}:
                        receipt, physical = self._reverify_vector(
                            slot.family, batch, ctx
                        )
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
                        if graph.sealed_receipt(receipt.generation_id) is None:
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

    def _reconcile_on_owner(
        self,
        ctx: ExecutionContext,
        *,
        verify_payloads: bool = False,
        verify_native: bool = False,
    ) -> RecoveryReport:
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
        if verify_payloads:
            total = 0
            with self.owner.maintenance_barrier():
                for ref in inventory.artifacts:
                    ctx.checkpoint()
                    total += ref.size_bytes
                    if total > 20 * 1024**3:
                        raise TychoraError(
                            "CAPABILITY_MISSING",
                            "Health payload scan exceeds 20 GiB budget",
                        )
                    with self.owner.artifacts.open_verified(ref):
                        pass
        native_snapshot_id = None
        native_records = native_generations = native_skipped = 0
        if verify_native:
            (
                native_snapshot_id,
                native_records,
                native_generations,
                native_skipped,
            ) = self._verify_native_on_owner(ctx)
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
            native_verified_snapshot_id=native_snapshot_id,
            native_verified_records=native_records,
            native_verified_generations=native_generations,
            native_skipped_pending_batches=native_skipped,
        )

    def _verify_native_on_owner(
        self, ctx: ExecutionContext
    ) -> tuple[str | None, int, int, int]:
        """Read bounded ready projections in the pinned active snapshot only."""
        store = self.owner.store
        if store.active_snapshot_id() is None:
            return None, 0, 0, 0
        records = generations = skipped = batches = 0
        with store.pin_snapshot(None) as snapshot:
            for slot in snapshot.slots:
                if slot.slot not in {"text", "image", "graph"}:
                    continue
                for batch in slot.batches:
                    ctx.checkpoint()
                    batches += 1
                    if batches > 128:
                        raise TychoraError(
                            "CAPABILITY_MISSING",
                            "Native health exceeds 128 batch budget",
                        )
                    row = self.owner.db.execute(
                        "SELECT state FROM index_batches WHERE batch_id=? AND family_id=?",
                        (batch.batch_id, slot.family.family_id),
                    ).fetchone()
                    if row is None or row[0] not in {"pending", "ready"}:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Native batch state differs"
                        )
                    if row[0] == "pending":
                        skipped += 1
                        continue
                    unavailable = getattr(self.owner, "_native_unavailable", {})
                    if slot.slot in unavailable:
                        raise TychoraError("INTEGRITY_ERROR", unavailable[slot.slot])
                    verify_canonical_native_batch(store, slot.family, batch)
                    if slot.slot in {"text", "image"}:
                        receipt = batch.receipt
                        if not isinstance(receipt, BatchReceipt):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Vector receipt type differs"
                            )
                        records += receipt.count
                        if records > 8192 or receipt.count > 2048:
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native health exceeds vector record budget",
                            )
                        index = store.vector_indexes.get(slot.family.family_id)
                        if index is None:
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native vector index is unavailable",
                            )
                        checked = index.verify_batch(receipt)
                        if (
                            not checked.ready
                            or not checked.config_matches
                            or checked.missing_record_ids
                            or checked.mismatched_record_ids
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Ready native vector bytes differ"
                            )
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
                        if (
                            len(manifest.evidence_ids) > 20_000
                            or len(manifest.assertion_ids) > 20_000
                        ):
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native health exceeds graph member budget",
                            )
                        graph = store.graph_indexes.get(receipt.generation_id)
                        if graph is None or not hasattr(graph, "prepare_build"):
                            raise TychoraError(
                                "CAPABILITY_MISSING",
                                "Native graph index is unavailable",
                            )
                        if graph.sealed_receipt(receipt.generation_id) != receipt:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Ready graph seal differs"
                            )
                        build = read_graph_build(store, manifest.spec, ctx)
                        if GraphManifest.from_build(build) != manifest:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Graph canonical input differs"
                            )
                        graph.prepare_build(build)
                        if (
                            graph.seal_generation(receipt.generation_id, manifest, ctx)
                            != receipt
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Ready graph members differ"
                            )
                        generations += 1
        return snapshot.snapshot_id, records, generations, skipped

    def resume_pending_deletions(
        self, scope: RequestScope, ctx: ExecutionContext
    ) -> RecoveryReport:
        return self.owner.run_blocking_sync(
            lambda: self._resume_on_owner(scope, ctx), ctx
        )

    def _resume_on_owner(
        self,
        scope: RequestScope,
        ctx: ExecutionContext,
        *,
        verify_payloads: bool = False,
        verify_native: bool = False,
    ) -> RecoveryReport:
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Deletion resume requires management grant"
            )
        checked = self._reconcile_on_owner(
            ctx, verify_payloads=verify_payloads, verify_native=verify_native
        )
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
        return self._reconcile_on_owner(ctx, verify_native=verify_native)
