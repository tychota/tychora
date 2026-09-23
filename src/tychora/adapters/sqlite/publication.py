"""Verified immutable batch bindings and one SQLite activation switch."""

from __future__ import annotations

import json
import sqlite3
import struct
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, NoReturn

from tychora.adapters.files.references import required_closure
from tychora.contracts.artifacts import ArtifactRef, ManifestRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.jobs import PublicationReceipt
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    PublicationPlan,
    SnapshotView,
)
from tychora.contracts.vectors import BatchReceipt
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.adapters.sqlite.corpus import CorpusStore


def _fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(store: CorpusStore, ref: ArtifactRef) -> dict:
    with store.artifacts.open_verified(ref) as stream:
        raw = stream.read(ref.size_bytes + 1)
    if len(raw) != ref.size_bytes:
        _fail("Publication artifact size differs")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _fail("Publication manifest is malformed")
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        _fail("Publication manifest is not canonical JSON")
    return value


def _manifest_document(plan: PublicationPlan) -> dict:
    inputs = {
        batch.input_manifest.sha256: batch.input_manifest
        for slot in plan.slots
        for batch in slot.batches
    }
    caches = {
        row.embedding_cache.sha256: row.embedding_cache
        for slot in plan.slots
        for batch in slot.batches
        for row in batch.vector_records
    }
    return {
        "type": "snapshot",
        "schema_version": "1",
        "required_artifacts": [
            ref.model_dump(mode="json")
            for ref in sorted(caches.values(), key=lambda ref: ref.sha256)
        ],
        "required_manifests": [
            ManifestRef(
                artifact=ref, type="index_input", schema_version="1"
            ).model_dump(mode="json")
            for ref in sorted(inputs.values(), key=lambda ref: ref.sha256)
        ],
        "data": plan.model_dump(mode="json", exclude={"manifest"}),
    }


def publication_manifest(plan: PublicationPlan) -> bytes:
    """Exact Task 7 typed retention header for a complete snapshot plan."""
    return canonical_bytes(_manifest_document(plan))


def _verify_artifacts(store: CorpusStore, plan: PublicationPlan) -> None:
    if _read_json(store, plan.manifest) != _manifest_document(plan):
        _fail("Snapshot manifest differs from complete publication plan")

    def load(ref: ManifestRef):
        return _read_json(store, ref.artifact)

    closure = required_closure(
        (ManifestRef(artifact=plan.manifest, type="snapshot", schema_version="1"),),
        load=load,
    )
    required = {
        plan.manifest.sha256,
        *(batch.input_manifest.sha256 for slot in plan.slots for batch in slot.batches),
        *(
            row.embedding_cache.sha256
            for slot in plan.slots
            for batch in slot.batches
            for row in batch.vector_records
        ),
    }
    if closure != required:
        _fail("Publication retention closure differs from required inputs")
    for slot in plan.slots:
        for batch in slot.batches:
            value = _read_json(store, batch.input_manifest)
            if value.get("type") != "index_input" or value.get("schema_version") != "1":
                _fail("Index batch input manifest has the wrong type")
            entries = value.get("data", {}).get("entries")
            if not isinstance(entries, list):
                _fail("Index input has no exact entry list")
            expected = (
                [row.chunk_id for row in batch.lexical_records]
                if slot.family.channel == "lexical"
                else [row.record_id for row in batch.vector_records]
                if slot.family.channel in {"text", "image"}
                else sorted(batch.graph_manifest.evidence_ids)
                if batch.graph_manifest is not None
                else None
            )
            if entries != expected:
                _fail("Index input entries differ from the immutable batch")
            if slot.slot == "text":
                represented = [
                    {
                        "record_id": binding.record_id,
                        "group_id": binding.group_id,
                        "dependency_ids": sorted(binding.dependency_ids),
                    }
                    for binding in batch.vector_records
                ]
                if value.get("data", {}).get("representations") != represented or any(
                    binding.group_id is None or not binding.dependency_ids
                    for binding in batch.vector_records
                ):
                    _fail("Text batch input omits complete group ownership")


