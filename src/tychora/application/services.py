"""Dependency construction for one opened local workspace."""

from __future__ import annotations

import asyncio
import os
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.application.acquired_source import AcquiredSource
from tychora.application.bridge import ClientBridge
from tychora.application.collections import CollectionService
from tychora.application.discovery import DiscoveryService
from tychora.application.inspection import InspectionService
from tychora.application.jobs import JobService
from tychora.application.maintenance import MaintenanceService
from tychora.application.query import QueryService
from tychora.application.sources import SourceService
from tychora.configuration.registry import Registry
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobHandle
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.evaluation.runner import EvaluationService
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches

if TYPE_CHECKING:
    from tychora.api import ProviderBindings
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.models import ResolvedConfig
    from tychora.ports.tokens import TokenCounter


@dataclass(frozen=True)
class Services:
    collections: CollectionService
    discovery: DiscoveryService
    inspection: InspectionService
    jobs: JobService
    maintenance: MaintenanceService
    evaluations: EvaluationService
    query: QueryService
    sources: SourceService
    source_jobs: SourceJobScheduler
    provider_bindings: ProviderBindings
    output_counter: TokenCounter
    owned_runtimes: tuple = ()

    def jobs_for_scope(self, scope: RequestScope) -> JobService:
        """Bind job reads and controls to an individual transport request grant."""
        narrowed = self.jobs.owner.scope.narrow(scope)
        return JobService(self.jobs.owner, self.jobs.repository, narrowed)


