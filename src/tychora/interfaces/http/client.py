"""Typed local HTTP clients and protected runtime delegation."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import QueryResult
from tychora.contracts.jobs import JobStatus
from tychora.contracts.operations import (
    DiscoverRequest,
    DiscoveryResult,
    EvaluationRequest,
    InspectionResult,
    InspectRequest,
    JobEvent,
    JobHandle,
    MaintenanceRequest,
    MaintenanceResult,
    RelationRequest,
    RelationResult,
    SourceChannelPage,
    SourceExtractionPage,
    SourceRevisionPage,
    SourceSelectionPage,
)
from tychora.contracts.requests import QueryRequest, RequestScope, SourceInput


def _endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Endpoint must be an exact loopback HTTP authority."
        )
    return f"http://{parsed.netloc}"


def _checked(response: httpx.Response) -> httpx.Response:
    if response.status_code >= 400:
        try:
            value = response.json()
            raise TychoraError(
                value["code"], value["message"], retryable=value.get("retryable", False)
            )
        except (ValueError, KeyError, TypeError):
            raise TychoraError(
                "PROVIDER_FAILURE", "Local HTTP response is invalid."
            ) from None
    return response


def _handshake(
    value: Mapping, expected_workspace_id: str, expected_instance_id: str | None = None
):
    if (
        value.get("protocol_version") != 1
        or value.get("workspace_id") != expected_workspace_id
        or not isinstance(value.get("instance_id"), str)
        or (
            expected_instance_id is not None
            and value["instance_id"] != expected_instance_id
        )
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Local endpoint identity or protocol differs."
        )
    try:
        scope = RequestScope.model_validate_json(json.dumps(value["scope"]))
    except (ValueError, KeyError, TypeError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Local endpoint scope is invalid."
        ) from None
    return scope, value.get("default_collection_id")


def _payload(value):
    if isinstance(value, Mapping):
        return dict(value)
    return value.model_dump(mode="json")


def _scope_header(scope: RequestScope | None) -> dict[str, str]:
    return {"X-Tychora-Scope": scope.model_dump_json()} if scope is not None else {}


def _timeout(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or value < 0 or value > 300):
        raise TychoraError(
            "INVALID_REQUEST", "Timeout must be between zero and 300 seconds."
        )
    return value


def _id(value: str) -> str:
    return quote(value, safe="")


def _page_params(cursor: str | None, limit: int) -> dict[str, str | int]:
    return {"limit": limit, **({"cursor": cursor} if cursor is not None else {})}


class _SyncSources:
    def __init__(self, client: HTTPClient):
        self.client = client

    def add(
        self, source: SourceInput | Mapping, *, idempotency_key: str | None = None
    ) -> JobHandle:
        response = self.client._request(
            "POST",
            "/v1/sources",
            json=_payload(source),
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return JobHandle.model_validate_json(response.content)

    def sync(self, source_id: str, *, idempotency_key: str | None = None) -> JobHandle:
        response = self.client._request(
            "POST",
            f"/v1/sources/{_id(source_id)}/sync",
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return JobHandle.model_validate_json(response.content)

    def revisions(
        self, source_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> SourceRevisionPage:
        return SourceRevisionPage.model_validate_json(
            self.client._request(
                "GET",
                f"/v1/sources/{_id(source_id)}/revisions",
                params=_page_params(cursor, limit),
            ).content
        )

    def extractions(
        self,
        source_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceExtractionPage:
        return SourceExtractionPage.model_validate_json(
            self.client._request(
                "GET",
                f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions",
                params=_page_params(cursor, limit),
            ).content
        )

    def channels(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SourceChannelPage:
        return SourceChannelPage.model_validate_json(
            self.client._request(
                "GET",
                f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions/{_id(extraction_id)}/channels",
                params=_page_params(cursor, limit),
            ).content
        )

    def selections(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceSelectionPage:
        return SourceSelectionPage.model_validate_json(
            self.client._request(
                "GET",
                f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions/{_id(extraction_id)}/selections",
                params=_page_params(cursor, limit),
            ).content
        )


class _SyncEvidence:
    def __init__(self, client: HTTPClient):
        self.client = client

    def get(
        self, unit_id: str, *, start: int | None = None, end: int | None = None
    ) -> InspectionResult:
        params = (
            {"start": start, "end": end}
            if start is not None or end is not None
            else None
        )
        return InspectionResult.model_validate_json(
            self.client._request(
                "GET", f"/v1/evidence/{_id(unit_id)}", params=params
            ).content
        )


class _SyncAssets:
    def __init__(self, client: HTTPClient):
        self.client = client

    def get(self, asset_id: str) -> InspectionResult:
        return InspectionResult.model_validate_json(
            self.client._request("GET", f"/v1/assets/{_id(asset_id)}").content
        )

    def read(self, asset_id: str) -> bytes:
        return self.client._request(
            "GET", f"/v1/assets/{_id(asset_id)}/content"
        ).content


class _SyncRelations:
    def __init__(self, client: HTTPClient):
        self.client = client

    def traverse(self, request: RelationRequest | Mapping) -> RelationResult:
        return RelationResult.model_validate_json(
            self.client._request(
                "POST", "/v1/relations", json=_payload(request)
            ).content
        )


class _SyncJobs:
    def __init__(self, client: HTTPClient):
        self.client = client

    def get(self, job_id: str) -> JobStatus:
        return JobStatus.model_validate_json(
            self.client._request("GET", f"/v1/jobs/{_id(job_id)}").content
        )

    def wait(self, job_id: str, *, timeout: float | None = None) -> JobStatus:
        allowed = 30.0 if timeout is None else _timeout(timeout)
        assert allowed is not None
        end = time.monotonic() + allowed
        while True:
            status = self.get(job_id)
            if (
                status.state in {"succeeded", "failed", "cancelled", "interrupted"}
                or time.monotonic() >= end
            ):
                return status
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def cancel(self, job_id: str) -> JobStatus:
        return JobStatus.model_validate_json(
            self.client._request("POST", f"/v1/jobs/{_id(job_id)}/cancel").content
        )

    def events(self, job_id: str, *, after: int = 0):
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        self.client._require_open()
        try:
            with self.client._http.stream(
                "GET",
                f"/v1/jobs/{_id(job_id)}/events",
                headers={"Last-Event-ID": str(after)},
                timeout=None,
            ) as response:
                _checked(response)
                event_type = None
                data = None
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                    elif not line:
                        if event_type == "job" and data is not None:
                            yield JobEvent.model_validate_json(data)
                        if event_type == "refresh":
                            return
                        event_type = data = None
        except httpx.HTTPError:
            raise TychoraError(
                "WORKSPACE_BUSY", "Local HTTP owner is unavailable."
            ) from None

    def events_after(self, job_id: str, *, after: int = 0) -> tuple[JobEvent, ...]:
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        response = self.client._request(
            "GET", f"/v1/jobs/{_id(job_id)}/events/page", params={"after": after}
        )
        return TypeAdapter(tuple[JobEvent, ...]).validate_json(response.content)


class _SyncEvaluations:
    def __init__(self, client: HTTPClient):
        self.client = client

    def run(self, request: EvaluationRequest | Mapping):
        return JobHandle.model_validate_json(
            self.client._request(
                "POST", "/v1/evaluations", json=_payload(request)
            ).content
        )

    def register_dataset(self, path: str | Path):
        directory = Path(path)
        manifest = (directory / "manifest.json").read_bytes()
        cases = (directory / "cases.jsonl").read_bytes()
        if len(manifest) > 1024 * 1024 or len(cases) > 64 * 1024 * 1024:
            raise TychoraError("INVALID_REQUEST", "Gold dataset exceeds byte limits")
        return self.client._request(
            "POST",
            "/v1/evaluations/datasets",
            json={
                "manifest_json": manifest.decode("utf-8"),
                "cases_jsonl": cases.decode("utf-8"),
            },
        ).json()

    def list(self):
        return self.client._request("GET", "/v1/evaluations").json()

    def get(self, evaluation_id: str):
        return self.client._request(
            "GET", f"/v1/evaluations/{_id(evaluation_id)}"
        ).json()


class _SyncManagement:
    def __init__(self, client: HTTPClient):
        self.client = client

    def run(
        self,
        request: MaintenanceRequest | Mapping,
        *,
        idempotency_key: str | None = None,
    ):
        return MaintenanceResult.model_validate_json(
            self.client._request(
                "POST",
                "/v1/jobs",
                json=_payload(request),
                headers={"Idempotency-Key": idempotency_key}
                if idempotency_key
                else None,
            ).content
        )


class HTTPClient:
    """Blocking public namespace over a verified local HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        credential: str,
        workspace_id: str,
        *,
        instance_id: str | None = None,
    ) -> None:
        self.endpoint = _endpoint(endpoint)
        self._http = httpx.Client(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {credential}"},
            timeout=30,
            trust_env=False,
        )
        self.closed = False
        try:
            response = self._request("GET", "/v1/handshake")
            self.scope, self._default_collection_id = _handshake(
                response.json(), workspace_id, instance_id
            )
        except BaseException:
            self._http.close()
            raise
        self.sources = _SyncSources(self)
        self.evidence = _SyncEvidence(self)
        self.assets = _SyncAssets(self)
        self.relations = _SyncRelations(self)
        self.jobs = _SyncJobs(self)
        self.evaluations = _SyncEvaluations(self)
        self.management = _SyncManagement(self)

    def _require_open(self):
        if self.closed:
            raise RuntimeError("Tychora client is closed")

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._require_open()
        try:
            return _checked(self._http.request(method, path, **kwargs))
        except httpx.TimeoutException:
            raise TychoraError(
                "DEADLINE_EXCEEDED", "Local HTTP request timed out."
            ) from None
        except httpx.HTTPError:
            raise TychoraError(
                "WORKSPACE_BUSY", "Local HTTP owner is unavailable."
            ) from None

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            self._http.close()

    def dashboard_launch_link(self) -> str:
        """Mint a one-time dashboard link from the live owner (owner token only)."""
        value = self._request("POST", "/v1/session/launch-codes").json()
        url = value.get("url") if isinstance(value, dict) else None
        if not isinstance(url, str) or not url.startswith(
            self.endpoint + "/dashboard/#launch="
        ):
            raise TychoraError("PROVIDER_FAILURE", "Local HTTP response is invalid.")
        return url

    @property
    def default_collection_id(self) -> UUID:
        self._require_open()
        if self._default_collection_id is None:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Default collection is outside the grant."
            )
        return UUID(self._default_collection_id)

    def discover(self, request: DiscoverRequest | Mapping) -> DiscoveryResult:
        parsed = (
            DiscoverRequest.from_mapping(request)
            if isinstance(request, Mapping)
            else request
        )
        response = self._request(
            "GET",
            f"/v1/discovery/{parsed.subject}",
            params={
                "limit": parsed.limit,
                **({"cursor": parsed.cursor} if parsed.cursor else {}),
            },
        )
        return DiscoveryResult.model_validate_json(response.content)

    def inspect(self, request: InspectRequest | Mapping) -> InspectionResult:
        return InspectionResult.model_validate_json(
            self._request("POST", "/v1/inspect", json=_payload(request)).content
        )

    def search(
        self,
        request: QueryRequest | Mapping,
        *,
        scope: RequestScope | None = None,
        timeout: float | None = None,
    ) -> QueryResult:
        parsed = (
            QueryRequest.from_mapping(request)
            if isinstance(request, Mapping)
            else request
        )
        response = self._request(
            "POST",
            "/v1/queries",
            json=parsed.model_dump(mode="json"),
            headers=_scope_header(scope),
            params={"timeout": _timeout(timeout)} if timeout is not None else None,
        )
        return QueryResult.model_validate_json(response.content)