def _verify_batches(
    store: CorpusStore, plan: PublicationPlan, ctx: ExecutionContext
) -> None:
    selected = {member.extraction_id for member in plan.members}
    slots = {slot.slot: slot for slot in plan.slots}
    if not store.required_slots <= slots.keys() or any(
        not slots[key].required for key in store.required_slots
    ):
        _fail("Publication omits a required index slot")
    for slot in plan.slots:
        ctx.checkpoint()
        channel = slot.family.channel
        if (
            slot.slot != channel
            or slot.family.config_json.get("effective_config_sha") != plan.config_sha
        ):
            _fail("Snapshot slot family or configuration differs")
        if channel == "graph":
            if len(slot.batches) != 1 or slot.batches[0].extraction_id is not None:
                _fail("Graph slot requires one complete membership generation")
        elif {batch.extraction_id for batch in slot.batches} != selected or len(
            slot.batches
        ) != len(selected):
            _fail("Source-local batch membership is incomplete")
        for batch in slot.batches:
            if batch.batch_id != digest(
                "index-batch-v1",
                batch.family_id,
                batch.extraction_id,
                batch.input_manifest.sha256,
            ):
                _fail("Index batch identity differs from exact input")
            receipt = batch.receipt
            if isinstance(receipt, LexicalBatchReceipt):
                ids = tuple(row.chunk_id for row in batch.lexical_records)
                if (
                    channel != "lexical"
                    or batch.vector_records
                    or receipt.family_id != batch.family_id
                    or receipt.count != batch.expected_count
                    or len(ids) != receipt.count
                    or len(set(ids)) != len(ids)
                    or receipt.entry_ids_sha != digest("index-entries-v1", ids)
                ):
                    _fail("Lexical batch receipt differs from records")
            elif isinstance(receipt, BatchReceipt):
                records = tuple(
                    sorted(receipt.records, key=lambda item: item.record_id)
                )
                bindings = tuple(
                    sorted(batch.vector_records, key=lambda item: item.record_id)
                )
                if (
                    channel not in {"text", "image"}
                    or batch.lexical_records
                    or receipt.family_id != batch.family_id
                    or receipt.config_sha != slot.family.config_sha
                    or receipt.count != batch.expected_count
                    or len(records) != receipt.count
                    or receipt.digest != receipt_digest(records)
                    or tuple(
                        (row.record_id, row.input_sha, row.value_sha) for row in records
                    )
                    != tuple(
                        (row.record_id, row.input_sha, row.value_sha)
                        for row in bindings
                    )
                ):
                    _fail("Vector batch receipt differs from records")
                if slot.family.dimension is None:
                    _fail("Vector family has no dimension")
                for row in bindings:
                    with store.artifacts.open_verified(row.embedding_cache) as stream:
                        raw = stream.read(row.embedding_cache.size_bytes + 1)
                    if (
                        len(raw) != 4 * slot.family.dimension
                        or sha256(raw).hexdigest() != row.value_sha
                    ):
                        _fail("Vector cache differs from actual normalized bytes")
                    struct.unpack("<" + "f" * slot.family.dimension, raw)
                index = store.vector_indexes.get(batch.family_id)
                if index is None:
                    raise TychoraError(
                        "CAPABILITY_MISSING", "Required vector index is unavailable"
                    )
                verification = index.verify_batch(receipt)
                if (
                    not verification.ready
                    or not verification.config_matches
                    or verification.missing_record_ids
                    or verification.mismatched_record_ids
                ):
                    _fail("Native vector batch is incomplete")
            elif isinstance(receipt, GraphBatchReceipt):
                manifest = batch.graph_manifest
                if (
                    channel != "graph"
                    or manifest is None
                    or batch.expected_count != receipt.evidence_count
                    or receipt.family_id != batch.family_id
                    or receipt.config_sha != slot.family.config_sha
                    or receipt.manifest_digest
                    != digest("graph-manifest-v1", manifest.model_dump(mode="json"))
                    or manifest.spec.workspace_id != plan.workspace_id
                    or {item.extraction_id for item in manifest.spec.memberships}
                    != selected
                    or manifest.spec.generation_id != receipt.generation_id
                    or manifest.spec.decision_watermark != receipt.decision_watermark
                ):
                    _fail("Graph receipt differs from complete canonical membership")
                graph = store.graph_indexes.get(receipt.generation_id)
                if graph is None:
                    raise TychoraError(
                        "CAPABILITY_MISSING", "Required graph generation is unavailable"
                    )
                if (
                    graph.seal_generation(receipt.generation_id, manifest, ctx)
                    != receipt
                ):
                    _fail("Native graph generation receipt differs")
            else:
                _fail("Unknown publication receipt")


