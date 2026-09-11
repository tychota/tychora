"""Explicit owner-side job controls; no scheduling or provider execution."""

import asyncio
import hashlib
import math
import time
from collections.abc import Callable

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue
from tychora.contracts.jobs import JobStatus
from tychora.contracts.operations import JobEvent, JobHandle
from tychora.contracts.requests import (
    BytesSource,
    RequestScope,
    SourceInput,
    TextSource,
)
from tychora.ports.execution import ExecutionContext
from tychora.ports.jobs import JobRepository

from .owner import WorkspaceOwner


def source_intent(source: SourceInput) -> dict[str, JsonValue]:
    """Preserve supplied settings and hash inline bytes without reading live paths."""
    value = source.root
    intent = value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, BytesSource):
        intent.pop("data", None)
        intent["content_sha256"] = hashlib.sha256(value.data).hexdigest()
    elif isinstance(value, TextSource):
        intent.pop("text", None)
        intent["content_sha256"] = hashlib.sha256(
            value.text.encode("utf-8")
        ).hexdigest()
    return intent


class JobService:
    def __init__(
        self, owner: WorkspaceOwner, repository: JobRepository, scope: RequestScope
    ):
        self.owner = owner
        self.repository = repository
        self.scope = scope

    async def _call[T](
        self,
        job_id: str,
        call: Callable[[], T],
        ctx: ExecutionContext,
        *,
        write: bool = False,
    ) -> T:
        def authorized() -> T:
            scope = self.owner.scope.narrow(self.scope)
            self.repository.authorize(job_id, scope, write=write)
            result = call()
            self.repository.authorize(job_id, scope, write=write)
            return result

        return await self.owner.run_blocking(authorized, ctx)

    async def accept(
        self,
        operation: str,
        key: str | None,
        intent: JsonValue,
        effective_config: ArtifactRef,
        ctx: ExecutionContext,
    ) -> JobHandle:
        return await self.owner.run_blocking(
            lambda: self.repository.accept(
                operation,
                self.owner.scope.narrow(self.scope),
                key,
                intent,
                effective_config,
            ),
            ctx,
        )

    async def get(self, job_id: str, ctx: ExecutionContext) -> JobStatus:
        return await self._call(job_id, lambda: self.repository.get(job_id), ctx)

    async def events_after(
        self, job_id: str, sequence: int, limit: int, ctx: ExecutionContext
    ) -> tuple[JobEvent, ...]:
        return await self._call(
            job_id, lambda: self.repository.events_after(job_id, sequence, limit), ctx
        )

    async def cancel(self, job_id: str, ctx: ExecutionContext) -> JobStatus:
        return await self._call(
            job_id, lambda: self.repository.cancel(job_id), ctx, write=True
        )

    async def retry(self, job_id: str, ctx: ExecutionContext) -> JobStatus:
        return await self._call(
            job_id, lambda: self.repository.retry(job_id), ctx, write=True
        )

    async def recover(self, job_id: str, ctx: ExecutionContext) -> JobStatus:
        return await self._call(
            job_id, lambda: self.repository.recover(job_id), ctx, write=True
        )

    async def wait(
        self, job_id: str, ctx: ExecutionContext, *, timeout: float = 30.0
    ) -> JobStatus:
        if not math.isfinite(timeout) or not 0 <= timeout <= 300:
            raise TychoraError(
                "INVALID_REQUEST", "Wait timeout must be between zero and 300 seconds"
            )
        end = time.monotonic() + timeout
        while True:
            status = await self.get(job_id, ctx)
            if (
                status.state in {"succeeded", "failed", "cancelled", "interrupted"}
                or time.monotonic() >= end
            ):
                return status
            await asyncio.sleep(min(0.05, max(0.0, end - time.monotonic())))
            ctx.checkpoint()
