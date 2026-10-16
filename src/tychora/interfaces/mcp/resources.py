"""Registered and scoped MCP resource templates."""

from __future__ import annotations

import asyncio

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import InspectRequest
from tychora.ports.execution import ExecutionContext

from .server import current_operation


def _read() -> tuple:
    services, scope = current_operation()
    if "read" not in scope.capabilities:
        raise TychoraError("FORBIDDEN_SCOPE", "Resource is outside the grant.")
    return services, scope


def register_resources(server: MCPServer) -> None:
    @server.resource("tychora://schemas/{name}", mime_type="application/schema+json")
    async def schema(name: str) -> str:
        try:
            services, _ = _read()
            value = services.discovery.registry.schemas.get(name)
            if value is None:
                raise TychoraError("NOT_FOUND", "Schema is unavailable.")
            return canonical_bytes(value).decode("utf-8")
        except TychoraError as error:
            raise ResourceError(error.to_json()) from None

    @server.resource("tychora://kinds/{id}", mime_type="application/json")
    async def kind(id: str) -> str:
        try:
            services, scope = _read()
            value = await asyncio.to_thread(
                services.discovery.definition, "kinds", id, scope
            )
            return canonical_bytes(value.model_dump(mode="json")).decode("utf-8")
        except TychoraError as error:
            raise ResourceError(error.to_json()) from None

    @server.resource("tychora://evidence/{id}", mime_type="application/json")
    async def evidence(id: str) -> str:
        try:
            services, scope = _read()
            value = await services.inspection.inspect(
                InspectRequest(subject="evidence", id=id, view="full"),
                scope,
                ExecutionContext(),
            )
            return canonical_bytes(value.model_dump(mode="json")).decode("utf-8")
        except TychoraError as error:
            raise ResourceError(error.to_json()) from None

    @server.resource("tychora://assets/{id}", mime_type="application/octet-stream")
    async def asset(id: str) -> bytes:
        try:
            services, scope = _read()
            value = await services.inspection.inspect(
                InspectRequest(subject="assets", id=id, view="full"),
                scope,
                ExecutionContext(),
            )
            if value.payload is None:
                raise TychoraError(
                    "NOT_FOUND", "Registered asset payload is unavailable."
                )
            if len(value.payload) > 16 * 1024 * 1024:
                raise TychoraError(
                    "BUDGET_TOO_SMALL", "Registered asset exceeds resource limit."
                )
            return value.payload
        except TychoraError as error:
            raise ResourceError(error.to_json()) from None
