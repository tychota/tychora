"""Owner-authorized source capture, extraction and snapshot publication."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from tychora.adapters.code.normalize import (
    normalize_code,
    replay_code_extraction,
    retain_code_extraction,
)
from tychora.adapters.code.python import PythonAdapter
from tychora.adapters.code.syntax import base_syntax_result
from tychora.adapters.code.typescript import TypeScriptAdapter
from tychora.adapters.docling.parser import DoclingParser
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.contracts.code import CapturedSyntax, CodeRegistration
from tychora.contracts.composition import DocumentCompositionPlan
from tychora.contracts.corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.errors import TychoraError
from tychora.contracts.extraction import ExtractionArtifact, NativeRegistration
from tychora.contracts.identity import canonical_bytes, digest, revision_id, source_id
from tychora.contracts.operations import JobHandle
from tychora.contracts.publication import PublicationPlan
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.composition import compose_document
from tychora.ingestion.documents import replay_document
from tychora.ingestion.native import NativeParser
from tychora.ingestion.normalize import normalize
from tychora.ports.acquisition import UrlReader
from tychora.ports.execution import ExecutionContext

from .jobs import source_intent
from .owner import WorkspaceOwner

if TYPE_CHECKING:
    from tychora.contracts.acquisition import CaptureManifest
    from tychora.contracts.artifacts import ArtifactRef
    from tychora.contracts.corpus import ExtractionBundle
    from tychora.indexing.coordinator import PreparedProjection, ProjectionCoordinator
    from tychora.ports.language import SyntaxResult


def _parse_code_file(paths: tuple[Path, ...], relative_path: str) -> SyntaxResult:
    payload = paths[0].read_bytes()
    suffix = Path(relative_path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return PythonAdapter().parse(payload, {})
    dialect = {
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".jsx": "jsx",
    }.get(suffix)
    if dialect is not None:
        return TypeScriptAdapter(
            cast(Literal["typescript", "tsx", "javascript", "jsx"], dialect)
        ).parse(payload, {})
    return base_syntax_result(
        language="unknown",
        grammar="unsupported",
        adapter_fingerprint=digest("unsupported-adapter-v1"),
        file_bytes=payload,
        text=payload.decode("latin-1"),
        encoding="latin-1",
        coverage="text_fallback",
        options={},
        root_type="unsupported_file",
    )


class SourceService:
    """Compose one owner lifecycle; provider calls always use owner authorization."""

    def __init__(
        self,
        owner: WorkspaceOwner,
        coordinator: ProjectionCoordinator,
        *,
        url_reader: UrlReader | None = None,
        docling_models: Path | None = None,
    ) -> None:
        if (
            coordinator.store is not owner.store
            or coordinator.artifacts is not owner.artifacts
        ):
            raise ValueError("Source service requires the owner's canonical ports")
        self.owner = owner
        self.coordinator = coordinator
        self.url_reader = url_reader
        self.docling_models = docling_models
        self._jobs: JobRepository | None = None
        self._acquirer: Acquirer | None = None

    async def _ports(self, ctx: ExecutionContext) -> tuple[JobRepository, Acquirer]:
        def initialize() -> tuple[JobRepository, Acquirer]:
            if self._jobs is None:
                self._jobs = JobRepository(
                    self.owner.db,
                    artifacts=self.owner.artifacts,
                    workspace_id=self.owner.workspace_id,
                    receipts=PublicationReceipts(self.owner.store),
                    operation_capabilities={"sync": "write"},
                )
            if self._acquirer is None:
                self._acquirer = Acquirer(
                    self.owner.artifacts, self.owner.config, urls=self.url_reader
                )
                if self.url_reader is not None and hasattr(self.url_reader, "close"):
                    self.owner.register_close(
                        cast(Callable[[], None], self.url_reader.close)
                    )
            return self._jobs, self._acquirer

        return await self.owner.run_blocking(initialize, ctx)

    async def _config_ref(self, ctx: ExecutionContext):
        return await self.owner.run_blocking(
            lambda: self.owner.artifacts.put_stream(
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
            ),
            ctx,
        )

    def _committed_capture(self, ref: ArtifactRef) -> CaptureManifest:
        from tychora.contracts.acquisition import CaptureManifest

        with self.owner.artifacts.open_verified(ref) as stream:
            manifest = json.load(stream)
        if (
            manifest.get("type") != "job_capture"
            or manifest.get("schema_version") != "1"
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Committed capture manifest type differs"
            )
        data = dict(manifest["data"])
        if data.pop("entries_partitioned", False):
            entries = []
            for link in manifest.get("required_manifests", []):
                if link.get("type") != "job_capture":
                    continue
                from tychora.contracts.artifacts import ArtifactRef

                child_ref = ArtifactRef.model_validate(link["artifact"])
                with self.owner.artifacts.open_verified(child_ref) as stream:
                    child = json.load(stream)
                if (
                    child.get("type") != "job_capture"
                    or child.get("schema_version") != "1"
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Capture partition type differs"
                    )
                entries.extend(child.get("data", {}).get("entries", []))
            data["entries"] = entries
        data["manifest_ref"] = ref.model_dump(mode="json")
        capture = CaptureManifest.model_validate_json(canonical_bytes(data))
        if capture.config_sha != self.owner.config.config_sha:
            raise TychoraError(
                "INTEGRITY_ERROR", "Committed capture configuration differs"
            )
        return capture

    @staticmethod
    def _plan_step(base: str | None, watermark: int | None = None) -> str:
        step = "projection/plan/" + (base or "genesis")
        return step if watermark is None else f"{step}/decision/{watermark}"

    @staticmethod
    def _plan_input(
        capture: CaptureManifest,
        config_ref: ArtifactRef,
        base: str | None,
        watermark: int | None = None,
    ) -> str:
        parts = (
            capture.manifest_ref.sha256,
            config_ref.sha256,
            base,
        )
        return digest(
            "projection-plan-input-v1",
            *(parts if watermark is None else (*parts, watermark)),
        )

    def _decision_watermark(self) -> int | None:
        if self.owner.config.values.index.graph == "disabled":
            return None
        return self.owner.db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]

    @staticmethod
    def _plan_watermark(plan: PublicationPlan) -> int | None:
        for slot in plan.slots:
            if slot.slot == "graph":
                from tychora.contracts.graph import GraphBatchReceipt

                receipt = slot.batches[0].receipt
                if not isinstance(receipt, GraphBatchReceipt):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Graph plan receipt is malformed"
                    )
                return receipt.decision_watermark
        return None

    def _completed_plan(
        self,
        jobs: JobRepository,
        handle: JobHandle,
        capture: CaptureManifest,
        config_ref: ArtifactRef,
        base: str | None,
        watermark: int | None,
    ) -> PublicationPlan | None:
        ref = jobs.completed_step(
            handle.id,
            self._plan_step(base, watermark),
            self._plan_input(capture, config_ref, base, watermark),
        )
        if ref is None:
            return None
        with self.owner.artifacts.open_verified(ref) as stream:
            manifest = json.load(stream)
        try:
            plan = PublicationPlan.model_validate_json(
                canonical_bytes(
                    {**manifest["data"], "manifest": ref.model_dump(mode="json")}
                )
            )
        except (KeyError, TypeError, ValueError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Committed projection plan is malformed"
            ) from None
        if (
            plan.job_id != handle.id
            or plan.base_snapshot_id != base
            or plan.config_sha != config_ref.sha256
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Committed projection plan input differs"
            )
        return plan

    async def add(
        self,
        source: SourceInput,
        scope: RequestScope,
        idempotency_key: str | None = None,
        *,
        ctx: ExecutionContext | None = None,
    ) -> JobHandle:
        return await self._run(
            "add", source, scope, idempotency_key, ctx or ExecutionContext()
        )

    async def accept_add(
        self,
        source: SourceInput,
        scope: RequestScope,
        idempotency_key: str | None = None,
        *,
        ctx: ExecutionContext | None = None,
    ) -> JobHandle:
        return await self._accept(
            "add", source, scope, idempotency_key, ctx or ExecutionContext()
        )

    async def accept_sync(
        self,
        source_id_value: str,
        scope: RequestScope,
        idempotency_key: str | None = None,
        *,
        ctx: ExecutionContext | None = None,
    ) -> tuple[JobHandle, SourceInput]:
        context = ctx or ExecutionContext()
        source = await self.owner.run_blocking(
            lambda: self._source_for_sync(source_id_value, scope), context
        )
        return (
            await self._accept("sync", source, scope, idempotency_key, context),
            source,
        )

    async def sync(
        self,
        source_id_value: str,
        scope: RequestScope,
        idempotency_key: str | None = None,
        *,
        ctx: ExecutionContext | None = None,
    ) -> JobHandle:
        context = ctx or ExecutionContext()
        source = await self.owner.run_blocking(
            lambda: self._source_for_sync(source_id_value, scope), context
        )
        return await self._run("sync", source, scope, idempotency_key, context)

    def _source_for_sync(
        self, source_id_value: str, scope: RequestScope
    ) -> SourceInput:
        granted = self.owner.scope.narrow(scope)
        row = self.owner.db.execute(
            "SELECT s.collection_id,s.registration_json,r.kind_id,r.metadata_json,r.original_sha "
            "FROM sources s JOIN revisions r USING(source_id) "
            "WHERE s.source_id=? AND s.collection_id IN (SELECT collection_id FROM collections WHERE workspace_id=?) "
            "ORDER BY r.captured_at DESC LIMIT 1",
            (source_id_value, self.owner.workspace_id),
        ).fetchone()
        if (
            row is None
            or row[0] not in granted.collection_ids
            or "write" not in granted.capabilities
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Registered source is unavailable")
        registration = json.loads(row[1])
        registration.update({"kind": row[2], "metadata": json.loads(row[3])})
        if registration["type"] in {"text", "bytes"}:
            artifact = self.owner.db.execute(
                "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM revision_artifacts ra JOIN artifacts a ON a.artifact_sha=ra.artifact_sha "
                "JOIN revisions r ON r.revision_id=ra.revision_id "
                "WHERE r.source_id=? AND r.original_sha=? AND ra.role='original' LIMIT 1",
                (source_id_value, row[4]),
            ).fetchone()
            if artifact is None:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Registered inline original is unavailable"
                )
            from tychora.contracts.artifacts import ArtifactRef

            ref = ArtifactRef(
                sha256=artifact[0],
                size_bytes=artifact[1],
                media_type=artifact[2],
                storage_key=artifact[3],
            )
            with self.owner.artifacts.open_verified(ref) as stream:
                payload = stream.read(ref.size_bytes + 1)
            if registration["type"] == "text":
                registration["text"] = payload.decode("utf-8")
            else:
                registration["data"] = payload
                registration["collection_id"] = UUID(registration["collection_id"])
                registration["sidecars"] = tuple(registration.get("sidecars", ()))
                return SourceInput.model_validate(registration)
        return SourceInput.from_mapping(registration)

    async def _run(
        self,
        operation: str,
        source: SourceInput,
        scope: RequestScope,
        key: str | None,
        ctx: ExecutionContext,
    ) -> JobHandle:
        handle = await self._accept(operation, source, scope, key, ctx)
        return await self.execute_accepted(handle, source, scope, ctx=ctx)

    async def _accept(
        self,
        operation: str,
        source: SourceInput,
        scope: RequestScope,
        key: str | None,
        ctx: ExecutionContext,
    ) -> JobHandle:
        granted = self.owner.scope.narrow(scope)
        if str(source.root.collection_id) not in granted.collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Source collection is outside the grant"
            )
        jobs, _ = await self._ports(ctx)
        config_ref = await self._config_ref(ctx)
        return await self.owner.run_blocking(
            lambda: jobs.accept(
                operation, granted, key, source_intent(source), config_ref
            ),
            ctx,
        )

    async def execute_accepted(
        self,
        handle: JobHandle,
        source: SourceInput,
        scope: RequestScope,
        *,
        ctx: ExecutionContext | None = None,
        retained_capture_sha: str | None = None,
    ) -> JobHandle:
        """Resume exact accepted intent or its verified retained retry capture."""
        context = ctx or ExecutionContext()
        granted = self.owner.scope.narrow(scope)
        jobs, acquirer = await self._ports(context)
        config_ref = await self._config_ref(context)

        def validated() -> JobHandle:
            status = jobs.get(handle.id)
            jobs.authorize(handle.id, granted, write=True)
            if (
                status.operation != handle.operation
                or (
                    status.request_digest
                    != digest(
                        "request-v1",
                        self.owner.workspace_id,
                        granted.principal_id,
                        handle.operation,
                        source_intent(source),
                    )
                    if retained_capture_sha is None
                    else status.retry_count < 1
                    or status.captured_inputs is None
                    or status.captured_inputs.sha256 != retained_capture_sha
                )
                or status.effective_config.sha256 != config_ref.sha256
                or str(source.root.collection_id) not in granted.collection_ids
            ):
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT", "Accepted job input differs."
                )
            return JobHandle(
                id=status.id, operation=status.operation, state=status.state
            )

        handle = await self.owner.run_blocking(validated, context)
        if handle.state != "queued":
            return handle
        operation = handle.operation
        ctx = context
        await self.owner.run_blocking(lambda: jobs.start(handle.id), ctx)
        try:
            await self.owner.run_blocking(lambda: jobs.stage(handle.id, "acquire"), ctx)
            captured_ref = await self.owner.run_blocking(
                lambda: jobs.get(handle.id).captured_inputs, ctx
            )
            if captured_ref is None:
                capture = await self.owner.run_blocking(
                    lambda: acquirer.capture(source, ctx), ctx
                )
                await self.owner.run_blocking(
                    lambda: jobs.capture(handle.id, capture.manifest_ref), ctx
                )
            else:
                capture = await self.owner.run_blocking(
                    lambda: self._committed_capture(captured_ref), ctx
                )
            await self.owner.run_blocking(lambda: jobs.stage(handle.id, "parse"), ctx)
            registration = await self.owner.run_blocking(
                lambda: self._registration(source, capture, granted), ctx
            )
            base = await self.owner.run_blocking(
                self.owner.store.active_snapshot_id, ctx
            )
            watermark = await self.owner.run_blocking(self._decision_watermark, ctx)
            committed = await self.owner.run_blocking(
                lambda: self._completed_plan(
                    jobs, handle, capture, config_ref, base, watermark
                ),
                ctx,
            )
            if committed is not None:
                try:
                    await self._publish(committed, jobs, handle, ctx)
                    return JobHandle(
                        id=handle.id, operation=operation, state="succeeded"
                    )
                except TychoraError as error:
                    if error.code != "WORKSPACE_BUSY":
                        raise
            if base is not None:
                selected_base = base
                can_reuse = await self.owner.run_blocking(
                    lambda: self._selected_revision(
                        selected_base, registration.revision.revision_id
                    ),
                    ctx,
                )
                if can_reuse:
                    plan = await self.owner.run_blocking(
                        lambda: self.coordinator.reuse_existing(
                            registration.source.source_id,
                            registration.revision.revision_id,
                            selected_base,
                            handle,
                            ctx,
                        ),
                        ctx,
                    )
                    try:
                        await self._publish(plan, jobs, handle, ctx)
                        return JobHandle(
                            id=handle.id, operation=operation, state="succeeded"
                        )
                    except TychoraError as error:
                        if error.code != "WORKSPACE_BUSY":
                            raise
            bundle = await self.owner.run_blocking(
                lambda: self._retained_bundle(registration, ctx), ctx
            )
            if bundle is None:
                bundle = await self._extract(registration, capture, ctx)
            previous_groups = None
            previous_records = None
            for _attempt in range(3):
                base = await self.owner.run_blocking(
                    self.owner.store.active_snapshot_id, ctx
                )
                prepared = await self.owner.run_blocking(
                    lambda base=base: self.coordinator.prepare(
                        bundle, base, handle, ctx
                    ),
                    ctx,
                )
                records = await self._text_records(
                    prepared, granted, previous_groups, previous_records, ctx
                )
                previous_groups, previous_records = prepared.text_groups, records
                visual_records = await self._visual_records(prepared, granted, ctx)
                await self.owner.run_blocking(
                    lambda: jobs.stage(handle.id, "project"), ctx
                )
                plan = await self.owner.run_blocking(
                    lambda prepared=prepared, records=records, visual_records=visual_records: (
                        self.coordinator.finalize(
                            prepared, records, ctx, visual_records
                        )
                    ),
                    ctx,
                )
                await self.owner.run_blocking(
                    lambda plan=plan, base=base: jobs.checkpoint(
                        handle.id,
                        self._plan_step(base, self._plan_watermark(plan)),
                        self._plan_input(
                            capture, config_ref, base, self._plan_watermark(plan)
                        ),
                        plan.manifest,
                    ),
                    ctx,
                )
                try:
                    await self._publish(plan, jobs, handle, ctx)
                    return JobHandle(
                        id=handle.id, operation=operation, state="succeeded"
                    )
                except TychoraError as error:
                    if error.code != "WORKSPACE_BUSY":
                        raise
            raise TychoraError(
                "WORKSPACE_BUSY", "Publication base changed repeatedly", retryable=True
            )
        except TychoraError as error:
            recovered = await self.owner.run_blocking(
                lambda: (
                    jobs.recover(handle.id)
                    if PublicationReceipts(self.owner.store).lookup(handle.id)
                    else None
                ),
                ExecutionContext(),
            )
            if recovered is not None and recovered.state == "succeeded":
                return JobHandle(id=handle.id, operation=operation, state="succeeded")
            failure = error
            await self.owner.run_blocking(
                lambda: (
                    jobs.fail(handle.id, failure)
                    if jobs.get(handle.id).state in {"queued", "running"}
                    else None
                ),
                ExecutionContext(),
            )
            raise
        except asyncio.CancelledError:
            await self.owner.run_blocking(
                lambda: (
                    jobs.fail(handle.id, TychoraError("CANCELLED", "Job was cancelled"))
                    if jobs.get(handle.id).state in {"queued", "running"}
                    else None
                ),
                ExecutionContext(),
            )
            raise
        except Exception as error:
            try:
                await self.owner.run_blocking(
                    lambda: self._reconcile_unexpected(jobs, handle.id),
                    ExecutionContext(),
                )
            except Exception as recovery_error:  # noqa: BLE001 - preserve original failure
                error.add_note(
                    "Job reconciliation failed: " + type(recovery_error).__name__
                )
            raise

    def _reconcile_unexpected(self, jobs: JobRepository, job_id: str) -> None:
        if PublicationReceipts(self.owner.store).lookup(job_id) is not None:
            jobs.recover(job_id)
        elif jobs.get(job_id).state in {"queued", "running"}:
            jobs.fail(
                job_id,
                TychoraError("JOB_INTERRUPTED", "Job was interrupted", retryable=True),
            )

    async def _publish(
        self, plan, jobs: JobRepository, handle: JobHandle, ctx: ExecutionContext
    ) -> str:
        def activate() -> str:
            self.coordinator.attach(plan, ctx)
            jobs.stage(handle.id, "publish")
            self.owner.store.publication_context = ctx
            try:
                return self.owner.store.publish_snapshot(plan)
            finally:
                self.owner.store.publication_context = None

        snapshot_id = await self.owner.run_blocking(activate, ctx)
        await self.owner.run_blocking(
            lambda: jobs.complete(handle.id, result_snapshot_id=snapshot_id), ctx
        )
        return snapshot_id

    async def _text_records(
        self,
        prepared: PreparedProjection,
        scope: RequestScope,
        previous_groups,
        previous_records,
        ctx: ExecutionContext,
    ):
        if not prepared.text_groups:
            return ()
        if prepared.text_groups == previous_groups and previous_records is not None:
            return previous_records
        embedder = self.coordinator.text_embedder
        if embedder is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured text provider is unavailable"
            )
        epoch = await self.owner.run_blocking(
            lambda: self.owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (self.owner.workspace_id,),
            ).fetchone()[0],
            ctx,
        )
        dependencies = frozenset().union(
            *(group.dependency_ids for group in prepared.text_groups)
        )
        if self.coordinator.local_text_embedder is embedder:
            return await self.owner.run_local_text(
                lambda: embedder.embed_groups(prepared.text_groups, ctx),
                ctx,
                scope=scope,
                dependencies=dependencies,
                policy_epoch=epoch,
            )
        return await self.owner.run_provider(
            embedder.capabilities.provider,
            lambda: embedder.embed_groups(prepared.text_groups, ctx),
            ctx,
            scope=scope,
            dependencies=dependencies,
            policy_epoch=epoch,
        )

    async def _visual_records(
        self, prepared: PreparedProjection, scope: RequestScope, ctx: ExecutionContext
    ):
        if not prepared.visual_inputs:
            return ()
        embedder = self.coordinator.visual_embedder
        if embedder is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured visual provider is unavailable"
            )
        return await self.owner.embed_visual_assets(
            embedder,
            prepared.visual_inputs,
            ctx,
            scope=scope,
        )

    def _selected_revision(self, base: str, revision: str) -> bool:
        return (
            self.owner.db.execute(
                "SELECT 1 FROM snapshot_members WHERE snapshot_id=? AND revision_id=?",
                (base, revision),
            ).fetchone()
            is not None
        )

    def _retained_bundle(
        self,
        registration: NativeRegistration | DocumentRegistration | CodeRegistration,
        ctx: ExecutionContext,
    ) -> ExtractionBundle | None:
        """Replay a registered revision independently of current membership."""
        from tychora.contracts.artifacts import ArtifactRef

        rows = self.owner.db.execute(
            "SELECT e.extraction_id,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
            "FROM extractions e JOIN artifacts a ON a.artifact_sha=e.document_artifact_sha "
            "WHERE e.revision_id=? ORDER BY e.created_at,e.extraction_id",
            (registration.revision.revision_id,),
        ).fetchall()
        for extraction_id, sha, size, media_type, storage_key in rows:
            ctx.checkpoint()
            ref = ArtifactRef(
                sha256=sha,
                size_bytes=size,
                media_type=media_type,
                storage_key=storage_key,
            )
            with self.owner.artifacts.open_verified(ref) as stream:
                payload = stream.read(ref.size_bytes + 1)
            root = json.loads(payload)
            if root.get("type") != "extraction" or root.get("schema_version") != "1":
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained extraction type differs"
                )
            data = root.get("data", {})
            if isinstance(registration, CodeRegistration):
                old_capture = self._code_replay_capture(
                    data["capture_manifest_sha"], registration
                )
                if old_capture is None:
                    continue
                replay_registration = CodeRegistration.model_validate(
                    registration.model_copy(
                        update={"capture": old_capture}
                    ).model_dump()
                )
                bundle = normalize_code(
                    replay_code_extraction(
                        ref, replay_registration, self.owner.artifacts
                    ),
                    self.owner.store.registry,
                )
            elif data.get("representation") == "document-composition@1":
                if not isinstance(registration, DocumentRegistration):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained extraction kind differs"
                    )
                links = root.get("required_manifests", [])
                parsed_refs = [
                    ArtifactRef.model_validate(link["artifact"])
                    for link in links
                    if link.get("type") == "extraction"
                ]
                if len(parsed_refs) != 1:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained document parser root differs"
                    )
                parsed_ref = parsed_refs[0]
                with self.owner.artifacts.open_verified(parsed_ref) as stream:
                    parsed_payload = stream.read(parsed_ref.size_bytes + 1)
                parsed_data = json.loads(parsed_payload)["data"]
                doc_ref = ArtifactRef.model_validate(parsed_data["docling_json_ref"])
                with self.owner.artifacts.open_verified(doc_ref) as stream:
                    doc_payload = stream.read(doc_ref.size_bytes + 1)
                parsed = replay_document(parsed_payload, doc_payload, parsed_ref)
                if (
                    parsed.registration.capture.config_sha
                    != registration.capture.config_sha
                ):
                    continue
                bundle = compose_document(
                    parsed,
                    self.owner.store.registry,
                    DocumentCompositionPlan.model_validate_json(
                        canonical_bytes(data["plan"])
                    ),
                    self.owner.artifacts,
                    ctx,
                ).bundle
            else:
                if not isinstance(registration, NativeRegistration):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained extraction kind differs"
                    )
                parsed = ExtractionArtifact.model_validate_json(
                    canonical_bytes(
                        {**data, "raw_document_ref": ref.model_dump(mode="json")}
                    )
                )
                if (
                    parsed.registration.capture.config_sha
                    != registration.capture.config_sha
                ):
                    continue
                bundle = normalize(
                    parsed,
                    registration.revision.revision_id,
                    self.owner.store.registry,
                )
            if (
                bundle.extraction.extraction_id != extraction_id
                or bundle.extraction.document_artifact_sha != ref.sha256
                or bundle.source != registration.source
                or bundle.revision != registration.revision
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained extraction identity differs"
                )
            return bundle
        return None

    def _code_replay_capture(
        self, capture_sha: str, registration: CodeRegistration
    ) -> CaptureManifest | None:
        """Resolve the exact capture bound into the retained code manifest."""
        from tychora.contracts.artifacts import ArtifactRef

        row = self.owner.db.execute(
            "SELECT artifact_sha,size_bytes,media_type,storage_key "
            "FROM artifacts WHERE artifact_sha=? AND payload_state='present'",
            (capture_sha,),
        ).fetchone()
        if row is None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained code capture is unavailable"
            )
        capture = self._committed_capture(
            ArtifactRef(
                sha256=row[0],
                size_bytes=row[1],
                media_type=row[2],
                storage_key=row[3],
            )
        )
        if (
            capture.original_sha == registration.capture.original_sha
            and capture.source_key == registration.capture.source_key
            and capture.config_sha == registration.capture.config_sha
        ):
            return capture
        return None

    def _registration(
        self, source: SourceInput, capture: CaptureManifest, scope: RequestScope
    ) -> NativeRegistration | DocumentRegistration | CodeRegistration:
        value = source.root
        cid = str(value.collection_id)
        row = self.owner.db.execute(
            "SELECT collection_id,workspace_id,slug,title,access_floor,policy_json "
            "FROM collections WHERE collection_id=?",
            (cid,),
        ).fetchone()
        if (
            row is None
            or row[1] != self.owner.workspace_id
            or row[4] > scope.access_ceiling
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Source collection is unavailable")
        collection = CollectionRow(
            collection_id=row[0],
            workspace_id=row[1],
            slug=row[2],
            title=row[3],
            access_floor=row[4],
            policy_json=json.loads(row[5]),
        )
        workspace = WorkspaceRow(
            workspace_id=self.owner.workspace_id,
            created_at=self.owner.db.execute(
                "SELECT created_at FROM workspaces WHERE workspace_id=?",
                (self.owner.workspace_id,),
            ).fetchone()[0],
        )
        sid = source_id(UUID(cid), value.source_key)
        old = self.owner.db.execute(
            "SELECT source_id,collection_id,source_key,source_type,registration_json,access_floor,created_at "
            "FROM sources WHERE source_id=?",
            (sid,),
        ).fetchone()
        if old is not None:
            source_row = SourceRow(
                source_id=old[0],
                collection_id=old[1],
                source_key=old[2],
                source_type=old[3],
                registration_json=json.loads(old[4]),
                access_floor=old[5],
                created_at=old[6],
            )
        else:
            registered = value.model_dump(
                mode="json", exclude={"text", "data", "metadata"}
            )
            source_row = SourceRow(
                source_id=sid,
                collection_id=cid,
                source_key=value.source_key,
                source_type="repository" if value.type == "repository" else "document",
                registration_json=registered,
                created_at=capture.captured_at.isoformat(),
            )
        metadata = value.metadata
        kind = value.kind
        self.owner.store.registry.validate_metadata(kind, metadata)
        msha = digest("metadata-v1", metadata)
        rid = revision_id(sid, capture.original_sha, "1", msha)
        old_revision = self.owner.db.execute(
            "SELECT captured_at FROM revisions WHERE revision_id=?", (rid,)
        ).fetchone()
        definition = cast(Mapping[str, object], self.owner.store.registry.kinds[kind])
        allowed = tuple(
            sorted(
                set(scope.uses) & set(cast(tuple[str, ...], definition["allowed_uses"]))
            )
        )
        if not allowed:
            raise TychoraError("FORBIDDEN_SCOPE", "No allowed source use is granted")
        revision = RevisionRow(
            revision_id=rid,
            source_id=sid,
            kind_id=kind,
            original_sha=capture.original_sha,
            edition_key="1",
            metadata_sha=msha,
            title=str(metadata["title"]),
            metadata_json=metadata,
            captured_at=old_revision[0]
            if old_revision
            else capture.captured_at.isoformat(),
            allowed_uses_json=allowed,
        )
        if value.type == "repository":
            return CodeRegistration(
                capture=capture,
                workspace=workspace,
                collection=collection,
                source=source_row,
                revision=revision,
            )
        fmt = (
            value.format
            or Path(getattr(value, "path", value.source_key)).suffix.lstrip(".").lower()
        )
        if fmt in {"md", "markdown"}:
            fmt = "markdown"
        elif fmt in {"txt", "text"}:
            fmt = "text"
        elif fmt in {"jpg", "jpeg"}:
            fmt = "jpeg"
        if fmt in {"markdown", "text", "json"}:
            return NativeRegistration(
                capture=capture,
                workspace=workspace,
                collection=collection,
                source=source_row,
                revision=revision,
                format=cast(Literal["markdown", "text", "json"], fmt),
            )
        if fmt in {"pdf", "docx", "png", "jpeg"}:
            return DocumentRegistration(
                capture=capture,
                workspace=workspace,
                collection=collection,
                source=source_row,
                revision=revision,
                format=cast(Literal["pdf", "docx", "png", "jpeg"], fmt),
            )
        raise TychoraError(
            "CAPABILITY_MISSING", "Source format has no qualified parser"
        )

    async def _extract(
        self,
        registration: NativeRegistration | DocumentRegistration | CodeRegistration,
        capture: CaptureManifest,
        ctx: ExecutionContext,
    ) -> ExtractionBundle:
        if isinstance(registration, NativeRegistration):
            return await self.owner.run_blocking(
                lambda: normalize(
                    NativeParser(self.owner.artifacts, registration).extract(
                        capture.original_ref, self.owner.config, ctx
                    ),
                    registration.revision.revision_id,
                    self.owner.store.registry,
                ),
                ctx,
            )
        if isinstance(registration, DocumentRegistration):
            if self.docling_models is None:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Docling model path is not provisioned"
                )
            parsed = await DoclingParser(
                self.owner, registration, self.docling_models
            ).aextract(capture.original_ref, self.owner.config, ctx)
            return await self.owner.run_blocking(
                lambda: (
                    compose_document(
                        parsed,
                        self.owner.store.registry,
                        DocumentCompositionPlan(
                            base_normalizer_fingerprint=parsed.normalizer_fingerprint
                        ),
                        self.owner.artifacts,
                        ctx,
                    ).bundle
                ),
                ctx,
            )
        files = []
        for captured in capture.entries:
            ctx.checkpoint()
            syntax = await self.owner.run_parser(
                partial(_parse_code_file, relative_path=captured.relative_path),
                (captured.artifact_ref,),
                ctx,
            )
            files.append(CapturedSyntax(captured=captured, syntax=syntax))
        return await self.owner.run_blocking(
            lambda: normalize_code(
                retain_code_extraction(
                    registration,
                    tuple(files),
                    self.owner.store.registry,
                    self.owner.artifacts,
                ),
                self.owner.store.registry,
            ),
            ctx,
        )
