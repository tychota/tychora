"""Suites measured against a fake client."""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tychora.contracts.operations import JobEvent
from tychora.evaluation.bench.corpus import BenchCorpus
from tychora.evaluation.bench.harness import (
    BenchUnavailable,
    Footprint,
    RunContext,
    SearchOutcome,
    SuiteInputs,
    run_suite,
)
from tychora.evaluation.bench.results import machine_fingerprint
from tychora.evaluation.bench.stages import WallClockStageSource
from tychora.evaluation.bench.synth import SynthSpec

STAGES = (
    "accepted",
    "start",
    "acquire",
    "capture",
    "parse",
    "project",
    "publish",
    "complete",
)


class FakeClient:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.searched: list[str] = []

    def add(self, source) -> str:
        self.added.append(source.source_key)
        return f"job-{len(self.added)}"

    def wait(self, job_id: str) -> str:
        return "succeeded"

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        return tuple(
            JobEvent(
                job_id=job_id,
                sequence=index + 1,
                timestamp=start + timedelta(seconds=index),
                stage=stage,
                state="succeeded" if stage == "complete" else "running",
            )
            for index, stage in enumerate(STAGES)
        )

    def search(self, question: str) -> SearchOutcome:
        self.searched.append(question)
        return SearchOutcome(
            run_id=None, unit_ids=("unit",), error_code=None, duration_ms=12.0
        )

    def footprint(self) -> Footprint:
        return Footprint(total_bytes=1000 * len(self.added), largest_artifact_bytes=500)


def _corpus(**synthetic) -> BenchCorpus:
    return BenchCorpus(
        name="fake",
        synthetic=SynthSpec(seed=3, count=10, queries=4, **synthetic),
        documents=(),
        queries=(),
    )


def _context(
    *,
    limit: int | None = None,
    checkpoints: tuple[int, ...] = (100, 1000, 10000),
    max_wall_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RunContext:
    return RunContext(
        commit="abc",
        profile="offline",
        machine=machine_fingerprint(),
        reference=False,
        retained_sha="c" * 64,
        checkpoints=checkpoints,
        max_wall_seconds=max_wall_seconds,
        limit=limit,
        clock=clock,
    )


def test_ingest_suite_buckets_job_stages(tmp_path):
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(),
        "ingest",
        _context(limit=5),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )
    assert len(client.added) == 5
    assert result.stages["parse"].n == 5
    assert result.stages["acquire_register"].p50 == 2.0
    assert result.stages["total"].budget == 25.5
    assert result.stages["total"].verdict == "informational"


def test_query_suite_records_totals_and_the_probe(tmp_path):
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(verification_share=1.0),
        "query",
        _context(),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )
    assert result.stages["total"].budget == 205.0
    executed = len(client.searched)
    assert executed == 2 * result.stages["total"].n
    assert client.searched[: executed // 2] == client.searched[executed // 2 :]
    assert result.stages["total"].n >= 1
    assert [assertion.passed for assertion in result.assertions] == [True]


def test_scale_suite_measures_each_checkpoint(tmp_path):
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(),
        "scale",
        _context(checkpoints=(5, 10)),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )
    assert [(point.documents, point.bytes_per_add) for point in result.scale] == [
        (5, 1000.0),
        (10, 1000.0),
    ]
    assert not result.aborted


def test_scale_suite_stops_at_the_wall_limit(tmp_path):
    ticks = iter(range(1000))
    client = FakeClient()
    result = run_suite(
        client,
        _corpus(),
        "scale",
        _context(
            checkpoints=(10,), max_wall_seconds=3.0, clock=lambda: float(next(ticks))
        ),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )
    assert result.aborted
    assert len(client.added) < 10


def test_families_suite_is_unavailable(tmp_path):
    client = FakeClient()
    with pytest.raises(BenchUnavailable, match="families suite"):
        run_suite(
            client,
            _corpus(),
            "families",
            _context(),
            SuiteInputs(tmp_path, WallClockStageSource(client.events)),
        )


def test_failed_searches_are_failures_not_latency_samples(tmp_path):
    class Failing(FakeClient):
        def search(self, question: str) -> SearchOutcome:
            super().search(question)
            return SearchOutcome(
                run_id=None, unit_ids=(), error_code="INTERNAL", duration_ms=1.0
            )

    client = Failing()
    result = run_suite(
        client,
        _corpus(),
        "query",
        _context(),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )
    assert result.failures
    assert result.stages["total"].n == 0
