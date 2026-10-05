"""Public DTO MCP tools with explicit result blocks and scoped job controls."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from tychora.api import DEFAULT_COLLECTION_ID
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import QueryResult
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.jobs import JobPage, JobStatus
from tychora.contracts.operations import (
    DiscoverRequest,
    DiscoveryResult,
    InspectionResult,
    InspectRequest,
    JobHandle,
    SourceChannelPage,
    SourceCoverageRequest,
    SourceExtractionPage,
    SourceRevisionPage,
    SourceSelectionPage,
)
from tychora.contracts.requests import (
    FileSource,
    FolderSource,
    QueryRequest,
    RepositorySource,
    SourceInput,
)
from tychora.formatting.budget import measure_payloads
from tychora.formatting.render import OutputProfile
from tychora.ports.execution import ExecutionContext

from .server import current_operation


class MCPInspectionResult(InspectionResult):
    """Inspection's binary payload encoded for MCP structured JSON."""

    payload: str | None = None


def _require(capability: str) -> None:
    _, scope = current_operation()
    if capability not in scope.capabilities:
        raise TychoraError("FORBIDDEN_SCOPE", "Operation is outside the grant.")


def _wire_order(value):
    if isinstance(value, dict):
        return {key: _wire_order(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return tuple(_wire_order(item) for item in value)
    if isinstance(value, list):
        return [_wire_order(item) for item in value]
    return value


def _result(value, *, text: str | None = None, wrapped: bool = False) -> CallToolResult:
    data = value.model_dump(mode="python")
    if isinstance(value, InspectionResult) and value.payload is not None:
        # The SDK validates Python output against the published schema before
        # serializing CallToolResult, whose own bytes codec assumes UTF-8.
        data["payload"] = value.model_dump(mode="json")["payload"]
    data = _wire_order(data)
    return CallToolResult(
        content=[] if text is None else [TextContent(type="text", text=text)],
        structured_content={"result": data} if wrapped else data,
    )


def _serialized_structured(block: CallToolResult) -> bytes:
    """The installed SDK's actual structuredContent JSON fragment."""
    wire = block.model_dump_json(by_alias=True, exclude_unset=True)
    marker = '"structuredContent":'
    start = wire.find(marker)
    if start < 0:
        raise TychoraError("INTEGRITY_ERROR", "Structured MCP content is absent.")
    start += len(marker)
    _, end = json.JSONDecoder().raw_decode(wire[start:])
    return wire[start : start + end].encode("utf-8")


def _error(error: TychoraError) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=error.to_json())], is_error=True
    )


def _source(value: dict[str, Any]) -> SourceInput:
    services, scope = current_operation()
    normalized = dict(value)
    if normalized.get("collection_id") == "default":
        if str(DEFAULT_COLLECTION_ID) not in scope.collection_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Default collection is outside the grant."
            )
        normalized["collection_id"] = str(DEFAULT_COLLECTION_ID)
    try:
        source = SourceInput.from_mapping(normalized)
    except (ValueError, TypeError):
        raise TychoraError("INVALID_REQUEST", "Source schema is invalid.") from None
    if str(source.root.collection_id) not in scope.collection_ids:
        raise TychoraError("FORBIDDEN_SCOPE", "Source collection is outside the grant.")
    if isinstance(source.root, (FileSource, FolderSource, RepositorySource)):
        roots = tuple(
            Path(path) for path in services.jobs.owner.config.values.acquisition.roots
        )
        path = Path(source.root.path).resolve(strict=False)
        if not any(path.is_relative_to(root) for root in roots):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Local source path is outside registered roots."
            )
    return source


