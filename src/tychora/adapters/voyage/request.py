"""Shared raw Voyage request transport and bounded retry execution."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from hashlib import sha256
from typing import TYPE_CHECKING

from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

from .request_types import RawVoyageResponse
from .runtime import VoyageRuntime

if TYPE_CHECKING:
    import httpx


class HttpxVoyageTransport:
    """Raw fixed-origin HTTP transport with no ambient or hidden retry state."""

    def __init__(
        self, *, api_key: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not api_key:
            raise ValueError("Voyage credential must be nonempty")
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(
            base_url="https://api.voyageai.com",
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def post_json(self, path: str, body: bytes, timeout: float) -> RawVoyageResponse:
        try:
            response = self._client.post(path, content=body, timeout=timeout)
        except self._httpx.TransportError:
            raise OSError("Voyage transport failed") from None
        return RawVoyageResponse(response.status_code, response.content)

    def close(self) -> None:
        self._client.close()


type RequestAuthorizer = Callable[[str, ExecutionContext], None]


class VoyageRequestExecutor:
    """Reauthorize immediately before each bounded raw HTTP attempt."""

    def __init__(
        self,
        runtime: VoyageRuntime,
        *,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        self._runtime = runtime
        self._backoff = backoff or (
            lambda attempt: random.uniform(0.0, min(0.25 * 2 ** (attempt - 1), 2.0))
        )

    def post(
        self,
        path: str,
        body: bytes,
        authorize: RequestAuthorizer,
        ctx: ExecutionContext,
    ) -> RawVoyageResponse:
        payload_sha = sha256(body).hexdigest()
        for attempt in range(1, 4):
            if attempt > 1:
                self._wait(self._backoff(attempt - 1), ctx)
            ctx.checkpoint()
            authorize(payload_sha, ctx)
            ctx.checkpoint()
            timeout = 30.0
            if ctx.deadline is not None:
                timeout = min(timeout, max(0.001, ctx.deadline - time.monotonic()))
            try:
                response = self._runtime.transport().post_json(path, body, timeout)
            except TychoraError:
                raise
            except (OSError, TimeoutError):
                if attempt < 3:
                    continue
                break
            except Exception:  # noqa: BLE001 - sanitize provider boundary errors
                raise TychoraError(
                    "PROVIDER_FAILURE", "Embedding provider request failed."
                ) from None
            if response.status_code == 200:
                return response
            transient = (
                response.status_code == 429 or 500 <= response.status_code <= 599
            )
            if transient and attempt < 3:
                continue
            raise TychoraError(
                "PROVIDER_FAILURE",
                "Embedding provider request failed.",
                retryable=transient,
            ) from None
        raise TychoraError(
            "PROVIDER_FAILURE", "Embedding provider request failed.", retryable=True
        )

    @staticmethod
    def _wait(delay: float, ctx: ExecutionContext) -> None:
        if delay <= 0:
            ctx.checkpoint()
            return
        if ctx.deadline is not None:
            delay = min(delay, max(0.0, ctx.deadline - time.monotonic()))
        ctx.cancelled.wait(delay)
        ctx.checkpoint()
