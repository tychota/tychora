"""Versioned local route contracts over owner-bound services."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from tychora.api import DEFAULT_COLLECTION_ID
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    DiscoverRequest,
    EvaluationRequest,
    ExtractionContentRequest,
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
    from tychora.contracts.collections import CollectionCreate, CollectionEdit

    @app.get("/v1/collections")
    async def collections(
        request: Request,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ):
        result = await _services(request).collections.list(
            _scope(request),
            ExecutionContext(),
            cursor=cursor,
            limit=limit,
            include_archived=include_archived,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.post("/v1/collections")
    async def create_collection(request: Request):
        parsed = CollectionCreate.from_mapping(await _body_map(request))
        result = await _services(request).collections.create(
            parsed,
            _scope(request),
            ExecutionContext(),
            managed_owner_authorized=request.state.managed_workspace_owner,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @app.get("/v1/collections/{collection_id}")
    async def get_collection(collection_id: str, request: Request):
        result = await _services(request).collections.get(
            collection_id, _scope(request), ExecutionContext()
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.patch("/v1/collections/{collection_id}")
    async def edit_collection(collection_id: str, request: Request):
        parsed = CollectionEdit.from_mapping(await _body_map(request))
        result = await _services(request).collections.edit(
            collection_id, parsed, _scope(request), ExecutionContext()
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.post("/v1/collections/{collection_id}/archive")
    async def archive_collection(collection_id: str, request: Request):
        result = await _services(request).collections.set_archived(
            collection_id, True, _scope(request), ExecutionContext()
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.post("/v1/collections/{collection_id}/reactivate")
    async def reactivate_collection(collection_id: str, request: Request):
        result = await _services(request).collections.set_archived(
            collection_id, False, _scope(request), ExecutionContext()
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/v1/collections/{collection_id}/consent")
    async def collection_consent(collection_id: str, request: Request):
        result = await _services(request).collections.consent(
            collection_id, _scope(request), ExecutionContext()
        )
        result = result.model_copy(
            update={
                "managed_grant_refresh_available": result.managed_grant_refresh_available
                and request.state.managed_workspace_owner
            }
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/v1/documents")
    async def documents(request: Request, cursor: str | None = None, limit: int = 50):
        result = await _services(request).collections.documents(
            _scope(request), ExecutionContext(), cursor=cursor, limit=limit
        )
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/v1/handshake")
    async def handshake(request: Request):
        owner = request.app.state.owner
        scope = _scope(request)
        return JSONResponse(
            {
                "managed_workspace_owner": request.state.managed_workspace_owner,
                "policy_epoch": await owner.run_write(
                    owner.policy_epoch, ExecutionContext()
                ),
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
        if after < 0 or after > 9223372036854775807:
            raise TychoraError("INVALID_REQUEST", "Event cursor is invalid.")
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

    @app.get("/v1/discovery/kinds/{kind_id}/metadata-schema")
    async def kind_metadata_schema(kind_id: str, request: Request):
        _require(request, "read")
        return _json(
            await _services(request).discovery.kind_metadata_schema(
                kind_id, _scope(request), ExecutionContext()
            )
        )

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

    @app.get(
        "/v1/sources/{source_id}/revisions/{revision_id}/extractions/{extraction_id}/selections"
    )
    async def list_selections(
        source_id: str,
        revision_id: str,
        extraction_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 20,
    ):
        _require(request, "read")
        result = await asyncio.to_thread(
            _services(request).discovery.source_selections,
            source_id,
            revision_id,
            extraction_id,
            _scope(request),
            cursor=cursor,
            limit=limit,
        )
        return _source_json(result)

    @app.get(
        "/v1/sources/{source_id}/revisions/{revision_id}/extractions/{extraction_id}/units"
    )
    async def list_extraction_units(
        source_id: str,
        revision_id: str,
        extraction_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 20,
        unit_id: str | None = None,
    ):
        _require(request, "read")
        parsed = ExtractionContentRequest(
            source_id=source_id,
            revision_id=revision_id,
            extraction_id=extraction_id,
            cursor=cursor,
            limit=limit,
            unit_id=unit_id,
        )
        return _source_json(
            await _services(request).inspection.extraction_units(
                parsed, _scope(request), ExecutionContext()
            )
        )

    @app.get(
        "/v1/sources/{source_id}/revisions/{revision_id}/extractions/{extraction_id}/windows"
    )
    async def list_extraction_windows(
        source_id: str,
        revision_id: str,
        extraction_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int = 20,
        unit_id: str | None = None,
        chunk_id: str | None = None,
    ):
        _require(request, "read")
        parsed = ExtractionContentRequest(
            source_id=source_id,
            revision_id=revision_id,
            extraction_id=extraction_id,
            cursor=cursor,
            limit=limit,
            unit_id=unit_id,
            chunk_id=chunk_id,
        )
        return _source_json(
            await _services(request).inspection.extraction_windows(
                parsed, _scope(request), ExecutionContext()
            )
        )

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
        result = await _services(request).maintenance.run(
            parsed,
            _scope(request),
            request.headers.get("idempotency-key"),
            ExecutionContext(),
        )
        return _json(result, status_code=202 if result.job else 200)

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
        parsed = EvaluationRequest.from_mapping(await _body_map(request))
        handle = await _services(request).evaluations.run(
            parsed, _scope(request), ExecutionContext()
        )
        return _json(handle, status_code=202)

    @app.post("/v1/evaluations/datasets")
    async def register_evaluation_dataset(request: Request):
        _require(request, "manage")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 70 * 1024 * 1024:
                raise TychoraError(
                    "INVALID_REQUEST", "Gold registration body exceeds byte limit"
                )
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise TychoraError(
                "INVALID_REQUEST", "Gold registration requires JSON"
            ) from None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"manifest_json", "cases_jsonl"}
            or not isinstance(payload["manifest_json"], str)
            or not isinstance(payload["cases_jsonl"], str)
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Gold registration requires manifest and cases bytes"
            )
        dataset = await _services(request).evaluations.register_dataset_bytes(
            payload["manifest_json"].encode("utf-8"),
            payload["cases_jsonl"].encode("utf-8"),
            _scope(request),
            ExecutionContext(),
        )
        return _json(dataset, status_code=201)

    @app.get("/v1/evaluations")
    async def list_evaluations(request: Request):
        _require(request, "manage")
        return JSONResponse(
            await _services(request).evaluations.list_runs(
                _scope(request), ExecutionContext()
            )
        )

    @app.get("/v1/evaluations/{evaluation_id}")
    async def get_evaluation(evaluation_id: str, request: Request):
        _require(request, "manage")
        return JSONResponse(
            await _services(request).evaluations.get(
                evaluation_id, _scope(request), ExecutionContext()
            )
        )

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
        config = await request.app.state.settings._activate_admitted(
            parsed, _scope(request)
        )
        return Response(
            content=canonical_bytes(config.inspect(owner_authorized=False)),
            media_type="application/json",
        )

    @app.get("/v1/settings/fields")
    async def settings_fields(request: Request):
        return await request.app.state.settings_draft.fields(
            _scope(request), ExecutionContext()
        )

    @app.post("/v1/settings/preview")
    async def settings_preview(request: Request):
        from tychora.contracts.administration import SettingsPatch

        patch = SettingsPatch.from_mapping(await _body_map(request))
        return await request.app.state.settings_draft.preview(
            patch, _scope(request), ExecutionContext()
        )

    @app.patch("/v1/settings")
    async def settings_apply(request: Request):
        from tychora.contracts.administration import SettingsPatch

        patch = SettingsPatch.from_mapping(await _body_map(request))
        return await request.app.state.settings_draft._apply_admitted(
            patch, _scope(request), ExecutionContext()
        )

    @app.get("/v1/models")
    async def models_catalog(request: Request):
        return await request.app.state.models.catalog(
            _scope(request), ExecutionContext()
        )

    @app.post("/v1/models/setup-plan")
    async def models_plan(request: Request):
        from tychora.contracts.administration import ModelSelections

        selected = ModelSelections.from_mapping(await _body_map(request))
        return await request.app.state.models.setup_plan(
            selected.selections, _scope(request), ExecutionContext()
        )

    @app.get("/v1/models/readiness")
    async def models_readiness(
        request: Request, selection: Annotated[list[str], Query()]
    ):
        from tychora.contracts.administration import ModelSelections

        selected = ModelSelections(selections=tuple(selection))
        return await request.app.state.models.readiness(
            selected.selections, _scope(request), ExecutionContext()
        )
