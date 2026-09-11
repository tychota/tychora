from datetime import UTC, datetime, timedelta

import pytest

from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobEvent


def accepted(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.capture(job.id, jobs.manifest("job_capture"))
    return job


def test_state_events_and_monotonic_pagination_survive_expiry(jobs):
    job = accepted(jobs)
    jobs.repo.start(job.id)
    jobs.repo.stage(job.id, "extract")
    before = jobs.repo.events_after(job.id, 0, 100)
    assert [event.sequence for event in before] == list(range(1, len(before) + 1))
    assert before[-1].stage == "extract"
    assert jobs.repo.events_after(job.id, before[-2].sequence, 1) == (before[-1],)
    jobs.repo.expire_diagnostics(datetime.now(UTC) + timedelta(days=31))
    assert jobs.repo.events_after(job.id, 0, 100) == ()
    sequence = jobs.repo.append_event(
        job.id,
        JobEvent(
            job_id=job.id,
            sequence=1,
            timestamp=datetime.now(UTC),
            stage="progress",
            state="running",
        ),
    )
    assert sequence == before[-1].sequence + 1
    assert jobs.repo.get(job.id).latest_sequence == sequence
    assert jobs.repo.get(job.id).retention_limited
    assert jobs.accept("add", jobs.scope, "key", {}, jobs.config).id == job.id
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", jobs.scope, "key", {"changed": True}, jobs.config)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_transition_and_event_are_one_transaction(jobs):
    job = accepted(jobs)
    jobs.db.execute(
        "CREATE TEMP TRIGGER reject_event BEFORE INSERT ON job_events BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    with pytest.raises(TychoraError):
        jobs.repo.start(job.id)
    assert jobs.repo.get(job.id).state == "queued"
    assert jobs.repo.get(job.id).latest_sequence == 2


@pytest.mark.parametrize("sequence,limit", [(-1, 1), (0, 0), (0, 1001), (True, 1)])
def test_event_reads_are_bounded(jobs, sequence, limit):
    job = accepted(jobs)
    with pytest.raises(TychoraError) as error:
        jobs.repo.events_after(job.id, sequence, limit)
    assert error.value.code == "INVALID_REQUEST"


def test_checkpoint_replays_only_identical_input_output_and_requires_capture(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    output = jobs.manifest("extraction", data={"units": []})
    with pytest.raises(TychoraError):
        jobs.repo.checkpoint(job.id, "extract", "a" * 64, output)
    jobs.repo.capture(job.id, jobs.manifest("job_capture"))
    jobs.repo.start(job.id)
    jobs.repo.checkpoint(job.id, "extract", "a" * 64, output)
    sequence = jobs.repo.get(job.id).latest_sequence
    jobs.repo.checkpoint(job.id, "extract", "a" * 64, output)
    assert jobs.repo.get(job.id).latest_sequence == sequence
    assert jobs.repo.completed_step(job.id, "extract", "a" * 64) == output
    for key, sha, ref in (
        ("extract", "b" * 64, output),
        ("extract", "a" * 64, jobs.config),
        ("__tychora__/sequence", "a" * 64, output),
    ):
        with pytest.raises(TychoraError):
            jobs.repo.checkpoint(job.id, key, sha, ref)
    (jobs.artifacts.root / output.storage_key).unlink()
    with pytest.raises(TychoraError) as error:
        jobs.repo.completed_step(job.id, "extract", "a" * 64)
    assert error.value.code == "INTEGRITY_ERROR"


def test_running_cancel_is_cooperative_and_terminal_at_next_stage(jobs):
    job = accepted(jobs)
    jobs.repo.start(job.id)
    assert jobs.repo.cancel(job.id).cancellation_requested
    with pytest.raises(TychoraError) as error:
        jobs.repo.stage(job.id, "next")
    assert error.value.code == "CANCELLED"
    assert jobs.repo.get(job.id).state == "cancelled"
    assert jobs.repo.events_after(job.id, 0, 100)[-1].state == "cancelled"
    with pytest.raises(TychoraError):
        jobs.repo.retry(job.id)


def test_queued_cancel_is_immediately_terminal(jobs):
    job = accepted(jobs)
    assert jobs.repo.cancel(job.id).state == "cancelled"
    with pytest.raises(TychoraError):
        jobs.repo.start(job.id)


def test_only_explicit_bounded_transient_retries_reuse_job(jobs):
    job = accepted(jobs)
    for attempt in range(4):
        jobs.repo.start(job.id)
        jobs.repo.fail(
            job.id, TychoraError("PROVIDER_FAILURE", "Unavailable", retryable=True)
        )
        if attempt < 3:
            retry = jobs.repo.retry(job.id)
            assert retry.id == job.id
            assert retry.state == "queued"
            assert retry.retry_count == attempt + 1
        else:
            with pytest.raises(TychoraError):
                jobs.repo.retry(job.id)
    assert jobs.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


@pytest.mark.parametrize(
    "error",
    [
        TychoraError("INVALID_REQUEST", "Invalid", retryable=True),
        TychoraError("PROVIDER_FAILURE", "Permanent"),
    ],
)
def test_permanent_failure_cannot_retry(jobs, error):
    job = accepted(jobs)
    jobs.repo.start(job.id)
    jobs.repo.fail(job.id, error)
    with pytest.raises(TychoraError):
        jobs.repo.retry(job.id)


def test_completed_result_binding_survives_diagnostic_expiry(jobs):
    job = accepted(jobs)
    result = jobs.manifest("retained_run", data={"count": 1})
    jobs.repo.start(job.id)
    jobs.repo.complete(job.id, result=result)
    jobs.repo.expire_diagnostics(datetime.now(UTC) + timedelta(days=31))
    status = jobs.repo.get(job.id)
    assert status.state == "succeeded"
    assert status.result == result
    assert status.retention_limited
    with pytest.raises(TychoraError):
        jobs.repo.retry(job.id)
    assert jobs.accept("add", jobs.scope, "key", {}, jobs.config).state == "succeeded"


def test_event_sequence_survives_expiry_and_reopen(jobs):
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.jobs import JobRepository

    job = accepted(jobs)
    jobs.repo.expire_diagnostics(datetime.now(UTC) + timedelta(days=31))
    db = open_corpus(jobs.path)
    try:
        repo = JobRepository(db, artifacts=jobs.artifacts, workspace_id="workspace")
        assert (
            repo.append_event(
                job.id,
                JobEvent(
                    job_id=job.id,
                    sequence=99,
                    timestamp=datetime.now(UTC),
                    stage="progress",
                    state="queued",
                ),
            )
            == 3
        )
    finally:
        db.close()


def test_cancelled_stage_cannot_publish_checkpoint(jobs):
    job = accepted(jobs)
    jobs.repo.start(job.id)
    jobs.repo.cancel(job.id)
    with pytest.raises(TychoraError) as error:
        jobs.repo.checkpoint(job.id, "extract", "a" * 64, jobs.config)
    assert error.value.code == "CANCELLED"
    assert (
        jobs.db.execute(
            "SELECT count(*) FROM job_steps WHERE step_key='extract'"
        ).fetchone()[0]
        == 0
    )
    assert jobs.repo.get(job.id).state == "cancelled"
