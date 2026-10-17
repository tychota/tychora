"""Budgets, stage derivation, verdicts and result documents."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobEvent
from tychora.evaluation.bench.budgets import (
    INGEST_STAGE_BUDGETS_S,
    QUERY_STAGE_BUDGETS_MS,
)
from tychora.evaluation.bench.results import (
    BenchResult,
    CorpusIdentity,
    is_reference,
    load_result,
    machine_fingerprint,
    nearest_rank,
    regressions,
    stage_stats,
    verdict,
    write_result,
)
from tychora.evaluation.bench.stages import WallClockStageSource, ingest_bucket

LOCAL = (
    "validate_scope_catalog",
    "eligibility",
    "candidates",
    "fusion_admission",
    "survivors_rerank_facts",
    "segments_assembly",
    "record",
)
INGEST = (
    "acquire_register",
    "parse",
    "normalize_commit",
    "windowing",
    "embedding",
    "batch_writes",
    "publication",
)


def test_budgets_match_the_spec_tables():
    assert sum(QUERY_STAGE_BUDGETS_MS[name] for name in LOCAL) == 205
    assert QUERY_STAGE_BUDGETS_MS["local_total"] == 205
    assert QUERY_STAGE_BUDGETS_MS["total"] == 205 + 300 + 450
    assert sum(INGEST_STAGE_BUDGETS_S[name] for name in INGEST) == pytest.approx(25.5)
    assert INGEST_STAGE_BUDGETS_S["projection"] == pytest.approx(1.5 + 1.0 + 6.0 + 1.0)


def test_nearest_rank_and_verdicts():
    samples = [float(value) for value in range(1, 101)]
    assert nearest_rank(samples, 0.95) == 95.0
    assert nearest_rank(samples, 0.5) == 50.0
    assert verdict(54.0, 50.0, reference=True) == "pass"
    assert verdict(56.0, 50.0, reference=True) == "fail"
    assert verdict(56.0, 50.0, reference=False) == "informational"
    assert verdict(56.0, None, reference=True) == "no_budget"
    stats = stage_stats(samples, unit="ms", budget=90.0, reference=True, quantile=0.95)
    assert (stats.n, stats.p95, stats.verdict) == (100, 95.0, "pass")


def _event(sequence: int, stage: str, at: datetime, state="running") -> JobEvent:
    return JobEvent(
        job_id="job", sequence=sequence, timestamp=at, stage=stage, state=state
    )


def test_wall_clock_source_derives_ingest_stages():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stages = (
        "accepted",
        "start",
        "acquire",
        "capture",
        "parse",
        "project",
        "publish",
        "complete",
    )
    events = tuple(
        _event(
            index + 1,
            stage,
            start + timedelta(seconds=index),
            "succeeded" if stage == "complete" else "running",
        )
        for index, stage in enumerate(stages)
    )
    samples = WallClockStageSource(lambda job_id: events).ingest_stages("job")
    durations = {sample.stage: sample.duration_ms for sample in samples}
    assert durations["parse"] == 1000.0
    assert durations["total"] == 7000.0
    assert [
        ingest_bucket(name)
        for name in ("acquire", "capture", "projection/plan/x", "publish")
    ] == [
        "acquire_register",
        "acquire_register",
        "projection",
        "publication",
    ]
    assert WallClockStageSource(lambda job_id: events).query_stages("run") == ()


def _result(p95: float) -> BenchResult:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return BenchResult(
        commit="abc",
        suite="query",
        profile="offline",
        corpus=CorpusIdentity(
            name="bench-small", digest="a" * 64, documents=5, queries=6
        ),
        machine=machine_fingerprint(),
        reference_machine=False,
        retained_sha="b" * 64,
        started_at=now,
        finished_at=now,
        stages={
            "total": stage_stats(
                [p95], unit="ms", budget=205.0, reference=False, quantile=0.95
            )
        },
        usage={},
    )


def test_result_round_trip_and_regressions(tmp_path):
    path = write_result(_result(100.0), tmp_path)
    assert path == tmp_path / "abc" / "query-offline.json"
    document = path.read_text()
    assert '"schema":"tychora.bench-result"' in document
    assert load_result(path) == _result(100.0)
    assert regressions(_result(119.0), _result(100.0)) == ()
    assert regressions(_result(121.0), _result(100.0)) == (
        "total: p95 121.0 ms > 120.0 ms",
    )


def test_reference_machine_requires_the_declared_runner_class():
    machine = machine_fingerprint()
    spec = {
        "runner_class": "github-ubuntu-22.04",
        "os_prefix": machine.os[:5],
        "min_cores": 1,
    }
    assert is_reference(machine, spec, "github-ubuntu-22.04")
    assert not is_reference(machine, spec, None)
    assert not is_reference(machine, spec, "laptop")


def test_regressions_refuse_incomparable_runs():
    other = _result(100.0).model_copy(update={"profile": "online"})
    with pytest.raises(TychoraError) as error:
        regressions(_result(100.0), other)
    assert error.value.reason == "bench.incomparable"
    shared = _result(100.0).model_copy(update={"reference_machine": True})
    with pytest.raises(TychoraError):
        regressions(_result(100.0), shared)


def test_regressions_compare_the_asserted_quantile_and_floor_zero_baselines():
    ingest = stage_stats(
        [1.0] * 9 + [10.0], unit="s", budget=None, reference=False, quantile=0.90
    )
    assert (ingest.quantile, ingest.asserted) == (0.90, 1.0)
    base = _result(0.0)
    assert regressions(_result(0.5), base) == ()
    assert regressions(_result(5.0), base) == ("total: p95 5.0 ms > 1.0 ms",)


def test_write_result_replaces_atomically(tmp_path):
    path = write_result(_result(100.0), tmp_path)
    write_result(_result(110.0), tmp_path)
    assert load_result(path) == _result(110.0)
    assert [item.name for item in path.parent.iterdir()] == [path.name]


def test_unknown_ingest_stages_map_to_unbudgeted_buckets():
    assert ingest_bucket("accepted") == "queue"
    assert ingest_bucket("something_new") == "other"


def test_failed_write_leaves_no_temp_file_and_keeps_the_target(tmp_path, monkeypatch):
    path = write_result(_result(100.0), tmp_path)
    before = path.read_bytes()

    def refuse(source, target):
        raise OSError("replace refused")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="replace refused"):
        write_result(_result(150.0), tmp_path)
    assert path.read_bytes() == before
    assert [item.name for item in path.parent.iterdir()] == [path.name]


def test_written_result_has_umask_permissions(tmp_path):
    previous = os.umask(0o022)
    try:
        path = write_result(_result(100.0), tmp_path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o644
