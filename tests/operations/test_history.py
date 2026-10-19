"""Query diagnostics retain operational metadata, never evidence bodies."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.history import HistoryRepository, RunMetrics
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Coverage, QueryResult
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.retrieval.response import DeliveredResult


@pytest.fixture
def history(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) VALUES (?,?,?)",
        ("workspace", 2, now.isoformat()),
    )
    db.execute(
        "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
        ("a" * 64, 1, "application/json", "a" * 64, now.isoformat()),
    )
    db.execute(
        "INSERT INTO snapshots(snapshot_id,workspace_id,membership_sha,config_sha,"
        "manifest_sha,published_at,job_id,member_count) VALUES (?,?,?,?,?,?,'job',0)",
        ("snapshot", "workspace", "b" * 64, "c" * 64, "a" * 64, now.isoformat()),
    )
    db.commit()
    try:
        yield HistoryRepository(db, now=lambda: now), db, now
    finally:
        db.close()


def _result() -> DeliveredResult:
    result = QueryResult(
        run_id="run-1",
        snapshot_id="snapshot",
        applied_scope=RequestScope(principal_id="principal"),
        coverage=Coverage(status="complete", completed_channels=("lexical",)),
        warnings=("private query text must not survive",),
    )
    return DeliveredResult(result, (b"secret evidence passage",), 4, 23)


def test_default_history_excludes_payload_and_cannot_replay(history) -> None:
    repository, db, _ = history
    repository.record(
        _result(),
        RunMetrics(
            policy_epoch=7,
            lexical_statistics_epoch=3,
            config_sha="c" * 64,
            request_fingerprint="d" * 64,
            duration_ms=12,
            candidate_count=2,
        ),
        None,
    )
    row = db.execute(
        "SELECT metrics_json,result_ids_json,retained_payload_sha FROM query_runs"
    ).fetchone()
    assert row is not None
    assert row[2] is None
    assert "secret evidence passage" not in row[0] + row[1]
    assert "private query text" not in row[0] + row[1]
    assert json.loads(row[0])["request_fingerprint"] == "d" * 64
    assert db.execute("SELECT policy_epoch FROM query_runs").fetchone()[0] == 7
    with pytest.raises(TychoraError) as error:
        repository.replay_request("run-1")
    assert error.value.code == "REPLAY_UNAVAILABLE"


def test_history_purge_is_independent_of_memory_cache(history) -> None:
    repository, db, now = history
    repository.record(
        _result(),
        RunMetrics(
            policy_epoch=7,
            lexical_statistics_epoch=3,
            config_sha="c" * 64,
            request_fingerprint="d" * 64,
            duration_ms=12,
            candidate_count=2,
        ),
        None,
    )
    assert repository.purge(now + timedelta(days=29)) == 0
    assert repository.purge(now + timedelta(days=31)) == 1
    assert db.execute("SELECT COUNT(*) FROM query_runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_opt_in_retained_request_checks_current_bindings_before_replay(
    history, tmp_path
) -> None:
    _, db, now = history
    artifacts = LocalArtifacts(tmp_path / "artifacts")
    repository = HistoryRepository(
        db, now=lambda: now, artifacts=artifacts, retain_payloads=True
    )
    request = QueryRequest.from_mapping(
        {"question": "private retained question", "context": {"code": "A1"}}
    )
    scope = RequestScope(principal_id="principal")
    retained = repository.retain_request(
        "run-1",
        request,
        scope=scope,
        snapshot_id="snapshot",
        config_sha="c" * 64,
        projection_fingerprint="e" * 64,
        provider_fingerprint="f" * 64,
    )
    repository.record(
        _result(),
        RunMetrics(
            policy_epoch=7,
            lexical_statistics_epoch=3,
            config_sha="c" * 64,
            request_fingerprint="d" * 64,
            duration_ms=12,
            candidate_count=2,
        ),
        retained,
    )
    assert (
        repository.replay_request(
            "run-1",
            scope=scope,
            snapshot_id="snapshot",
            config_sha="c" * 64,
            projection_fingerprint="e" * 64,
            provider_fingerprint="f" * 64,
            providers_available=True,
        )
        == request
    )
    with pytest.raises(TychoraError) as error:
        repository.replay_request(
            "run-1",
            scope=scope,
            snapshot_id="snapshot",
            config_sha="x" * 64,
            projection_fingerprint="e" * 64,
            provider_fingerprint="f" * 64,
            providers_available=True,
        )
    assert error.value.code == "REPLAY_UNAVAILABLE"
    repository.supersede("run-1")
    with pytest.raises(TychoraError) as superseded:
        repository.replay_request(
            "run-1",
            scope=scope,
            snapshot_id="snapshot",
            config_sha="c" * 64,
            projection_fingerprint="e" * 64,
            provider_fingerprint="f" * 64,
            providers_available=True,
        )
    assert superseded.value.code == "REPLAY_UNAVAILABLE"
