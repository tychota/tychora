import pytest

from tychora.contracts.errors import TychoraError


def test_key_conflict_does_not_schedule_new_work(jobs):
    a = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    b = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    assert a.id == b.id
    with pytest.raises(TychoraError) as err:
        jobs.accept("add", jobs.scope, "k", {"path": "b.md"}, jobs.config)
    assert err.value.code == "IDEMPOTENCY_CONFLICT"
    assert jobs.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_replay_preserves_config_before_verifying_unused_new_default(jobs):
    first = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    changed = jobs.manifest(data={"offline": False})
    (jobs.artifacts.root / changed.storage_key).unlink()
    replay = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, changed)
    assert replay == first
    assert (
        jobs.db.execute("SELECT effective_config_sha FROM jobs").fetchone()[0]
        == jobs.config.sha256
    )


@pytest.mark.parametrize(
    "changed",
    [{"path": "a.md", "option": None}, {"path": "a.md", "config": "explicit-v2"}],
)
def test_omitted_and_explicit_settings_remain_distinct(jobs, changed):
    jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    with pytest.raises(TychoraError, match="different input"):
        jobs.accept("add", jobs.scope, "k", changed, jobs.config)


def test_inline_content_hash_changes_conflict(jobs):
    from hashlib import sha256

    jobs.accept(
        "add",
        jobs.scope,
        "k",
        {"inline_sha256": sha256(b"first").hexdigest()},
        jobs.config,
    )
    with pytest.raises(TychoraError) as error:
        jobs.accept(
            "add",
            jobs.scope,
            "k",
            {"inline_sha256": sha256(b"changed").hexdigest()},
            jobs.config,
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_fresh_key_and_unkeyed_requests_create_fresh_jobs(jobs):
    handles = [
        jobs.accept("add", jobs.scope, key, {}, jobs.config)
        for key in ("one", "two", None, None)
    ]
    assert len({handle.id for handle in handles}) == 4


def test_capture_survives_changed_live_path_and_repository_restart(jobs, tmp_path):
    from io import BytesIO

    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.jobs import JobRepository

    source = tmp_path / "a.md"
    source.write_bytes(b"original")
    intent = {"path": str(source)}
    job = jobs.accept("add", jobs.scope, "k", intent, jobs.config)
    original = jobs.artifacts.put_stream(BytesIO(source.read_bytes()), "text/plain")
    capture = jobs.manifest(
        "job_capture",
        original_artifacts=[original.model_dump(mode="json")],
        data={"captured_at": "2026-01-01T00:00:00Z"},
    )
    jobs.repo.capture(job.id, capture)
    source.write_bytes(b"changed")
    reopened = open_corpus(jobs.path)
    try:
        repo = JobRepository(
            reopened, artifacts=jobs.artifacts, workspace_id="workspace"
        )
        assert repo.accept("add", jobs.scope, "k", intent, jobs.config) == job
        assert repo.get(job.id).captured_inputs == capture
        assert repo.get(job.id).effective_config == jobs.config
        with jobs.artifacts.open_verified(original) as stream:
            assert stream.read() == b"original"
    finally:
        reopened.close()


def test_missing_committed_capture_fails_without_recapture(jobs):
    job = jobs.accept("add", jobs.scope, "k", {}, jobs.config)
    capture = jobs.manifest("job_capture", data={"captured_at": "2026-01-01T00:00:00Z"})
    jobs.repo.capture(job.id, capture)
    (jobs.artifacts.root / capture.storage_key).unlink()
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", jobs.scope, "k", {}, jobs.config)
    assert error.value.code == "INTEGRITY_ERROR"


def test_capture_is_immutable_and_requires_all_declared_payloads(jobs):
    from io import BytesIO

    job = jobs.accept("add", jobs.scope, "k", {}, jobs.config)
    original = jobs.artifacts.put_stream(BytesIO(b"source"), "text/plain")
    capture = jobs.manifest(
        "job_capture", original_artifacts=[original.model_dump(mode="json")]
    )
    (jobs.artifacts.root / original.storage_key).unlink()
    with pytest.raises(TychoraError):
        jobs.repo.capture(job.id, capture)
    assert jobs.db.execute("SELECT captured_inputs_sha FROM jobs").fetchone()[0] is None
    jobs.artifacts.put_stream(BytesIO(b"source"), "text/plain")
    jobs.repo.capture(job.id, capture)
    jobs.repo.capture(job.id, capture)
    with pytest.raises(TychoraError):
        jobs.repo.capture(
            job.id, jobs.manifest("job_capture", data={"different": True})
        )


@pytest.mark.parametrize("equal", [True, False])
def test_concurrent_scoped_acceptance_is_atomic(jobs, equal):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tychora.adapters.sqlite.connection import open_corpus
    from tychora.adapters.sqlite.jobs import JobRepository

    barrier = Barrier(2)

    def accept(value):
        db = open_corpus(jobs.path)
        try:
            repo = JobRepository(db, artifacts=jobs.artifacts, workspace_id="workspace")
            barrier.wait(timeout=5)
            try:
                return repo.accept(
                    "add", jobs.scope, "k", {"value": value}, jobs.config
                ).id
            except TychoraError as error:
                return error.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, [1, 1 if equal else 2]))
    if equal:
        assert outcomes[0] == outcomes[1]
    else:
        assert outcomes.count("IDEMPOTENCY_CONFLICT") == 1
    assert jobs.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_replay_scope_binding_survives_retention_and_scope_narrowing(jobs):
    from datetime import UTC, datetime, timedelta

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.repo.expire_diagnostics(datetime.now(UTC) + timedelta(days=31))
    narrower = jobs.scope.model_copy(update={"collection_ids": frozenset()})
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", narrower, "key", {}, jobs.config)
    assert error.value.code == "FORBIDDEN_SCOPE"
    with pytest.raises(TychoraError):
        jobs.repo.authorize(job.id, narrower, write=False)
    jobs.repo.authorize(job.id, jobs.scope, write=False)


