"""Verify the batches an add wrote, then switch the active snapshot in one transaction."""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from tychora.adapters.files.references import MAX_MANIFEST_BYTES
from tychora.adapters.sqlite.repositories import artifact_ref
from tychora.application.collection_policy import (
    collection_is_active,
    require_active_collection,
)
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    GraphBatchReceipt,
    GraphManifest,
    graph_manifest_digest,
)
from tychora.contracts.identity import canonical_bytes, digest, source_id
from tychora.contracts.jobs import JobAuthorization, PublicationReceipt
from tychora.contracts.publication import (
    LexicalBatchReceipt,
    NewBatch,
    PublicationPlan,
    SnapshotView,
    snapshot_root_document,
)
from tychora.contracts.snapshots import MembershipPage
from tychora.contracts.vector_batches import decode_vector_batch, row_value_sha
from tychora.contracts.vectors import BatchReceipt
from tychora.indexing.receipts import receipt_digest
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import ReadTransaction, WriteTransaction

if TYPE_CHECKING:
    from tychora.adapters.sqlite.corpus import CorpusStore


def _fail(message: str, reason: str = "canonical.binding_mismatch") -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message, reason=reason)


def _rebase(message: str) -> TychoraError:
    return TychoraError(
        "WORKSPACE_BUSY", message, retryable=True, reason="publication.rebase"
    )


def _read_json(store: CorpusStore, ref: ArtifactRef) -> dict:
    with store.artifacts.open_verified(ref) as stream:
        raw = stream.read(ref.size_bytes + 1)
    if len(raw) != ref.size_bytes:
        _fail("Publication artifact size differs", "payload.hash_mismatch")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _fail("Publication manifest is malformed", "canonical.row_invalid")
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        _fail("Publication manifest is not canonical JSON", "canonical.row_invalid")
    return value


def page_ref(page: MembershipPage) -> ArtifactRef:
    """Return the content-addressed reference of a page's manifest bytes."""
    raw = page.manifest_bytes()
    sha = sha256(raw).hexdigest()
    return ArtifactRef(
        sha256=sha, size_bytes=len(raw), media_type="application/json", storage_key=sha
    )


def _bound_batch_ids(plan: PublicationPlan) -> tuple[str, ...]:
    """Every batch the new pages and the root bind, reused or new."""
    return tuple(
        sorted(
            {
                batch.batch_id
                for page in plan.new_pages
                for member in page.members
                for batch in member.batches
            }
            | {batch.batch_id for batch in plan.root_batches}
        )
    )


def _require_base(store: CorpusStore, plan: PublicationPlan) -> SnapshotView | None:
    """Refuse a plan whose base is no longer active before anything reads it."""
    with store.uow.read() as tx:
        if tx.snapshots.active_id() != plan.base_snapshot_id:
            raise _rebase("Publication base changed; rebase required.")
    if plan.base_snapshot_id is None:
        return None
    return store.resolve_snapshot(plan.base_snapshot_id)


def _verify_documents(store: CorpusStore, plan: PublicationPlan) -> None:
    """Check the new pages exist and the root binds exactly the plan's pages."""
    with store.uow.read() as tx:
        manifests = dict(tx.snapshots.page_manifests(plan.reused_page_ids))
    for page in plan.new_pages:
        ref = page_ref(page)
        if not store.artifacts.stat(ref).available:
            _fail("Membership page bytes are missing", "payload.missing")
        manifests[page.page_id] = ref
    if _read_json(store, plan.manifest) != snapshot_root_document(plan, manifests):
        _fail("Snapshot root differs from its publication plan")


def _verify_families(
    store: CorpusStore, plan: PublicationPlan, base: SnapshotView | None
) -> None:
    """Require the configured slots; a pinned family must come from the base."""
    slots = {item.slot: item for item in plan.families}
    if not store.required_slots <= slots.keys() or any(
        not slots[key].required for key in store.required_slots
    ):
        _fail("Publication omits a required index slot")
    for item in plan.families:
        origin = item.family.config_json.get("effective_config_sha")
        if not isinstance(origin, str):
            _fail("Snapshot slot family has no configuration binding", "config.drift")
        if origin == plan.config_sha:
            continue
        pinned = base.family(item.slot) if base is not None else None
        if (
            pinned is None
            or pinned.family != item.family
            or not pinned_runtime_compatible(
                store.retained_config_sha(origin),
                store.retained_config_sha(plan.config_sha),
            )
        ):
            _fail("Snapshot slot family or configuration differs", "config.drift")