def _register_artifact(store: CorpusStore, ref: ArtifactRef) -> None:
    row = store.db.execute(
        "SELECT size_bytes,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
        (ref.sha256,),
    ).fetchone()
    if row is not None:
        if row != (ref.size_bytes, ref.storage_key, "present"):
            _fail("Required publication artifact is unavailable")
        return
    store.db.execute(
        "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
        (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key, _now()),
    )


def insert_snapshot_bindings(store: CorpusStore, plan: PublicationPlan) -> None:
    """Insert all immutable bindings inside the caller's immediate transaction."""
    if not store.db.in_transaction:
        _fail("Snapshot binding insert requires an immediate transaction")
    db = store.db
    for ref in {
        ref.sha256: ref
        for ref in (
            plan.manifest,
            *(batch.input_manifest for slot in plan.slots for batch in slot.batches),
            *(
                row.embedding_cache
                for slot in plan.slots
                for batch in slot.batches
                for row in batch.vector_records
            ),
        )
    }.values():
        _register_artifact(store, ref)
    for slot in plan.slots:
        family = slot.family
        store._insert(
            "index_families",
            {
                "family_id": family.family_id,
                "channel": family.channel,
                "backend": family.backend,
                "provider": family.provider,
                "model": family.model,
                "dimension": family.dimension,
                "config_sha": family.config_sha,
                "config_json": canonical_bytes(family.config_json).decode(),
                "backend_namespace": family.backend_namespace,
                "created_at": family.created_at,
            },
            ("family_id",),
        )
        for batch in slot.batches:
            receipt = batch.receipt
            count = (
                receipt.count
                if isinstance(receipt, (LexicalBatchReceipt, BatchReceipt))
                else receipt.evidence_count
            )
            entry_ids = (
                tuple(row.chunk_id for row in batch.lexical_records)
                if family.channel == "lexical"
                else tuple(row.record_id for row in batch.vector_records)
                if family.channel in {"text", "image"}
                else tuple(batch.graph_manifest.evidence_ids)
                if batch.graph_manifest
                else ()
            )
            store._insert(
                "index_batches",
                {
                    "batch_id": batch.batch_id,
                    "family_id": batch.family_id,
                    "extraction_id": batch.extraction_id,
                    "input_manifest_sha": batch.input_manifest.sha256,
                    "expected_count": batch.expected_count,
                    "actual_count": count,
                    "entry_ids_sha": digest("index-entries-v1", entry_ids),
                    "state": "ready",
                    "receipt_json": canonical_bytes(
                        receipt.model_dump(mode="json")
                    ).decode(),
                },
                ("batch_id",),
            )
            for row in batch.lexical_records:
                existing = db.execute(
                    "SELECT batch_id,chunk_id,identifiers,title,headings,body,context FROM lexical_records WHERE batch_id=? AND chunk_id=?",
                    (batch.batch_id, row.chunk_id),
                ).fetchone()
                values = (
                    batch.batch_id,
                    row.chunk_id,
                    row.identifiers,
                    row.title,
                    row.headings,
                    row.body,
                    row.context,
                )
                if existing is not None:
                    if existing != values:
                        _fail("Immutable lexical record differs")
                else:
                    db.execute(
                        "INSERT INTO lexical_records (batch_id,chunk_id,identifiers,title,headings,body,context) VALUES (?,?,?,?,?,?,?)",
                        values,
                    )
            for row in batch.vector_records:
                store._insert(
                    "vector_records",
                    {
                        "record_id": row.record_id,
                        "batch_id": batch.batch_id,
                        "chunk_id": row.chunk_id,
                        "asset_id": row.asset_id,
                        "input_sha": row.input_sha,
                        "embedding_cache_sha": row.embedding_cache.sha256,
                    },
                    ("record_id",),
                )
    store._insert(
        "snapshots",
        {
            "snapshot_id": plan.snapshot_id,
            "workspace_id": plan.workspace_id,
            "parent_snapshot_id": plan.base_snapshot_id,
            "membership_sha": digest(
                "snapshot-membership-v1",
                [member.model_dump(mode="json") for member in plan.members],
            ),
            "config_sha": plan.config_sha,
            "manifest_sha": plan.manifest.sha256,
            "published_at": _now(),
        },
        ("snapshot_id",),
    )
    for member in plan.members:
        store._insert(
            "snapshot_members",
            {
                "snapshot_id": plan.snapshot_id,
                "source_id": member.source_id,
                "revision_id": member.revision_id,
                "extraction_id": member.extraction_id,
                "is_default": int(member.is_default),
            },
            ("snapshot_id", "revision_id"),
        )
    for slot in plan.slots:
        store._insert(
            "snapshot_indexes",
            {
                "snapshot_id": plan.snapshot_id,
                "slot": slot.slot,
                "family_id": slot.family.family_id,
                "required": int(slot.required),
            },
            ("snapshot_id", "slot"),
        )
        for batch in slot.batches:
            store._insert(
                "snapshot_batches",
                {
                    "snapshot_id": plan.snapshot_id,
                    "slot": slot.slot,
                    "family_id": slot.family.family_id,
                    "batch_id": batch.batch_id,
                },
                ("snapshot_id", "slot", "batch_id"),
            )