@pytest.mark.parametrize("operation", ["remove", "evaluate", "setup", "unknown"])
def test_operation_capabilities_are_not_all_write(jobs, operation):
    with pytest.raises(TychoraError):
        jobs.accept(operation, jobs.scope, "key", {}, jobs.config)
    assert jobs.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_first_config_is_a_verified_registry_manifest(jobs):
    wrong = jobs.manifest("job_capture")
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", jobs.scope, "key", {}, wrong)
    assert error.value.code == "INTEGRITY_ERROR"
    assert jobs.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_accept_holds_config_and_authorization_pins_until_binding_commit(jobs):
    observed = []

    def at_commit(sql):
        if sql == "COMMIT":
            auth = jobs.db.execute(
                "SELECT output_artifact_sha FROM job_steps WHERE step_key='__tychora__/authorization'"
            ).fetchone()
            observed.append(
                (
                    jobs.artifacts.payload_is_pinned(jobs.config.sha256),
                    jobs.artifacts.payload_is_pinned(auth[0]),
                )
            )

    jobs.db.set_trace_callback(at_commit)
    try:
        jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    finally:
        jobs.db.set_trace_callback(None)
    assert observed == [(True, True)]
    assert not jobs.artifacts.payload_is_pinned(jobs.config.sha256)


def test_repository_rejects_foreign_workspace_identity(jobs):
    from tychora.adapters.sqlite.jobs import JobRepository

    with pytest.raises(TychoraError):
        JobRepository(jobs.db, artifacts=jobs.artifacts, workspace_id="different")