def _verify_lexical(
    store: CorpusStore, batch: NewBatch, receipt: LexicalBatchReceipt
) -> None:
    entries = _read_json(store, batch.input_manifest).get("data", {}).get("entries")
    if not isinstance(entries, list):
        _fail("Lexical batch input has no exact entry list", "canonical.row_invalid")
    staged = {
        row[0]
        for row in store.db.execute(
            "SELECT chunk_id FROM lexical_records WHERE batch_id=?", (batch.batch_id,)
        )
    }
    if (
        receipt.family_id != batch.family_id
        or receipt.count != batch.expected_count
        or len(entries) != receipt.count
        or staged != set(entries)
        or receipt.entry_ids_sha != digest("index-entries-v1", tuple(entries))
    ):
        _fail("Lexical batch rows differ from their receipt")


def _verify_vector_artifact(
    store: CorpusStore, batch: NewBatch, receipt: BatchReceipt, dimension: int | None
) -> None:
    """Check the batch artifact's rows against the receipt records."""
    if batch.vectors is None:
        _fail("Vector batch artifact is missing", "payload.missing")
    with store.artifacts.open_verified(batch.vectors) as stream:
        decoded = decode_vector_batch(stream.read(batch.vectors.size_bytes + 1))
    rows = tuple(
        (row.record_id, row.input_sha, row_value_sha(row.values))
        for row in decoded.rows
    )
    if (
        decoded.family_id != batch.family_id
        or decoded.dimension != dimension
        or rows
        != tuple((r.record_id, r.input_sha, r.value_sha) for r in receipt.records)
    ):
        _fail("Vector batch artifact differs from its receipt", "payload.hash_mismatch")


def _verify_vector(
    store: CorpusStore, plan: PublicationPlan, batch: NewBatch, receipt: BatchReceipt
) -> None:
    family = next(
        item.family
        for item in plan.families
        if item.family.family_id == batch.family_id
    )
    if (
        receipt.family_id != batch.family_id
        or receipt.config_sha != family.config_sha
        or receipt.count != batch.expected_count
        or len(receipt.records) != receipt.count
        or receipt.digest != receipt_digest(receipt.records)
    ):
        _fail("Vector batch receipt differs from its records")
    if receipt.records:
        _verify_vector_artifact(store, batch, receipt, family.dimension)
    index = store.vector_indexes.get(batch.family_id)
    if index is None:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Required vector index is unavailable",
            reason="native.unavailable",
        )
    verification = index.verify_batch(receipt)
    if (
        not verification.ready
        or not verification.config_matches
        or verification.missing_record_ids
        or verification.mismatched_record_ids
    ):
        _fail("Native vector batch is incomplete")


def _member_extractions(store: CorpusStore, plan: PublicationPlan) -> set[str]:
    """Every extraction the planned snapshot selects, from SQL and the new pages."""
    reused = {
        row[0]
        for row in store.db.execute(
            "SELECT extraction_id FROM page_members "
            "WHERE page_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(plan.reused_page_ids)),),
        )
    }
    return reused | {m.extraction_id for p in plan.new_pages for m in p.members}


