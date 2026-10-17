"""Private, durable PDF receipt lifecycle on the workspace owner worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from functools import wraps

from tychora.application.collection_policy import require_active_collection
from tychora.application.runtime import private_directory
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.pdf_intake import PdfIntakeDescriptor, PdfIntakeStatus
from tychora.contracts.requests import canonical_scope_projection
from tychora.ports.execution import ExecutionContext

ID_PATTERN = r"[0-9a-f]{64}"
ACTIVE = {"prepared", "receiving", "staged", "analysing", "analysed"}


def _operation(method):
    @wraps(method)
    async def admitted(self, *args, **kwargs):
        runtime = self.owner.service_runtime
        with runtime.operation() if runtime else nullcontext():
            return await method(self, *args, **kwargs)

    return admitted


class PdfIntakeService:
    def __init__(self, owner, services, *, max_retained_bytes=1073741824):
        if not 0 < max_retained_bytes <= 1073741824:
            raise TychoraError("INVALID_REQUEST", "Invalid PDF processing budget.")
        self.owner = owner
        self.services = services
        self.max_retained_bytes = max_retained_bytes
        self.directory = owner.path / ".pdf-intakes"
        self._bindings = {}
        self._binding_users = {}
        self._ready = False
        if getattr(owner, "pdf_intakes", None) is None:
            owner.pdf_intakes = self

    @property
    def byte_ceiling(self):
        limits = self.owner.config.values.acquisition.limits
        return min(104857600, limits.max_file_bytes, limits.max_total_bytes)

    def _ensure_analysis_recovered(self):
        # A previous owner may have left a live child reading this copy. Never
        # delete it or reset its storage accounting by admitting further inputs.
        with os.scandir(self.directory) as entries:
            if any(entry.name.startswith(".analysis-") for entry in entries):
                raise TychoraError(
                    "WORKSPACE_BUSY", "PDF analysis recovery is required."
                )

    def _initialize(self):
        if self._ready:
            return
        private_directory(self.directory)
        private_directory(self.directory / "history")
        for path in self.directory.glob("*.json"):
            if not re.fullmatch(ID_PATTERN + r"\.json", path.name):
                continue
            record = self._reconcile_handoff(self._read(path.stem))
            if record["state"] in {"receiving", "analysing"}:
                record["state"] = "aborted"
            if record["state"] not in ACTIVE:
                self._write(record)
                self._unlink(record)
        self._ready = True
        self._reap()

    def _reconcile_handoff(self, record, *, reject_conflict=False):
        """An accepted retained capture outranks the unsubmitted receipt TTL."""
        if (
            record.get("submission") is None
            or record.get("job") is not None
            or record["workspace_id"] != self.owner.workspace_id
            or record["principal_id"] != self.owner.scope.principal_id
        ):
            return record
        row = self.owner.db.execute(
            "SELECT job_id FROM jobs WHERE principal_id=? AND operation='add' AND idempotency_key=?",
            (record["principal_id"], record["submission"]["key"]),
        ).fetchone()
        if row is None:
            return record
        from tychora.application.jobs import source_intent

        services = (
            self.owner.service_runtime.services
            if self.owner.service_runtime
            else self.services
        )
        status = services.jobs.repository.get(row[0])
        # Intent exists before CAS promotion. Its identity must not depend on a
        # trusted AcquiredSource or an original_ref that may not exist yet.
        source = self._source(record)
        if status.request_digest != digest(
            "request-v1",
            self.owner.workspace_id,
            record["principal_id"],
            "add",
            source_intent(source),
        ):
            if reject_conflict:
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT",
                    "PDF handoff is bound to different job input.",
                )
            return record
        if status.captured_inputs is None:
            return record
        capture = services.sources._committed_capture(
            status.captured_inputs, historical=True
        )
        if (
            capture.original_sha != record["original_sha256"]
            or capture.config_sha != record["config_sha"]
            or canonical_bytes(capture.source.get("pdf_processing"))
            != canonical_bytes(record["analysis"]["proposed_policy"])
        ):
            if reject_conflict:
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT", "PDF handoff capture differs."
                )
            return record
        record.update(
            state="submitted",
            original_ref=capture.original_ref.model_dump(mode="json"),
            job={"id": status.id, "operation": status.operation, "state": status.state},
        )
        self._write(record)
        self._unlink(record)
        if not self._binding_users.get(record.get("binding")):
            self._bindings.pop(record.get("binding"), None)
        return record

    def _read(self, intake_id):
        if not re.fullmatch(ID_PATTERN, intake_id):
            raise TychoraError("FORBIDDEN_SCOPE", "PDF intake is unavailable.")
        try:
            # Archive wins if a crash happened after rename but before active unlink.
            try:
                fd = os.open(
                    self.directory / "history" / (intake_id + ".json"),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except FileNotFoundError:
                fd = os.open(
                    self.directory / (intake_id + ".json"),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > 1048576
                ):
                    raise ValueError
                record = json.load(stream)
            if record["intake_id"] != intake_id:
                raise ValueError
            return record
        except FileNotFoundError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "PDF intake is unavailable."
            ) from None
        except (OSError, ValueError, KeyError):
            raise TychoraError("INTEGRITY_ERROR", "PDF receipt is invalid.") from None

    def _write(self, record):
        destination = (
            self.directory if record["state"] in ACTIVE else self.directory / "history"
        )
        temp = destination / (".record-" + secrets.token_hex(16))
        try:
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination / (record["intake_id"] + ".json"))
            directory_fd = os.open(
                destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            if destination != self.directory:
                (self.directory / (record["intake_id"] + ".json")).unlink(
                    missing_ok=True
                )
                active_fd = os.open(
                    self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    os.fsync(active_fd)
                finally:
                    os.close(active_fd)
        finally:
            temp.unlink(missing_ok=True)

    def _unlink(self, record):
        (self.directory / (record["intake_id"] + ".pdf")).unlink(missing_ok=True)

    def _reap(self):
        for path in self.directory.glob("*.json"):
            if not re.fullmatch(ID_PATTERN + r"\.json", path.name):
                continue
            record = self._reconcile_handoff(self._read(path.stem))
            if (
                record["state"] in ACTIVE - {"receiving", "analysing"}
                and record["expires"] < time.time()
            ):
                record["state"] = "expired"
                self._write(record)
                self._unlink(record)

    def _grant(self, descriptor, scope, *, active=True):
        granted = self.owner.scope.narrow(scope)
        cid = str(descriptor.collection_id)
        if "write" not in granted.capabilities or cid not in granted.collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "PDF destination is outside the grant."
            )
        row = self.owner.db.execute(
            "SELECT workspace_id,access_floor,policy_json FROM collections WHERE collection_id=?",
            (cid,),
        ).fetchone()
        if (
            row is None
            or row[0] != self.owner.workspace_id
            or row[1] > granted.access_ceiling
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "PDF destination is unavailable.")
        if active:
            require_active_collection(row[2])
        self.owner.store.registry.validate_metadata(
            descriptor.kind, descriptor.metadata
        )
        return granted

    def _authorized(self, intake_id, scope, *, current=True):
        self._initialize()
        record = self._read(intake_id)
        descriptor = PdfIntakeDescriptor.model_validate_json(
            canonical_bytes(record["descriptor"])
        )
        granted = self._grant(descriptor, scope, active=current)
        if record["workspace_id"] != self.owner.workspace_id or record[
            "grant"
        ] != digest("pdf-grant-v1", canonical_scope_projection(granted)):
            raise TychoraError("FORBIDDEN_SCOPE", "PDF intake is outside the grant.")
        if current and record["config_sha"] != self.owner.config.config_sha:
            raise TychoraError("SCOPE_CHANGED", "PDF intake configuration changed.")
        record = self._reconcile_handoff(record)
        if (
            record["state"] in ACTIVE - {"receiving", "analysing"}
            and record["expires"] < time.time()
        ):
            record["state"] = "expired"
            self._write(record)
            self._unlink(record)
        return record

    def _status(self, record):
        return PdfIntakeStatus.model_validate_json(
            canonical_bytes(
                {
                    "intake_id": record["intake_id"],
                    "state": record["state"],
                    "byte_ceiling": record["byte_ceiling"],
                    "received_bytes": record["received_bytes"],
                    "expires_at": datetime.fromtimestamp(
                        record["expires"], UTC
                    ).isoformat(),
                    "analysis": record.get("analysis"),
                    "job": record.get("job"),
                    "error": record.get("error"),
                }
            )
        )

    @_operation
    async def prepare(self, descriptor, scope, ctx):
        def prepare():
            self._initialize()
            self._reap()
            granted = self._grant(descriptor, scope)
            self._ensure_analysis_recovered()
            if descriptor.expected_size_bytes > min(
                self.byte_ceiling, self.max_retained_bytes
            ):
                raise TychoraError(
                    "LIMIT_EXCEEDED", "PDF exceeds the configured input limit."
                )
            active = [
                self._read(path.stem)
                for path in self.directory.glob("*.json")
                if re.fullmatch(ID_PATTERN + r"\.json", path.name)
            ]
            active = [item for item in active if item["state"] in ACTIVE]
            if (
                len(active) >= 8
                or sum(item["principal_id"] == granted.principal_id for item in active)
                >= 4
                or sum(item["byte_ceiling"] for item in active)
                + descriptor.expected_size_bytes
                > 536870912
            ):
                raise TychoraError(
                    "LIMIT_EXCEEDED", "PDF intake reservation capacity is exhausted."
                )
            record = {
                "intake_id": secrets.token_hex(32),
                "state": "prepared",
                "workspace_id": self.owner.workspace_id,
                "principal_id": granted.principal_id,
                "grant": digest("pdf-grant-v1", canonical_scope_projection(granted)),
                "descriptor": descriptor.model_dump(mode="json"),
                "config_sha": self.owner.config.config_sha,
                "byte_ceiling": descriptor.expected_size_bytes,
                "received_bytes": 0,
                "expires": time.time() + 1800,
            }
            self._write(record)
            return self._status(record)

        return await self.owner.run_blocking(prepare, ctx)

    @_operation
    async def inspect(self, intake_id, scope, ctx):
        return await self.owner.run_blocking(
            lambda: self._status(self._authorized(intake_id, scope, current=False)), ctx
        )

    @_operation
    async def admit_content(self, intake_id, scope, declared, ctx):
        def check():
            record = self._authorized(intake_id, scope)
            if record["state"] != "prepared":
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT",
                    "PDF intake cannot receive content in this state.",
                )
            if declared is not None and declared > min(
                record["byte_ceiling"], self.byte_ceiling
            ):
                raise TychoraError(
                    "LIMIT_EXCEEDED", "PDF exceeds its reserved byte limit."
                )
            return min(record["byte_ceiling"], self.byte_ceiling)

        return await self.owner.run_blocking(check, ctx)

    @_operation
    async def receive(self, intake_id, chunks, scope, ctx):
        record = None
        fd = -1
        begun = False

        def begin():
            nonlocal record, fd, begun
            record = self._authorized(intake_id, scope)
            if record["state"] != "prepared":
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT",
                    "PDF intake cannot receive content in this state.",
                )
            fd = os.open(
                self.directory / (intake_id + ".pdf"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            begun = True
            record["state"] = "receiving"
            self._write(record)
            return record, fd

        hasher = hashlib.sha256()
        received = 0

        def remaining(current):
            seconds = current["expires"] - time.time()
            if seconds <= 0:
                raise TychoraError("SCOPE_CHANGED", "PDF intake expired during upload.")
            return seconds

        try:
            record, fd = await self.owner.run_blocking(begin, ctx)
            iterator = aiter(chunks)
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(iterator), timeout=min(60, remaining(record))
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    remaining(record)  # Distinguish absolute expiry from idle timeout.
                    raise
                received += len(chunk)
                if received > min(record["byte_ceiling"], self.byte_ceiling):
                    raise TychoraError(
                        "LIMIT_EXCEEDED", "PDF upload exceeds its byte limit."
                    )

                def write(chunk=chunk):
                    current = self._authorized(intake_id, scope)
                    remaining(current)
                    ctx.checkpoint()
                    view = memoryview(chunk)
                    while view:
                        remaining(current)
                        ctx.checkpoint()
                        written = os.write(fd, view)
                        view = view[written:]
                    hasher.update(chunk)

                await self.owner.run_blocking(write, ctx)

            def finish():
                nonlocal fd
                assert record is not None
                current = self._authorized(intake_id, scope)
                remaining(current)
                if received != record["descriptor"]["expected_size_bytes"]:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "PDF upload length differs from the prepared input.",
                    )
                sha = hasher.hexdigest()
                if record["descriptor"].get("expected_sha256") not in (None, sha):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "PDF upload checksum differs."
                    )
                os.fsync(fd)
                info = os.fstat(fd)
                os.close(fd)
                fd = -1
                record.update(
                    state="staged",
                    received_bytes=received,
                    original_sha256=sha,
                    stamp=[
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    ],
                )
                self._write(record)
                return self._status(record)

            return await self.owner.run_blocking(finish, ctx)
        except BaseException as error:

            def cleanup():
                nonlocal fd
                if fd >= 0:
                    os.close(fd)
                    fd = -1
                if record is None or not begun:
                    return
                record.update(state="aborted", received_bytes=received)
                try:
                    self._write(record)
                finally:
                    self._unlink(record)

            await self.owner.run_blocking(cleanup, ExecutionContext())
            if isinstance(error, TimeoutError):
                raise TychoraError(
                    "DEADLINE_EXCEEDED", "PDF upload idle deadline exceeded."
                ) from None
            if isinstance(error, OSError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "PDF upload could not be retained."
                ) from None
            raise

    @_operation
    async def abort(self, intake_id, scope, ctx):
        def abort():
            record = self._authorized(intake_id, scope, current=False)
            if record["state"] in {"receiving", "analysing", "submitted"}:
                raise TychoraError(
                    "WORKSPACE_BUSY", "PDF intake has active or submitted work."
                )
            if record["state"] not in {"expired", "aborted"}:
                record["state"] = "aborted"
                self._write(record)
                self._unlink(record)
            return self._status(record)

        return await self.owner.run_blocking(abort, ctx)

    def _verified_original(self, record):
        """Return a no-follow fd only after checking the reserved inode and hash."""
        try:
            fd = os.open(
                self.directory / (record["intake_id"] + ".pdf"),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
            info = os.fstat(fd)
            stamp = [
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stamp != record["stamp"]
            ):
                os.close(fd)
                raise TychoraError("INTEGRITY_ERROR", "Staged PDF identity changed.")
            with os.fdopen(fd, "rb") as stream:
                hasher = hashlib.sha256()
                size = 0
                while block := stream.read(
                    min(1048576, record["byte_ceiling"] - size + 1)
                ):
                    size += len(block)
                    if size > record["byte_ceiling"]:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Staged PDF size changed."
                        )
                    hasher.update(block)
                after = os.fstat(stream.fileno())
                if (
                    hasher.hexdigest() != record["original_sha256"]
                    or size != record["received_bytes"]
                    or [
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ]
                    != record["stamp"]
                ):
                    raise TychoraError("INTEGRITY_ERROR", "Staged PDF bytes changed.")
                stream.seek(0)
                return os.dup(stream.fileno())
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Staged PDF is unavailable."
            ) from None

    @_operation
    async def analyse(self, intake_id, scope, ctx):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from tychora.application.pdf_processing import PDF_LIMITS, resolve_pdf_policy
        from tychora.contracts.pdf_intake import PdfAnalysis

        def analyse():
            record = self._authorized(intake_id, scope)
            self._ensure_analysis_recovered()
            if record["state"] == "analysed":
                return self._status(record)
            if record["state"] != "staged":
                raise TychoraError("IDEMPOTENCY_CONFLICT", "PDF intake is not staged.")
            record["state"] = "analysing"
            self._write(record)
            try:
                with TemporaryDirectory(
                    prefix=".analysis-", dir=self.directory
                ) as temporary:
                    path = Path(temporary) / "original.pdf"
                    # Independent bounded copy fences child reads from staging mutation.
                    with (
                        os.fdopen(self._verified_original(record), "rb") as original,
                        path.open("xb") as copy,
                    ):
                        copied = 0
                        copy_hash = hashlib.sha256()
                        while block := original.read(
                            min(1048576, record["byte_ceiling"] - copied + 1)
                        ):
                            ctx.checkpoint()
                            copied += len(block)
                            if copied > record["byte_ceiling"]:
                                raise TychoraError(
                                    "INTEGRITY_ERROR", "Staged PDF size changed."
                                )
                            copy_hash.update(block)
                            copy.write(block)
                        if (
                            copied != record["received_bytes"]
                            or copy_hash.hexdigest() != record["original_sha256"]
                        ):
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Staged PDF bytes changed."
                            )
                    facts = _isolated_analysis(path, PDF_LIMITS, ctx)
                record = self._authorized(intake_id, scope)
                descriptor = PdfIntakeDescriptor.model_validate_json(
                    canonical_bytes(record["descriptor"])
                )
                services = (
                    self.owner.service_runtime.services
                    if self.owner.service_runtime
                    else self.services
                )
                policy, blockers = resolve_pdf_policy(
                    self.owner,
                    services,
                    descriptor,
                    facts,
                    max_retained_bytes=self.max_retained_bytes,
                )
                data = {
                    **facts,
                    "original_sha256": record["original_sha256"],
                    "size_bytes": record["received_bytes"],
                    "proposed_policy": policy.model_dump(mode="json")
                    if policy
                    else None,
                    "blockers": blockers,
                }
                from tychora.application.pdf_processing import policy_sha256

                data["policy_sha256"] = (
                    policy_sha256(policy) if policy is not None else None
                )
                data["analysis_sha256"] = digest(
                    "pdf-analysis-v1",
                    record["config_sha"],
                    record["grant"],
                    record["descriptor"],
                    data,
                )
                analysis = PdfAnalysis.model_validate_json(canonical_bytes(data))
                record.update(
                    state="analysed", analysis=analysis.model_dump(mode="json")
                )
                self._write(record)
                return self._status(record)
            except BaseException as error:
                record["state"] = "failed"
                if isinstance(error, TychoraError):
                    record["error"] = json.loads(error.to_json())
                self._write(record)
                self._unlink(record)
                raise

        return await self.owner.run_blocking(analyse, ctx)

    def _source(self, record):
        from tychora.contracts.requests import SourceInput

        descriptor = record["descriptor"]
        return SourceInput.from_mapping(
            {
                key: descriptor[key]
                for key in ("source_key", "collection_id", "kind", "metadata", "format")
            }
            | {
                "type": "upload",
                "content_sha256": record["original_sha256"],
                "size_bytes": record["received_bytes"],
                "pdf_processing": record["analysis"]["proposed_policy"],
            }
        )

    def _acquired(self, record, scope):
        from tychora.application.acquired_source import AcquiredSource
        from tychora.contracts.artifacts import ArtifactRef
        from tychora.contracts.pdf_policy import PdfProcessingPolicy

        self._bindings[record["binding"]] = record["intake_id"]
        return AcquiredSource(
            workspace_id=self.owner.workspace_id,
            scope=self.owner.scope.narrow(scope),
            source=self._source(record),
            original_ref=ArtifactRef.model_validate_json(
                canonical_bytes(record["original_ref"])
            ),
            receipt_binding_sha256=record["binding"],
            policy=PdfProcessingPolicy.model_validate_json(
                canonical_bytes(record["analysis"]["proposed_policy"])
            ),
        )

    def validate_acquired(self, acquired, scope):
        """Owner-worker-only proof lookup; no public CAS identifier is sufficient."""
        if (
            acquired.workspace_id != self.owner.workspace_id
            or acquired.scope != self.owner.scope.narrow(scope)
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Acquired PDF is outside this owner grant."
            )
        self._initialize()
        intake_id = self._bindings.get(acquired.receipt_binding_sha256)
        if intake_id is None:
            raise TychoraError("FORBIDDEN_SCOPE", "Trusted PDF receipt is unavailable.")
        record = self._authorized(intake_id, scope)
        if (
            record["state"] not in {"analysed", "submitted"}
            or self._acquired(record, scope) != acquired
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Acquired PDF receipt binding differs."
            )
        self._validate_submission(record, record["submission"])

    def _validate_submission(self, record, submission):
        from tychora.application.pdf_processing import (
            policy_sha256,
            revalidate_pdf_policy,
        )
        from tychora.contracts.pdf_policy import PdfProcessingPolicy

        analysis = record.get("analysis")
        if (
            analysis is None
            or analysis["analysis_sha256"] != submission["analysis_sha256"]
        ):
            raise TychoraError("IDEMPOTENCY_CONFLICT", "PDF analysis identity differs.")
        # Recompute the complete receipt-bound analysis identity, not just its public echo.
        payload = {
            key: value for key, value in analysis.items() if key != "analysis_sha256"
        }
        if analysis["analysis_sha256"] != digest(
            "pdf-analysis-v1",
            record["config_sha"],
            record["grant"],
            record["descriptor"],
            payload,
        ):
            raise TychoraError("INTEGRITY_ERROR", "PDF analysis binding differs.")
        if analysis["proposed_policy"] is None or analysis["blockers"]:
            raise TychoraError(
                "CAPABILITY_MISSING", "PDF processing prerequisites are unavailable."
            )
        policy = PdfProcessingPolicy.model_validate_json(
            canonical_bytes(analysis["proposed_policy"])
        )
        if "policy_sha256" in analysis and analysis["policy_sha256"] != policy_sha256(
            policy
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Advertised PDF policy identity differs."
            )
        if policy_sha256(policy) != submission["policy_sha256"]:
            raise TychoraError("IDEMPOTENCY_CONFLICT", "PDF processing policy differs.")
        if (
            len(policy.native_pages) + len(policy.ocr_pages) + len(policy.blank_pages)
            != analysis["page_count"]
        ):
            raise TychoraError("INTEGRITY_ERROR", "PDF page coverage differs.")
        descriptor = PdfIntakeDescriptor.model_validate_json(
            canonical_bytes(record["descriptor"])
        )
        services = (
            self.owner.service_runtime.services
            if self.owner.service_runtime
            else self.services
        )
        revalidate_pdf_policy(self.owner, services, descriptor, analysis, policy)

    @_operation
    async def submit(self, intake_id, submission, scope, key, ctx):
        services = (
            self.owner.service_runtime.services
            if self.owner.service_runtime
            else self.services
        )
        pins = []
        bindings = []

        def promote():
            record = self._authorized(intake_id, scope)
            record = self._reconcile_handoff(record, reject_conflict=True)
            if record["state"] not in {"analysed", "submitted"}:
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT", "PDF intake is not analysed."
                )
            selected = submission.model_dump(mode="json")
            handoff = {**selected, "key": key or "pdf-intake:" + intake_id}
            if record.get("submission") not in (None, handoff):
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT",
                    "PDF submission is bound to different input or key.",
                )
            self._validate_submission(record, selected)
            if record.get("job"):
                return record, None
            # Persist the deterministic handoff before either CAS promotion or job acceptance.
            record["submission"] = handoff
            record["binding"] = digest(
                "pdf-receipt-binding-v1",
                self.owner.workspace_id,
                record["grant"],
                record["descriptor"],
                record["config_sha"],
                record["original_sha256"],
                handoff,
            )
            self._write(record)
            pin = self.owner.artifacts.pin_payloads(
                frozenset({record["original_sha256"]})
            )
            pin.__enter__()
            pins.append(pin)
            with os.fdopen(self._verified_original(record), "rb") as original:
                ref = self.owner.artifacts.put_stream(original, "application/pdf")
            if (
                ref.sha256 != record["original_sha256"]
                or ref.size_bytes != record["received_bytes"]
            ):
                raise TychoraError("INTEGRITY_ERROR", "Promoted PDF original differs.")
            record["original_ref"] = ref.model_dump(mode="json")
            self._write(record)
            bindings.append(record["binding"])
            self._binding_users[record["binding"]] = (
                self._binding_users.get(record["binding"], 0) + 1
            )
            return record, self._acquired(record, scope)

        try:
            record, acquired = await self.owner.run_blocking(promote, ctx)
            if acquired is None:
                status = await services.jobs_for_scope(scope).get(
                    record["job"]["id"], ctx
                )
                from tychora.contracts.operations import JobHandle

                return JobHandle(
                    id=status.id, operation=status.operation, state=status.state
                )
            handle = await services.source_jobs.add_acquired_async(
                acquired, scope, record["submission"]["key"]
            )

            def committed():
                current = self._authorized(intake_id, scope)
                status = services.jobs.repository.get(handle.id)
                if status.captured_inputs is None:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Accepted PDF job has no retained capture."
                    )
                current.update(state="submitted", job=handle.model_dump(mode="json"))
                self._write(current)
                self._unlink(current)

            await self.owner.run_blocking(committed, ctx)
            return handle
        finally:

            def release():
                for pin in reversed(pins):
                    pin.__exit__(None, None, None)
                for binding in bindings:
                    remaining = self._binding_users[binding] - 1
                    if remaining:
                        self._binding_users[binding] = remaining
                    else:
                        self._binding_users.pop(binding, None)
                        self._bindings.pop(binding, None)

            await self.owner.run_blocking(release, ExecutionContext())


def _analysis_child(path, limits, sender):
    from tychora.ingestion.pdf_analysis import analyse_pdf

    try:
        sender.send((True, dict(analyse_pdf(path, limits))))
    except TychoraError as error:
        sender.send((False, (error.code, error.message, error.reason)))
    except Exception:  # noqa: BLE001 - sanitize failures at the child boundary
        sender.send((False, ("INVALID_REQUEST", "PDF analysis failed.", None)))
    finally:
        sender.close()


def _child_error(result: tuple[str, str, str | None]) -> TychoraError:
    """Rebuild a child's typed failure, reason included."""
    code, message, reason = result
    return TychoraError(code, message, reason=reason)


def _isolated_analysis(path, limits, ctx):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_analysis_child, args=(path, limits, sender))
    deadline = time.monotonic() + min(60, limits["analysis_timeout_seconds"])
    try:
        child.start()
        sender.close()
        while not receiver.poll(0.05):
            ctx.checkpoint()
            if time.monotonic() >= deadline:
                raise TychoraError(
                    "DEADLINE_EXCEEDED", "PDF analysis deadline exceeded."
                )
            if not child.is_alive():
                raise TychoraError("INVALID_REQUEST", "PDF analysis process failed.")
        try:
            success, result = receiver.recv()
        except EOFError:
            raise TychoraError(
                "INVALID_REQUEST", "PDF analysis process failed."
            ) from None
        child.join(timeout=max(0, deadline - time.monotonic()))
        if child.is_alive():
            raise TychoraError("DEADLINE_EXCEEDED", "PDF analysis deadline exceeded.")
        if not success:
            raise _child_error(result)
        if child.exitcode != 0:
            raise TychoraError("INVALID_REQUEST", "PDF analysis process failed.")
        return result
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join()
            child.close()
        receiver.close()
        sender.close()
