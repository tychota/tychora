"""Responsive async facade over owner-aware service coroutines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from functools import wraps
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


def _admitted(method):
    """Hold shared admission until an async public operation completes."""

    @wraps(method)
    async def call(self, *args, **kwargs):
        client = self.kb if hasattr(self, "kb") else self
        ws = client._open()
        with ws.runtime.operation():
            return await method(self, *args, **kwargs)

    return call


class _AsyncSources:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    @_admitted
    async def add(
        self, source: SourceInput | Mapping, *, idempotency_key: str | None = None
    ):
        ws = self.kb._open()
        return await ws.services.source_jobs.add_async(
            ws.parse_source(source), ws.scope, idempotency_key
        )

    @_admitted
    async def sync(self, source_id: str, *, idempotency_key: str | None = None):
        ws = self.kb._open()
        return await ws.services.source_jobs.sync_async(
            source_id, ws.scope, idempotency_key
        )

    @_admitted
    async def revisions(
        self, source_id: str, *, cursor: str | None = None, limit: int = 20
    ):
        ws = self.kb._open()
        return await asyncio.to_thread(
            ws.services.discovery.source_revisions,
            source_id,
            ws.scope,
            cursor=cursor,
            limit=limit,
        )

    @_admitted
    async def extractions(
        self,
        source_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ):
        ws = self.kb._open()
        return await asyncio.to_thread(
            ws.services.discovery.source_extractions,
            source_id,
            revision_id,
            ws.scope,
            cursor=cursor,
            limit=limit,
        )

    @_admitted
    async def channels(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ):
        ws = self.kb._open()
        return await asyncio.to_thread(
            ws.services.discovery.source_channels,
            source_id,
            revision_id,
            extraction_id,
            ws.scope,
            cursor=cursor,
            limit=limit,
        )


class _AsyncEvidence:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    @_admitted
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

    @_admitted
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

    @_admitted
    async def read(self, asset_id: str) -> bytes:
        result = await self.get(asset_id)
        if result.payload is None:
            raise TychoraError("NOT_FOUND", "Registered asset payload is unavailable.")
        return result.payload


class _AsyncRelations:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    @_admitted
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

    @_admitted
    async def get(self, job_id: str):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(ws.services.jobs.get(job_id, ctx), ctx)

    @_admitted
    async def wait(self, job_id: str, *, timeout: float | None = None):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.jobs.wait(
                job_id, ctx, timeout=30.0 if timeout is None else timeout
            ),
            ctx,
        )

    @_admitted
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
            with ws.runtime.operation():
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

    @_admitted
    async def events_after(self, job_id: str, *, after: int = 0):
        ws = self.kb._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.jobs.events_after(job_id, after, 100, ctx), ctx
        )


class _AsyncEvaluations:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    @_admitted
    async def run(self, request: EvaluationRequest | Mapping):
        ws = self.kb._open()
        if "manage" not in ws.scope.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Evaluation is outside the grant.")
        _parsed(EvaluationRequest, request)
        raise TychoraError("CAPABILITY_MISSING", "Evaluation service is unavailable.")


class _AsyncManagement:
    def __init__(self, kb: AsyncTychora) -> None:
        self.kb = kb

    @_admitted
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
        if parsed.root.operation == "setup":
            from tychora.application.setup import run_public_setup

            return await ws.bridge.acall(
                run_public_setup(
                    ws.owner, ws.services.jobs.repository, ws.scope, parsed.root
                )
            )
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
        credential_resolver: Callable[[str], str] | None = None,
    ) -> AsyncTychora:
        return cls(
            LocalWorkspace(
                path,
                scope=scope,
                config=config,
                providers=providers,
                token_counter=token_counter,
                credential_resolver=credential_resolver,
            )
        )

    @classmethod
    def connect(cls, endpoint: str, credential: str, workspace_id: str):
        from tychora.interfaces.http.client import AsyncHTTPClient

        return AsyncHTTPClient(endpoint, credential, workspace_id)

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

    @_admitted
    async def discover(self, request: DiscoverRequest | Mapping):
        ws = self._open()
        return await ws.bridge.acall(
            asyncio.to_thread(
                ws.services.discovery.discover,
                _parsed(DiscoverRequest, request),
                ws.scope,
            )
        )

    @_admitted
    async def inspect(self, request: InspectRequest | Mapping):
        ws = self._open()
        ctx = _context()
        return await ws.bridge.acall(
            ws.services.inspection.inspect(
                _parsed(InspectRequest, request), ws.scope, ctx
            ),
            ctx,
        )

    @_admitted
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
