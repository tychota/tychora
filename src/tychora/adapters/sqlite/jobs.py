"""Durable job bindings. Invoke on the workspace's canonical owner thread."""

import json
import sqlite3
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from io import BytesIO
from typing import NoReturn
from uuid import uuid4

from tychora.adapters.files.references import (
    MAX_MANIFEST_BYTES,
    required_closure,
    workspace_closure_budget,
)
from tychora.adapters.sqlite.repositories import artifact_ref
from tychora.contracts.artifacts import ArtifactRef, ManifestRef, physical_identity
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.jobs import JobAuthorization, JobStatus
from tychora.contracts.operations import JobEvent, JobHandle, NamedCount
from tychora.contracts.requests import RequestScope
from tychora.ports.artifacts import Artifacts
from tychora.ports.jobs import PublicationReceiptQuery


def _fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


def _orphaned() -> TychoraError:
    """The retryable failure of a job whose process stopped."""
    return TychoraError("JOB_INTERRUPTED", "Job was interrupted", reason="job.orphaned")


def _now() -> str:
    return datetime.now(UTC).isoformat()


OPERATION_CAPABILITIES = {
    "add": "write",
    "health": "read",
    **dict.fromkeys(
        (
            "setup",
            "sync",
            "remove",
            "rebuild",
            "export",
            "import",
            "evaluate",
            "history-purge",
            "cache-purge",
            "purge-originals",
            "migrate",
        ),
        "manage",
    ),
}


