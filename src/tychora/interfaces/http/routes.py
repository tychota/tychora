"""Versioned local route contracts over owner-bound services."""

from __future__ import annotations

import asyncio
import copy
import math
import time
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from tychora.api import DEFAULT_COLLECTION_ID
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    DiscoverRequest,
    EvaluationRequest,
    InspectRequest,
    MaintenanceRequest,
    RelationRequest,
    SettingsActivation,
)
from tychora.contracts.requests import (
    FileSource,
    FolderSource,
    QueryRequest,
    RepositorySource,
    SourceInput,
)
from tychora.formatting.render import OutputProfile
from tychora.ports.execution import ExecutionContext

from .events import event_stream


def _services(request: Request):
    return request.app.state.runtime.services


def _scope(request: Request):
    return request.state.request_scope


def _require(request: Request, capability: str) -> None:
    if capability not in _scope(request).capabilities:
        raise TychoraError("FORBIDDEN_SCOPE", "Operation is outside the grant.")


def _source(request: Request, value: Mapping) -> SourceInput:
    normalized = dict(value)
    if normalized.get("collection_id") == "default":
        if str(DEFAULT_COLLECTION_ID) not in _scope(request).collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Default collection is outside the grant."
            )
        normalized["collection_id"] = str(DEFAULT_COLLECTION_ID)
    source = SourceInput.from_mapping(normalized)
    if isinstance(source.root, (FileSource, FolderSource, RepositorySource)):
        roots = tuple(
            Path(path)
            for path in request.app.state.owner.config.values.acquisition.roots
        )
        path = Path(source.root.path).resolve(strict=False)
        if not any(path.is_relative_to(root) for root in roots):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Local source path is outside registered roots."
            )
    return source


async def _body_map(request: Request) -> Mapping:
    value = await request.json()
    if not isinstance(value, Mapping):
        raise TychoraError("INVALID_REQUEST", "JSON object is required.")
    return value


def _selected_query(request: Request):
    accept = request.headers.get("accept", "application/json")
    if accept in {"*/*", "application/json"}:
        service = copy.copy(_services(request).query)
        service.profiles = (OutputProfile(renderer="canonical-json@1"),)
        return service, "application/json"
    if accept == "application/vnd.tychora.hierarchical+json":
        service = copy.copy(_services(request).query)
        service.profiles = (OutputProfile(renderer="hierarchical-evidence@1"),)
        return service, accept
    raise TychoraError("NOT_ACCEPTABLE", "Requested output media type is unavailable.")


def _json(value, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value.model_dump(mode="json"), status_code=status_code)


def _source_json(value) -> Response:
    payload = value.model_dump_json().encode("utf-8")
    if len(payload) > 1048576:
        raise TychoraError("BUDGET_TOO_SMALL", "Source response exceeds local limit.")
    return Response(content=payload, media_type="application/json")


def _inspection_json(value) -> Response:
    payload = value.model_dump_json().encode("utf-8")
    if len(payload) > 24 * 1024 * 1024:
        raise TychoraError(
            "OUTPUT_BUDGET_EXCEEDED", "Inspection response exceeds local limit."
        )
    return Response(content=payload, media_type="application/json")