class SourceJobScheduler:
    """One bounded, owned job loop shared by Python and HTTP callers."""

    def __init__(self, sources: SourceService, jobs: JobService) -> None:
        self.sources = sources
        self.jobs = jobs
        self.bridge = ClientBridge()
        self._slots: asyncio.Semaphore | None = None
        self._tasks: dict[str, tuple[asyncio.Task[JobHandle], ExecutionContext]] = {}
        self._submitting = 0
        self._closing = False

    async def _submit(
        self,
        operation: str,
        source: SourceInput | str,
        scope: RequestScope,
        key: str | None,
        acquired: AcquiredSource | None = None,
    ) -> JobHandle:
        if self._closing:
            raise TychoraError(
                "WORKSPACE_BUSY", "Job scheduler is closing.", retryable=True
            )
        self._submitting += 1
        try:
            if self._slots is None:
                self._slots = asyncio.Semaphore(8)
            slots = self._slots
            await slots.acquire()
            try:
                if self._closing:
                    raise TychoraError(
                        "WORKSPACE_BUSY", "Job scheduler is closing.", retryable=True
                    )
                ctx = ExecutionContext()
                if operation == "add":
                    assert isinstance(source, SourceInput)
                    if acquired is None:
                        handle = await self.sources.accept_add(
                            source, scope, key, ctx=ctx
                        )
                    else:
                        handle = await self.sources.accept_add(
                            source, scope, key, ctx=ctx, acquired=acquired
                        )
                else:
                    assert isinstance(source, str)
                    handle, source = await self.sources.accept_sync(
                        source, scope, key, ctx=ctx
                    )
                if handle.state != "queued" or handle.id in self._tasks:
                    slots.release()
                    return handle
                work_ctx = ExecutionContext()
                execution = (
                    self.sources.execute_accepted(handle, source, scope, ctx=work_ctx)
                    if acquired is None
                    else self.sources.execute_accepted(
                        handle, source, scope, ctx=work_ctx, acquired=acquired
                    )
                )
                task = asyncio.create_task(execution)
                self._tasks[handle.id] = (task, work_ctx)

                def completed(finished: asyncio.Task[JobHandle]) -> None:
                    self._tasks.pop(handle.id, None)
                    slots.release()
                    if not finished.cancelled():
                        finished.exception()

                task.add_done_callback(completed)
                return handle
            except BaseException:
                slots.release()
                raise
        finally:
            self._submitting -= 1

    def add(
        self, source: SourceInput, scope: RequestScope, key: str | None = None
    ) -> JobHandle:
        return self.bridge.call(self._submit("add", source, scope, key))

    async def add_async(
        self, source: SourceInput, scope: RequestScope, key: str | None = None
    ) -> JobHandle:
        return await self.bridge.acall(self._submit("add", source, scope, key))

    async def add_acquired_async(
        self, acquired: AcquiredSource, scope: RequestScope, key: str | None = None
    ) -> JobHandle:
        return await self.bridge.acall(
            self._submit("add", acquired.source, scope, key, acquired)
        )

    def sync(
        self, source_id: str, scope: RequestScope, key: str | None = None
    ) -> JobHandle:
        return self.bridge.call(self._submit("sync", source_id, scope, key))

    async def sync_async(
        self, source_id: str, scope: RequestScope, key: str | None = None
    ) -> JobHandle:
        return await self.bridge.acall(self._submit("sync", source_id, scope, key))

    async def _idle(self) -> bool:
        return not self._submitting and not self._tasks and not self._closing

    async def is_idle_async(self) -> bool:
        return await self.bridge.acall(self._idle())

    async def _retry(self, job_id: str, scope: RequestScope) -> JobHandle:
        if self._closing:
            raise TychoraError(
                "WORKSPACE_BUSY", "Job scheduler is closing.", retryable=True
            )
        if self._slots is None:
            self._slots = asyncio.Semaphore(8)
        slots = self._slots
        await slots.acquire()
        launched = False
        try:
            scoped = JobService(self.jobs.owner, self.jobs.repository, scope)
            status = await scoped.get(job_id, ExecutionContext())
            if status.operation not in {"add", "sync"}:
                await self.jobs.owner.run_blocking(
                    lambda: self.jobs.repository.authorize(
                        job_id, self.jobs.owner.scope.narrow(scope), write=True
                    ),
                    ExecutionContext(),
                )
                raise TychoraError(
                    "CAPABILITY_MISSING", "Maintenance retry execution is unavailable."
                )
            while job_id in self._tasks and self._tasks[job_id][0].done():
                await asyncio.sleep(0)
            if job_id in self._tasks:
                return JobHandle(
                    id=job_id, operation=status.operation, state=status.state
                )
            capture_ref = status.captured_inputs
            if capture_ref is None:
                raise TychoraError(
                    "REPLAY_UNAVAILABLE",
                    "Retained source input is unavailable for retry.",
                )
            capture = await self.jobs.owner.run_blocking(
                lambda: self.sources._committed_capture(capture_ref),
                ExecutionContext(),
            )

            def retained_source():
                value = dict(capture.source)
                if value.get("type") in {"text", "bytes"}:
                    with self.jobs.owner.artifacts.open_verified(
                        capture.original_ref
                    ) as stream:
                        payload = stream.read(capture.original_ref.size_bytes + 1)
                    if len(payload) != capture.original_ref.size_bytes:
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Retained source bytes differ."
                        )
                    if value["type"] == "text":
                        value["text"] = payload.decode("utf-8")
                    else:
                        value["data"] = b64encode(payload).decode("ascii")
                return SourceInput.from_mapping(value)

            source = await self.jobs.owner.run_blocking(
                retained_source, ExecutionContext()
            )

            def admit_retry():
                from .collection_policy import require_active_collection

                row = self.jobs.owner.db.execute(
                    "SELECT workspace_id,access_floor,policy_json FROM collections WHERE collection_id=?",
                    (str(source.root.collection_id),),
                ).fetchone()
                granted = self.jobs.owner.scope.narrow(scope)
                if (
                    row is None
                    or row[0] != self.jobs.owner.workspace_id
                    or row[1] > granted.access_ceiling
                    or str(source.root.collection_id) not in granted.collection_ids
                ):
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Retry collection is unavailable."
                    )
                require_active_collection(row[2])

            await self.jobs.owner.run_blocking(admit_retry, ExecutionContext())
            if status.state == "running":
                status = await scoped.recover(job_id, ExecutionContext())
            if status.state in {"failed", "interrupted"}:
                status = await scoped.retry(job_id, ExecutionContext())
            elif status.state != "queued":
                raise TychoraError("IDEMPOTENCY_CONFLICT", "Job is not retryable.")
            handle = JobHandle(id=job_id, operation=status.operation, state="queued")
            work_ctx = ExecutionContext()
            task = asyncio.create_task(
                self.sources.execute_accepted(
                    handle,
                    source,
                    scope,
                    ctx=work_ctx,
                    retained_capture_sha=capture_ref.sha256,
                )
            )
            self._tasks[job_id] = (task, work_ctx)
            launched = True

            def completed(finished: asyncio.Task[JobHandle]) -> None:
                self._tasks.pop(job_id, None)
                slots.release()
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(completed)
            return handle
        finally:
            if not launched:
                slots.release()

    async def retry_async(self, job_id: str, scope: RequestScope) -> JobHandle:
        return await self.bridge.acall(self._retry(job_id, scope))

    async def _signal_cancel(self, job_id: str) -> None:
        active = self._tasks.get(job_id)
        if active is not None:
            active[1].cancelled.set()

    def signal_cancel(self, job_id: str) -> None:
        """Call only after the requesting scope durably cancels the job."""
        self.bridge.call(self._signal_cancel(job_id))

    async def signal_cancel_async(self, job_id: str) -> None:
        """Call only after the requesting scope durably cancels the job."""
        await self.bridge.acall(self._signal_cancel(job_id))

    async def _drain(self) -> None:
        while self._submitting or self._tasks:
            active = tuple(self._tasks.items())
            for job_id, (_, ctx) in active:
                ctx.cancelled.set()
                try:
                    await self.jobs.cancel(job_id, ExecutionContext())
                except TychoraError:
                    # A terminal or concurrently recovered receipt still needs
                    # its owned worker drained before the owner closes.
                    pass
            if active:
                await asyncio.gather(
                    *(task for _, (task, _) in active), return_exceptions=True
                )
            else:
                await asyncio.sleep(0.01)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.bridge.call(self._drain())
        finally:
            self.bridge.close()

    async def aclose(self) -> None:
        """Drain without blocking the caller's event loop."""
        close_task = asyncio.create_task(asyncio.to_thread(self.close))
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.shield(close_task)
            raise


