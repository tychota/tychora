"""Responsive async facade over owner-aware service coroutines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Self

from tychora.api import LocalWorkspace
from tychora.clients.sync import _context, _parsed
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import (
    DiscoverRequest,
    EvaluationRequest,
    InspectRequest,
    MaintenanceRequest,
    RelationRequest,
)
from tychora.contracts.requests import QueryRequest, SourceInput

if TYPE_CHECKING:
    from tychora.api import ProviderBindings
    from tychora.configuration.models import ResolvedConfig
    from tychora.contracts.requests import RequestScope
    from tychora.ports.tokens import TokenCounter


class _AsyncSources:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def add(
        self, source: SourceInput | Mapping, *, idempotency_key: str | None = None
    ):
        ws = self.kb._open()
        return await ws.services.source_jobs.add_async(
            ws.parse_source(source), ws.scope, idempotency_key
        )

    async def sync(self, source_id: str, *, idempotency_key: str | None = None):
        ws = self.kb._open()
        return await ws.services.source_jobs.sync_async(
            source_id, ws.scope, idempotency_key
        )


class _AsyncEvidence:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def get(
        self, unit_id: str, *, start: int | None = None, end: int | None = None
    ):
        ws = self.kb._open()
        if (start is None) != (end is None):
            raise TychoraError("INVALID_REQUEST", "Provide both range bounds.")
        selected = {"char_start": start, "char_end": end} if start is not None else None
        request = InspectRequest.from_mapping(
            {
                "subject": "evidence",
                "id": unit_id,
                "view": "full",
                **({"range": selected} if selected else {}),
            }
        )
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.inspection.inspect(request, ws.scope, ctx), ctx
        )


class _AsyncAssets:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def get(self, asset_id: str):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.inspection.inspect(
                InspectRequest(subject="assets", id=asset_id, view="full"),
                ws.scope,
                ctx,
            ),
            ctx,
        )

    async def read(self, asset_id: str) -> bytes:
        result = await self.get(asset_id)
        if result.payload is None:
            raise TychoraError("NOT_FOUND", "Registered asset payload is unavailable.")
        return result.payload


class _AsyncRelations:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def traverse(self, request: RelationRequest | Mapping):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.inspection.relations(
                _parsed(RelationRequest, request), ws.scope, ctx
            ),
            ctx,
        )


class _AsyncJobs:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def get(self, job_id: str):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(ws.services.jobs.get(job_id, ctx), ctx)

    async def wait(self, job_id: str, *, timeout: float | None = None):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.jobs.wait(
                job_id, ctx, timeout=30.0 if timeout is None else timeout
            ),
            ctx,
        )

    async def cancel(self, job_id: str):
        ws = self.kb._open()
        ctx = _context()
        status = await ws.bridge.acall(ws.services.jobs.cancel(job_id, ctx), ctx)
        await ws.services.source_jobs.signal_cancel_async(job_id)
        return status

    async def events(self, job_id: str, *, after: int = 0) -> AsyncIterator:
        ws = self.kb._open()
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        cursor = after
        while True:
            ctx = _context()
            events = await ws.bridge.acall(
                ws.services.jobs.events_after(job_id, cursor, 100, ctx), ctx
            )
            for event in events:
                cursor = event.sequence
                yield event
            if (await self.get(job_id)).state in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return
            await asyncio.sleep(0.05)


class _AsyncEvaluations:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def run(self, request: EvaluationRequest | Mapping):
        ws = self.kb._open()
        if "manage" not in ws.scope.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Evaluation is outside the grant.")
        _parsed(EvaluationRequest, request)
        raise TychoraError("CAPABILITY_MISSING", "Evaluation service is unavailable.")


class _AsyncManagement:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    async def run(
        self,
        request: MaintenanceRequest | Mapping,
        *,
        idempotency_key: str | None = None,
    ):
        ws = self.kb._open()
        parsed = _parsed(MaintenanceRequest, request)
        required = "read" if parsed.root.operation == "health" else "manage"
        if required not in ws.scope.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Maintenance is outside the grant.")
        raise TychoraError("CAPABILITY_MISSING", "Maintenance service is unavailable.")


class AsyncTychora:
    def __init__(self, workspace: LocalWorkspace) -> None:
        self._workspace = workspace
        self.sources = _AsyncSources(self)
        self.evidence = _AsyncEvidence(self)
        self.assets = _AsyncAssets(self)
        self.relations = _AsyncRelations(self)
        self.jobs = _AsyncJobs(self)
        self.evaluations = _AsyncEvaluations(self)
        self.management = _AsyncManagement(self)

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        scope: RequestScope | None = None,
        config: ResolvedConfig | None = None,
        providers: ProviderBindings | None = None,
        token_counter: TokenCounter | None = None,
    ) -> AsyncTychora:
        return cls(
            LocalWorkspace(
                path,
                scope=scope,
                config=config,
                providers=providers,
                token_counter=token_counter,
            )
        )

    @classmethod
    def connect(cls, endpoint: str, credential: str, workspace_id: str) -> AsyncTychora:
        raise TychoraError("CAPABILITY_MISSING", "Remote transport is unavailable.")

    async def __aenter__(self) -> Self:
        self._open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        close_task = asyncio.create_task(asyncio.to_thread(self._workspace.close))
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.shield(close_task)
            raise

    def _open(self) -> LocalWorkspace:
        self._workspace.require_open()
        return self._workspace

    @property
    def default_collection_id(self):
        return self._open().default_collection_id

    @property
    def scope(self):
        return self._open().scope

    async def discover(self, request: DiscoverRequest | Mapping):
        ws = self._open()
        return await ws.bridge.acall(
            asyncio.to_thread(
                ws.services.discovery.discover,
                _parsed(DiscoverRequest, request),
                ws.scope,
            )
        )

    async def search(
        self,
        request: QueryRequest | Mapping,
        *,
        scope: RequestScope | None = None,
        timeout: float | None = None,
    ):
        ws = self._open()
        parsed = _parsed(QueryRequest, request)
        granted = ws.scope if scope is None else ws.scope.narrow(scope)
        ctx = _context(timeout)
        return (
            await ws.bridge.acall(ws.services.query.search(parsed, granted, ctx), ctx)
        ).result
