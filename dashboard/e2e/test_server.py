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


def test_large_pdf_fixture_exceeds_json_cap_with_real_scanned_native_blank_pages(
    tmp_path,
):
    from dashboard.e2e.pdf_fixture import generate_pdf
    from tychora.application.pdf_processing import PDF_LIMITS
    from tychora.ingestion.pdf_analysis import analyse_pdf

    path = tmp_path / "synthetic.pdf"
    path.write_bytes(generate_pdf())
    assert path.stat().st_size > 1048576
    facts = analyse_pdf(path, PDF_LIMITS)
    assert facts["native_text_pages"] == (2,)
    assert facts["image_only_pages"] == (1,)
    assert facts["blank_pages"] == (3,)
