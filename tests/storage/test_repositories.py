"""SQLite repositories over one canonical connection."""

import sqlite3
from typing import cast

import pytest

from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.repositories import (
    SqliteUnitOfWork,
    artifact_ref,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError

SHA = "a" * 64
REF = ArtifactRef(sha256=SHA, size_bytes=3, media_type="text/plain", storage_key=SHA)


@pytest.fixture
def db(tmp_path):
    connection = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    connection.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('workspace',2,'2026-01-01T00:00:00+00:00')"
    )
    yield connection
    connection.close()


def test_artifact_ref_from_row():
    ref = artifact_ref((SHA, 3, "text/plain", SHA))
    assert ref == REF
    assert ArtifactRef.from_row((SHA, 3, "text/plain", SHA, "present")) == ref


def test_workspace_reads(db):
    uow = SqliteUnitOfWork(db)
    with uow.read() as tx:
        assert tx.workspace.workspace_id() == "workspace"
        assert (tx.workspace.policy_epoch(), tx.workspace.lexical_epoch()) == (0, 0)
        assert tx.workspace.decision_watermark() == 0
    assert not db.in_transaction


def test_register_requires_a_write_transaction(db):
    with SqliteUnitOfWork(db).read() as tx, pytest.raises(TychoraError) as error:
        tx.artifacts.register(REF)
    assert error.value.reason == "invariant.unexpected"


def test_write_rolls_back_on_error_and_refuses_nesting(db):
    uow = SqliteUnitOfWork(db)
    with pytest.raises(RuntimeError), uow.write() as tx:
        tx.artifacts.register(REF)
        raise RuntimeError("boom")
    with uow.read() as tx:
        assert tx.artifacts.get(SHA) is None
    db.execute("BEGIN")
    try:
        with pytest.raises(TychoraError) as error, uow.write():
            pass
        assert error.value.reason == "invariant.unexpected"
        assert db.in_transaction
    finally:
        db.rollback()


def test_artifact_register_and_get(db):
    uow = SqliteUnitOfWork(db)
    with uow.write() as tx:
        assert tx.artifacts.register(REF) is True
        assert tx.artifacts.register(REF) is False
    with uow.read() as tx:
        assert tx.artifacts.get(SHA) == REF
        assert tx.artifacts.get("b" * 64) is None
    conflicting = REF.model_copy(update={"size_bytes": 4})
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.artifacts.register(conflicting)
    assert error.value.reason == "canonical.binding_mismatch"


def test_ambiguous_workspace_row_is_a_typed_error(db):
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('second',2,'2026-01-01T00:00:00+00:00')"
    )
    with SqliteUnitOfWork(db).read() as tx, pytest.raises(TychoraError) as error:
        tx.workspace.workspace_id()
    assert error.value.reason == "canonical.row_invalid"


def test_no_sqlite_type_escapes(db):
    with SqliteUnitOfWork(db).read() as tx:
        assert not isinstance(tx.workspace.policy_epoch(), sqlite3.Row)


def test_read_joining_an_open_transaction_leaves_it_open(db):
    db.execute("BEGIN")
    try:
        with SqliteUnitOfWork(db).read() as tx:
            assert tx.workspace.workspace_id() == "workspace"
        assert db.in_transaction
    finally:
        db.rollback()


def test_write_rolls_back_when_commit_raises(tmp_path):
    class FailingCommit:
        """A connection whose commit fails after the work ran."""

        def __init__(self, inner):
            self._inner = inner

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    connection = open_corpus(tmp_path / "failing.sqlite3", initialize=True)
    try:
        with (
            pytest.raises(sqlite3.OperationalError),
            SqliteUnitOfWork(
                cast(sqlite3.Connection, FailingCommit(connection))
            ).write() as tx,
        ):
            tx.artifacts.register(REF)
        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    finally:
        connection.close()