def _verify_graph(
    store: CorpusStore,
    plan: PublicationPlan,
    batch: NewBatch,
    receipt: GraphBatchReceipt,
    ctx: ExecutionContext,
) -> None:
    data = _read_json(store, batch.input_manifest).get("data", {})
    try:
        manifest = GraphManifest.model_validate_json(
            canonical_bytes(data["graph_manifest"])
        )
    except (KeyError, TypeError, ValueError):
        _fail("Graph batch input has no generation manifest", "canonical.row_invalid")
    spec = manifest.spec
    if (
        receipt.family_id != batch.family_id
        or receipt.evidence_count != batch.expected_count
        or receipt.manifest_digest != graph_manifest_digest(manifest)
        or data.get("entries") != sorted(manifest.evidence_ids)
        or (spec.workspace_id, spec.generation_id, spec.decision_watermark)
        != (plan.workspace_id, receipt.generation_id, receipt.decision_watermark)
        or {item.extraction_id for item in spec.memberships}
        != _member_extractions(store, plan)
    ):
        _fail("Graph receipt differs from complete canonical membership")
    graph = store.graph_indexes.get(receipt.generation_id)
    if graph is None:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Required graph generation is unavailable",
            reason="native.unavailable",
        )
    if graph.seal_generation(receipt.generation_id, manifest, ctx) != receipt:
        _fail("Native graph generation receipt differs")


def _require_ready(tx: ReadTransaction, plan: PublicationPlan) -> None:
    """Every bound batch, reused or new, must be ready; one statement."""
    bound = _bound_batch_ids(plan)
    if tx.batches.count_ready(bound) != len(bound):
        raise TychoraError(
            "INDEX_INCOMPLETE", "A bound batch is not ready", reason="batch.missing"
        )


def _verify_new_batches(
    store: CorpusStore, plan: PublicationPlan, ctx: ExecutionContext
) -> None:
    """Verify only what this publication wrote; reused batches are counted, not opened."""
    with store.uow.read() as tx:
        _require_ready(tx, plan)
    for batch in plan.new_batches:
        ctx.checkpoint()
        receipt = batch.receipt
        if isinstance(receipt, LexicalBatchReceipt):
            _verify_lexical(store, batch, receipt)
        elif isinstance(receipt, BatchReceipt):
            _verify_vector(store, plan, batch, receipt)
        else:
            _verify_graph(store, plan, batch, receipt, ctx)


def _check_base_and_job(
    store: CorpusStore, plan: PublicationPlan, tx: WriteTransaction
) -> None:
    """Inside the activation: same base, same decisions, a running job, one workspace."""
    if tx.snapshots.active_id() != plan.base_snapshot_id:
        raise _rebase("Publication base changed; rebase required.")
    if (
        plan.graph_watermark is not None
        and tx.workspace.decision_watermark() != plan.graph_watermark
    ):
        raise _rebase("Graph decisions changed; rebase required.")
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
        _fail("Publication job is absent, cancelled, or has conflicting bindings")
    if tx.workspace.workspace_id() != plan.workspace_id:
        _fail("Publication workspace identity differs")


def _prior_extractions(store: CorpusStore, plan: PublicationPlan) -> set[str]:
    """Extractions the base selected in the buckets the new pages replace."""
    if plan.base_snapshot_id is None:
        return set()
    return {
        row[0]
        for row in store.db.execute(
            "SELECT m.extraction_id FROM snapshot_pages sp "
            "JOIN page_members m USING(page_id) WHERE sp.snapshot_id=? "
            "AND sp.bucket IN (SELECT value FROM json_each(?))",
            (
                plan.base_snapshot_id,
                json.dumps([page.bucket for page in plan.new_pages]),
            ),
        )
    }


def _check_members(
    store: CorpusStore, plan: PublicationPlan, target: str | None
) -> None:
    """New pages' members are registered, not withdrawn and in an active collection.

    An archived collection keeps a member the base already selected, unless the
    member is this publication's own source.
    """
    members = {m.extraction_id: m for page in plan.new_pages for m in page.members}
    rows = {
        row[0]: row[1:]
        for row in store.db.execute(
            "SELECT e.extraction_id,r.source_id,e.revision_id,c.workspace_id,"
            "c.policy_json FROM extractions e JOIN revisions r USING(revision_id) "
            "JOIN sources s USING(source_id) JOIN collections c USING(collection_id) "
            "WHERE e.extraction_id IN (SELECT value FROM json_each(?)) "
            "AND s.withdrawn_at IS NULL",
            (json.dumps(sorted(members)),),
        )
    }
    prior: set[str] | None = None
    for extraction_id, member in members.items():
        row = rows.get(extraction_id)
        if row is None or row[:3] != (
            member.source_id,
            member.revision_id,
            plan.workspace_id,
        ):
            _fail("Snapshot member is not a registered extraction")
        if collection_is_active(row[3]):
            continue
        prior = _prior_extractions(store, plan) if prior is None else prior
        if target is None or member.source_id == target or extraction_id not in prior:
            require_active_collection(row[3])


