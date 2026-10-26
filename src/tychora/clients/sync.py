"""Blocking public client facade over one bounded off-loop service bridge."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterator, Mapping
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from tychora.api import LocalWorkspace
from tychora.contracts.corpus import AllowedUsesOverride
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import (
    DiscoverRequest,
    EvaluationRequest,
    InspectRequest,
    MaintenanceRequest,
    RelationRequest,
)
from tychora.contracts.requests import QueryRequest, SourceInput
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.api import ProviderBindings
    from tychora.configuration.models import ResolvedConfig
    from tychora.contracts.requests import RequestScope
    from tychora.ports.tokens import TokenCounter


def _context(timeout: float | None = None) -> ExecutionContext:
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise TychoraError("INVALID_REQUEST", "Timeout must be finite and nonnegative.")
    return ExecutionContext(
        deadline=time.monotonic() + timeout if timeout is not None else None
    )


def _parsed(model: Any, value: Any) -> Any:
    return model.from_mapping(value) if isinstance(value, Mapping) else value


def _admitted(method):
    """Hold shared owner admission through a complete public Python operation."""

    @wraps(method)
    def call(self, *args, **kwargs):
        client = self.kb if hasattr(self, "kb") else self
        ws = client._open()
        with ws.runtime.operation():
            return method(self, *args, **kwargs)

    return call


class _Sources:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def add(self, source: SourceInput | Mapping, *, idempotency_key: str | None = None):
        """Register a source and start ingesting it; returns the job handle."""
        ws = self.kb._open()
        parsed = ws.parse_source(source)
        return ws.services.source_jobs.add(parsed, ws.scope, idempotency_key)

    @_admitted
    def sync(self, source_id: str, *, idempotency_key: str | None = None):
        """Capture a registered source again and ingest what changed; returns the job handle."""
        ws = self.kb._open()
        return ws.services.source_jobs.sync(source_id, ws.scope, idempotency_key)

    @_admitted
    def set_allowed_uses(self, source_id: str, override: AllowedUsesOverride) -> None:
        """Narrow the uses a source grants (``never_generate``, ``reference_only`` or a use list)."""
        ws = self.kb._open()
        ws.bridge.call(
            ws.services.source_jobs.sources.set_allowed_uses(
                source_id, override, ws.scope, _context()
            )
        )

    @_admitted
    def revisions(self, source_id: str, *, cursor: str | None = None, limit: int = 20):
        """Page through the retained revisions of a source."""
        ws = self.kb._open()
        return ws.services.discovery.source_revisions(
            source_id, ws.scope, cursor=cursor, limit=limit
        )

    @_admitted
    def extractions(
        self,
        source_id: str,
        revision_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ):
        """Page through the extractions of one source revision."""
        ws = self.kb._open()
        return ws.services.discovery.source_extractions(
            source_id, revision_id, ws.scope, cursor=cursor, limit=limit
        )

    @_admitted
    def channels(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ):
        """Page through the indexed channels of one extraction."""
        ws = self.kb._open()
        return ws.services.discovery.source_channels(
            source_id,
            revision_id,
            extraction_id,
            ws.scope,
            cursor=cursor,
            limit=limit,
        )

    @_admitted
    def selections(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ):
        """Page through the evidence selections of one extraction."""
        ws = self.kb._open()
        return ws.services.discovery.source_selections(
            source_id,
            revision_id,
            extraction_id,
            ws.scope,
            cursor=cursor,
            limit=limit,
        )


class _Evidence:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def get(self, unit_id: str, *, start: int | None = None, end: int | None = None):
        """Inspect one evidence unit in full, or only its characters from start to end."""
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
        return ws.bridge.call(
            ws.services.inspection.inspect(request, ws.scope, _context())
        )


class _Assets:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def get(self, asset_id: str):
        """Inspect one registered asset with its metadata and payload."""
        ws = self.kb._open()
        request = InspectRequest(subject="assets", id=asset_id, view="full")
        return ws.bridge.call(
            ws.services.inspection.inspect(request, ws.scope, _context())
        )

    @_admitted
    def read(self, asset_id: str) -> bytes:
        """Return the bytes of a registered asset; raises NOT_FOUND when the payload is gone."""
        result = self.get(asset_id)
        if result.payload is None:
            raise TychoraError("NOT_FOUND", "Registered asset payload is unavailable.")
        return result.payload


class _Relations:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def traverse(self, request: RelationRequest | Mapping):
        """Follow relations from authorized seed evidence within the caller's scope."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.inspection.relations(
                _parsed(RelationRequest, request), ws.scope, _context()
            )
        )


