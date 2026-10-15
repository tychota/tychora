"""One-time dashboard launch codes exchanged for the owner bearer token."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from tychora.contracts.errors import TychoraError

if TYPE_CHECKING:
    from .auth import LocalAuth

LAUNCH_PATH = "/v1/session/launch"
MINT_PATH = "/v1/session/launch-codes"
LAUNCH_TTL_SECONDS = 120.0
MAX_OUTSTANDING = 8
MAX_FAILURES = 20
FAILURE_WINDOW_SECONDS = 60.0
MAX_EXCHANGE_BYTES = 1024
_CODE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def _invalid_code() -> TychoraError:
    # One generic answer for unknown, expired, reused and malformed codes.
    return TychoraError("FORBIDDEN_SCOPE", "Launch link is invalid or expired.")


def _digest(code: str) -> bytes:
    return hashlib.sha256(code.encode("ascii")).digest()


class LaunchCodes:
    """Bounded, in-memory, single-use codes for one owner process.

    Only SHA-256 digests are retained. Codes vanish on use, expiry, eviction
    and process exit; nothing is persisted.
    """

    def __init__(
        self,
        token: str,
        *,
        ttl: float = LAUNCH_TTL_SECONDS,
        max_outstanding: int = MAX_OUTSTANDING,
        max_failures: int = MAX_FAILURES,
        failure_window: float = FAILURE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token or not 0 < ttl <= LAUNCH_TTL_SECONDS or max_outstanding < 1:
            raise TychoraError("INVALID_REQUEST", "Launch code policy is invalid.")
        self._token = token
        self.ttl = ttl
        self._max_outstanding = max_outstanding
        self._max_failures = max_failures
        self._failure_window = failure_window
        self._clock = clock
        self._codes: dict[bytes, float] = {}
        self._failures: deque[float] = deque()
        self._lock = Lock()

    def is_holder(self, authorization: str) -> bool:
        return hmac.compare_digest(authorization, "Bearer " + self._token)

    def _purge(self, now: float) -> None:
        for key, expires in tuple(self._codes.items()):
            if expires <= now:
                del self._codes[key]
        while self._failures and self._failures[0] <= now - self._failure_window:
            self._failures.popleft()

    def mint(self) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._purge(now)
            while len(self._codes) >= self._max_outstanding:
                # Dicts keep insertion order: evict the oldest outstanding code.
                del self._codes[next(iter(self._codes))]
            self._codes[_digest(code)] = now + self.ttl
        return code

    @property
    def outstanding(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return len(self._codes)

    def redeem(self, code: object) -> str:
        with self._lock:
            now = self._clock()
            self._purge(now)
            if len(self._failures) >= self._max_failures:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Too many launch attempts; retry shortly.",
                    retryable=True,
                )
            matched = None
            if isinstance(code, str) and _CODE.fullmatch(code):
                presented = _digest(code)
                for key in tuple(self._codes):
                    if hmac.compare_digest(key, presented):
                        matched = key
            if matched is None:
                self._failures.append(now)
                raise _invalid_code()
            del self._codes[matched]
            return self._token


def launch_url(origin: str, code: str) -> str:
    """The code travels in the fragment, which browsers never send or refer."""
    return f"{origin}/dashboard/#launch={code}"


async def exchange(scope, receive, send, auth: LocalAuth) -> None:
    """Unauthenticated, origin-bound exchange of one code for the bearer token.

    The caller has already applied exact Host/Origin admission.
    """
    codes = auth.launch_codes
    if codes is None:
        raise TychoraError("NOT_FOUND", "HTTP route or request is invalid.")
    if scope.get("method") != "POST":
        raise TychoraError("INVALID_REQUEST", "Launch exchange requires POST.")
    headers: dict[bytes, bytes] = {}
    for name, value in scope.get("headers", []):
        lower = name.lower()
        if lower in {b"sec-fetch-site", b"content-type"}:
            if lower in headers:
                raise TychoraError("INVALID_REQUEST", "Duplicate HTTP control header.")
            headers[lower] = value
    site = headers.get(b"sec-fetch-site")
    if site is not None and site not in {b"same-origin", b"none"}:
        raise TychoraError("FORBIDDEN_SCOPE", "Local authority or origin is invalid.")
    media = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
    if media != b"application/json":
        raise TychoraError("INVALID_REQUEST", "Launch exchange requires JSON.")
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body.extend(message.get("body", b""))
        if len(body) > MAX_EXCHANGE_BYTES:
            raise TychoraError(
                "BUDGET_TOO_SMALL", "Request body exceeds the local limit."
            )
        if not message.get("more_body", False):
            break
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise TychoraError("INVALID_REQUEST", "JSON request is invalid.") from None
    if not isinstance(value, dict) or set(value) != {"code"}:
        raise TychoraError("INVALID_REQUEST", "Request schema is invalid.")
    token = codes.redeem(value["code"])
    response = JSONResponse(
        {"schema_version": "1", "token_type": "Bearer", "token": token},
        headers={"Referrer-Policy": "no-referrer"},
    )
    await response(scope, receive, send)


def register_launch(app: FastAPI) -> None:
    @app.post(MINT_PATH, status_code=201)
    async def mint_launch_code(request: Request) -> Response:
        auth = request.app.state.auth
        codes = auth.launch_codes
        if codes is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Dashboard launch links are not enabled."
            )
        scope = request.state.request_scope
        # Only the holder of the very token a code hands out, with an unnarrowed
        # manage grant, may mint; a narrower token can never escalate through it.
        if "manage" not in scope.capabilities or not codes.is_holder(
            request.headers.get("authorization", "")
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Operation is outside the grant.")
        code = codes.mint()
        return JSONResponse(
            {
                "schema_version": "1",
                "code": code,
                "expires_in_seconds": int(codes.ttl),
                "url": launch_url(auth.origin, code),
            },
            status_code=201,
        )