def register_tools(server: MCPServer) -> None:
    @server.tool(
        description="Discover scoped registered definitions and counts; use subject from the public discovery schema."
    )
    async def discover(
        request: DiscoverRequest,
    ) -> Annotated[CallToolResult, DiscoveryResult]:
        try:
            _require("read")
            services, scope = current_operation()
            value = await asyncio.to_thread(services.discovery.discover, request, scope)
            return _result(value)
        except TychoraError as error:
            return _error(error)

    @server.tool(
        description="Search scoped evidence and return the canonical QueryResult. Optional render adds one budgeted text block."
    )
    async def search(
        request: dict[str, Any] | None = None,
        replay_run_id: str | None = None,
        render: Literal["markdown@1", "agent@1", "hierarchical-evidence@1"]
        | None = None,
    ) -> Annotated[CallToolResult, QueryResult]:
        try:
            _require("read")
            if (request is None) == (replay_run_id is None):
                raise TychoraError(
                    "INVALID_REQUEST", "Supply a request or replay run ID."
                )
            services, scope = current_operation()
            query = copy.copy(services.query)
            query.profiles = (
                OutputProfile(renderer="canonical-json@1"),
                *(() if render is None else (OutputProfile(renderer=render),)),
            )
            query.delivery_serializer = lambda value: _serialized_structured(
                _result(value)
            )
            query.delivery_serialization = "mcp-structured-json@2.2.0"
            if request is not None:
                delivered = await query.search(
                    QueryRequest.from_mapping(request), scope, ExecutionContext()
                )
            else:
                assert replay_run_id is not None
                delivered = await query.replay(replay_run_id, scope, ExecutionContext())
            if delivered.payloads[0] != _serialized_structured(
                _result(delivered.result)
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Serialized MCP result differs from fitted output.",
                )
            text = delivered.payloads[1].decode("utf-8") if render else None
            block = _result(delivered.result, text=text)
            measured = measure_payloads(
                (_serialized_structured(block),)
                + (() if text is None else (text.encode("utf-8"),)),
                services.output_counter,
            )
            if (measured.tokens, measured.bytes) != (
                delivered.token_count,
                delivered.byte_count,
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Serialized MCP output measure differs."
                )
            return block
        except TychoraError as error:
            return _error(error)
        except (ValueError, TypeError):
            return _error(TychoraError("INVALID_REQUEST", "Query schema is invalid."))

    @server.tool(
        description="Inspect one scoped evidence, asset, revision, job, run, or diagnostic record."
    )
    async def inspect(
        request: dict[str, Any],
    ) -> Annotated[
        CallToolResult,
        MCPInspectionResult
        | SourceRevisionPage
        | SourceExtractionPage
        | SourceChannelPage
        | SourceSelectionPage,
    ]:
        try:
            _require("read")
            services, scope = current_operation()
            if request.get("subject") in {
                "source_revisions",
                "source_extractions",
                "source_channels",
                "source_selections",
            }:
                coverage = SourceCoverageRequest.model_validate_json(
                    canonical_bytes(request)
                )
                if coverage.subject == "source_revisions":
                    value = await asyncio.to_thread(
                        services.discovery.source_revisions,
                        coverage.source_id,
                        scope,
                        cursor=coverage.cursor,
                        limit=coverage.limit,
                    )
                elif coverage.subject == "source_extractions":
                    assert coverage.revision_id is not None
                    value = await asyncio.to_thread(
                        services.discovery.source_extractions,
                        coverage.source_id,
                        coverage.revision_id,
                        scope,
                        cursor=coverage.cursor,
                        limit=coverage.limit,
                    )
                elif coverage.subject == "source_channels":
                    assert coverage.revision_id is not None
                    assert coverage.extraction_id is not None
                    value = await asyncio.to_thread(
                        services.discovery.source_channels,
                        coverage.source_id,
                        coverage.revision_id,
                        coverage.extraction_id,
                        scope,
                        cursor=coverage.cursor,
                        limit=coverage.limit,
                    )
                else:
                    assert coverage.revision_id is not None
                    assert coverage.extraction_id is not None
                    value = await asyncio.to_thread(
                        services.discovery.source_selections,
                        coverage.source_id,
                        coverage.revision_id,
                        coverage.extraction_id,
                        scope,
                        cursor=coverage.cursor,
                        limit=coverage.limit,
                    )
            else:
                parsed = InspectRequest.model_validate_json(canonical_bytes(request))
                value = await services.inspection.inspect(
                    parsed, scope, ExecutionContext()
                )
                if value.subject == "assets":
                    value = value.model_copy(update={"payload": None})
            return _result(value, wrapped=True)
        except TychoraError as error:
            return _error(error)
        except (ValueError, TypeError):
            return _error(
                TychoraError("INVALID_REQUEST", "Inspection schema is invalid.")
            )

    @server.tool(
        description="Accept a source ingestion or sync job under the current write grant."
    )
    async def add_sources(
        action: Literal["add", "sync"],
        source: dict[str, Any] | None = None,
        source_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Annotated[CallToolResult, JobHandle]:
        try:
            _require("write")
            services, scope = current_operation()
            if action == "add" and source is not None and source_id is None:
                value = await services.source_jobs.add_async(
                    _source(source), scope, idempotency_key
                )
            elif action == "sync" and source_id is not None and source is None:
                value = await services.source_jobs.sync_async(
                    source_id, scope, idempotency_key
                )
            else:
                raise TychoraError(
                    "INVALID_REQUEST", "Source action arguments are invalid."
                )
            return _result(value)
        except TychoraError as error:
            return _error(error)

    @server.tool(
        description="List/read scoped jobs or perform a separately authorized cancel/retry action."
    )
    async def manage_job(
        action: Literal["list", "get", "cancel", "retry"],
        job_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Annotated[CallToolResult, JobStatus | JobPage]:
        try:
            _require("read" if action in {"list", "get"} else "write")
            services, scope = current_operation()
            jobs = services.jobs_for_scope(scope)
            if action == "list":
                if job_id is not None:
                    raise TychoraError(
                        "INVALID_REQUEST", "List action does not accept job ID."
                    )
                value = await jobs.list(ExecutionContext(), cursor=cursor, limit=limit)
            elif job_id is None or cursor is not None:
                raise TychoraError(
                    "INVALID_REQUEST", "Job action arguments are invalid."
                )
            elif action == "get":
                value = await jobs.get(job_id, ExecutionContext())
            elif action == "cancel":
                value = await jobs.cancel(job_id, ExecutionContext())
                await services.source_jobs.signal_cancel_async(job_id)
            else:
                await services.source_jobs.retry_async(job_id, scope)
                value = await jobs.get(job_id, ExecutionContext())
            return _result(value, wrapped=True)
        except TychoraError as error:
            return _error(error)

    # Public schema is the authoritative agent-facing request vocabulary;
    # the JSON-normalizing boundary accepts its documented question shorthand.
    search_tool = server._tool_manager.get_tool("search")
    if search_tool is not None:
        search_tool.parameters["properties"]["request"] = {
            "anyOf": [
                QueryRequest.model_json_schema(),
                {
                    "type": "object",
                    "required": ["question"],
                    "properties": {"question": {"type": "string", "minLength": 1}},
                },
            ],
            "description": "Public QueryRequest or its question shorthand.",
        }

    inspect_tool = server._tool_manager.get_tool("inspect")
    if inspect_tool is not None:
        inspect_tool.parameters["properties"]["request"] = {
            "anyOf": [
                InspectRequest.model_json_schema(),
                SourceCoverageRequest.model_json_schema(),
            ],
            "description": "Public inspection or bounded source-lineage coverage request.",
        }

    add_tool = server._tool_manager.get_tool("add_sources")
    if add_tool is not None:
        source_schema = SourceInput.model_json_schema()
        definitions = source_schema.pop("$defs")
        for definition in definitions.values():
            collection = definition.get("properties", {}).get("collection_id")
            if collection is not None:
                collection.clear()
                collection.update(
                    {
                        "anyOf": [
                            {"type": "string", "format": "uuid"},
                            {"type": "string", "const": "default"},
                        ]
                    }
                )
        add_tool.parameters.setdefault("$defs", {}).update(definitions)
        add_tool.parameters["properties"]["source"] = {
            "anyOf": [{"$ref": source_schema["$ref"]}, {"type": "null"}],
            "default": None,
            "description": "Public SourceInput; discover the registered kind and metadata schema first. The default collection shorthand requires its grant.",
        }
