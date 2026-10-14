"""The test-only stream gate releases only on the actual successful SSE response."""

import asyncio

import pytest

from dashboard.e2e.server import StreamGate


@pytest.mark.asyncio
async def test_stream_gate_requires_the_matching_successful_sse_headers():
    gate = StreamGate()
    waiting = asyncio.create_task(gate.wait_for_stream("held-job", timeout=1))
    await asyncio.sleep(0)
    for path, status, media in (
        ("/v1/jobs/other/events", 200, b"text/event-stream"),
        ("/v1/jobs/held-job", 200, b"application/json"),
        ("/v1/jobs/held-job/events", 403, b"text/event-stream"),
        ("/v1/jobs/held-job/events", 200, b"application/json"),
    ):
        gate.observe(
            path,
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", media)],
            },
        )
    assert not gate._waiting["held-job"].is_set()
    assert not waiting.done()
    gate.observe(
        "/v1/jobs/held-job/events",
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
        },
    )
    await asyncio.wait_for(waiting, timeout=1)


@pytest.mark.asyncio
async def test_stream_gate_fails_with_a_bounded_missing_subscription():
    with pytest.raises(TimeoutError, match="SSE subscription did not start"):
        await StreamGate().wait_for_stream("missing-job", timeout=0.01)