def publish_snapshot(store: CorpusStore, plan: PublicationPlan) -> str:
    try:
        plan = PublicationPlan.model_validate_json(
            canonical_bytes(plan.model_dump(mode="json"))
        )
    except ValueError:
        _fail("Publication plan is invalid")
    if store.db.in_transaction:
        _fail("Publication requires its own transaction")
    ctx = store.publication_context or ExecutionContext()
    keys = frozenset(
        {
            plan.snapshot_id,
            *(batch.batch_id for slot in plan.slots for batch in slot.batches),
            *(
                batch.receipt.generation_id
                for slot in plan.slots
                for batch in slot.batches
                if isinstance(batch.receipt, GraphBatchReceipt)
            ),
        }
    )
    payloads = frozenset(
        {
            plan.manifest.sha256,
            *(
                batch.input_manifest.sha256
                for slot in plan.slots
                for batch in slot.batches
            ),
            *(
                row.embedding_cache.sha256
                for slot in plan.slots
                for batch in slot.batches
                for row in batch.vector_records
            ),
        }
    )
    with ExitStack() as pins:
        pins.enter_context(store.leases.pin(keys))
        pins.enter_context(store.artifacts.pin_payloads(payloads))
        _verify_artifacts(store, plan)
        _verify_batches(store, plan, ctx)
        ctx.checkpoint()
        try:
            with store.immediate_transaction():
                ctx.checkpoint()
                if store.active_snapshot_id() != plan.base_snapshot_id:
                    raise TychoraError(
                        "WORKSPACE_BUSY",
                        "Publication base changed; rebase required.",
                        retryable=True,
                    )
                graph = next(
                    (slot for slot in plan.slots if slot.slot == "graph"), None
                )
                if graph is not None:
                    receipt = graph.batches[0].receipt
                    if not isinstance(receipt, GraphBatchReceipt):
                        _fail("Graph publication receipt is invalid")
                    watermark = store.db.execute(
                        "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
                    ).fetchone()[0]
                    if receipt.decision_watermark != watermark:
                        raise TychoraError(
                            "WORKSPACE_BUSY",
                            "Graph decisions changed; rebase required.",
                            retryable=True,
                        )
                job = store.db.execute(
                    "SELECT state,effective_config_sha,result_snapshot_id FROM jobs WHERE job_id=?",
                    (plan.job_id,),
                ).fetchone()
                if (
                    job is None
                    or job[0] != "running"
                    or job[1] != plan.config_sha
                    or job[2] not in (None, plan.snapshot_id)
                ):
                    _fail(
                        "Publication job is absent, cancelled, or has conflicting bindings"
                    )
                if store.db.execute(
                    "SELECT workspace_id FROM workspaces"
                ).fetchall() != [(plan.workspace_id,)]:
                    _fail("Publication workspace identity differs")
                for member in plan.members:
                    row = store.db.execute(
                        "SELECT r.source_id,e.revision_id,c.workspace_id FROM extractions e JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) JOIN collections c USING(collection_id) WHERE e.extraction_id=?",
                        (member.extraction_id,),
                    ).fetchone()
                    if row != (member.source_id, member.revision_id, plan.workspace_id):
                        _fail("Snapshot member is not a registered extraction")
                for slot in plan.slots:
                    for batch in slot.batches:
                        for row in batch.lexical_records:
                            owner = store.db.execute(
                                "SELECT extraction_id FROM chunks WHERE chunk_id=?",
                                (row.chunk_id,),
                            ).fetchone()
                            if owner != (batch.extraction_id,):
                                _fail(
                                    "Lexical record is not owned by selected extraction"
                                )
                        for row in batch.vector_records:
                            owner = (
                                store.db.execute(
                                    "SELECT extraction_id FROM chunks WHERE chunk_id=?",
                                    (row.chunk_id,),
                                ).fetchone()
                                if row.chunk_id
                                else store.db.execute(
                                    "SELECT extraction_id FROM assets WHERE asset_id=?",
                                    (row.asset_id,),
                                ).fetchone()
                            )
                            if owner != (batch.extraction_id,):
                                _fail(
                                    "Vector record is not owned by selected extraction"
                                )
                store.insert_snapshot_bindings(plan)
                store.db.execute(
                    "UPDATE jobs SET base_snapshot_id=?,result_snapshot_id=?,updated_at=? WHERE job_id=?",
                    (plan.base_snapshot_id, plan.snapshot_id, _now(), plan.job_id),
                )
                ctx.checkpoint()
                store.set_active_snapshot(plan.snapshot_id)
        except sqlite3.IntegrityError:
            _fail("Publication foreign-key or uniqueness constraint failed")
    return plan.snapshot_id


