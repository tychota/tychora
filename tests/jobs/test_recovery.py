import pytest

from tychora.contracts.errors import TychoraError


class NoPublication:
    def lookup(self, job_id):
        return None


def test_recovery_marks_interruption_and_explicit_retry_allows_first_acquisition(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.start(job.id)
    recovered = jobs.repo.recover(job.id)
    assert recovered.state == "interrupted"
    assert recovered.error_code == "JOB_INTERRUPTED"
    assert recovered.captured_inputs is None
    assert jobs.accept("add", jobs.scope, "key", {}, jobs.config).state == "interrupted"
    assert jobs.repo.retry(job.id).state == "queued"
    jobs.repo.capture(job.id, jobs.manifest("job_capture"))


def test_recovery_requires_verified_committed_capture(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    capture = jobs.manifest("job_capture")
    jobs.repo.capture(job.id, capture)
    jobs.repo.start(job.id)
    (jobs.artifacts.root / capture.storage_key).unlink()
    with pytest.raises(TychoraError) as error:
        jobs.repo.recover(job.id)
    assert error.value.code == "INTEGRITY_ERROR"
    assert jobs.repo.get(job.id).state == "running"


@pytest.mark.parametrize("action", ["recover", "retry"])
def test_publication_receipt_reconciles_completion_without_restarting(jobs, action):
    from tychora.contracts.jobs import PublicationReceipt

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.capture(job.id, jobs.manifest("job_capture"))
    jobs.repo.start(job.id)
    if action == "retry":
        jobs.repo.fail(
            job.id, TychoraError("PROVIDER_FAILURE", "Unavailable", retryable=True)
        )
    result = jobs.manifest("retained_run", data={"count": 1})
    jobs.db.execute(
        "INSERT INTO snapshots VALUES ('published','workspace',NULL,?,?,?,?,'job',0)",
        ("a" * 64, jobs.config.sha256, jobs.config.sha256, "2026-01-01T00:00:00Z"),
    )
    calls = []

    class ReceiptQuery:
        def lookup(self, job_id):
            calls.append((job_id, jobs.db.in_transaction))
            return PublicationReceipt(
                job_id=job.id, result_snapshot_id="published", result_artifact=result
            )

    jobs.repo.receipts = ReceiptQuery()
    status = getattr(jobs.repo, action)(job.id)
    assert status.state == "succeeded"
    assert status.result_snapshot_id == "published"
    assert status.result == result
    assert status.retry_count == 0
    assert calls == [(job.id, False)]
    assert jobs.repo.events_after(job.id, 0, 100)[-1].state == "succeeded"


def test_mismatched_publication_receipt_fails_closed(jobs):
    from tychora.contracts.jobs import PublicationReceipt

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.start(job.id)

    class WrongReceipt:
        def lookup(self, job_id):
            return PublicationReceipt(job_id="other-job", result_snapshot_id="missing")

    jobs.repo.receipts = WrongReceipt()
    with pytest.raises(TychoraError) as error:
        jobs.repo.recover(job.id)
    assert error.value.code == "INTEGRITY_ERROR"
    assert jobs.repo.get(job.id).state == "running"


def test_missing_receipt_port_never_guesses_publication_state(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.start(job.id)
    jobs.repo.receipts = None
    with pytest.raises(TychoraError) as error:
        jobs.repo.recover(job.id)
    assert error.value.code == "CAPABILITY_MISSING"
    assert jobs.repo.get(job.id).state == "running"


def test_snapshot_completion_requires_same_workspace_and_config(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.start(job.id)
    jobs.db.execute(
        "INSERT INTO snapshots VALUES ('wrong','workspace',NULL,?,?,?,?,'job',0)",
        ("a" * 64, "b" * 64, jobs.config.sha256, "2026-01-01T00:00:00Z"),
    )
    with pytest.raises(TychoraError):
        jobs.repo.complete(job.id, result_snapshot_id="wrong")
    assert jobs.repo.get(job.id).state == "running"


def test_recovery_of_published_capture_does_not_require_retired_original(jobs):
    from io import BytesIO

    from tychora.contracts.jobs import PublicationReceipt

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    original = jobs.artifacts.put_stream(BytesIO(b"original"), "text/plain")
    jobs.repo.capture(
        job.id,
        jobs.manifest(
            "job_capture", original_artifacts=[original.model_dump(mode="json")]
        ),
    )
    jobs.repo.start(job.id)
    jobs.db.execute(
        "INSERT INTO snapshots VALUES ('published','workspace',NULL,?,?,?,?,'job',0)",
        ("a" * 64, jobs.config.sha256, jobs.config.sha256, "2026-01-01T00:00:00Z"),
    )

    class Published:
        def lookup(self, job_id):
            return PublicationReceipt(job_id=job_id, result_snapshot_id="published")

    jobs.repo.receipts = Published()
    (jobs.artifacts.root / original.storage_key).unlink()
    assert jobs.repo.recover(job.id).state == "succeeded"


def test_completed_receipt_cannot_add_a_new_result_artifact(jobs):
    from tychora.contracts.jobs import PublicationReceipt

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.start(job.id)
    jobs.db.execute(
        "INSERT INTO snapshots VALUES ('published','workspace',NULL,?,?,?,?,'job',0)",
        ("a" * 64, jobs.config.sha256, jobs.config.sha256, "2026-01-01T00:00:00Z"),
    )
    jobs.repo.complete(job.id, result_snapshot_id="published")

    class Changed:
        def lookup(self, job_id):
            return PublicationReceipt(
                job_id=job_id,
                result_snapshot_id="published",
                result_artifact=jobs.config,
            )

    jobs.repo.receipts = Changed()
    with pytest.raises(TychoraError):
        jobs.repo.recover(job.id)


def test_retry_budget_survives_repository_restart(jobs):
    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.jobs import JobRepository

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    for _ in range(3):
        jobs.repo.start(job.id)
        jobs.repo.fail(
            job.id, TychoraError("PROVIDER_FAILURE", "Unavailable", retryable=True)
        )
        jobs.repo.retry(job.id)
    jobs.repo.start(job.id)
    jobs.repo.fail(
        job.id, TychoraError("PROVIDER_FAILURE", "Unavailable", retryable=True)
    )
    db = open_corpus(jobs.path)
    try:
        repo = JobRepository(
            db,
            artifacts=jobs.artifacts,
            workspace_id="workspace",
            receipts=NoPublication(),
        )
        assert repo.get(job.id).retry_count == 3
        with pytest.raises(TychoraError):
            repo.retry(job.id)
    finally:
        db.close()