def _check_ownership(store: CorpusStore, plan: PublicationPlan) -> None:
    """New batch rows must belong to their batch's extraction (one query per kind)."""
    expected = {
        b.batch_id: b.extraction_id
        for b in plan.new_batches
        if b.extraction_id is not None
    }
    ids = json.dumps(sorted(expected))
    found = list(
        store.db.execute(
            "SELECT l.batch_id,c.extraction_id FROM lexical_records l "
            "LEFT JOIN chunks c USING(chunk_id) "
            "WHERE l.batch_id IN (SELECT value FROM json_each(?)) GROUP BY 1,2",
            (ids,),
        )
    ) + list(
        store.db.execute(
            "SELECT v.batch_id,COALESCE(c.extraction_id,a.extraction_id) "
            "FROM vector_records v LEFT JOIN chunks c USING(chunk_id) "
            "LEFT JOIN assets a USING(asset_id) "
            "WHERE v.batch_id IN (SELECT value FROM json_each(?)) GROUP BY 1,2",
            (ids,),
        )
    )
    if any(expected.get(batch_id) != owner for batch_id, owner in found):
        _fail("Index record is not owned by its batch's extraction")


def register_fresh(store: CorpusStore, tx: WriteTransaction, ref: ArtifactRef) -> None:
    """Register a publication-owned artifact; a released one returns once verified."""
    if tx.artifacts.revive(ref):
        with store.artifacts.open_verified(ref):
            pass
    tx.artifacts.register(ref)


def _activate(store: CorpusStore, plan: PublicationPlan, ctx: ExecutionContext) -> None:
    """Write pages, the snapshot and the active pointer in one write transaction."""
    try:
        with store.uow.write() as tx:
            ctx.checkpoint()
            _check_base_and_job(store, plan, tx)
            _require_ready(tx, plan)
            _check_members(store, plan, check_publication_target(store, plan))
            _check_ownership(store, plan)
            for page in plan.new_pages:
                ref = page_ref(page)
                register_fresh(store, tx, ref)
                tx.snapshots.insert_page(page, ref)
            register_fresh(store, tx, plan.manifest)
            tx.snapshots.insert_snapshot(plan)
            store.db.execute(
                "UPDATE jobs SET base_snapshot_id=?,result_snapshot_id=?,updated_at=? "
                "WHERE job_id=?",
                (
                    plan.base_snapshot_id,
                    plan.snapshot_id,
                    datetime.now(UTC).isoformat(),
                    plan.job_id,
                ),
            )
            ctx.checkpoint()
            tx.snapshots.set_active(plan.snapshot_id)
    except sqlite3.IntegrityError:
        _fail("Publication foreign-key or uniqueness constraint failed")


def _canonical_plan(plan: PublicationPlan) -> PublicationPlan:
    """Revalidate a plan from its canonical JSON so no unchecked value survives."""
    try:
        return PublicationPlan.model_validate_json(
            canonical_bytes(plan.model_dump(mode="json"))
        )
    except ValueError:
        _fail("Publication plan is invalid", "canonical.row_invalid")


def _new_payloads(plan: PublicationPlan) -> frozenset[str]:
    """The payloads this publication registers or verifies."""
    return frozenset(
        {
            plan.manifest.sha256,
            *(page_ref(page).sha256 for page in plan.new_pages),
            *(batch.input_manifest.sha256 for batch in plan.new_batches),
            *(b.vectors.sha256 for b in plan.new_batches if b.vectors is not None),
        }
    )


