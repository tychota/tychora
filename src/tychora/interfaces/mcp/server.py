"""Official MCP SDK composition over one owner-bound service runtime."""

from __future__ import annotations

from contextvars import ContextVar

from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent
from tychora.application.runtime import ServiceRuntime
from tychora.application.services import Services
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope

_current: ContextVar[tuple[Services, RequestScope] | None] = ContextVar(
    "tychora_mcp_operation", default=None
)


def current_operation() -> tuple[Services, RequestScope]:
    value = _current.get()
    if value is None:
        raise TychoraError("INTEGRITY_ERROR", "MCP operation context is unavailable.")
    return value


MAX_MCP_TOOL_RESULT_BYTES = 4 * 1024 * 1024


def _tool_error(error: TychoraError) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=error.to_json())], is_error=True
    )


class BoundedMCPServer(MCPServer):
    """Normalize SDK validation and bound every serialized tool result."""

    async def call_tool(self, name, arguments, context=None):
        try:
            result = await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            raise
        except ToolError:
            # SDK argument validation runs before a tool body can sanitize it.
            # Never return its ToolError text, which may contain caller values.
            return _tool_error(
                TychoraError("INVALID_REQUEST", "Tool arguments are invalid.")
            )
        if (
            len(
                result.model_dump_json(by_alias=True, exclude_unset=True).encode(
                    "utf-8"
                )
            )
            > MAX_MCP_TOOL_RESULT_BYTES
        ):
            return _tool_error(
                TychoraError(
                    "OUTPUT_BUDGET_EXCEEDED",
                    "MCP protocol result exceeds framing limit.",
                )
            )
        return result


class RuntimeMiddleware:
    """Resolve each HTTP message's effective grant and pin active services only."""

    def __init__(self, runtime: ServiceRuntime, principal: RequestScope) -> None:
        self.runtime = runtime
        self.principal = principal

    async def __call__(self, ctx: ServerRequestContext, call_next):
        request = ctx.request
        granted = (
            getattr(getattr(request, "state", None), "request_scope", None)
            if request is not None
            else None
        )
        if request is not None and granted is None:
            raise TychoraError("FORBIDDEN_SCOPE", "MCP request grant is unavailable.")
        scope = (
            self.principal.narrow(granted) if granted is not None else self.principal
        )
        if ctx.method not in {
            "tools/list",
            "tools/call",
            "resources/list",
            "resources/templates/list",
            "resources/read",
        }:
            return await call_next(ctx)
        try:
            with self.runtime.operation() as services:
                marker = _current.set((services, scope))
                try:
                    result = await call_next(ctx)
                    if (
                        ctx.method == "tools/list"
                        and "write" not in scope.capabilities
                        and isinstance(result, dict)
                    ):
                        result = {
                            **result,
                            "tools": [
                                item
                                for item in result.get("tools", [])
                                if item.get("name") != "add_sources"
                            ],
                        }
                    return result
                finally:
                    _current.reset(marker)
        except TychoraError as error:
            if ctx.method == "tools/call":
                return _tool_error(error).model_dump(
                    mode="json", by_alias=True, exclude_unset=True
                )
            raise MCPError(code=-32000, message=error.to_json()) from None


def build_mcp(services: Services, principal: RequestScope) -> MCPServer:
    """Build one server; HTTP sessions use per-message grants, stdio uses principal."""
    owner = services.jobs.owner
    if principal.principal_id != owner.scope.principal_id:
        raise TychoraError("FORBIDDEN_SCOPE", "MCP principal differs from owner.")
    runtime = owner.service_runtime
    if runtime is None:
        runtime = ServiceRuntime(owner, services)
        owner.service_runtime = runtime
    elif runtime.services is not services:
        raise TychoraError("INTEGRITY_ERROR", "MCP requires current owner services.")
    server = BoundedMCPServer(
        "tychora",
        description="Scoped local evidence discovery and retrieval",
        middleware=[RuntimeMiddleware(runtime, owner.scope.narrow(principal))],
    )
    from .resources import register_resources
    from .tools import register_tools

    register_tools(server)
    register_resources(server)
    return server


async def serve_stdio(
    path, *, principal: RequestScope | None = None, token_counter=None
) -> None:
    """Own stdio and a private authenticated sibling-control listener together."""
    import asyncio
    import socket

    import uvicorn

    from tychora import Tychora
    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    with Tychora.open(path, token_counter=token_counter) as kb:
        owner = kb._workspace.owner
        scope = owner.scope if principal is None else owner.scope.narrow(principal)
        mcp_server = build_mcp(kb._workspace.services, scope)
        token = owner._runtime.record["token"]
        if not isinstance(token, str):
            raise TychoraError(
                "INTEGRITY_ERROR", "Owner control credential is invalid."
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sock.setblocking(False)
            authority = f"127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                owner,
                kb._workspace.services,
                LocalAuth({token: owner.scope}, authority=authority),
            )
            control = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=sock.getsockname()[1],
                    workers=1,
                    log_level="error",
                    access_log=False,
                )
            )
            control_task = asyncio.create_task(control.serve(sockets=[sock]))
            try:
                for _ in range(200):
                    if control.started:
                        break
                    if control_task.done():
                        await control_task
                    await asyncio.sleep(0.01)
                if not control.started:
                    raise TychoraError(
                        "WORKSPACE_BUSY", "Control listener did not start."
                    )
                owner._runtime.publish_endpoint(authority)
                await mcp_server.run_stdio_async()
            finally:
                control.should_exit = True
                await asyncio.wait_for(control_task, 5)
