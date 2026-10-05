"""Bounded query-channel scheduling over one pinned request context."""

import asyncio
import time

import pytest

from tychora.application.query import _run_independent_channels
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_independent_channels_overlap_and_return_declaration_order():
    both_started = asyncio.Event()
    started: set[str] = set()
    ctx = ExecutionContext()

    async def channel(name):
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return name

    results = await _run_independent_channels(
        (lambda: channel("text"), lambda: channel("visual")), ctx
    )
    assert results == ("text", "visual")


@pytest.mark.asyncio
async def test_cancelling_query_drains_both_channel_tasks():
    started = asyncio.Event()
    finished: set[str] = set()
    ctx = ExecutionContext()

    async def channel(name):
        if name == "visual":
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.add(name)

    running = asyncio.create_task(
        _run_independent_channels(
            (lambda: channel("text"), lambda: channel("visual")), ctx
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 1)
    assert finished == {"text", "visual"}


@pytest.mark.asyncio
async def test_shared_deadline_stops_and_drains_both_channel_tasks():
    finished: set[str] = set()
    ctx = ExecutionContext(deadline=time.monotonic() + 0.05)

    async def channel(name):
        try:
            await asyncio.Event().wait()
        finally:
            finished.add(name)

    with pytest.raises(TychoraError) as error:
        await asyncio.wait_for(
            _run_independent_channels(
                (lambda: channel("text"), lambda: channel("visual")), ctx
            ),
            1,
        )
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert finished == {"text", "visual"}
