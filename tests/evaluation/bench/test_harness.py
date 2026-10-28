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
    e3_assertions,
    run_suite,
)
from tychora.evaluation.bench.results import Assertion, ScalePoint, machine_fingerprint
from tychora.evaluation.bench.stages import WallClockStageSource
from tychora.evaluation.bench.synth import SynthSpec
from tychora.ports.repositories import PublicationFootprint

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
        self.artifact_bytes = 300

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

    def publication(self) -> PublicationFootprint:
        return PublicationFootprint(
            snapshot_id="snap",
            root_bytes=300,
            new_page_bytes=100,
            new_pages=1,
            largest_artifact_bytes=self.artifact_bytes,
        )


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
    assert [
        (point.publication_bytes_per_add, point.largest_snapshot_artifact_bytes)
        for point in result.scale
    ] == [(400, 300), (400, 300)]
    assert [point.publication_p90_ms for point in result.scale] == [1000.0, 1000.0]
    assert [(a.name, a.measured) for a in result.assertions] == [
        ("E3.bytes_ratio", False),
        ("E3.artifact_ceiling", True),
        ("E3.publication_p90", False),
        ("E4.latency_ratio", False),
    ]


def _point(documents, bytes_per_add, artifact=300, p90=100.0) -> ScalePoint:
    return ScalePoint(
        documents=documents,
        add_p50_ms=1.0,
        bytes_per_add=1.0,
        search_p50_ms=1.0,
        search_p95_ms=1.0,
        largest_artifact_bytes=1,
        footprint_bytes=1,
        publication_bytes_per_add=bytes_per_add,
        largest_snapshot_artifact_bytes=artifact,
        publication_p90_ms=p90,
    )


def test_e3_verdicts_pass_and_fail_on_their_thresholds():
    flat = e3_assertions([_point(1000, 100), _point(10_000, 200)], 10_000)
    assert {a.name: a.passed for a in flat} == {
        "E3.bytes_ratio": True,
        "E3.artifact_ceiling": True,
        "E3.publication_p90": True,
    }
    grown = e3_assertions(
        [_point(1000, 100), _point(10_000, 201, artifact=1024 * 1024 + 1, p90=501.0)],
        10_000,
    )
    assert {a.name: a.passed for a in grown} == {
        "E3.bytes_ratio": False,
        "E3.artifact_ceiling": False,
        "E3.publication_p90": False,
    }


def _stopped_run(tmp_path, client, **context):
    ticks = iter(range(1000))
    return run_suite(
        client,
        _corpus(),
        "scale",
        _context(
            checkpoints=(5, 10),
            max_wall_seconds=context.pop("max_wall_seconds", 8.0),
            clock=lambda: float(next(ticks)),
        ),
        SuiteInputs(tmp_path, WallClockStageSource(client.events)),
    )


def test_scale_suite_stops_at_the_wall_limit(tmp_path):
    client = FakeClient()
    result = _stopped_run(tmp_path, client)
    assert result.aborted
    assert len(client.added) < 10
    verdicts = {a.name: a for a in result.assertions}
    assert set(verdicts) == {
        "E3.bytes_ratio",
        "E3.artifact_ceiling",
        "E3.publication_p90",
        "E4.latency_ratio",
    }
    assert verdicts["E3.artifact_ceiling"].measured
    assert verdicts["E3.artifact_ceiling"].passed
    for name in ("E3.bytes_ratio", "E3.publication_p90", "E4.latency_ratio"):
        assert not verdicts[name].measured
        assert verdicts[name].detail == (
            f"not measured: stopped at {len(client.added)} documents"
        )


def test_stopped_run_keeps_a_failed_artifact_ceiling(tmp_path):
    client = FakeClient()
    client.artifact_bytes = 1024 * 1024 + 1
    result = _stopped_run(tmp_path, client)
    assert result.aborted
    ceiling = next(a for a in result.assertions if a.name == "E3.artifact_ceiling")
    assert ceiling.measured and not ceiling.passed


def test_run_smaller_by_design_reports_not_measured_without_failing(tmp_path):
    client = FakeClient()
    result = _stopped_run(tmp_path, client, max_wall_seconds=1000.0)
    assert not result.aborted
    unreached = [a for a in result.assertions if not a.measured]
    assert [a.name for a in unreached] == [
        "E3.bytes_ratio",
        "E3.publication_p90",
        "E4.latency_ratio",
    ]
    assert all(a.passed and "stopped at 10 documents" in a.detail for a in unreached)


def test_assertions_without_the_measured_field_still_load():
    assert Assertion.model_validate({"name": "x", "passed": True}).measured


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