class _Jobs:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def get(self, job_id: str):
        """Return the persisted status of a job."""
        ws = self.kb._open()
        return ws.bridge.call(ws.services.jobs.get(job_id, _context()))

    @_admitted
    def wait(self, job_id: str, *, timeout: float | None = None):
        """Wait until a job finishes or timeout seconds pass (default 30) and return its status."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.jobs.wait(
                job_id, _context(), timeout=30.0 if timeout is None else timeout
            )
        )

    @_admitted
    def cancel(self, job_id: str):
        """Request cancellation of a job, signal its worker and return its status."""
        ws = self.kb._open()
        status = ws.bridge.call(ws.services.jobs.cancel(job_id, _context()))
        ws.services.source_jobs.signal_cancel(job_id)
        return status

    def events(self, job_id: str, *, after: int = 0) -> Iterator:
        """Yield the job's events after the given sequence until the job reaches a final state."""
        ws = self.kb._open()
        if after < 0:
            raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
        cursor = after
        while True:
            with ws.runtime.operation():
                events = ws.bridge.call(
                    ws.services.jobs.events_after(job_id, cursor, 100, _context())
                )
            for event in events:
                cursor = event.sequence
                yield event
            if self.get(job_id).state in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return
            time.sleep(0.05)

    @_admitted
    def events_after(self, job_id: str, *, after: int = 0):
        """Return up to 100 job events after the given sequence."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.jobs.events_after(job_id, after, 100, _context())
        )


class _Evaluations:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def run(self, request: EvaluationRequest | Mapping):
        """Run a retrieval evaluation against a registered gold dataset; returns the job handle."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.evaluations.run(
                _parsed(EvaluationRequest, request), ws.scope, _context()
            )
        )

    @_admitted
    def register_dataset(self, path: str | Path):
        """Register a local gold dataset directory holding manifest.json and cases.jsonl."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.evaluations.register_dataset(Path(path), ws.scope, _context())
        )

    @_admitted
    def get(self, evaluation_id: str):
        """Return the report of one evaluation run."""
        ws = self.kb._open()
        return ws.bridge.call(
            ws.services.evaluations.get(evaluation_id, ws.scope, _context())
        )

    @_admitted
    def list(self):
        """List the saved evaluation run summaries the caller may read."""
        ws = self.kb._open()
        return ws.bridge.call(ws.services.evaluations.list_runs(ws.scope, _context()))


class _Management:
    def __init__(self, kb: Tychora) -> None:
        self.kb = kb

    @_admitted
    def run(
        self,
        request: MaintenanceRequest | Mapping,
        *,
        idempotency_key: str | None = None,
    ):
        """Run a maintenance request, such as backup, health or rebuild, and return its MaintenanceResult; an evaluate request carries a job handle in result.job."""
        ws = self.kb._open()
        parsed = _parsed(MaintenanceRequest, request)
        return ws.bridge.call(
            ws.services.maintenance.run(parsed, ws.scope, idempotency_key, _context())
        )


class Tychora:
    """Synchronous client for a local workspace; use it as a context manager or call close()."""

    def __init__(self, workspace: LocalWorkspace) -> None:
        self._workspace = workspace
        self.restore_receipt = None
        self.sources = _Sources(self)
        self.evidence = _Evidence(self)
        self.assets = _Assets(self)
        self.relations = _Relations(self)
        self.jobs = _Jobs(self)
        self.evaluations = _Evaluations(self)
        self.management = _Management(self)

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
    ) -> Tychora:
        """Open, or create, the workspace at path and return a client bound to it."""
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
    def restore(
        cls,
        archive: str | Path,
        destination: str | Path,
        *,
        scope: RequestScope | None = None,
        config: ResolvedConfig | None = None,
    ) -> Tychora:
        """Bootstrap an explicitly local archive before opening its clean destination."""
        from tychora.application.restore import RestoreService

        receipt = RestoreService(config=config).import_backup(
            Path(archive), Path(destination), scope, _context()
        )
        client = cls.open(destination, scope=receipt.opening_scope)
        client.restore_receipt = receipt
        return client

    @classmethod
    def connect(cls, endpoint: str, credential: str, workspace_id: str):
        """Return a client for a workspace served over HTTP by `tychora serve`."""
        from tychora.interfaces.http.client import HTTPClient

        return HTTPClient(endpoint, credential, workspace_id)

    def __enter__(self) -> Self:
        self._workspace.require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the workspace and release its resources."""
        self._workspace.close()

    def _open(self) -> LocalWorkspace:
        self._workspace.require_open()
        return self._workspace

    @property
    def default_collection_id(self):
        """Identifier of the default collection of this workspace."""
        return self._open().default_collection_id

    @property
    def scope(self):
        """The request scope every call of this client runs under."""
        return self._open().scope

    @_admitted
    def discover(self, request: DiscoverRequest | Mapping):
        """List kinds, sources or other discovery subjects the scope can read."""
        ws = self._open()
        parsed = _parsed(DiscoverRequest, request)
        return ws.bridge.call(
            asyncio.to_thread(ws.services.discovery.discover, parsed, ws.scope)
        )

    @_admitted
    def inspect(self, request: InspectRequest | Mapping):
        """Inspect evidence, assets, revisions, jobs, query runs or diagnostics by identifier."""
        ws = self._open()
        return ws.bridge.call(
            ws.services.inspection.inspect(
                _parsed(InspectRequest, request), ws.scope, _context()
            )
        )

    @_admitted
    def search(
        self,
        request: QueryRequest | Mapping,
        *,
        scope: RequestScope | None = None,
        timeout: float | None = None,
    ):
        """Search the workspace and return cited evidence segments."""
        ws = self._open()
        parsed = _parsed(QueryRequest, request)
        granted = ws.scope if scope is None else ws.scope.narrow(scope)
        return ws.bridge.call(
            ws.services.query.search(parsed, granted, _context(timeout))
        ).result