def test_published_capture_no_longer_requires_original_payload_on_replay(jobs):
    from io import BytesIO

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    original = jobs.artifacts.put_stream(BytesIO(b"original"), "text/plain")
    nested = jobs.manifest(
        "repository_capture", original_artifacts=[original.model_dump(mode="json")]
    )
    capture = jobs.manifest(
        "job_capture",
        required_manifests=[
            {
                "artifact": nested.model_dump(mode="json"),
                "type": "repository_capture",
                "schema_version": "1",
            }
        ],
    )
    jobs.repo.capture(job.id, capture)
    jobs.repo.start(job.id)
    jobs.db.execute(
        "INSERT INTO snapshots VALUES ('published','workspace',NULL,?,?,?,?)",
        ("a" * 64, jobs.config.sha256, jobs.config.sha256, "2026-01-01T00:00:00Z"),
    )
    jobs.repo.complete(job.id, result_snapshot_id="published")
    (jobs.artifacts.root / original.storage_key).unlink()
    jobs.db.execute(
        "UPDATE artifacts SET payload_state='deleted',payload_deleted_at='2026-01-02' WHERE artifact_sha=?",
        (original.sha256,),
    )
    assert jobs.accept("add", jobs.scope, "key", {}, jobs.config).state == "succeeded"
    (jobs.artifacts.root / capture.storage_key).unlink()
    with pytest.raises(TychoraError):
        jobs.accept("add", jobs.scope, "key", {}, jobs.config)


def test_replay_checks_current_collection_policy(jobs):
    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    jobs.db.execute(
        "UPDATE collections SET access_floor=1 WHERE collection_id='collection'"
    )
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    assert error.value.code == "FORBIDDEN_SCOPE"
    with pytest.raises(TychoraError):
        jobs.repo.authorize(job.id, jobs.scope, write=False)


def test_required_config_closure_remains_unavailable_when_metadata_deleted(jobs):
    from io import BytesIO

    required = jobs.artifacts.put_stream(BytesIO(b"config dependency"), "text/plain")
    config = jobs.manifest(
        "registry", required_artifacts=[required.model_dump(mode="json")]
    )
    jobs.accept("add", jobs.scope, "key", {}, config)
    jobs.db.execute(
        "UPDATE artifacts SET payload_state='deleted',payload_deleted_at='2026-01-02' WHERE artifact_sha=?",
        (required.sha256,),
    )
    with pytest.raises(TychoraError) as error:
        jobs.accept("add", jobs.scope, "key", {}, config)
    assert error.value.code == "INTEGRITY_ERROR"


def test_binding_mime_hints_share_physical_job_artifacts(jobs):
    from io import BytesIO

    first = jobs.artifacts.put_stream(BytesIO(b"same payload"), "text/plain")
    alternate = first.model_copy(update={"media_type": "application/custom"})
    job = jobs.accept("add", jobs.scope, "mime-first", {}, jobs.config)
    capture = jobs.manifest(
        "job_capture",
        original_artifacts=[
            first.model_dump(mode="json"),
            alternate.model_dump(mode="json"),
        ],
    )
    jobs.repo.capture(job.id, capture)
    jobs.repo.capture(
        job.id, capture.model_copy(update={"media_type": "application/custom"})
    )
    assert jobs.repo.get(job.id).captured_inputs.sha256 == capture.sha256
    row = jobs.db.execute(
        "SELECT media_type FROM artifacts WHERE artifact_sha=?", (first.sha256,)
    ).fetchone()
    assert row == ("text/plain",)
    jobs.repo.start(job.id)
    jobs.repo.checkpoint(job.id, "step", "a" * 64, first)
    jobs.repo.checkpoint(job.id, "step", "a" * 64, alternate)
    jobs.repo.complete(job.id, result=alternate)
    jobs.repo.complete(job.id, result=first)


def test_acquired_bytes_can_equal_a_preexisting_generated_json_artifact(jobs):
    job = jobs.accept("add", jobs.scope, "json-source", {}, jobs.config)
    as_source = jobs.config.model_copy(
        update={"media_type": "application/octet-stream"}
    )
    capture = jobs.manifest(
        "job_capture", original_artifacts=[as_source.model_dump(mode="json")]
    )
    jobs.repo.capture(job.id, capture)
    assert jobs.repo.get(job.id).captured_inputs.sha256 == capture.sha256
    assert jobs.db.execute(
        "SELECT media_type FROM artifacts WHERE artifact_sha=?", (jobs.config.sha256,)
    ).fetchone() == ("application/json",)
