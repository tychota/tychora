"""Bounded query-channel scheduling, and a large add that does not stall queries."""

import asyncio
import time

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import AsyncTychora
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


_PAGE = "Operators inspect the pump seal and record the verification outcome. " * 30


def _source(key, pages):
    return {
        "type": "text",
        "source_key": key,
        "collection_id": "default",
        "kind": "document@1",
        "format": "markdown",
        "text": "\n\n".join(f"# Page {n}\n\n{_PAGE}" for n in range(pages)),
        "metadata": {"title": key},
    }


async def _timed(kb, question):
    started = time.perf_counter()
    await kb.search({"question": question})
    return time.perf_counter() - started


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered)) - 1)]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_add_of_300_pages_with_20_queries(tmp_path):
    """No busy refusals, and query p95 stays within 1.5x of idle during the add."""
    async with AsyncTychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for index in range(5):
            job = await kb.sources.add(_source(f"seed-{index}.md", 3))
            assert (await kb.jobs.wait(job.id)).state == "succeeded"
        await _timed(kb, "pump seal")
        idle = [await _timed(kb, "verification outcome") for _ in range(20)]
        job = await kb.sources.add(_source("book.md", 300))
        busy = await asyncio.gather(
            *(_timed(kb, "verification outcome") for _ in range(20))
        )
        assert (await kb.jobs.wait(job.id)).state == "succeeded"
        callers = await asyncio.gather(
            *(kb.search({"question": f"seal {n}"}) for n in range(33)),
            return_exceptions=True,
        )
    assert not [c for c in callers if isinstance(c, BaseException)]
    assert _p95(busy) <= 1.5 * max(_p95(idle), 0.05)
