"""Bounded query-channel scheduling, and a large add that does not stall queries."""

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import AsyncTychora
from tychora.api import ProviderBindings
from tychora.application.query import _run_independent_channels
from tychora.configuration.resolve import resolve_config
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


_CONCURRENCY = 20
# Stages up to parsing; every later stage writes canonical rows or indexes.
_EARLY_STAGES = {"accepted", "start", "acquire", "capture", "parse"}
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


async def _round(kb):
    """One sample: ``_CONCURRENCY`` concurrent queries and when they ran."""
    started = datetime.now(UTC)
    timings = await asyncio.gather(
        *(_timed(kb, "verification outcome") for _ in range(_CONCURRENCY))
    )
    return started, datetime.now(UTC), timings


async def _events(kb, job_id):
    events = []
    while batch := await kb.jobs.events_after(job_id, after=len(events)):
        events.extend(batch)
    return events


def _writer_window(events):
    """When the job was past parsing: its first later stage until it settled."""
    later = [e.timestamp for e in events if e.stage not in _EARLY_STAGES]
    settled = [e.timestamp for e in events if e.state in _TERMINAL]
    assert later and settled
    return min(later), min(settled)


async def _add_under_sampled_queries(kb):
    """Idle and busy p95 of 20 concurrent queries, and whether busy overlapped writes.

    Busy rounds run back to back from the add until the job settles, so they
    cover the writer-heavy stages, not only capture and parse.
    """
    for index in range(5):
        job = await kb.sources.add(_source(f"seed-{index}.md", 3))
        assert (await kb.jobs.wait(job.id)).state == "succeeded"
    await _timed(kb, "pump seal")
    _, _, idle = await _round(kb)
    job = await kb.sources.add(_source("book.md", 300))
    rounds = []
    while True:
        rounds.append(await _round(kb))
        if (await kb.jobs.get(job.id)).state in _TERMINAL:
            break
    assert (await kb.jobs.wait(job.id)).state == "succeeded"
    writes_began, settled = _writer_window(await _events(kb, job.id))
    overlapped = [
        timings
        for started, ended, timings in rounds
        if started <= settled and ended >= writes_began
    ]
    busy = [value for _, _, timings in rounds for value in timings]
    return _p95(idle), _p95(busy), bool(overlapped)


async def _no_busy_refusals(kb):
    callers = await asyncio.gather(
        *(kb.search({"question": f"seal {n}"}) for n in range(33)),
        return_exceptions=True,
    )
    assert not [c for c in callers if isinstance(c, BaseException)]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_add_of_300_pages_with_20_queries(tmp_path):
    """No busy refusals; 20 concurrent queries keep p95 within 1.5x of idle during the add."""
    async with AsyncTychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        idle, busy, overlapped = await _add_under_sampled_queries(kb)
        await _no_busy_refusals(kb)
    assert overlapped
    assert busy <= 1.5 * max(idle, 0.05)


class _ScoringTransport:
    """A synthetic rerank endpoint that scores every document it is sent."""

    calls: ClassVar[list[int]] = []

    def post_json(self, path, body, timeout):
        from tychora.adapters.voyage.request_types import RawVoyageResponse

        documents = json.loads(body)["documents"]
        self.calls.append(len(documents))
        data = [
            {"index": index, "relevance_score": 1.0 / (index + 1)}
            for index in range(len(documents))
        ]
        return RawVoyageResponse(200, json.dumps({"data": data}).encode())

    def close(self):
        pass


def _online_rerank_config(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="general-documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:SYNTHETIC_UNUSED_KEY"\n'
    )
    return resolve_config(settings, "local", {})


async def _bind_fake_reranker(kb):
    """Bind a synthetic reranker the way an owned runtime is bound per query."""
    from tests.providers.test_reranker import LengthCounter
    from tychora.adapters.voyage.runtime import VoyageRuntime
    from tychora.application.reranking import _create_injected_reranker

    workspace = kb._open()
    owner, service = workspace.owner, workspace.services.query
    granted = await kb.management.run(
        {
            "operation": "provider-upload-consent",
            "collection_id": "default",
            "provider": "voyage",
            "action": "grant",
        }
    )
    assert granted.upload_consent is not None
    runtime = VoyageRuntime(
        _ScoringTransport,
        workspace_id=owner.workspace_id,
        credential_binding_id="synthetic",
    )
    counter = LengthCounter()
    ctx = ExecutionContext()
    reranker = await workspace.bridge.acall(
        _create_injected_reranker(
            owner, runtime=runtime, scope=kb.scope, counter=counter, ctx=ctx
        ),
        ctx,
    )
    service.bindings = replace(
        service.bindings, reranker=reranker, rerank_counter=counter
    )
    # Owned runtimes rebind the reranker to each query's scope on the writer.
    service.auto_rerank = True


async def _writer_priorities(kb, question):
    """The lanes of the writer tasks one query submits, with nothing else running."""
    worker = kb._open().owner._worker
    submit, lanes = worker.submit, []

    def recording(call, priority="background"):
        lanes.append(priority)
        return submit(call, priority)

    worker.submit = recording
    try:
        await kb.search({"question": question})
    finally:
        del worker.submit
    return lanes


@pytest.mark.slow
@pytest.mark.asyncio
async def test_reranked_queries_keep_pace_with_a_300_page_add(tmp_path):
    """Online-shaped: each query binds and calls a reranker through the writer.

    A reranked query's writer tasks must all run on the interactive lane, or
    they queue behind ingestion; the lane check pins that directly, and the
    sampled rounds check that the add does not stall reranked queries.
    """
    async with AsyncTychora.open(
        tmp_path / "ws",
        config=_online_rerank_config(tmp_path),
        providers=ProviderBindings(),
        token_counter=OutputCounter(),
    ) as kb:
        await _bind_fake_reranker(kb)
        _ScoringTransport.calls.clear()
        idle, busy, overlapped = await _add_under_sampled_queries(kb)
        lanes = await _writer_priorities(kb, "pump seal verification")
        probe = await kb.search({"question": "verification outcome"})
        await _no_busy_refusals(kb)
    # Every sampled query reranked through the synthetic endpoint.
    assert "rerank" not in probe.coverage.unavailable_channels
    assert len(_ScoringTransport.calls) >= 2 * _CONCURRENCY
    assert lanes and set(lanes) == {"interactive"}
    assert overlapped
    assert busy <= 1.5 * max(idle, 0.05)