def resolve_snapshot(store: CorpusStore, selector: str | None) -> SnapshotView:
    snapshot_id = selector or store.active_snapshot_id()
    if snapshot_id is None:
        raise TychoraError("NOT_FOUND", "No published snapshot is available")
    row = store.db.execute(
        "SELECT workspace_id,parent_snapshot_id,membership_sha,config_sha,manifest_sha FROM snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise TychoraError("NOT_FOUND", "Snapshot is unavailable")
    artifact = store.db.execute(
        "SELECT artifact_sha,size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
        (row[4],),
    ).fetchone()
    if artifact is None or artifact[4] != "present":
        _fail("Snapshot manifest is unavailable")
    ref = ArtifactRef(
        sha256=artifact[0],
        size_bytes=artifact[1],
        media_type=artifact[2],
        storage_key=artifact[3],
    )
    document = _read_json(store, ref)
    if document.get("type") != "snapshot" or document.get("schema_version") != "1":
        _fail("Snapshot manifest type differs")
    try:
        plan = PublicationPlan.model_validate_json(
            canonical_bytes(
                {**document["data"], "manifest": ref.model_dump(mode="json")}
            )
        )
    except (ValueError, KeyError, TypeError):
        _fail("Snapshot manifest plan is malformed")
    if (
        plan.snapshot_id != snapshot_id
        or plan.workspace_id != row[0]
        or plan.base_snapshot_id != row[1]
        or plan.config_sha != row[3]
        or digest(
            "snapshot-membership-v1",
            [member.model_dump(mode="json") for member in plan.members],
        )
        != row[2]
        or document != _manifest_document(plan)
    ):
        _fail("Snapshot manifest differs from canonical binding")
    selected_members = set(
        store.db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    if selected_members != {
        (m.source_id, m.revision_id, m.extraction_id, int(m.is_default))
        for m in plan.members
    }:
        _fail("Snapshot canonical membership differs")
    selected_slots = set(
        store.db.execute(
            "SELECT slot,family_id,required FROM snapshot_indexes WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    if selected_slots != {
        (slot.slot, slot.family.family_id, int(slot.required)) for slot in plan.slots
    }:
        _fail("Snapshot canonical slots differ")
    selected_batches = set(
        store.db.execute(
            "SELECT slot,family_id,batch_id FROM snapshot_batches WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    if selected_batches != {
        (slot.slot, slot.family.family_id, batch.batch_id)
        for slot in plan.slots
        for batch in slot.batches
    }:
        _fail("Snapshot canonical batches differ")
    epoch = store.db.execute(
        "SELECT lexical_statistics_epoch FROM workspaces WHERE workspace_id=?",
        (plan.workspace_id,),
    ).fetchone()[0]
    return SnapshotView(
        snapshot_id=snapshot_id,
        workspace_id=plan.workspace_id,
        parent_snapshot_id=plan.base_snapshot_id,
        membership_sha=row[2],
        config_sha=plan.config_sha,
        manifest=ref,
        members=plan.members,
        slots=plan.slots,
        lexical_statistics_epoch=epoch,
    )


class PublicationReceipts:
    """Trusted canonical job lookup, including publication before completion event."""

    def __init__(self, store: CorpusStore):
        self.store = store

    def lookup(self, job_id: str) -> PublicationReceipt | None:
        row = self.store.db.execute(
            "SELECT result_snapshot_id FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        view = self.store.resolve_snapshot(row[0])
        document = _read_json(self.store, view.manifest)
        if document["data"]["job_id"] != job_id:
            _fail("Publication receipt job binding differs")
        return PublicationReceipt(job_id=job_id, result_snapshot_id=view.snapshot_id)
