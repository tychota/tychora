"""Owner-authorized source capture, extraction and snapshot publication."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from functools import partial
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from tychora.adapters.code.normalize import (
    has_current_code_projection,
    normalize_code,
    replay_code_extraction,
    retain_code_extraction,
)
from tychora.adapters.code.parse_cache import (
    MAX_ENTRY_BYTES,
    SyntaxCache,
    file_adapter,
    parse_identity,
)
from tychora.adapters.code.syntax import base_syntax_result
from tychora.adapters.docling.parser import DoclingParser
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.application.snapshot_gc import SnapshotGc
from tychora.configuration.collection_policy import require_active_collection
from tychora.configuration.global_facets import (
    ROLE as GLOBAL_FACET_ROLE,
)
from tychora.configuration.global_facets import (
    SOURCE_REF as GLOBAL_FACET_SOURCE_REF,
)
from tychora.configuration.global_facets import (
    binding_bytes as global_facet_binding_bytes,
)
from tychora.configuration.global_facets import (
    normalized_metadata,
)
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig, retained_registry_document
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.code import CapturedSyntax, CodeRegistration
from tychora.contracts.composition import DocumentCompositionPlan, VisionCapabilityGap
from tychora.contracts.corpus import (
    AllowedUsesOverride,
    CollectionRow,
    FacetRow,
    RevisionArtifactRow,
    RevisionRow,
    SourceRow,
    WorkspaceRow,
)
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.errors import TychoraError
from tychora.contracts.extraction import ExtractionArtifact, NativeRegistration
from tychora.contracts.identity import canonical_bytes, digest, revision_id, source_id
from tychora.contracts.operations import JobHandle
from tychora.contracts.publication import PublicationPlan, publication_plan_document
from tychora.contracts.requests import (
    RequestScope,
    SourceInput,
    UploadSource,
    VisionSelection,
    source_registration_descriptor,
)
from tychora.ingestion.acquire import Acquirer
from tychora.ingestion.composition import compose_document
from tychora.ingestion.documents import replay_document
from tychora.ingestion.native import NativeParser
from tychora.ingestion.normalize import normalize
from tychora.ingestion.vision import planned_vision_proxy
from tychora.ports.acquisition import UrlReader
from tychora.ports.execution import ExecutionContext

from .acquired_source import AcquiredSource
from .jobs import source_intent
from .owner import WorkspaceOwner

if TYPE_CHECKING:
    from tychora.contracts.acquisition import CaptureManifest
    from tychora.contracts.artifacts import ArtifactRef
    from tychora.contracts.corpus import ExtractionBundle
    from tychora.contracts.indexing import ContextGroup
    from tychora.indexing.coordinator import PreparedProjection, ProjectionCoordinator
    from tychora.ports.language import SyntaxResult

# Matches the owner's per-authorization dependency bound.
_LOG = logging.getLogger(__name__)
_STALE_CHECKPOINT_REASONS = frozenset({"batch.missing", "payload.missing"})
MAX_UPLOAD_DEPENDENCIES = 2048


def upload_batches(
    groups: tuple[ContextGroup, ...],
) -> tuple[tuple[ContextGroup, ...], ...]:
    """Split ordered groups so each separately authorized upload stays bounded."""
    batches: list[list[ContextGroup]] = []
    current: list[ContextGroup] = []
    dependencies: frozenset[str] = frozenset()
    for group in groups:
        if len(group.dependency_ids) > MAX_UPLOAD_DEPENDENCIES:
            raise TychoraError(
                "INVALID_REQUEST",
                "One embedding group exceeds the upload authorization bound",
            )
        merged = dependencies | group.dependency_ids
        if current and len(merged) > MAX_UPLOAD_DEPENDENCIES:
            batches.append(current)
            current, merged = [], group.dependency_ids
        current.append(group)
        dependencies = merged
    if current:
        batches.append(current)
    if len(batches) == 1:
        return (groups,)
    renumbered = []
    for batch in batches:
        # Request indices only pack provider calls; each batch restarts at zero.
        indices = {
            index: position
            for position, index in enumerate(
                dict.fromkeys(group.request_index for group in batch)
            )
        }
        renumbered.append(
            tuple(
                group.model_copy(update={"request_index": indices[group.request_index]})
                for group in batch
            )
        )
    return tuple(renumbered)


def _parse_code_file(
    paths: tuple[Path, ...], relative_path: str, options: FrozenJsonMap
) -> SyntaxResult:
    payload = paths[0].read_bytes()
    adapter = file_adapter(relative_path)
    if adapter is not None:
        return adapter.parse(payload, options)
    return base_syntax_result(
        language="unknown",
        grammar="unsupported",
        adapter_fingerprint=digest("unsupported-adapter-v1"),
        file_bytes=payload,
        text=payload.decode("latin-1"),
        encoding="latin-1",
        coverage="text_fallback",
        options=options,
        root_type="unsupported_file",
    )


_pdf_job_id: ContextVar[str | None] = ContextVar("pdf_job_id", default=None)


@dataclass(frozen=True)
class _Attempt:
    """What one projection attempt of an accepted add works from."""

    bundle: ExtractionBundle
    handle: JobHandle
    jobs: JobRepository
    capture: CaptureManifest
    config_ref: ArtifactRef
    scope: RequestScope


def _plan_batch_ids(plan: PublicationPlan) -> frozenset[str]:
    """Every batch a plan's new pages and root bind."""
    return frozenset(
        {
            batch.batch_id
            for page in plan.new_pages
            for member in page.members
            for batch in member.batches
        }
        | {batch.batch_id for batch in plan.root_batches}
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
        auto_visual: bool = False,
        vision_credential: Callable[[], str] | None = None,
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
        self.auto_visual = auto_visual
        self.vision_credential = vision_credential
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

        return await self.owner.run_write(initialize, ctx)

    async def _config_ref(self, ctx: ExecutionContext):
        return await self.owner.run_write(self._ensure_registry_artifact, ctx)

    def _ensure_registry_artifact(self) -> ArtifactRef:
        """Return the retained registry artifact, writing it only when absent."""
        config = self.owner.config
        with self.owner.store.uow.read() as tx:
            existing = tx.artifacts.get(config.retained_sha)
        if existing is not None:
            return existing
        ref = self.owner.artifacts.put_stream(
            BytesIO(canonical_bytes(retained_registry_document(config))),
            "application/json",
        )
        if ref.sha256 != config.retained_sha:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Retained registry digest differs from its document",
                reason="config.drift",
            )
        return ref

    def _committed_capture(
        self, ref: ArtifactRef, *, historical: bool = False
    ) -> CaptureManifest:
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
        if not historical and capture.config_sha != self.owner.config.config_sha:
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
        return self.owner.decision_watermark()

    @staticmethod
    def _plan_watermark(plan: PublicationPlan) -> int | None:
        return plan.graph_watermark

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
            if manifest.get("type") != "publication_plan":
                raise ValueError("Checkpoint is not a publication plan")
            plan = PublicationPlan.model_validate_json(
                canonical_bytes(manifest["data"])
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Committed projection plan is malformed",
                reason="canonical.row_invalid",
            ) from None
        if (
            plan.job_id != handle.id
            or plan.base_snapshot_id != base
            or plan.config_sha != config_ref.sha256
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Committed projection plan input differs",
                reason="canonical.binding_mismatch",
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
        acquired: AcquiredSource | None = None,
    ) -> JobHandle:
        return await self._accept(
            "add",
            source,
            scope,
            idempotency_key,
            ctx or ExecutionContext(),
            acquired=acquired,
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
        source = await self.owner.run_write(
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
        source = await self.owner.run_write(
            lambda: self._source_for_sync(source_id_value, scope), context
        )
        return await self._run("sync", source, scope, idempotency_key, context)

    async def set_allowed_uses(
        self,
        source_id: str,
        override: AllowedUsesOverride,
        scope: RequestScope,
        ctx: ExecutionContext,
    ) -> None:
        """Narrow the uses a source's revisions grant; ``None`` restores them."""
        granted = self.owner.scope.narrow(scope)
        if "manage" not in granted.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE",
                "Changing allowed uses requires the manage capability",
                reason="scope.capability_missing",
            )
        await self.owner.run_write(
            lambda: self.owner.store.set_allowed_uses_override(
                source_id, override, collection_ids=granted.collection_ids
            ),
            ctx,
        )

    def _source_for_sync(
        self, source_id_value: str, scope: RequestScope
    ) -> SourceInput:
        granted = self.owner.scope.narrow(scope)
        row = self.owner.db.execute(
            "SELECT s.collection_id,s.registration_json,r.kind_id,r.metadata_json,r.original_sha,s.withdrawn_at "
            "FROM sources s JOIN revisions r USING(source_id) "
            "WHERE s.source_id=? AND s.collection_id IN (SELECT collection_id FROM collections WHERE workspace_id=?) "
            "ORDER BY r.captured_at DESC LIMIT 1",
            (source_id_value, self.owner.workspace_id),
        ).fetchone()
        if (
            row is None
            or row[0] not in granted.collection_ids
            or "write" not in granted.capabilities
            or row[5] is not None
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

            ref = ArtifactRef.from_row(artifact)
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
        acquired: AcquiredSource | None = None,
    ) -> JobHandle:
        granted = self.owner.scope.narrow(scope)
        if (
            isinstance(source.root, UploadSource)
            and acquired is None
            and operation != "sync"
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Upload requires a trusted receipt binding."
            )
        if acquired is not None:
            if self.owner.pdf_intakes is None:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Trusted PDF intake service is unavailable."
                )
            intakes = self.owner.pdf_intakes
            await self.owner.run_write(
                lambda: intakes.validate_acquired(acquired, scope), ctx
            )
        if source.root.vision:
            value = source.root
            selected_format = (
                value.format
                or Path(getattr(value, "path", value.source_key))
                .suffix.lstrip(".")
                .lower()
            )
            if value.type not in {"file", "bytes", "url"} or selected_format not in {
                "pdf",
                "docx",
                "png",
                "jpeg",
                "jpg",
            }:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Selected vision requires a supported document source.",
                )
        if str(source.root.collection_id) not in granted.collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Source collection is outside the grant"
            )
        jobs, acquirer = await self._ports(ctx)
        config_ref = await self._config_ref(ctx)

        def accept():
            row = self.owner.db.execute(
                "SELECT workspace_id,access_floor,policy_json FROM collections WHERE collection_id=?",
                (str(source.root.collection_id),),
            ).fetchone()
            if (
                row is None
                or row[0] != self.owner.workspace_id
                or row[1] > granted.access_ceiling
            ):
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Source collection is unavailable."
                )
            require_active_collection(row[2])
            if acquired is not None:
                assert self.owner.pdf_intakes is not None
                self.owner.pdf_intakes.validate_acquired(acquired, scope)
            if operation == "add" and isinstance(source.root, UploadSource):
                sid = source_id(source.root.collection_id, source.root.source_key)
                registered = self.owner.db.execute(
                    "SELECT 1 FROM sources WHERE source_id=?", (sid,)
                ).fetchone()
                repeated = (
                    self.owner.db.execute(
                        "SELECT 1 FROM jobs WHERE principal_id=? AND operation='add' AND idempotency_key=?",
                        (granted.principal_id, key),
                    ).fetchone()
                    if key is not None
                    else None
                )
                if registered is not None and repeated is None:
                    raise TychoraError(
                        "IDEMPOTENCY_CONFLICT",
                        "A source already uses this logical key; choose a new key for a new upload.",
                    )
            handle = jobs.accept(
                operation, granted, key, source_intent(source), config_ref
            )
            if (
                isinstance(source.root, UploadSource)
                and jobs.get(handle.id).captured_inputs is None
            ):
                from dataclasses import replace

                trusted = (
                    acquired
                    if acquired is not None
                    else self._registered_upload(source, granted)
                )
                canonical = self._canonical_source(source)
                capture = acquirer.capture_acquired(
                    canonical, replace(trusted, source=canonical), ctx
                )
                self._validate_upload_capture(canonical, capture)
                jobs.capture(handle.id, capture.manifest_ref)
            return handle

        return await self.owner.run_write(accept, ctx)

    async def execute_accepted(
        self,
        handle: JobHandle,
        source: SourceInput,
        scope: RequestScope,
        *,
        ctx: ExecutionContext | None = None,
        retained_capture_sha: str | None = None,
        acquired: AcquiredSource | None = None,
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

        handle = await self.owner.run_write(validated, context)
        if handle.state != "queued":
            return handle
        operation = handle.operation
        if source.root.pdf_processing is not None:
            import time

            deadline = time.monotonic() + 5400
            context = ExecutionContext(
                deadline=min(context.deadline, deadline)
                if context.deadline is not None
                else deadline,
                cancelled=context.cancelled,
            )
        ctx = context
        await self.owner.run_write(lambda: jobs.start(handle.id), ctx)
        try:
            # Keep the accepted request digest in caller form while capture and
            # registration share the canonical immutable facet binding.
            captured_source = await self.owner.run_write(
                lambda: self._canonical_source(source), ctx
            )
            await self.owner.run_write(lambda: jobs.stage(handle.id, "acquire"), ctx)
            captured_ref = await self.owner.run_write(
                lambda: jobs.get(handle.id).captured_inputs, ctx
            )
            if captured_ref is None:
                capture = await self.owner.run_write(
                    lambda: acquirer.capture(captured_source, ctx), ctx
                )
                await self.owner.run_write(
                    lambda: jobs.capture(handle.id, capture.manifest_ref), ctx
                )
            else:
                capture = await self.owner.run_write(
                    lambda: self._committed_capture(captured_ref), ctx
                )
            if isinstance(captured_source.root, UploadSource):
                await self.owner.run_write(
                    lambda: self._validate_upload_capture(captured_source, capture), ctx
                )
            await self.owner.run_write(lambda: jobs.stage(handle.id, "parse"), ctx)
            registration = await self.owner.run_write(
                lambda: self._registration(
                    captured_source,
                    capture,
                    granted,
                    original_metadata=source.root.metadata,
                ),
                ctx,
            )
            base = await self.owner.run_write(self.owner.store.active_snapshot_id, ctx)
            watermark = await self.owner.run_write(self._decision_watermark, ctx)
            committed = await self.owner.run_write(
                lambda: self._completed_plan(
                    jobs, handle, capture, config_ref, base, watermark
                ),
                ctx,
            )
            if committed is not None and await self._resume_plan(
                committed, jobs, handle, self._plan_step(base, watermark), ctx
            ):
                return JobHandle(id=handle.id, operation=operation, state="succeeded")
            if (
                base is not None
                and not source.root.vision
                and source.root.pdf_processing is None
            ):
                selected_base = base
                can_reuse = await self.owner.run_write(
                    lambda: self._selected_revision(selected_base, registration, ctx),
                    ctx,
                )
                if can_reuse:
                    plan = await self.owner.run_write(
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
            bundle = (
                None
                if source.root.vision
                else await self.owner.run_write(
                    lambda: self._retained_bundle(registration, ctx), ctx
                )
            )
            if bundle is None:
                token = _pdf_job_id.set(handle.id)
                try:
                    bundle = await self._extract(
                        registration, capture, ctx, source, granted
                    )
                finally:
                    _pdf_job_id.reset(token)
            bundle = await self.owner.run_write(
                lambda: self._bind_source_metadata(bundle), ctx
            )
            attempt = _Attempt(bundle, handle, jobs, capture, config_ref, granted)
            previous: tuple = (None, None)
            for _attempt in range(3):
                with ExitStack() as leases:
                    published, previous = await self._project(
                        attempt, previous, leases, ctx
                    )
                if published:
                    return JobHandle(
                        id=handle.id, operation=operation, state="succeeded"
                    )
            raise TychoraError(
                "WORKSPACE_BUSY", "Publication base changed repeatedly", retryable=True
            )
        except TychoraError as error:
            recovered = await self.owner.run_write(
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
            await self.owner.run_write(
                lambda: (
                    jobs.fail(handle.id, failure)
                    if jobs.get(handle.id).state in {"queued", "running"}
                    else None
                ),
                ExecutionContext(),
            )
            raise
        except asyncio.CancelledError:
            await self.owner.run_write(
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
                await self.owner.run_write(
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

    async def _project(
        self,
        attempt: _Attempt,
        previous: tuple,
        leases: ExitStack,
        ctx: ExecutionContext,
    ) -> tuple[bool, tuple]:
        """Prepare, embed, write and publish once against the current base.

        The base snapshot and every batch the plan reuses stay leased until
        publication returns, so collection cannot remove them in between.
        Return whether the plan was published and the text groups and records
        a retry may reuse.
        """
        store = self.owner.store

        def prepare():
            base = store.active_snapshot_id()
            leases.enter_context(
                store.leases.pin(frozenset(() if base is None else (base,)))
            )
            prepared = self.coordinator.prepare(
                attempt.bundle,
                base,
                attempt.handle,
                ctx,
                on_stage=lambda stage: attempt.jobs.mark(attempt.handle.id, stage),
            )
            leases.enter_context(store.leases.pin(frozenset(prepared.reused_batch_ids)))
            return base, prepared

        base, prepared = await self.owner.run_write(prepare, ctx)
        records = await self._text_records(
            prepared, attempt.scope, previous[0], previous[1], ctx
        )
        visual_records = await self._visual_records(prepared, attempt.scope, ctx)
        await self.owner.run_write(
            lambda: attempt.jobs.stage(attempt.handle.id, "project"), ctx
        )

        def finalize() -> PublicationPlan:
            plan = self.coordinator.finalize(prepared, records, ctx, visual_records)
            leases.enter_context(store.leases.pin(_plan_batch_ids(plan)))
            return plan

        plan = await self.owner.run_write(finalize, ctx)
        await self.owner.run_write(lambda: self._checkpoint(attempt, base, plan), ctx)
        try:
            await self._publish(plan, attempt.jobs, attempt.handle, ctx)
        except TychoraError as error:
            if error.code != "WORKSPACE_BUSY":
                raise
            return False, (prepared.text_groups, records)
        return True, (prepared.text_groups, records)

    def _checkpoint(
        self, attempt: _Attempt, base: str | None, plan: PublicationPlan
    ) -> None:
        """Commit the plan document so a resumed job publishes the same plan."""
        plan_ref = self.owner.artifacts.put_stream(
            BytesIO(canonical_bytes(publication_plan_document(plan))),
            "application/json",
        )
        watermark = self._plan_watermark(plan)
        attempt.jobs.checkpoint(
            attempt.handle.id,
            self._plan_step(base, watermark),
            self._plan_input(attempt.capture, attempt.config_ref, base, watermark),
            plan_ref,
        )

    async def _resume_plan(
        self,
        plan: PublicationPlan,
        jobs: JobRepository,
        handle: JobHandle,
        step: str,
        ctx: ExecutionContext,
    ) -> bool:
        """Publish a checkpointed plan; ``False`` when the add must prepare afresh.

        A busy workspace or a checkpoint that outlived its batches or page bytes
        (``manage gc`` ran after the interruption) is not an error: the stale
        checkpoint is discarded so a fresh plan can take its place.
        """
        try:
            await self._publish(plan, jobs, handle, ctx)
        except TychoraError as error:
            if error.code == "WORKSPACE_BUSY":
                return False
            if error.reason not in _STALE_CHECKPOINT_REASONS:
                raise
            await self.owner.run_write(lambda: jobs.discard_step(handle.id, step), ctx)
            return False
        return True

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

        async with self.owner.comparison_admission.mutation(ctx):
            snapshot_id = await self.owner.run_write(activate, ctx)
            await self.owner.run_write(
                lambda: jobs.complete(handle.id, result_snapshot_id=snapshot_id), ctx
            )
            await self.owner.run_write(
                self._collect_after_publication, ExecutionContext()
            )
        return snapshot_id

    def _collect_after_publication(self) -> None:
        """Run one bounded collection pass; its failure never fails the job.

        The pass only marks released payloads; ``manage gc`` and the next owner
        start unlink them. A failed pass is retried by the next publication.
        """
        try:
            SnapshotGc(self.owner).collect(ExecutionContext(), limit=64)
        except TychoraError as error:
            _LOG.warning(
                "Snapshot collection after publication failed: %s (%s)",
                error.code,
                error.reason,
            )
        except Exception:  # noqa: BLE001 - native stores raise their own types
            # Chroma and Ladybug errors are untyped; the job already completed,
            # so ``manage gc`` finishes whatever this pass left.
            _LOG.warning("Snapshot collection after publication failed", exc_info=True)

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
        epoch = await self.owner.run_write(self.owner.policy_epoch, ctx)
        records = []
        # Every batch is authorized independently against the same policy epoch.
        for batch in upload_batches(prepared.text_groups):
            dependencies = frozenset().union(*(group.dependency_ids for group in batch))
            if self.coordinator.local_text_embedder is embedder:
                records.extend(
                    await self.owner.run_local_text(
                        partial(embedder.embed_groups, batch, ctx),
                        ctx,
                        scope=scope,
                        dependencies=dependencies,
                        policy_epoch=epoch,
                    )
                )
            else:
                records.extend(
                    await self.owner.run_provider(
                        embedder.capabilities.provider,
                        partial(embedder.embed_groups, batch, ctx),
                        ctx,
                        scope=scope,
                        dependencies=dependencies,
                        policy_epoch=epoch,
                    )
                )
        return tuple(records)

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
        if self.auto_visual:
            from tychora.adapters.voyage.visual import VoyageVisualEmbedder
            from tychora.application.visual import create_visual_embedder

            if isinstance(embedder, VoyageVisualEmbedder):
                embedder = await create_visual_embedder(
                    self.owner,
                    capabilities=embedder.capabilities,
                    counter=embedder._counter,
                    runtime=embedder._runtime,
                    scope=scope,
                    ctx=ctx,
                )
        return await self.owner.embed_visual_assets(
            embedder,
            prepared.visual_inputs,
            ctx,
            scope=scope,
        )

    def _code_options(self) -> FrozenJsonMap:
        version = self.owner.config.values.extraction.representation_version
        # Preserve the original default parser identity; explicit new representation
        # versions invalidate parse facts and revision-qualified extraction IDs.
        return FrozenJsonMap(
            {} if version == "1" else {"representation_version": version}
        )

    def _selected_revision(
        self,
        base: str,
        registration: NativeRegistration | DocumentRegistration | CodeRegistration,
        ctx: ExecutionContext | None = None,
    ) -> bool:
        row = self.owner.db.execute(
            "SELECT e.document_artifact_sha,e.extraction_id,e.parser_fingerprint,e.normalizer_fingerprint FROM snapshot_members m "
            "JOIN extractions e USING(extraction_id) "
            "WHERE m.snapshot_id=? AND m.revision_id=?",
            (base, registration.revision.revision_id),
        ).fetchone()
        if row is None:
            return False
        retained = self._retained_bundle(registration, ctx or ExecutionContext())
        return retained is not None and retained.extraction.extraction_id == row[1]

    def _retained_extraction_config(
        self, capture: CaptureManifest, extraction_id: str
    ) -> ResolvedConfig:
        """Resolve only exact capture/extraction lineage, within explicit budgets."""
        from tychora.configuration.resolve import validate_retained_config

        if (
            capture.manifest_ref is None
            or self._committed_capture(capture.manifest_ref, historical=True) != capture
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained capture manifest binding differs"
            )
        refs = self.owner.db.execute(
            "SELECT DISTINCT effective_config_sha FROM jobs WHERE captured_inputs_sha=? LIMIT 65",
            (capture.manifest_ref.sha256,),
        ).fetchall()
        if not refs:
            refs = self.owner.db.execute(
                "SELECT DISTINCT s.config_sha FROM snapshots s JOIN snapshot_members m USING(snapshot_id) "
                "WHERE m.extraction_id=? LIMIT 65",
                (extraction_id,),
            ).fetchall()
        if len(refs) > 64:
            raise TychoraError(
                "BUDGET_TOO_SMALL", "Retained configuration candidate budget exceeded"
            )
        total = 0
        for (sha,) in refs:
            row = self.owner.db.execute(
                "SELECT size_bytes FROM artifacts WHERE artifact_sha=? AND payload_state='present'",
                (sha,),
            ).fetchone()
            if row is None:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained configuration lineage is unavailable"
                )
            total += row[0]
        if total > 16 * 1024 * 1024:
            raise TychoraError(
                "BUDGET_TOO_SMALL", "Retained configuration byte budget exceeded"
            )
        matched = None
        for (sha,) in refs:
            config = validate_retained_config(self.owner.store.retained_config_sha(sha))
            if config.config_sha == capture.config_sha:
                matched = config
        if matched is not None:
            return matched
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained capture configuration binding is unavailable"
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
            ref = ArtifactRef.from_row((sha, size, media_type, storage_key))
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
                retained_code = replay_code_extraction(
                    ref, replay_registration, self.owner.artifacts
                )
                options = self._code_options()
                if any(
                    (
                        item.syntax.language,
                        item.syntax.grammar,
                        item.syntax.adapter_fingerprint,
                    )
                    != parse_identity(item.captured.relative_path, options)
                    or item.syntax.options != options
                    for item in retained_code.files
                ):
                    continue
                retained_config = self._retained_extraction_config(
                    old_capture, extraction_id
                )
                if not pinned_runtime_compatible(retained_config, self.owner.config):
                    continue
                retained_registry = Registry.from_config(retained_config)
                bundle = normalize_code(retained_code, retained_registry)
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

                def read_declared(ref):
                    with self.owner.artifacts.open_verified(ref) as stream:
                        return stream.read(ref.size_bytes + 1)

                parsed = replay_document(
                    parsed_payload, doc_payload, parsed_ref, read_declared=read_declared
                )
                if parsed.pdf_processing != registration.capture.source.get(
                    "pdf_processing"
                ):
                    continue
                retained_config = self._retained_extraction_config(
                    parsed.registration.capture, extraction_id
                )
                if not pinned_runtime_compatible(retained_config, self.owner.config):
                    continue
                retained_registry = Registry.from_config(retained_config)
                from tychora.ingestion.document_records import (
                    document_normalizer_fingerprint,
                )

                kind = cast(
                    Mapping[str, Any],
                    retained_registry.kinds[registration.revision.kind_id],
                )
                current_fingerprint = document_normalizer_fingerprint(
                    kind, retained_registry
                )
                if parsed.normalizer_fingerprint != current_fingerprint:
                    if parsed.normalizer_fingerprint == document_normalizer_fingerprint(
                        kind, retained_registry, legacy_v1=True
                    ):
                        continue
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained document normalizer differs"
                    )
                bundle = compose_document(
                    parsed,
                    retained_registry,
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
                from tychora.ingestion.native import native_parser_fingerprint

                retained_config = self._retained_extraction_config(
                    parsed.registration.capture, extraction_id
                )
                if not pinned_runtime_compatible(retained_config, self.owner.config):
                    continue
                if parsed.parser_fingerprint != native_parser_fingerprint(
                    registration.format, retained_config
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained native parser binding differs"
                    )
                retained_registry = Registry.from_config(retained_config)
                bundle = normalize(
                    parsed,
                    registration.revision.revision_id,
                    retained_registry,
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
            if isinstance(
                registration, CodeRegistration
            ) and not has_current_code_projection(
                retained_code, self.owner.store.registry
            ):
                continue
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
            ArtifactRef.from_row(row),
            historical=True,
        )
        if (
            capture.original_sha == registration.capture.original_sha
            and capture.source_key == registration.capture.source_key
        ):
            return capture
        return None

    def _registered_upload(self, source, scope):
        """Sync resolves only a currently authorized registered immutable original."""
        from tychora.contracts.artifacts import ArtifactRef

        sid = source_id(source.root.collection_id, source.root.source_key)
        registered = self._source_for_sync(sid, scope)
        if registered != source or source.root.pdf_processing is None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Registered upload descriptor differs."
            )
        row = self.owner.db.execute(
            "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key FROM revisions r "
            "JOIN revision_artifacts ra USING(revision_id) JOIN artifacts a ON a.artifact_sha=ra.artifact_sha "
            "WHERE r.source_id=? AND r.original_sha=? AND ra.role='original' AND a.payload_state='present' LIMIT 1",
            (sid, source.root.content_sha256),
        ).fetchone()
        if row is None:
            raise TychoraError(
                "REPLAY_UNAVAILABLE", "Registered upload original is unavailable."
            )
        ref = ArtifactRef.from_row(row)
        return AcquiredSource(
            workspace_id=self.owner.workspace_id,
            scope=scope,
            source=source,
            original_ref=ref,
            receipt_binding_sha256=digest("registered-upload-v1", sid, ref.sha256),
            policy=source.root.pdf_processing,
        )

    def _validate_upload_capture(self, source, capture):
        from tychora.application.pdf_processing import revalidate_pdf_policy
        from tychora.contracts.pdf_intake import PdfIntakeDescriptor

        value = source.root
        policy = value.pdf_processing
        if (
            policy is None
            or capture.original_sha != value.content_sha256
            or capture.original_ref.size_bytes != value.size_bytes
            or canonical_bytes(capture.source.get("pdf_processing"))
            != canonical_bytes(policy.model_dump(mode="json"))
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Retained upload capture differs from policy or original.",
            )
        facts = {
            "page_count": len(policy.native_pages)
            + len(policy.ocr_pages)
            + len(policy.blank_pages),
            "native_text_pages": policy.native_pages,
            "image_only_pages": policy.ocr_pages,
            "blank_pages": policy.blank_pages,
            "unknown_pages": (),
            "requires_ocr": bool(policy.ocr_pages),
        }
        descriptor = PdfIntakeDescriptor(
            source_key=value.source_key,
            collection_id=value.collection_id,
            kind=value.kind,
            metadata=value.metadata,
            expected_size_bytes=value.size_bytes,
        )
        if self.owner.service_runtime is not None:
            services = self.owner.service_runtime.services
        else:
            assert self.owner.pdf_intakes is not None
            services = self.owner.pdf_intakes.services
        revalidate_pdf_policy(self.owner, services, descriptor, facts, policy)

    def _canonical_source(self, source: SourceInput) -> SourceInput:
        metadata = source.root.metadata
        kind = source.root.kind
        self.owner.store.registry.validate_metadata(kind, metadata)
        canonical = normalized_metadata(metadata, self.owner.store.registry)
        self.owner.store.registry.validate_metadata(kind, canonical)
        if canonical == metadata:
            return source
        return SourceInput(source.root.model_copy(update={"metadata": canonical}))

    def _registration(
        self,
        source: SourceInput,
        capture: CaptureManifest,
        scope: RequestScope,
        *,
        original_metadata: FrozenJsonMap,
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
        require_active_collection(row[5])
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
            if isinstance(value, UploadSource) and canonical_bytes(
                json.loads(old[4])
            ) != canonical_bytes(source_registration_descriptor(source)):
                raise TychoraError(
                    "IDEMPOTENCY_CONFLICT",
                    "Registered upload identity differs; use a new logical source key.",
                )
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
            registered = source_registration_descriptor(source)
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
        if capture.source.get("metadata") != metadata:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Retained capture lacks a canonical global facet binding; "
                "publish a new edition",
            )

        def retained_date(key: str) -> str | None:
            selected = metadata.get(key)
            if selected is None:
                return None
            if not isinstance(selected, str):
                raise TychoraError("INVALID_REQUEST", f"{key} must be an ISO date")
            try:
                if date.fromisoformat(selected).isoformat() != selected:
                    raise ValueError("Non-canonical date")
            except ValueError as error:
                raise TychoraError(
                    "INVALID_REQUEST", f"{key} must be a strict ISO date"
                ) from error
            return selected

        published_on = retained_date("publication_date")
        effective_from = retained_date("effective_start")
        effective_until = retained_date("effective_end")
        if (
            effective_from is not None
            and effective_until is not None
            and effective_from >= effective_until
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Effective interval must be half-open and nonempty"
            )
        edition = metadata.get("edition", "1")
        approval = metadata.get("approval", "unreviewed")
        if not isinstance(edition, str) or not edition:
            raise TychoraError("INVALID_REQUEST", "Edition must be nonempty")
        if not isinstance(approval, str) or approval not in {
            "unreviewed",
            "accepted",
            "rejected",
        }:
            raise TychoraError("INVALID_REQUEST", "Approval state is invalid")
        msha = digest("metadata-v1", metadata)
        rid = revision_id(sid, capture.original_sha, edition, msha)
        if original_metadata != metadata:
            old_raw_id = revision_id(
                sid,
                capture.original_sha,
                edition,
                digest("metadata-v1", original_metadata),
            )
            if self.owner.db.execute(
                "SELECT 1 FROM revisions WHERE revision_id=?", (old_raw_id,)
            ).fetchone():
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Historical global facet alias lacks a verified binding; "
                    "publish a new edition or changed metadata",
                )
        old_revision = self.owner.db.execute(
            "SELECT captured_at,edition_key,published_on,effective_from,"
            "effective_until,declared_review_state FROM revisions WHERE revision_id=?",
            (rid,),
        ).fetchone()
        if old_revision is not None and old_revision[1:] != (
            edition,
            published_on,
            effective_from,
            effective_until,
            approval,
        ):
            raise TychoraError(
                "IDEMPOTENCY_CONFLICT",
                "Retained revision predates metadata projection; publish a new "
                "edition or changed metadata instead of rewriting its identity",
            )
        projected_facets = self._source_metadata_facets(metadata, rid)
        if old_revision is not None and projected_facets:
            retained_facets = {
                (key, ordinal): (text, number, boolean)
                for key, ordinal, text, number, boolean in self.owner.db.execute(
                    "SELECT facet_key,ordinal,text_value,number_value,boolean_value "
                    "FROM facet_values WHERE revision_id=? AND unit_id IS NULL",
                    (rid,),
                )
            }
            for facet in projected_facets:
                definition = self.owner.store.registry.facet(facet.facet_key)
                value_type = definition["value_type"]
                expected = (
                    facet.value
                    if value_type in ("string", "date", "date-time")
                    else None,
                    facet.value if value_type in ("integer", "number") else None,
                    int(facet.value) if value_type == "boolean" else None,
                )
                if retained_facets.get((facet.facet_key, facet.ordinal)) != expected:
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Retained revision lacks its registered metadata facet; "
                        "publish a new edition or changed metadata",
                    )
        _, global_binding = global_facet_binding_bytes(
            metadata, rid, msha, self.owner.store.registry
        )
        if old_revision is not None and global_binding is not None:
            markers = self.owner.db.execute(
                "SELECT ra.source_ref,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM revision_artifacts ra LEFT JOIN artifacts a USING(artifact_sha) "
                "WHERE ra.revision_id=? AND ra.role=?",
                (rid, GLOBAL_FACET_ROLE),
            ).fetchall()
            if (
                len(markers) != 1
                or markers[0][0] != GLOBAL_FACET_SOURCE_REF
                or markers[0][1] != sha256(global_binding).hexdigest()
                or markers[0][2] > 65536
            ):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Retained global facet lacks a verified binding; publish a new edition",
                )
            marker = markers[0]
            from tychora.contracts.artifacts import ArtifactRef

            try:
                with self.owner.artifacts.open_verified(
                    ArtifactRef.from_row(marker[1:5])
                ) as stream:
                    if stream.read(65537) != global_binding:
                        raise TychoraError(
                            "PROFILE_INCOMPATIBLE", "Global facet semantics differ"
                        )
            except OSError as error:
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE", "Global facet binding is unavailable"
                ) from error
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
            edition_key=edition,
            metadata_sha=msha,
            title=str(metadata["title"]),
            published_on=published_on,
            effective_from=effective_from,
            effective_until=effective_until,
            declared_review_state=approval,
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

    def _source_metadata_facets(
        self, metadata: FrozenJsonMap, revision_id: str
    ) -> tuple[FacetRow, ...]:
        """Read only registered scalar metadata paths, retaining their source scope."""
        projected: list[FacetRow] = []
        for key in self.owner.store.registry.facets:
            definition = self.owner.store.registry.facet(key)
            binding = definition["source_binding"]
            if not isinstance(binding, str) or not binding.startswith("metadata."):
                continue
            value: object = metadata
            for part in binding.split(".")[1:]:
                if not isinstance(value, Mapping):
                    value = None
                    break
                value = value.get(part)
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                raise TychoraError(
                    "INVALID_REQUEST", "Registered metadata facet requires a scalar"
                )
            self.owner.store.registry.facet_matches(key, value, value)
            projected.append(
                FacetRow(
                    revision_id=revision_id,
                    facet_key=key,
                    value=value,
                )
            )
        return tuple(projected)

    def _bind_source_metadata(self, bundle: ExtractionBundle) -> ExtractionBundle:
        """Bind registered metadata values and explicit global permission."""
        projected = self._source_metadata_facets(
            bundle.revision.metadata_json, bundle.revision.revision_id
        )
        existing = {
            (row.facet_key, row.ordinal): row
            for row in bundle.facets
            if row.revision_id == bundle.revision.revision_id
        }
        added = []
        for row in projected:
            retained = existing.get((row.facet_key, row.ordinal))
            if retained is not None and retained != row:
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Retained revision facet binding differs; publish a new edition",
                )
            if retained is None:
                added.append(row)
        if added:
            bundle = bundle.model_copy(
                update={
                    "facets": (*bundle.facets, *added),
                }
            )
        return self._bind_global_facets(bundle)

    def _bind_global_facets(self, bundle: ExtractionBundle) -> ExtractionBundle:
        _, payload = global_facet_binding_bytes(
            bundle.revision.metadata_json,
            bundle.revision.revision_id,
            bundle.revision.metadata_sha,
            self.owner.store.registry,
        )
        if payload is None:
            return bundle
        ref = self.owner.artifacts.put_stream(BytesIO(payload), "application/json")
        existing = [
            row for row in bundle.revision_artifacts if row.role == GLOBAL_FACET_ROLE
        ]
        if existing:
            if len(existing) != 1 or existing[0].artifact_sha != ref.sha256:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained global facet binding differs"
                )
            return bundle
        return bundle.model_copy(
            update={
                "artifacts": (*bundle.artifacts, ref),
                "revision_artifacts": (
                    *bundle.revision_artifacts,
                    RevisionArtifactRow(
                        revision_id=bundle.revision.revision_id,
                        role=GLOBAL_FACET_ROLE,
                        source_ref=GLOBAL_FACET_SOURCE_REF,
                        artifact_sha=ref.sha256,
                    ),
                ),
            }
        )

    def _pdf_progress(self, job_id, pages):
        from datetime import UTC, datetime

        from tychora.contracts.operations import JobEvent, NamedCount

        assert self._jobs is not None
        status = self._jobs.get(job_id)
        self._jobs.append_event(
            job_id,
            JobEvent(
                job_id=job_id,
                sequence=status.latest_sequence + 1,
                timestamp=datetime.now(UTC),
                stage="parse",
                state=status.state,
                counters=(NamedCount(name="accepted_pdf_pages", count=pages[-1]),),
            ),
        )

    async def _extract(
        self,
        registration: NativeRegistration | DocumentRegistration | CodeRegistration,
        capture: CaptureManifest,
        ctx: ExecutionContext,
        source: SourceInput,
        scope: RequestScope,
    ) -> ExtractionBundle:
        if isinstance(registration, NativeRegistration):
            return await self.owner.run_write(
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
            job_id = _pdf_job_id.get()
            if self.docling_models is None:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Docling model path is not provisioned"
                )
            parser = DoclingParser(
                self.owner,
                registration,
                self.docling_models,
                policy=source.root.pdf_processing,
                progress=(lambda pages: self._pdf_progress(job_id, pages))
                if job_id
                else None,
            )
            parsed = await parser.aextract(capture.original_ref, self.owner.config, ctx)
            pdf_budget = parser.retention_budget
            if source.root.vision and canonical_bytes(
                capture.source.get("vision")
            ) != canonical_bytes(
                [item.model_dump(mode="json") for item in source.root.vision]
            ):
                raise TychoraError("INTEGRITY_ERROR", "Captured vision intent differs.")
            proxies = []
            pixel_regions = ()
            if parsed.pdf_processing is not None:
                from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr

                initial_proxies, pixel_regions = await self.owner.run_write(
                    lambda: plan_pdf_ocr(self.owner, parsed, ctx, budget=pdf_budget),
                    ctx,
                )
                proxies.extend(initial_proxies)
            gaps = []
            for selected in source.root.vision:
                proxy, gap = await self._selected_vision_proxy(
                    parsed,
                    selected,
                    scope,
                    ctx,
                    **(
                        {"before_retain": pdf_budget.reserve_payload}
                        if pdf_budget is not None
                        else {}
                    ),
                )
                if proxy is not None:
                    proxies.append(proxy)
                if gap is not None:
                    gaps.append(gap)
            return await self.owner.run_write(
                lambda: (
                    compose_document(
                        parsed,
                        self.owner.store.registry,
                        DocumentCompositionPlan(
                            base_normalizer_fingerprint=parsed.normalizer_fingerprint,
                            input_strategy="pdf-streamed-images@1"
                            if parsed.pdf_processing is not None
                            else None,
                            proxies=tuple(proxies),
                            pixel_regions=pixel_regions,
                            vision_gaps=tuple(gaps),
                        ),
                        self.owner.artifacts,
                        ctx,
                        **(
                            {"pdf_budget": pdf_budget} if pdf_budget is not None else {}
                        ),
                    ).bundle
                ),
                ctx,
            )
        files = []
        cache = SyntaxCache(self.owner.path / "cache" / "syntax-v1")
        options = self._code_options()
        for captured in capture.entries:
            ctx.checkpoint()
            identity = parse_identity(captured.relative_path, options)

            def read_cached(captured=captured, identity=identity):
                if captured.artifact_ref.size_bytes > MAX_ENTRY_BYTES:
                    return None
                with self.owner.artifacts.open_verified(
                    captured.artifact_ref
                ) as stream:
                    return cache.get(stream.read(), identity, options)

            syntax = await self.owner.run_write(read_cached, ctx)
            if syntax is None:
                syntax = await self.owner.run_parser(
                    partial(
                        _parse_code_file,
                        relative_path=captured.relative_path,
                        options=options,
                    ),
                    (captured.artifact_ref,),
                    ctx,
                )
                await self.owner.run_write(
                    partial(cache.put, syntax, identity, options), ctx
                )
            files.append(CapturedSyntax(captured=captured, syntax=syntax))
        return await self.owner.run_write(
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

    async def _selected_vision_proxy(
        self,
        parsed: ExtractionArtifact,
        selected: VisionSelection,
        scope: RequestScope,
        ctx: ExecutionContext,
        *,
        before_retain=None,
    ):
        from tychora.adapters.vision.anthropic import (
            dispatch_captured_vision,
            response_schema,
        )
        from tychora.adapters.vision.local import LocalOcrProvider
        from tychora.application.vision_selection import (
            derive_selected_region,
            retain_remote_observation,
        )
        from tychora.ports.vision import (
            CapturedRegionAuthorization,
            CapturedSourceRegistration,
        )

        def gap(
            reason: Literal["provider_unavailable", "provider_failure"],
        ) -> VisionCapabilityGap:
            return VisionCapabilityGap(
                selection_sha256=digest(
                    "vision-selection-v1", selected.model_dump(mode="json")
                ),
                reason=reason,
            )

        schema = dict(selected.task_schema)
        response_schema(schema, frozenset())
        region, proof = await self.owner.run_write(
            lambda: derive_selected_region(
                self.owner,
                parsed,
                selected,
                **(
                    {"before_retain": before_retain}
                    if before_retain is not None
                    else {}
                ),
            ),
            ctx,
        )
        observation = None
        if selected.task == "ocr":
            model_root = (
                Path(os.environ.get("TYCHORA_MODEL_CACHE", self.owner.path / "models"))
                / "rapidocr/3.9.2"
            )
            try:
                observation = await asyncio.to_thread(
                    lambda: LocalOcrProvider(
                        self.owner,
                        model_root,
                        **(
                            {"before_retain": before_retain}
                            if before_retain is not None
                            else {}
                        ),
                    ).interpret(region, schema, ctx)
                )
            except TychoraError as error:
                if error.code not in {"CAPABILITY_MISSING", "PROVIDER_FAILURE"}:
                    raise
            if observation is not None and not observation.literal_transcription:
                observation = None
        if observation is None:
            if "vision" not in self.owner.scope.narrow(scope).capabilities:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Vision is outside the current grant."
                )
            registration = parsed.registration
            captured = CapturedSourceRegistration(
                source=registration.source, kind_id=registration.revision.kind_id
            )
            epoch = await self.owner.run_write(self.owner.policy_epoch, ctx)

            def ensure_registration() -> None:
                row = self.owner.db.execute(
                    "SELECT 1 FROM sources WHERE source_id=?",
                    (captured.source.source_id,),
                ).fetchone()
                if row is None:
                    self.owner.upload_policy.register_captured_source(
                        captured, scope, policy_epoch=epoch
                    )

            await self.owner.run_write(ensure_registration, ctx)
            authorization = CapturedRegionAuthorization(
                registration=captured,
                revision=registration.revision,
                proof=proof,
                region=region,
            )
            if self.vision_credential is None:
                await self.owner.run_write(
                    lambda: self.owner.upload_policy.authorize_captured(
                        scope, "anthropic", authorization, epoch
                    ),
                    ctx,
                )
                return None, gap("provider_unavailable")
            try:
                observation = await dispatch_captured_vision(
                    self.owner,
                    authorization,
                    schema,
                    ctx,
                    scope=scope,
                    policy_epoch=epoch,
                    model=self.owner.config.values.providers.vision.model or "",
                    api_key=self.vision_credential,
                )
            except TychoraError as error:
                if error.code == "PROVIDER_FAILURE":
                    return None, gap("provider_failure")
                if error.code == "CAPABILITY_MISSING":
                    return None, gap("provider_unavailable")
                raise
            observation = await self.owner.run_write(
                lambda: retain_remote_observation(
                    self.owner,
                    region,
                    observation,
                    **(
                        {"before_retain": before_retain}
                        if before_retain is not None
                        else {}
                    ),
                ),
                ctx,
            )
        asset_native_ref = region.source_native_ref
        if parsed.original_image is not None:
            linked = [
                asset.native_ref
                for asset in parsed.assets
                if parsed.original_image.native_ref in asset.source_units
            ]
            if len(linked) != 1:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Standalone image asset binding differs."
                )
            asset_native_ref = linked[0]
        return planned_vision_proxy(
            region, observation, asset_native_ref=asset_native_ref
        ), None
