"""Durable, scoped Server-Sent Events over the canonical job log."""

from __future__ import annotations

import asyncio

from fastapi.responses import StreamingResponse

from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


def event_stream(services, scope, job_id: str, after: int) -> StreamingResponse:
    if after < 0:
        raise TychoraError("INVALID_REQUEST", "Event cursor must be nonnegative.")
    jobs = services.jobs_for_scope(scope)

    async def generate():
        cursor = after
        while True:
            status = await jobs.get(job_id, ExecutionContext())
            events = await jobs.events_after(job_id, cursor, 100, ExecutionContext())
            if (
                status.retention_limited
                and status.latest_sequence > cursor
                and (not events or events[0].sequence > cursor + 1)
            ):
                yield f'event: refresh\ndata: {{"job_id":"{job_id}"}}\n\n'.encode()
                return
            for event in events:
                cursor = event.sequence
                yield (
                    f"id: {event.sequence}\nevent: job\ndata: {event.model_dump_json()}\n\n"
                ).encode()
            if status.state in _TERMINAL and cursor >= status.latest_sequence:
                return
            await asyncio.sleep(0.05)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )
