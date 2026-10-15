"""Authenticated loopback FastAPI composition."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from tychora.application.pdf_intake import ID_PATTERN, PdfIntakeService
from tychora.application.runtime import ServiceRuntime
from tychora.application.settings import SettingsService
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

from .auth import LocalAuth, checked_authority_headers, checked_headers
from .dashboard import is_dashboard_static_request, register_dashboard
from .launch import LAUNCH_PATH, MAX_EXCHANGE_BYTES, exchange, register_launch
from .pdf_intakes import register_pdf_intakes
from .routes import register_routes

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.application.services import Services


def error_response(error: TychoraError) -> Response:
    return Response(
        content=error.to_json(),
        status_code=error.http_status,
        media_type="application/json",
    )


class RawBoundary:
    def __init__(
        self,
        app,
        owner,
        auth: LocalAuth,
        runtime: ServiceRuntime,
        pdf_intakes: PdfIntakeService,
    ) -> None:
        self.app = app
        self.owner = owner
        self.auth = auth
        self.runtime = runtime
        self.pdf_intakes = pdf_intakes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def uncached_send(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            if is_dashboard_static_request(scope):
                checked_authority_headers(scope.get("headers", []), self.auth)
                await self.app(scope, receive, uncached_send)
                return
            if scope.get("path") == LAUNCH_PATH:
                # Unauthenticated by design, but still exact Host/Origin bound.
                checked_authority_headers(
                    scope.get("headers", []),
                    self.auth,
                    max_request_bytes=MAX_EXCHANGE_BYTES,
                )
                await exchange(scope, receive, uncached_send, self.auth)
                return
            binary = (
                re.fullmatch(
                    r"/v1/pdf-intakes/(" + ID_PATTERN + r")/content",
                    scope.get("path", ""),
                )
                if scope.get("method") == "PUT"
                else None
            )
            request_scope, declared = checked_headers(
                scope.get("headers", []),
                self.auth,
                self.owner.scope,
                # Authenticate the binary request before applying its configured limit;
                # checked_authority_headers still bounds decimal length to 20 digits.
                max_request_bytes=10**20 if binary else None,
            )
            scope.setdefault("state", {})["request_scope"] = request_scope
            scope["state"]["managed_workspace_owner"] = self.auth.is_managed_identity(
                next(
                    (
                        value.decode("ascii")
                        for name, value in scope.get("headers", [])
                        if name.lower() == b"authorization"
                    ),
                    "",
                )
            )
            if binary:
                self.runtime.admit()
                try:
                    ceiling = await self.pdf_intakes.admit_content(
                        binary[1], request_scope, declared, ExecutionContext()
                    )
                    received = 0

                    async def counted_receive():
                        nonlocal received
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            raise TychoraError("CANCELLED", "PDF upload disconnected.")
                        received += len(message.get("body", b""))
                        if received > ceiling:
                            raise TychoraError(
                                "LIMIT_EXCEEDED", "PDF upload exceeds its byte limit."
                            )
                        if (
                            not message.get("more_body", False)
                            and declared is not None
                            and declared != received
                        ):
                            raise TychoraError(
                                "INVALID_REQUEST",
                                "Declared PDF content length differs.",
                            )
                        return message

                    await self.app(scope, counted_receive, uncached_send)
                finally:
                    self.runtime.release()
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.auth.max_request_bytes:
                    raise TychoraError(
                        "BUDGET_TOO_SMALL", "Request body exceeds the local limit."
                    )
                if not message.get("more_body", False):
                    break
            scope.setdefault("state", {})["request_scope"] = request_scope
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            activation = scope.get("path") == "/v1/settings" and scope.get(
                "method"
            ) in {"PUT", "PATCH"}
            is_mcp = scope.get("path", "").startswith("/mcp/")
            if not is_mcp:
                self.runtime.admit(activation=activation)
            try:
                await self.app(scope, replay, uncached_send)
            finally:
                if not is_mcp:
                    self.runtime.release(activation=activation)
        except TychoraError as error:
            response = error_response(error)
            await response(scope, receive, uncached_send)


def create_app(owner: WorkspaceOwner, services: Services, auth: LocalAuth) -> FastAPI:
    if any(scope.principal_id != owner.scope.principal_id for _, scope in auth._grants):
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Local token principal differs from owner."
        )

    auth.bind_managed_owner(owner)

    @asynccontextmanager
    async def lifespan(current: FastAPI):
        try:
            async with current.state.mcp_server.session_manager.run():
                yield
        finally:
            try:
                await current.state.runtime.services.source_jobs.aclose()
            finally:
                await current.state.runtime.aclose_retired()

    app = FastAPI(
        title="Tychora Local API",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.owner = owner
    runtime = owner.service_runtime
    if runtime is None:
        runtime = ServiceRuntime(owner, services)
        owner.service_runtime = runtime
    elif runtime.services is not services:
        raise TychoraError(
            "INTEGRITY_ERROR", "HTTP app requires the owner's current services."
        )
    app.state.runtime = runtime
    app.state.settings = SettingsService(owner, app.state.runtime)
    from tychora.application.settings_draft import SettingsDraftService

    app.state.settings_draft = SettingsDraftService(app.state.settings)
    from tychora.application.model_inventory import ModelInventoryService

    app.state.models = ModelInventoryService(owner, services.maintenance.repository)
    app.state.auth = auth
    from tychora.interfaces.mcp.server import build_mcp

    app.state.mcp_server = build_mcp(services, owner.scope)
    mcp_app = app.state.mcp_server.streamable_http_app(streamable_http_path="/")
    app.mount("/mcp", mcp_app)
    if owner.pdf_intakes is None:
        owner.pdf_intakes = PdfIntakeService(owner, services)
    app.state.pdf_intakes = owner.pdf_intakes
    app.add_middleware(
        RawBoundary,
        owner=owner,
        auth=auth,
        runtime=app.state.runtime,
        pdf_intakes=app.state.pdf_intakes,
    )

    @app.exception_handler(TychoraError)
    async def typed_error(_request: Request, error: TychoraError):
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_route_request(_request: Request, _error: RequestValidationError):
        return error_response(
            TychoraError("INVALID_REQUEST", "Request schema is invalid.")
        )

    @app.exception_handler(ValidationError)
    async def invalid_model(_request: Request, _error: ValidationError):
        return error_response(
            TychoraError("INVALID_REQUEST", "Request schema is invalid.")
        )

    @app.exception_handler(HTTPException)
    async def invalid_http(_request: Request, error: HTTPException):
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_REQUEST"
        return error_response(TychoraError(code, "HTTP route or request is invalid."))

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(_request: Request, _error: json.JSONDecodeError):
        return error_response(
            TychoraError("INVALID_REQUEST", "JSON request is invalid.")
        )

    register_pdf_intakes(app)
    register_launch(app)
    register_routes(app)
    register_dashboard(app)
    return app