def build_services(
    owner: WorkspaceOwner,
    config: ResolvedConfig,
    providers: ProviderBindings,
    counter: TokenCounter,
    *,
    owned_runtimes: tuple = (),
) -> Services:
    """Construct all canonical ports for one owner; never create remote clients."""
    caches = (
        providers.caches
        or (providers.text_query.caches if providers.text_query else None)
        or (providers.visual_query.caches if providers.visual_query else None)
        or QueryCaches()
    )
    if any(
        factory is not None and factory.caches is not caches
        for factory in (providers.text_query, providers.visual_query)
    ):
        raise TychoraError("INVALID_REQUEST", "Query factories must share one cache.")
    if providers.local_text is not None and providers.text_embedder not in (
        None,
        providers.local_text.embedder,
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Local and remote document bindings conflict."
        )
    local_text = providers.local_text.embedder if providers.local_text else None
    text_embedder = local_text or providers.text_embedder
    if (
        text_embedder is not None
        and providers.text_counter is None
        and providers.text_query is None
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Document embedding requires a model text counter."
        )
    text_counter = providers.text_counter or (
        providers.text_query.counter if providers.text_query else counter
    )
    if (
        text_embedder is not None
        and text_embedder.capabilities.tokenizer_id != text_counter.tokenizer_id
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Text provider and chunking counter differ."
        )
    if (
        providers.text_query is not None
        and text_embedder is not None
        and providers.text_query.capabilities != text_embedder.capabilities
    ):
        raise TychoraError("INVALID_REQUEST", "Text query and document spaces differ.")
    coordinator = ProjectionCoordinator(
        store=owner.store,
        artifacts=owner.artifacts,
        config=config,
        counter=text_counter,
        text_embedder=text_embedder,
        local_text_embedder=local_text,
        text_index=providers.text_index,
        visual_embedder=providers.visual_embedder,
        image_index=providers.visual_index,
        graph_factory=providers.graph_factory,
    )
    repository = JobRepository(
        owner.db,
        artifacts=owner.artifacts,
        workspace_id=owner.workspace_id,
        receipts=PublicationReceipts(owner.store),
    )
    cache_root = Path(os.environ.get("TYCHORA_MODEL_CACHE", owner.path / "models"))
    sources = SourceService(
        owner,
        coordinator,
        auto_visual=bool(owned_runtimes),
        docling_models=cache_root,
        vision_credential=providers.vision_credential,
    )
    jobs = JobService(owner, repository, owner.scope)
    if owner.managed_default_owner:
        owner._managed_scope_listeners.append(
            lambda scope: setattr(jobs, "scope", scope)
        )
    query = QueryService(
        owner,
        output_counter=counter,
        text_provider=providers.text_query,
        text_index=providers.text_index,
        visual_provider=providers.visual_query,
        visual_index=providers.visual_index,
        reranker=providers.reranker,
        rerank_counter=providers.rerank_counter,
        caches=caches,
        graph=providers.graph,
        auto_rerank=bool(owned_runtimes),
    )
    evaluations = EvaluationService(owner, query, repository)
    return Services(
        collections=CollectionService(owner),
        discovery=DiscoveryService(config, Registry.from_config(config), owner),
        inspection=InspectionService(owner, graph=providers.graph),
        jobs=jobs,
        maintenance=MaintenanceService(owner, repository, evaluations),
        evaluations=evaluations,
        query=query,
        sources=sources,
        source_jobs=SourceJobScheduler(sources, jobs),
        provider_bindings=providers,
        output_counter=counter,
        owned_runtimes=owned_runtimes,
    )