def register_routes(app: FastAPI) -> None:
    @app.get("/v1/handshake")
    async def handshake(request: Request):
        owner = request.app.state.owner
        scope = _scope(request)
        return JSONResponse(
            {
                "schema_version": "1",
                "protocol_version": 1,
                "workspace_id": owner.workspace_id,
                "instance_id": owner._runtime.instance_id,
                "scope": scope.model_dump(mode="json"),
                "default_collection_id": str(DEFAULT_COLLECTION_ID)
                if str(DEFAULT_COLLECTION_ID) in scope.collection_ids
                else None,
            }
        )

    @app.post("/v1/sources")
    async def add_source(request: Request):
        _require(request, "write")
        source = _source(request, await _body_map(request))
        handle = await _services(request).source_jobs.add_async(
            source, _scope(request), request.headers.get("idempotency-key")
        )
        return _json(handle, status_code=202)

    @app.post("/v1/sources/{source_id}/sync")
    async def sync_source(source_id: str, request: Request):
        _require(request, "write")
        handle = await _services(request).source_jobs.sync_async(
            source_id, _scope(request), request.headers.get("idempotency-key")
        )
        return _json(handle, status_code=202)

    @app.get("/v1/jobs")
    async def list_jobs(request: Request, cursor: str | None = None, limit: int = 100):
        _require(request, "read")
        page = (
            await _services(request)
            .jobs_for_scope(_scope(request))
            .list(ExecutionContext(), cursor=cursor, limit=limit)
        )
        return _json(page)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        return _json(
            await _services(request)
            .jobs_for_scope(_scope(request))
            .get(job_id, ExecutionContext())
        )

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        jobs = _services(request).jobs_for_scope(_scope(request))
        status = await jobs.cancel(job_id, ExecutionContext())
        await _services(request).source_jobs.signal_cancel_async(job_id)
        return _json(status)

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        raw = request.headers.get("last-event-id", "0")
        if (
            len(raw) > 19
            or not raw.isascii()
            or not raw.isdecimal()
            or int(raw) > 9223372036854775807
        ):
            raise TychoraError("INVALID_REQUEST", "Event cursor is invalid.")
        # Authorize before starting the stream so failures remain canonical JSON.
        await (
            _services(request)
            .jobs_for_scope(_scope(request))
            .get(job_id, ExecutionContext())
        )
        return event_stream(_services(request), _scope(request), job_id, int(raw))

    @app.get("/v1/jobs/{job_id}/events/page")
    async def job_event_page(job_id: str, request: Request, after: int = 0):
        events = (
            await _services(request)
            .jobs_for_scope(_scope(request))
            .events_after(job_id, after, 100, ExecutionContext())
        )
        payload = canonical_bytes([event.model_dump(mode="json") for event in events])
        if len(payload) > 1024 * 1024:
            raise TychoraError(
                "OUTPUT_BUDGET_EXCEEDED", "Job event page exceeds local limit."
            )
        return Response(content=payload, media_type="application/json")

    @app.post("/v1/inspect")
    async def inspect_record(request: Request):
        _require(request, "read")
        parsed = InspectRequest.from_mapping(await _body_map(request))
        result = await _services(request).inspection.inspect(
            parsed, _scope(request), ExecutionContext()
        )
        return _inspection_json(result)

    @app.get("/v1/discovery")
    async def discover_default(
        request: Request,
        subject: str = "kinds",
        cursor: str | None = None,
        limit: int = 100,
    ):
        _require(request, "read")
        parsed = DiscoverRequest.from_mapping(
            {"subject": subject, "cursor": cursor, "limit": limit}
        )
        result = await asyncio.to_thread(
            _services(request).discovery.discover, parsed, _scope(request)
        )
        return _source_json(result) if parsed.subject == "sources" else _json(result)

    @app.get("/v1/discovery/{subject}")
    async def discover_subject(
        subject: str, request: Request, cursor: str | None = None, limit: int = 100
    ):
        _require(request, "read")
        parsed = DiscoverRequest.from_mapping(
            {"subject": subject, "cursor": cursor, "limit": limit}
        )
        result = await asyncio.to_thread(
            _services(request).discovery.discover, parsed, _scope(request)
        )
        return _source_json(result) if parsed.subject == "sources" else _json(result)

    @app.get("/v1/sources")
    async def list_sources(
        request: Request, cursor: str | None = None, limit: int = 100
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.discover,
            DiscoverRequest(subject="sources", cursor=cursor, limit=limit),
            _scope(request),
        )
        return _source_json(result)

    @app.get("/v1/sources/{source_id}")
    async def get_source(source_id: str, request: Request):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.definition,
            "sources",
            source_id,
            _scope(request),
        )
        return _source_json(result)

    @app.get("/v1/sources/{source_id}/revisions")
    async def list_revisions(
        source_id: str, request: Request, cursor: str | None = None, limit: int = 20
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.source_revisions,
            source_id,
            _scope(request),
            cursor=cursor,
            limit=limit,
        )
        return _source_json(result)

    @app.get("/v1/sources/{source_id}/revisions/{revision_id}/extractions")
    async def list_extractions(
        source_id: str,
        revision_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 20,
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.source_extractions,
            source_id,
            revision_id,
            _scope(request),
            cursor=cursor,
            limit=limit,
        )
        return _source_json(result)

    @app.get(
        "/v1/sources/{source_id}/revisions/{revision_id}/extractions/{extraction_id}/channels"
    )
    async def list_channels(
        source_id: str,
        revision_id: str,
        extraction_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 50,
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.source_channels,
            source_id,
            revision_id,
            extraction_id,
            _scope(request),
            cursor=cursor,
            limit=limit,
        )
        return _source_json(result)

    @app.get("/v1/evidence/{unit_id}")
    async def get_evidence(
        unit_id: str, request: Request, start: int | None = None, end: int | None = None
    ):
        _require(request, "read")
        if (start is None) != (end is None):
            raise TychoraError("INVALID_REQUEST", "Provide both range bounds.")
        selected = {"char_start": start, "char_end": end} if start is not None else None
        parsed = InspectRequest.from_mapping(
            {
                "subject": "evidence",
                "id": unit_id,
                "view": "full",
                **({"range": selected} if selected else {}),
            }
        )
        return _json(
            await _services(request).inspection.inspect(
                parsed, _scope(request), ExecutionContext()
            )
        )

    @app.get("/v1/assets/{asset_id}")
    async def get_asset(asset_id: str, request: Request):
        _require(request, "read")
        result = await _services(request).inspection.inspect(
            InspectRequest(subject="assets", id=asset_id, view="full"),
            _scope(request),
            ExecutionContext(),
        )
        return _json(result.model_copy(update={"payload": None}))

    @app.get("/v1/assets/{asset_id}/content")
    async def get_asset_content(asset_id: str, request: Request):
        _require(request, "read")
        result = await _services(request).inspection.inspect(
            InspectRequest(subject="assets", id=asset_id, view="full"),
            _scope(request),
            ExecutionContext(),
        )
        if result.payload is None:
            raise TychoraError("NOT_FOUND", "Registered asset payload is unavailable.")
        return Response(
            content=result.payload,
            media_type=result.media_type or "application/octet-stream",
        )

    @app.post("/v1/relations")
    async def relations(request: Request):
        _require(request, "read")
        parsed = RelationRequest.from_mapping(await _body_map(request))
        result = await _services(request).inspection.relations(
            parsed, _scope(request), ExecutionContext()
        )
        return _json(result)

    @app.post("/v1/queries")
    async def query(request: Request, timeout: float | None = None):
        _require(request, "read")
        if timeout is not None and (
            not math.isfinite(timeout) or not 0 <= timeout <= 300
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Timeout must be between zero and 300 seconds."
            )
        parsed = QueryRequest.from_mapping(await _body_map(request))
        service, media_type = _selected_query(request)
        ctx = ExecutionContext(
            deadline=time.monotonic() + timeout if timeout is not None else None
        )
        delivered = await service.search(parsed, _scope(request), ctx)
        return Response(content=delivered.payloads[0], media_type=media_type)

    @app.get("/v1/queries")
    async def list_queries(
        request: Request, cursor: str | None = None, limit: int = 100
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.discover,
            DiscoverRequest(subject="runs", cursor=cursor, limit=limit),
            _scope(request),
        )
        return _json(result)

    @app.get("/v1/queries/{run_id}")
    async def get_query(run_id: str, request: Request):
        _require(request, "read")
        result = await _services(request).inspection.inspect(
            InspectRequest(subject="runs", id=run_id),
            _scope(request),
            ExecutionContext(),
        )
        return _json(result)

    @app.post("/v1/queries/{run_id}/replay")
    async def replay_query(run_id: str, request: Request):
        _require(request, "read")
        service, media_type = _selected_query(request)
        delivered = await service.replay(run_id, _scope(request), ExecutionContext())
        return Response(content=delivered.payloads[0], media_type=media_type)

    @app.post("/v1/jobs")
    async def run_maintenance(request: Request):
        parsed = MaintenanceRequest.from_mapping(await _body_map(request))
        _require(request, "read" if parsed.root.operation == "health" else "manage")
        if parsed.root.operation == "setup":
            from tychora.application.setup import run_public_setup

            result = await run_public_setup(
                request.app.state.owner,
                _services(request).jobs.repository,
                _scope(request),
                parsed.root,
            )
            return _json(result, status_code=202)
        raise TychoraError("CAPABILITY_MISSING", "Maintenance service is unavailable.")

    @app.post("/v1/jobs/{job_id}/retry")
    async def retry_job(job_id: str, request: Request):
        _require(request, "write")
        handle = await _services(request).source_jobs.retry_async(
            job_id, _scope(request)
        )
        return _json(handle, status_code=202)

    @app.post("/v1/evaluations")
    async def run_evaluation(request: Request):
        _require(request, "manage")
        EvaluationRequest.from_mapping(await _body_map(request))
        raise TychoraError("CAPABILITY_MISSING", "Evaluation service is unavailable.")

    @app.get("/v1/evaluations")
    async def list_evaluations(request: Request):
        _require(request, "manage")
        raise TychoraError("CAPABILITY_MISSING", "Evaluation service is unavailable.")

    @app.get("/v1/evaluations/{evaluation_id}")
    async def get_evaluation(evaluation_id: str, request: Request):
        _require(request, "manage")
        raise TychoraError("CAPABILITY_MISSING", "Evaluation service is unavailable.")

    @app.get("/v1/settings")
    async def get_settings(request: Request):
        _require(request, "read")
        return Response(
            content=canonical_bytes(
                request.app.state.owner.config.inspect(owner_authorized=False)
            ),
            media_type="application/json",
        )

    @app.put("/v1/settings")
    async def put_settings(request: Request):
        _require(request, "manage")
        parsed = SettingsActivation.from_mapping(await _body_map(request))
        config = await request.app.state.settings.activate(parsed)
        return Response(
            content=canonical_bytes(config.inspect(owner_authorized=False)),
            media_type="application/json",
        )
