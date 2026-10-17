"""Raw local HTTP admission before framework body parsing."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from urllib.parse import urlsplit

from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope


class LocalAuth:
    """Map installation credentials to immutable, server-granted scopes."""

    def __init__(
        self,
        token_scopes: Mapping[str, RequestScope],
        *,
        authority: str,
        origin: str | None = None,
        max_request_bytes: int = 1024 * 1024,
        managed_owner_token: str | None = None,
        launch_token: str | None = None,
    ) -> None:
        parsed = urlsplit(f"http://{authority}")
        if (
            parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.port is None
            or parsed.netloc != authority
            or parsed.path
            or not 1 <= parsed.port <= 65535
        ):
            raise TychoraError("INVALID_REQUEST", "Local HTTP authority is invalid.")
        if not token_scopes or any(
            not key or not isinstance(value, RequestScope)
            for key, value in token_scopes.items()
        ):
            raise TychoraError("INVALID_REQUEST", "Local credentials are invalid.")
        if max_request_bytes < 1:
            raise TychoraError("INVALID_REQUEST", "Request limit must be positive.")
        if managed_owner_token is not None and managed_owner_token not in token_scopes:
            raise TychoraError(
                "INVALID_REQUEST", "Managed token must have an explicit initial grant."
            )
        if launch_token is not None and launch_token not in token_scopes:
            raise TychoraError(
                "INVALID_REQUEST", "Launch token must have an explicit grant."
            )
        from .launch import LaunchCodes

        # Dashboard launch codes are opt-in composition, used by `serve`.
        self.launch_codes = (
            LaunchCodes(launch_token) if launch_token is not None else None
        )
        self._managed_slot = (
            (managed_owner_token, token_scopes[managed_owner_token])
            if managed_owner_token is not None
            else None
        )
        self._managed_bound = False
        self._grants = tuple(
            (token, scope)
            for token, scope in token_scopes.items()
            if token != managed_owner_token
        )
        self.authority = authority
        self.origin = origin or f"http://{authority}"
        if self.origin != f"http://{authority}":
            raise TychoraError("INVALID_REQUEST", "Local origin must match authority.")
        self.max_request_bytes = max_request_bytes

    def bind_managed_owner(self, owner) -> None:
        if self._managed_slot is None:
            return
        if (
            self._managed_bound
            or getattr(owner, "_managed_auth_bound", False)
            or not owner.managed_default_owner
            or self._managed_slot[1] != owner.scope
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE",
                "Managed binding requires the exact managed owner grant.",
            )
        self._managed_bound = True
        owner._managed_auth_bound = True
        owner._managed_scope_listeners.append(self.refresh_managed_owner)

    def refresh_managed_owner(self, scope: RequestScope) -> None:
        if self._managed_slot is not None and self._managed_bound:
            self._managed_slot = (self._managed_slot[0], scope)

    def is_managed_identity(self, authorization: str) -> bool:
        return (
            self._managed_bound
            and self._managed_slot is not None
            and hmac.compare_digest(authorization, "Bearer " + self._managed_slot[0])
        )

    def authenticate(
        self, authorization: str, owner_scope: RequestScope
    ) -> RequestScope:
        if not authorization.startswith("Bearer "):
            raise TychoraError("FORBIDDEN_SCOPE", "Local credential is required.")
        credential = authorization[7:]
        granted = None
        for token, scope in (
            *self._grants,
            *((self._managed_slot,) if self._managed_slot is not None else ()),
        ):
            if hmac.compare_digest(credential, token):
                granted = scope
        if granted is None or granted.principal_id != owner_scope.principal_id:
            raise TychoraError("FORBIDDEN_SCOPE", "Local credential is invalid.")
        return owner_scope.narrow(granted)


def checked_authority_headers(
    raw: list[tuple[bytes, bytes]],
    auth: LocalAuth,
    *,
    max_request_bytes: int | None = None,
) -> tuple[dict[bytes, bytes], int | None]:
    """Reject ambiguous security and length headers before route parsing."""
    selected: dict[bytes, bytes] = {}
    names = {
        b"host",
        b"authorization",
        b"origin",
        b"content-length",
        b"x-tychora-scope",
        b"idempotency-key",
        b"last-event-id",
    }
    for name, value in raw:
        lower = name.lower()
        if lower in names:
            if lower in selected:
                raise TychoraError("INVALID_REQUEST", "Duplicate HTTP control header.")
            selected[lower] = value
    if b"host" not in selected:
        raise TychoraError("FORBIDDEN_SCOPE", "Local Host is required.")
    try:
        host = selected[b"host"].decode("ascii")
        for value in selected.values():
            value.decode("ascii")
        origin = selected[b"origin"].decode("ascii") if b"origin" in selected else None
        length_text = (
            selected[b"content-length"].decode("ascii")
            if b"content-length" in selected
            else None
        )
    except UnicodeDecodeError:
        raise TychoraError(
            "INVALID_REQUEST", "HTTP control header is malformed."
        ) from None
    if host != auth.authority or (origin is not None and origin != auth.origin):
        raise TychoraError("FORBIDDEN_SCOPE", "Local authority or origin is invalid.")
    if length_text is not None:
        if len(length_text) > 20 or not length_text.isdecimal():
            raise TychoraError("INVALID_REQUEST", "Content length is malformed.")
        length = int(length_text)
        if length > (
            auth.max_request_bytes if max_request_bytes is None else max_request_bytes
        ):
            raise TychoraError(
                "BUDGET_TOO_SMALL", "Request body exceeds the local limit."
            )
    else:
        length = None
    return selected, length


def checked_headers(
    raw: list[tuple[bytes, bytes]],
    auth: LocalAuth,
    owner_scope: RequestScope,
    *,
    max_request_bytes: int | None = None,
) -> tuple[RequestScope, int | None]:
    """Validate local authority, then authenticate and narrow the API grant."""
    selected, length = checked_authority_headers(
        raw, auth, max_request_bytes=max_request_bytes
    )
    if b"authorization" not in selected:
        raise TychoraError("FORBIDDEN_SCOPE", "Local credential is required.")
    authorization = selected[b"authorization"].decode("ascii")
    scope = auth.authenticate(authorization, owner_scope)
    if b"x-tychora-scope" in selected:
        raw_scope = selected[b"x-tychora-scope"]
        if len(raw_scope) > 8192:
            raise TychoraError("INVALID_REQUEST", "Requested scope is too large.")
        try:
            requested = RequestScope.model_validate_json(raw_scope)
        except ValueError:
            raise TychoraError(
                "INVALID_REQUEST", "Requested scope is invalid."
            ) from None
        scope = scope.narrow(requested)
    return scope, length