class JobRepository:
    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        artifacts: Artifacts,
        workspace_id: str,
        receipts: PublicationReceiptQuery | None = None,
        operation_capabilities: Mapping[str, str] | None = None,
    ):
        if db.execute("SELECT workspace_id FROM workspaces").fetchall() != [
            (workspace_id,)
        ]:
            _fail("Job repository requires the canonical workspace identity")
        self.db = db
        self.artifacts = artifacts
        self.workspace_id = workspace_id
        self.receipts = receipts
        self.operation_capabilities = {
            **OPERATION_CAPABILITIES,
            **(operation_capabilities or {}),
        }

    @contextmanager
    def _write(self):
        if self.db.in_transaction:
            _fail("Job operations require their own transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except sqlite3.Error:
            self.db.rollback()
            raise TychoraError("INTEGRITY_ERROR", "Job transaction failed") from None
        except BaseException:
            self.db.rollback()
            raise

    def _register(self, ref: ArtifactRef) -> None:
        current = self.db.execute(
            "SELECT size_bytes,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        expected = (ref.size_bytes, ref.storage_key, "present")
        if current is not None:
            if current[:2] != expected[:2]:
                _fail("Artifact metadata or availability differs")
            if current[2] != "present":
                if not self.db.execute(
                    "SELECT 1 FROM original_payload_tombstones WHERE artifact_sha=?",
                    (ref.sha256,),
                ).fetchone():
                    _fail("Deleted artifact has no original-removal provenance")
                # A new captured binding can reuse bytes whose prior revision
                # was tombstoned. Verify rematerialization before restoring the
                # payload state; old revision tombstones remain untouched.
                with self.artifacts.open_verified(ref):
                    pass
                self.db.execute(
                    "UPDATE artifacts SET payload_state='present',payload_deleted_at=NULL WHERE artifact_sha=?",
                    (ref.sha256,),
                )
            return
        self.db.execute(
            "INSERT INTO artifacts VALUES (?,?,?,?,'present',NULL,?)",
            (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key, _now()),
        )

    def accept(
        self,
        operation: str,
        scope: RequestScope,
        key: str | None,
        intent: JsonValue,
        effective_config: ArtifactRef,
    ) -> JobHandle:
        capability = self.operation_capabilities.get(operation)
        if capability is None or capability not in scope.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Operation is not permitted")
        self._authorize_collections(scope, scope.collection_ids)
        if not operation or (key is not None and not key):
            raise TychoraError(
                "INVALID_REQUEST", "Operation and supplied key must be nonempty"
            )
        try:
            canonical_bytes(intent)
            request_digest = digest(
                "request-v1", self.workspace_id, scope.principal_id, operation, intent
            )
        except (TypeError, ValueError) as error:
            raise TychoraError(
                "INVALID_REQUEST", "Caller intent must be finite JSON"
            ) from error
        with ExitStack() as pins, self._write():
            current = (
                self.db.execute(
                    "SELECT job_id,request_digest,state FROM jobs WHERE principal_id=? AND operation=? AND idempotency_key=?",
                    (scope.principal_id, operation, key),
                ).fetchone()
                if key is not None
                else None
            )
            if current is not None:
                if current[1] != request_digest:
                    raise TychoraError(
                        "IDEMPOTENCY_CONFLICT", "This key is bound to different input."
                    )
                self.authorize(current[0], scope, write=True)
                status = self.get(current[0])
                self._verify_bindings(status)
                return JobHandle(
                    id=status.id, operation=status.operation, state=status.state
                )
            refs = pins.enter_context(
                self._verified_manifest(effective_config, "registry")
            )
            for ref in refs:
                self._register(ref)
            job_id, now = str(uuid4()), _now()
            self.db.execute(
                "INSERT INTO jobs (job_id,operation,principal_id,idempotency_key,request_digest,effective_config_sha,state,base_snapshot_id,created_at,updated_at) VALUES (?,?,?,?,?,?,'queued',(SELECT active_snapshot_id FROM workspaces WHERE workspace_id=?),?,?)",
                (
                    job_id,
                    operation,
                    scope.principal_id,
                    key,
                    request_digest,
                    effective_config.sha256,
                    self.workspace_id,
                    now,
                    now,
                ),
            )
            self._meta(job_id, "sequence")
            self._meta(job_id, "retry")
            self._bind_authorization(job_id, scope, capability, pins)
            self._emit(job_id, "accepted")
        return JobHandle(id=job_id, operation=operation, state="queued")

    def _ref(self, sha: str | None) -> ArtifactRef | None:
        if sha is None:
            return None
        row = self.db.execute(
            "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
            (sha,),
        ).fetchone()
        if row is None:
            _fail("Committed artifact metadata is missing")
        return artifact_ref((sha, *row[:3]))

    def get(self, job_id: str) -> JobStatus:
        row = self.db.execute(
            "SELECT operation,state,request_digest,effective_config_sha,captured_inputs_sha,base_snapshot_id,result_snapshot_id,error_code FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Job does not exist")
        config = self._ref(row[3])
        assert config is not None
        meta = {
            r[0]: r[1:]
            for r in self.db.execute(
                "SELECT step_key,completed_count,attempt,state,output_artifact_sha FROM job_steps WHERE job_id=? AND step_key LIKE '__tychora__/%'",
                (job_id,),
            )
        }
        retry = meta.get("__tychora__/retry", (0, 0, "durable", None))
        result = meta.get("__tychora__/result", (0, 0, "durable", None))[3]
        result_unavailable = result is not None and self.db.execute(
            "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (result,)
        ).fetchone() in {("delete_pending",), ("deleted",)}
        return JobStatus(
            result=self._ref(result),
            latest_sequence=meta.get("__tychora__/sequence", (0,))[0],
            retry_count=retry[1],
            retention_limited="__tychora__/retention" in meta or result_unavailable,
            id=job_id,
            operation=row[0],
            state=(
                "interrupted"
                if row[1] == "failed" and row[7] == "JOB_INTERRUPTED"
                else {"complete": "succeeded", "cancelling": "running"}.get(
                    row[1], row[1]
                )
            ),
            request_digest=row[2],
            effective_config=config,
            captured_inputs=self._ref(row[4]),
            base_snapshot_id=row[5],
            result_snapshot_id=row[6],
            error_code=row[7],
            cancellation_requested=row[1] == "cancelling",
        )

    def _verify_bindings(self, status: JobStatus) -> None:
        with self._verified_manifest(status.effective_config, "registry") as refs:
            self._require_registered(refs)
        if status.captured_inputs is not None:
            with self._verified_manifest(
                status.captured_inputs,
                "job_capture",
                published=status.state == "succeeded"
                and status.result_snapshot_id is not None,
            ) as refs:
                self._require_registered(refs)

    def _require_registered(self, refs: tuple[ArtifactRef, ...]) -> None:
        for ref in refs:
            row = self.db.execute(
                "SELECT size_bytes,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                (ref.sha256,),
            ).fetchone()
            if row != (ref.size_bytes, ref.storage_key, "present"):
                _fail("Required artifact metadata or availability differs")

    @contextmanager
    def _verified_manifest(
        self, root: ArtifactRef, kind: str, *, published: bool = False
    ):
        """Reuse the retention grammar, keeping every verified payload pinned."""
        refs: dict[str, ArtifactRef] = {root.sha256: root}
        captures: set[str] = set()
        decoded: dict[str, JsonValue] = {}
        with ExitStack() as pins:

            def remember(ref: ArtifactRef) -> None:
                old = refs.get(ref.sha256)
                if old is not None and physical_identity(old) != physical_identity(ref):
                    _fail("Conflicting artifact declarations")
                refs.setdefault(ref.sha256, ref)

            def load(link: ManifestRef):
                remember(link.artifact)
                if link.type in {"job_capture", "repository_capture"}:
                    captures.add(link.artifact.sha256)
                if link.artifact.sha256 in decoded:
                    return decoded[link.artifact.sha256]
                pins.enter_context(
                    self.artifacts.pin_payloads(frozenset({link.artifact.sha256}))
                )
                with self.artifacts.open_verified(link.artifact) as stream:
                    payload = stream.read(MAX_MANIFEST_BYTES + 1)
                if len(payload) > MAX_MANIFEST_BYTES:
                    _fail("Manifest exceeds byte limit")

                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            _fail("Duplicate manifest object key")
                        value[key] = item
                    return value

                try:
                    if link.type == "assertion":
                        data = [
                            json.loads(line, object_pairs_hook=unique)
                            for line in payload.splitlines()
                        ]
                    else:
                        data = json.loads(payload, object_pairs_hook=unique)
                except (ValueError, UnicodeError):
                    _fail("Malformed capture or config manifest")
                for record in data if isinstance(data, list) else [data]:
                    if isinstance(record, Mapping):
                        for field in ("required_artifacts", "original_artifacts"):
                            for value in record.get(field, []):
                                remember(
                                    ArtifactRef.model_validate_json(
                                        canonical_bytes(value)
                                    )
                                )
                decoded[link.artifact.sha256] = data
                return data

            budget = workspace_closure_budget(self.db)
            linked = (ManifestRef(artifact=root, type=kind, schema_version="1"),)
            try:
                closure = required_closure(linked, load=load, budget=budget)
                if published:
                    closure = required_closure(
                        linked,
                        load=load,
                        budget=budget,
                        published_captures=frozenset(captures),
                    )
                for sha in closure:
                    ref = refs[sha]
                    pins.enter_context(
                        self.artifacts.pin_payloads(frozenset({ref.sha256}))
                    )
                    with self.artifacts.open_verified(ref):
                        pass
                yield tuple(refs[sha] for sha in sorted(closure))
            except (ValueError, TypeError, KeyError, RecursionError):
                _fail("Malformed required artifact closure")

    def capture(self, job_id: str, manifest: ArtifactRef) -> None:
        status = self.get(job_id)
        if status.captured_inputs is not None and physical_identity(
            status.captured_inputs
        ) != physical_identity(manifest):
            _fail("Committed capture is immutable")
        with self._verified_manifest(manifest, "job_capture") as refs, self._write():
            current = self.get(job_id)
            if current.captured_inputs is not None and physical_identity(
                current.captured_inputs
            ) != physical_identity(manifest):
                _fail("Committed capture is immutable")
            for ref in refs:
                self._register(ref)
            if current.captured_inputs is None:
                if (
                    current.state not in {"queued", "running"}
                    or current.cancellation_requested
                ):
                    _fail("Cannot capture inputs for a terminal or cancelling job")
                self.db.execute(
                    "UPDATE jobs SET captured_inputs_sha=?,updated_at=? WHERE job_id=?",
                    (manifest.sha256, _now(), job_id),
                )
                self._emit(job_id, "capture")

    def _meta(self, job_id: str, name: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO job_steps (job_id,step_key,input_sha,state) VALUES (?,?,?,'durable')",
            (job_id, "__tychora__/" + name, digest("job-metadata-v1", name)),
        )

    def _emit(self, job_id: str, stage: str, event: JobEvent | None = None) -> int:
        self.db.execute(
            "UPDATE job_steps SET completed_count=completed_count+1 WHERE job_id=? AND step_key='__tychora__/sequence'",
            (job_id,),
        )
        status = self.get(job_id)
        if status.latest_sequence < 1:
            _fail("Missing durable event sequence")
        if event is None:
            event = JobEvent(
                job_id=job_id,
                sequence=status.latest_sequence,
                timestamp=datetime.now(UTC),
                stage=stage,
                state=status.state,
                counters=(NamedCount(name="cancellation_requested", count=1),)
                if status.cancellation_requested
                else (),
            )
        else:
            event = event.model_copy(update={"sequence": status.latest_sequence})
        self.db.execute(
            "INSERT INTO job_events VALUES (?,?,?,?,?)",
            (
                job_id,
                status.latest_sequence,
                stage,
                event.model_dump_json(),
                event.timestamp.isoformat(),
            ),
        )
        return status.latest_sequence

    def append_event(self, job_id: str, event: JobEvent) -> int:
        event = JobEvent.model_validate(event.model_dump())
        if event.job_id != job_id or len(event.model_dump_json().encode()) > 65536:
            raise TychoraError("INVALID_REQUEST", "Invalid event identity or size")
        with self._write():
            if event.state != self.get(job_id).state:
                _fail("Event state differs from authoritative job state")
            return self._emit(job_id, event.stage, event)

    def events_after(
        self, job_id: str, sequence: int, limit: int
    ) -> tuple[JobEvent, ...]:
        if (
            type(sequence) is not int
            or sequence < 0
            or sequence > 9223372036854775807
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise TychoraError("INVALID_REQUEST", "Invalid event cursor or page limit")
        self.get(job_id)
        rows = self.db.execute(
            "SELECT payload_json FROM job_events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (job_id, sequence, limit),
        ).fetchall()
        return tuple(JobEvent.model_validate_json(row[0]) for row in rows)

    def expire_diagnostics(self, before: datetime) -> int:
        if before.tzinfo is None:
            raise TychoraError(
                "INVALID_REQUEST", "Retention cutoff requires a timezone"
            )
        with self._write():
            for (job_id,) in self.db.execute(
                "SELECT DISTINCT job_id FROM job_events WHERE created_at<?",
                (before.astimezone(UTC).isoformat(),),
            ).fetchall():
                self._meta(job_id, "retention")
            return self.db.execute(
                "DELETE FROM job_events WHERE created_at<?",
                (before.astimezone(UTC).isoformat(),),
            ).rowcount

    def _state(
        self, job_id: str, state: str, stage: str, error_code: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE jobs SET state=?,error_code=?,updated_at=? WHERE job_id=?",
            (state, error_code, _now(), job_id),
        )
        self._emit(job_id, stage)

    def start(self, job_id: str) -> JobStatus:
        status = self.get(job_id)
        self._verify_bindings(status)
        with self._write():
            if self.get(job_id).state != "queued":
                _fail("Only a queued job may start")
            self._state(job_id, "running", "start")
        return self.get(job_id)

    def stage(self, job_id: str, stage: str) -> None:
        """Emit a real stage event after verifying the job's bindings."""
        self._stage(job_id, stage, verify=True)

    def mark(self, job_id: str, stage: str) -> None:
        """Emit a sub-stage event inside the running stage.

        Observes cancellation like ``stage`` but skips the binding verification:
        the enclosing real stage already verified the bindings and leases pin them.
        """
        self._stage(job_id, stage, verify=False)

    def _stage(self, job_id: str, stage: str, *, verify: bool) -> None:
        """Emit a stage event, observing cancellation; optionally verify bindings."""
        if not stage or stage.startswith("__tychora__/"):
            raise TychoraError("INVALID_REQUEST", "Invalid stage key")
        with self._write():
            status = self.get(job_id)
            cancelled = status.cancellation_requested
            if cancelled:
                self._state(job_id, "cancelled", "cancellation")
            else:
                if status.state != "running":
                    _fail("Stage requires a running job")
                if verify:
                    if stage != "acquire" and status.captured_inputs is None:
                        _fail("Commit captured inputs before processing stages")
                    self._verify_bindings(status)
                self._emit(job_id, stage)
        if cancelled:
            raise TychoraError("CANCELLED", "Job was cancelled")

    def cancel(self, job_id: str) -> JobStatus:
        with self._write():
            status = self.get(job_id)
            if status.state == "queued":
                self._state(job_id, "cancelled", "cancellation")
            elif status.state == "running" and not status.cancellation_requested:
                self._state(job_id, "cancelling", "cancellation")
        return self.get(job_id)

    def fail(self, job_id: str, error: TychoraError) -> JobStatus:
        with self._write():
            status = self.get(job_id)
            if status.state not in {"running", "queued"}:
                _fail("Cannot fail a terminal job")
            transient = error.retryable and error.code in {
                "PROVIDER_FAILURE",
                "WORKSPACE_BUSY",
                "SCOPE_CHANGED",
                "DEADLINE_EXCEEDED",
                "JOB_INTERRUPTED",
            }
            self.db.execute(
                "UPDATE job_steps SET state=? WHERE job_id=? AND step_key='__tychora__/retry'",
                ("transient" if transient else "permanent", job_id),
            )
            self._state(
                job_id,
                "cancelled" if status.cancellation_requested else "failed",
                "failure",
                error.code,
            )
        return self.get(job_id)

    def interrupt_removal_jobs(self) -> int:
        """Close orphaned management jobs; their committed effects may remain."""
        with self._write():
            ids = tuple(
                job_id
                for (job_id,) in self.db.execute(
                    "SELECT job_id FROM jobs WHERE operation IN ('remove','purge-originals') "
                    "AND state IN ('running','cancelling') ORDER BY job_id"
                )
            )
            for job_id in ids:
                self.db.execute(
                    "UPDATE job_steps SET state='permanent' WHERE job_id=? "
                    "AND step_key='__tychora__/retry'",
                    (job_id,),
                )
                self._state(
                    job_id,
                    "failed",
                    "interrupted-effect-may-have-committed",
                    "JOB_INTERRUPTED",
                )
        return len(ids)

    def interrupt_orphaned_jobs(self) -> tuple[str, ...]:
        """Settle unfinished jobs of a process that stopped; return their IDs.

        The owner lock is exclusive, so when an owner opens no other process
        runs a job and none of its leases survive. A job whose publication
        committed is reconciled as succeeded, a cancelling one is cancelled and
        any other fails retryably with ``JOB_INTERRUPTED``. A queued job is
        orphaned too: only the accepting process ever runs it. Removal jobs are
        closed first by ``interrupt_removal_jobs``.
        """
        self.interrupt_removal_jobs()
        ids = tuple(
            job_id
            for (job_id,) in self.db.execute(
                "SELECT job_id FROM jobs WHERE state IN ('queued','running','cancelling') "
                "AND operation NOT IN ('remove','purge-originals') ORDER BY job_id"
            ).fetchall()
        )
        for job_id in ids:
            if self.recover(job_id).state == "queued":
                self.fail(job_id, _orphaned())
        return ids

    def retry(self, job_id: str) -> JobStatus:
        reconciled = self._reconcile(job_id)
        if reconciled is not None:
            return reconciled
        status = self.get(job_id)
        self._verify_bindings(status)
        with self._write():
            status = self.get(job_id)
            eligible = self.db.execute(
                "SELECT state,attempt FROM job_steps WHERE job_id=? AND step_key='__tychora__/retry'",
                (job_id,),
            ).fetchone()
            if (
                status.state not in {"failed", "interrupted"}
                or eligible is None
                or eligible[0] != "transient"
                or eligible[1] >= 3
            ):
                _fail("Job is not eligible for another retry")
            self.db.execute(
                "UPDATE job_steps SET attempt=attempt+1,state='durable' WHERE job_id=? AND step_key='__tychora__/retry'",
                (job_id,),
            )
            self._state(job_id, "queued", "retry")
        return self.get(job_id)

    def _step_key(self, step_key: str, input_sha: str) -> None:
        if (
            not step_key
            or step_key.startswith("__tychora__/")
            or len(input_sha) != 64
            or any(c not in "0123456789abcdef" for c in input_sha)
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Invalid checkpoint key or input digest"
            )

    def completed_step(
        self, job_id: str, step_key: str, input_sha: str
    ) -> ArtifactRef | None:
        self._step_key(step_key, input_sha)
        status = self.get(job_id)
        self._verify_bindings(status)
        row = self.db.execute(
            "SELECT input_sha,output_artifact_sha FROM job_steps WHERE job_id=? AND step_key=?",
            (job_id, step_key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != input_sha:
            _fail("Checkpoint input differs")
        ref = self._ref(row[1])
        if ref is None:
            _fail("Checkpoint output is missing")
        self._require_registered((ref,))
        with self.artifacts.open_verified(ref):
            pass
        return ref

    def discard_step(self, job_id: str, step_key: str) -> None:
        """Forget one completed checkpoint so the step can be recomputed.

        Reserved ``__tychora__/`` steps are never discarded. The output
        artifact stays registered; nothing references it afterward.
        """
        if not step_key or step_key.startswith("__tychora__/"):
            raise TychoraError("INVALID_REQUEST", "Invalid checkpoint key")
        with self._write():
            if self.get(job_id).state != "running":
                _fail("Only a running job may discard a checkpoint")
            self.db.execute(
                "DELETE FROM job_steps WHERE job_id=? AND step_key=?",
                (job_id, step_key),
            )

    def checkpoint(
        self, job_id: str, step_key: str, input_sha: str, output: ArtifactRef
    ) -> None:
        self._step_key(step_key, input_sha)
        previous = self.completed_step(job_id, step_key, input_sha)
        if previous is not None:
            if physical_identity(previous) != physical_identity(output):
                _fail("Checkpoint output differs")
            return
        status = self.get(job_id)
        if status.cancellation_requested:
            self.stage(job_id, step_key)
        if status.captured_inputs is None or status.state != "running":
            _fail("Checkpoint requires a running job with committed capture")
        with self.artifacts.open_verified(output), self._write():
            self._register(output)
            self.db.execute(
                "INSERT INTO job_steps (job_id,step_key,input_sha,state,output_artifact_sha) VALUES (?,?,?,'complete',?)",
                (job_id, step_key, input_sha, output.sha256),
            )
            self._emit(job_id, step_key)

    def complete(
        self,
        job_id: str,
        *,
        result: ArtifactRef | None = None,
        result_snapshot_id: str | None = None,
    ) -> JobStatus:
        with ExitStack() as pins:
            if result is not None:
                pins.enter_context(self.artifacts.open_verified(result))
            with self._write():
                status = self.get(job_id)
                if status.state == "succeeded":
                    if (
                        physical_identity(status.result) != physical_identity(result)
                        or status.result_snapshot_id != result_snapshot_id
                    ):
                        _fail("Committed result differs")
                    return status
                if status.state != "running" or status.cancellation_requested:
                    _fail("Only a running job may complete")
                if result_snapshot_id is not None:
                    snapshot = self.db.execute(
                        "SELECT workspace_id,config_sha FROM snapshots WHERE snapshot_id=?",
                        (result_snapshot_id,),
                    ).fetchone()
                    if snapshot != (self.workspace_id, status.effective_config.sha256):
                        _fail("Result snapshot binding differs")
                if result is not None:
                    self._register(result)
                    self._meta(job_id, "result")
                    self.db.execute(
                        "UPDATE job_steps SET output_artifact_sha=? WHERE job_id=? AND step_key='__tychora__/result'",
                        (result.sha256, job_id),
                    )
                self.db.execute(
                    "UPDATE jobs SET result_snapshot_id=? WHERE job_id=?",
                    (result_snapshot_id, job_id),
                )
                self._state(job_id, "complete", "complete")
        return self.get(job_id)

    def _bind_authorization(
        self, job_id: str, scope: RequestScope, capability: str, pins: ExitStack
    ) -> None:
        data = JobAuthorization(
            principal_id=scope.principal_id,
            collection_ids=scope.collection_ids,
            access_ceiling=scope.access_ceiling,
            uses=scope.uses,
            capability=capability,
        ).model_dump(mode="json")
        ref = self.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {"type": "retained_run", "schema_version": "1", "data": data}
                )
            ),
            "application/json",
        )
        pins.enter_context(self.artifacts.open_verified(ref))
        self._register(ref)
        self._meta(job_id, "authorization")
        self.db.execute(
            "UPDATE job_steps SET output_artifact_sha=? WHERE job_id=? AND step_key='__tychora__/authorization'",
            (ref.sha256, job_id),
        )

    def authorize(self, job_id: str, scope: RequestScope, *, write: bool) -> None:
        row = self.db.execute(
            "SELECT output_artifact_sha FROM job_steps WHERE job_id=? AND step_key='__tychora__/authorization'",
            (job_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("FORBIDDEN_SCOPE", "Job is not available in this scope")
        ref = self._ref(row[0])
        if ref is None:
            _fail("Missing durable authorization binding")
        self._require_registered((ref,))
        with self.artifacts.open_verified(ref) as stream:
            try:
                data = JobAuthorization.from_mapping(
                    json.loads(stream.read(MAX_MANIFEST_BYTES + 1))["data"]
                )
            except (ValueError, KeyError, TypeError):
                _fail("Malformed durable authorization binding")
        self._authorize_collections(scope, data.collection_ids)
        if (
            scope.principal_id != data.principal_id
            or not data.collection_ids <= scope.collection_ids
            or not data.uses <= scope.uses
            or scope.access_ceiling < data.access_ceiling
            or (data.capability if write else "read") not in scope.capabilities
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Job is not available in this scope")

    def _authorize_collections(
        self, scope: RequestScope, collection_ids: frozenset[str]
    ) -> None:
        for collection_id in collection_ids:
            row = self.db.execute(
                "SELECT workspace_id,access_floor FROM collections WHERE collection_id=?",
                (collection_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != self.workspace_id
                or row[1] > scope.access_ceiling
            ):
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Job is not available in this scope"
                )

    def _require_snapshot_binding(self, snapshot_id: str, config_sha: str) -> None:
        """Refuse a receipt snapshot of another workspace or configuration.

        A collected snapshot has no row; the job's result stays authoritative.
        """
        snapshot = self.db.execute(
            "SELECT workspace_id,config_sha FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is not None and snapshot != (self.workspace_id, config_sha):
            _fail("Publication receipt snapshot binding differs")

    def _reconcile(self, job_id: str) -> JobStatus | None:
        if self.db.in_transaction:
            _fail("Receipt lookup requires an independent canonical read")
        status = self.get(job_id)
        if self.receipts is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Publication receipt lookup is required"
            )
        receipt = self.receipts.lookup(job_id)
        if receipt is None:
            if status.result_snapshot_id is not None:
                _fail("Committed publication has no receipt")
            return None
        if receipt.job_id != job_id:
            _fail("Publication receipt belongs to a different job")
        self._require_snapshot_binding(
            receipt.result_snapshot_id, status.effective_config.sha256
        )
        if (
            status.result_snapshot_id is not None
            and status.result_snapshot_id != receipt.result_snapshot_id
        ):
            _fail("Publication receipt conflicts with committed result")
        if status.result is not None and physical_identity(
            status.result
        ) != physical_identity(receipt.result_artifact):
            _fail("Publication receipt conflicts with committed artifact")
        if status.state == "succeeded" and (
            physical_identity(status.result)
            != physical_identity(receipt.result_artifact)
            or status.result_snapshot_id != receipt.result_snapshot_id
        ):
            _fail("Publication receipt conflicts with terminal result")
        self._verify_bindings(
            status.model_copy(
                update={
                    "state": "succeeded",
                    "result_snapshot_id": receipt.result_snapshot_id,
                }
            )
        )
        with ExitStack() as pins:
            result = receipt.result_artifact
            if result is not None:
                pins.enter_context(self.artifacts.open_verified(result))
            with self._write():
                if status.state == "succeeded":
                    return status
                if status.state == "cancelled":
                    _fail("Cancelled job has a conflicting publication receipt")
                if result is not None:
                    self._register(result)
                    self._meta(job_id, "result")
                    self.db.execute(
                        "UPDATE job_steps SET output_artifact_sha=? WHERE job_id=? AND step_key='__tychora__/result'",
                        (result.sha256, job_id),
                    )
                self.db.execute(
                    "UPDATE jobs SET result_snapshot_id=? WHERE job_id=?",
                    (receipt.result_snapshot_id, job_id),
                )
                self._state(job_id, "complete", "reconciled")
        return self.get(job_id)

    def recover(self, job_id: str) -> JobStatus:
        reconciled = self._reconcile(job_id)
        if reconciled is not None:
            return reconciled
        status = self.get(job_id)
        self._verify_bindings(status)
        if status.state == "running":
            if status.cancellation_requested:
                with self._write():
                    self._state(job_id, "cancelled", "recovery")
            else:
                self.fail(job_id, _orphaned())
        return self.get(job_id)