class _AsyncSources:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def add(
        self, source: SourceInput | Mapping, *, idempotency_key: str | None = None
    ) -> JobHandle:
        response = await self.client._request(
            "POST",
            "/v1/sources",
            json=_payload(source),
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return JobHandle.model_validate_json(response.content)

    async def sync(
        self, source_id: str, *, idempotency_key: str | None = None
    ) -> JobHandle:
        response = await self.client._request(
            "POST",
            f"/v1/sources/{_id(source_id)}/sync",
            headers={"Idempotency-Key": idempotency_key} if idempotency_key else None,
        )
        return JobHandle.model_validate_json(response.content)

    async def revisions(
        self, source_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> SourceRevisionPage:
        return SourceRevisionPage.model_validate_json(
            (
                await self.client._request(
                    "GET",
                    f"/v1/sources/{_id(source_id)}/revisions",
                    params=_page_params(cursor, limit),
                )
            ).content
        )

    async def extractions(
        self,
        source_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceExtractionPage:
        return SourceExtractionPage.model_validate_json(
            (
                await self.client._request(
                    "GET",
                    f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions",
                    params=_page_params(cursor, limit),
                )
            ).content
        )

    async def channels(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SourceChannelPage:
        return SourceChannelPage.model_validate_json(
            (
                await self.client._request(
                    "GET",
                    f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions/{_id(extraction_id)}/channels",
                    params=_page_params(cursor, limit),
                )
            ).content
        )

    async def selections(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceSelectionPage:
        return SourceSelectionPage.model_validate_json(
            (
                await self.client._request(
                    "GET",
                    f"/v1/sources/{_id(source_id)}/revisions/{_id(revision_id)}/extractions/{_id(extraction_id)}/selections",
                    params=_page_params(cursor, limit),
                )
            ).content
        )


class _AsyncEvidence:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def get(
        self, unit_id: str, *, start: int | None = None, end: int | None = None
    ) -> InspectionResult:
        params = (
            {"start": start, "end": end}
            if start is not None or end is not None
            else None
        )
        return InspectionResult.model_validate_json(
            (
                await self.client._request(
                    "GET", f"/v1/evidence/{_id(unit_id)}", params=params
                )
            ).content
        )


class _AsyncAssets:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def get(self, asset_id: str) -> InspectionResult:
        return InspectionResult.model_validate_json(
            (await self.client._request("GET", f"/v1/assets/{_id(asset_id)}")).content
        )

    async def read(self, asset_id: str) -> bytes:
        return (
            await self.client._request("GET", f"/v1/assets/{_id(asset_id)}/content")
        ).content


class _AsyncRelations:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def traverse(self, request: RelationRequest | Mapping) -> RelationResult:
        return RelationResult.model_validate_json(
            (
                await self.client._request(
                    "POST", "/v1/relations", json=_payload(request)
                )
            ).content
        )


class _AsyncJobs:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def get(self, job_id: str) -> JobStatus:
        return JobStatus.model_validate_json(
            (await self.client._request("GET", f"/v1/jobs/{_id(job_id)}")).content
        )

    async def wait(self, job_id: str, *, timeout: float | None = None) -> JobStatus:
        allowed = 30.0 if timeout is None else _timeout(timeout)
        assert allowed is not None
        end = time.monotonic() + allowed
        while True:
            status = await self.get(job_id)
            if (
                status.state in {"succeeded", "failed", "cancelled", "interrupted"}
                or time.monotonic() >= end
            ):
                return status
            await asyncio.sleep(min(0.05, max(0.0, end - time.monotonic())))

    async def cancel(self, job_id: str) -> JobStatus:
        return JobStatus.model_validate_json(
            (
                await self.client._request("POST", f"/v1/jobs/{_id(job_id)}/cancel")
            ).content
        )

    async def events(self, job_id: str, *, after: int = 0):
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        await self.client._ensure_handshake()
        try:
            async with self.client._http.stream(
                "GET",
                f"/v1/jobs/{_id(job_id)}/events",
                headers={"Last-Event-ID": str(after)},
                timeout=None,
            ) as response:
                _checked(response)
                event_type = None
                data = None
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                    elif not line:
                        if event_type == "job" and data is not None:
                            yield JobEvent.model_validate_json(data)
                        if event_type == "refresh":
                            return
                        event_type = data = None
        except httpx.HTTPError:
            raise TychoraError(
                "WORKSPACE_BUSY", "Local HTTP owner is unavailable."
            ) from None

    async def events_after(
        self, job_id: str, *, after: int = 0
    ) -> tuple[JobEvent, ...]:
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        response = await self.client._request(
            "GET", f"/v1/jobs/{_id(job_id)}/events/page", params={"after": after}
        )
        return TypeAdapter(tuple[JobEvent, ...]).validate_json(response.content)


class _AsyncEvaluations:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def run(self, request: EvaluationRequest | Mapping):
        return JobHandle.model_validate_json(
            (
                await self.client._request(
                    "POST", "/v1/evaluations", json=_payload(request)
                )
            ).content
        )

    async def register_dataset(self, path: str | Path):
        directory = Path(path)
        manifest = (directory / "manifest.json").read_bytes()
        cases = (directory / "cases.jsonl").read_bytes()
        if len(manifest) > 1024 * 1024 or len(cases) > 64 * 1024 * 1024:
            raise TychoraError("INVALID_REQUEST", "Gold dataset exceeds byte limits")
        return (
            await self.client._request(
                "POST",
                "/v1/evaluations/datasets",
                json={
                    "manifest_json": manifest.decode("utf-8"),
                    "cases_jsonl": cases.decode("utf-8"),
                },
            )
        ).json()

    async def list(self):
        return (await self.client._request("GET", "/v1/evaluations")).json()

    async def get(self, evaluation_id: str):
        return (
            await self.client._request("GET", f"/v1/evaluations/{_id(evaluation_id)}")
        ).json()


class _AsyncManagement:
    def __init__(self, client: AsyncHTTPClient):
        self.client = client

    async def run(
        self,
        request: MaintenanceRequest | Mapping,
        *,
        idempotency_key: str | None = None,
    ):
        return MaintenanceResult.model_validate_json(
            (
                await self.client._request(
                    "POST",
                    "/v1/jobs",
                    json=_payload(request),
                    headers={"Idempotency-Key": idempotency_key}
                    if idempotency_key
                    else None,
                )
            ).content
        )


class AsyncHTTPClient:
    """Async public namespace; handshake is awaited before first operation."""

    def __init__(
        self,
        endpoint: str,
        credential: str,
        workspace_id: str,
        *,
        instance_id: str | None = None,
    ) -> None:
        self.endpoint = _endpoint(endpoint)
        self._http = httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {credential}"},
            timeout=30,
            trust_env=False,
        )
        self._workspace_id = workspace_id
        self._instance_id = instance_id
        self.scope = None
        self._default_collection_id = None
        self.closed = False
        self.sources = _AsyncSources(self)
        self.evidence = _AsyncEvidence(self)
        self.assets = _AsyncAssets(self)
        self.relations = _AsyncRelations(self)
        self.jobs = _AsyncJobs(self)
        self.evaluations = _AsyncEvaluations(self)
        self.management = _AsyncManagement(self)

    async def _ensure_handshake(self):
        if self.closed:
            raise RuntimeError("Tychora client is closed")
        if self.scope is None:
            response = await self._request("GET", "/v1/handshake", handshake=True)
            self.scope, self._default_collection_id = _handshake(
                response.json(), self._workspace_id, self._instance_id
            )

    async def _request(
        self, method: str, path: str, *, handshake: bool = False, **kwargs
    ) -> httpx.Response:
        if not handshake:
            await self._ensure_handshake()
        try:
            return _checked(await self._http.request(method, path, **kwargs))
        except httpx.TimeoutException:
            raise TychoraError(
                "DEADLINE_EXCEEDED", "Local HTTP request timed out."
            ) from None
        except httpx.HTTPError:
            raise TychoraError(
                "WORKSPACE_BUSY", "Local HTTP owner is unavailable."
            ) from None

    async def __aenter__(self):
        await self._ensure_handshake()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        if not self.closed:
            self.closed = True
            await self._http.aclose()

    @property
    def default_collection_id(self) -> UUID:
        if self.scope is None:
            raise RuntimeError("Async client handshake is pending")
        if self._default_collection_id is None:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Default collection is outside the grant."
            )
        return UUID(self._default_collection_id)

    async def discover(self, request: DiscoverRequest | Mapping) -> DiscoveryResult:
        parsed = (
            DiscoverRequest.from_mapping(request)
            if isinstance(request, Mapping)
            else request
        )
        response = await self._request(
            "GET",
            f"/v1/discovery/{parsed.subject}",
            params={
                "limit": parsed.limit,
                **({"cursor": parsed.cursor} if parsed.cursor else {}),
            },
        )
        return DiscoveryResult.model_validate_json(response.content)

    async def inspect(self, request: InspectRequest | Mapping) -> InspectionResult:
        return InspectionResult.model_validate_json(
            (await self._request("POST", "/v1/inspect", json=_payload(request))).content
        )

    async def search(
        self,
        request: QueryRequest | Mapping,
        *,
        scope: RequestScope | None = None,
        timeout: float | None = None,
    ) -> QueryResult:
        parsed = (
            QueryRequest.from_mapping(request)
            if isinstance(request, Mapping)
            else request
        )
        response = await self._request(
            "POST",
            "/v1/queries",
            json=parsed.model_dump(mode="json"),
            headers=_scope_header(scope),
            params={"timeout": _timeout(timeout)} if timeout is not None else None,
        )
        return QueryResult.model_validate_json(response.content)


def connect_local(path: str | Path) -> HTTPClient:
    """Read a protected owner record, then verify its live HTTP identity."""
    root = Path(path).resolve(strict=True)
    runtime = root / ".runtime"
    try:
        info = runtime.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError
        lock = os.open(runtime / "owner.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                raise TychoraError(
                    "WORKSPACE_BUSY", "Local endpoint has no live owner."
                )
        finally:
            os.close(lock)
        fd = os.open(runtime / "endpoint.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            record_info = os.fstat(fd)
            if (
                not stat.S_ISREG(record_info.st_mode)
                or record_info.st_uid != os.getuid()
                or record_info.st_mode & 0o177
            ):
                raise ValueError
            with os.fdopen(fd) as stream:
                fd = -1
                record = json.load(stream)
        finally:
            if fd != -1:
                os.close(fd)
        with sqlite3.connect(f"file:{root / 'corpus.sqlite3'}?mode=ro", uri=True) as db:
            workspace = db.execute("SELECT workspace_id FROM workspaces").fetchall()
        if (
            len(workspace) != 1
            or workspace[0][0] != record["workspace_id"]
            or record["protocol_version"] != 1
        ):
            raise ValueError
        return HTTPClient(
            f"http://{record['authority']}",
            record["token"],
            record["workspace_id"],
            instance_id=record["instance_id"],
        )
    except TychoraError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        json.JSONDecodeError,
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Local runtime endpoint is unavailable or unsafe."
        ) from None