def publish_snapshot(store: CorpusStore, plan: PublicationPlan) -> str:
    """Verify a plan's new pages and batches, then activate its snapshot atomically.

    A failure at any point, the activation transaction included, leaves the
    previous active snapshot and every row it binds untouched.
    """
    plan = _canonical_plan(plan)
    if store.db.in_transaction:
        _fail("Publication requires its own transaction", "invariant.unexpected")
    ctx = store.publication_context or ExecutionContext()
    base = _require_base(store, plan)
    leases = frozenset(
        {plan.snapshot_id, *_bound_batch_ids(plan)}
        | ({plan.base_snapshot_id} if plan.base_snapshot_id else set())
    )
    with ExitStack() as pins:
        pins.enter_context(store.leases.pin(leases))
        pins.enter_context(store.artifacts.pin_payloads(_new_payloads(plan)))
        _verify_families(store, plan, base)
        _verify_documents(store, plan)
        _verify_new_batches(store, plan, ctx)
        ctx.checkpoint()
        _activate(store, plan, ctx)
    return plan.snapshot_id


def check_publication_target(store: CorpusStore, plan: PublicationPlan) -> str | None:
    """Resolve actual source identity from verified durable capture, never full grant.

    Called before native attachment and again inside final canonical activation.
    Old synthetic/internal publications without a capture get no exemption for
    archived members. No new field changes historical publication identities.
    """
    row = store.db.execute(
        "SELECT j.captured_inputs_sha,a.size_bytes,a.media_type,a.storage_key "
        "FROM jobs j LEFT JOIN artifacts a ON a.artifact_sha=j.captured_inputs_sha WHERE j.job_id=?",
        (plan.job_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    if row[1] is None or row[1] > MAX_MANIFEST_BYTES:
        _fail("Publication capture exceeds its verified metadata budget")
    capture = _read_json(
        store,
        artifact_ref(row),
    )
    auth_row = store.db.execute(
        "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key FROM job_steps js "
        "JOIN artifacts a ON a.artifact_sha=js.output_artifact_sha "
        "WHERE js.job_id=? AND js.step_key='__tychora__/authorization'",
        (plan.job_id,),
    ).fetchone()
    if auth_row is None or auth_row[1] > 1048576:
        _fail("Publication source authorization is unavailable")
    auth_doc = _read_json(
        store,
        artifact_ref(auth_row),
    )
    try:
        if capture["type"] != "job_capture" or capture["schema_version"] != "1":
            raise ValueError
        source = capture["data"]["source"]
        cid = str(UUID(source["collection_id"]))
        key = source["source_key"]
        if key != capture["data"]["source_key"]:
            raise ValueError
        sid = source_id(UUID(cid), key)
        authorization = JobAuthorization.from_mapping(auth_doc["data"])
        if cid not in authorization.collection_ids:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        _fail("Publication capture source binding is invalid")
    current = store.db.execute(
        "SELECT c.policy_json,c.access_floor,s.access_floor,c.workspace_id,s.withdrawn_at "
        "FROM collections c JOIN sources s USING(collection_id) WHERE c.collection_id=? AND s.source_id=?",
        (cid, sid),
    ).fetchone()
    if (
        current is None
        or current[3] != plan.workspace_id
        or current[4] is not None
        or max(current[1:3]) > authorization.access_ceiling
    ):
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Publication target is outside the current grant."
        )
    require_active_collection(current[0])
    return sid


class PublicationReceipts:
    """Trusted canonical job lookup, including publication before completion event."""

    def __init__(self, store: CorpusStore):
        self.store = store

    def lookup(self, job_id: str) -> PublicationReceipt | None:
        """Return the job's published snapshot, from SQL rows only."""
        row = self.store.db.execute(
            "SELECT j.result_snapshot_id,s.job_id FROM jobs j "
            "LEFT JOIN snapshots s ON s.snapshot_id=j.result_snapshot_id "
            "WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        if row[1] != job_id:
            _fail("Publication receipt job binding differs")
        return PublicationReceipt(job_id=job_id, result_snapshot_id=row[0])
