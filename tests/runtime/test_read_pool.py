"""The read pool: WAL snapshots, read-only connections and the boundary guard."""

import asyncio
import logging
import sqlite3
import threading

import pytest

from tychora.adapters.sqlite.connection import open_read_connection
from tychora.application.owner import WorkspaceOwner
from tychora.application.runtime import READ_BOUND, READ_THREAD_PREFIX, ReadPool
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def owner(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    settings = resolve_config(path, "test", {})
    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as opened:
        yield opened


def _bump(owner):
    owner.db.execute("UPDATE workspaces SET policy_epoch=policy_epoch+1")


@pytest.mark.asyncio
async def test_read_sees_one_wal_snapshot_while_writer_commits(owner):
    ctx = ExecutionContext()

    def read(tx):
        first = tx.workspace.policy_epoch()
        owner.run_write_sync(lambda: _bump(owner), ExecutionContext())
        return first, tx.workspace.policy_epoch(), threading.current_thread().name

    first, second, thread = await owner.run_read(read, ctx)
    assert first == second
    assert thread.startswith(READ_THREAD_PREFIX)
    assert (
        await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ctx) == first + 1
    )


@pytest.mark.asyncio
async def test_read_connection_refuses_writes(owner):
    def write(tx):
        tx.workspace._db.execute("UPDATE workspaces SET policy_epoch=policy_epoch+1")

    with pytest.raises(TychoraError) as error:
        await owner.run_read(write, ExecutionContext())
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "invariant.unexpected",
    )
    assert error.value.diagnostic_id
    assert (
        await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ExecutionContext())
        == 0
    )


@pytest.mark.asyncio
async def test_read_task_cannot_reach_the_writer_connection(owner, caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(TychoraError) as error:
        await owner.run_read(
            lambda tx: owner.db.execute("SELECT 1"), ExecutionContext()
        )
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "invariant.unexpected",
    )
    assert error.value.diagnostic_id
    assert error.value.diagnostic_id in caplog.text
    assert "ProgrammingError" in caplog.text


def test_read_connection_is_query_only_on_a_wal_database(tmp_path):
    path = tmp_path / "wal.sqlite3"
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x)")
        writer.commit()
        db = open_read_connection(path)
        try:
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("CREATE TEMP TABLE scratch (x)")
        finally:
            db.close()
    finally:
        writer.close()


def test_read_connection_refuses_a_database_not_in_wal_mode(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    writer = sqlite3.connect(path)
    try:
        writer.execute("CREATE TABLE t (x)")
        writer.commit()
    finally:
        writer.close()
    with pytest.raises(TychoraError) as error:
        open_read_connection(path)
    assert error.value.reason == "invariant.unexpected"


@pytest.mark.asyncio
async def test_unexpected_exception_becomes_typed_error_with_diagnostic(owner, caplog):
    def malformed(tx):
        return {}["missing column"]

    with caplog.at_level(logging.ERROR), pytest.raises(TychoraError) as error:
        await owner.run_read(malformed, ExecutionContext())
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "invariant.unexpected",
    )
    assert error.value.diagnostic_id
    assert error.value.diagnostic_id in caplog.text
    assert "KeyError" in caplog.text
    # The owner keeps serving reads and writes.
    assert await owner.run_read(
        lambda tx: tx.workspace.workspace_id(), ExecutionContext()
    )
    assert await owner.run_write(lambda: 1, ExecutionContext()) == 1


@pytest.mark.asyncio
async def test_typed_errors_cross_the_boundary_unchanged(owner):
    def refuse(tx):
        raise TychoraError("NOT_FOUND", "absent", reason="snapshot.absent")

    with pytest.raises(TychoraError) as error:
        await owner.run_read(refuse, ExecutionContext())
    assert (error.value.reason, error.value.diagnostic_id) == ("snapshot.absent", None)


def test_read_pool_bound_128():
    started = threading.Barrier(3, timeout=5)
    release = threading.Event()
    pool = ReadPool(
        lambda: sqlite3.connect(":memory:", check_same_thread=False), workers=2
    )

    def held():
        started.wait()
        assert release.wait(5)

    running = [pool.submit(held) for _ in range(2)]
    started.wait()
    queued = [pool.submit(lambda: 1) for _ in range(READ_BOUND)]
    try:
        with pytest.raises(TychoraError) as error:
            pool.submit(lambda: 2)
        assert error.value.reason == "queue.full"
    finally:
        release.set()
    for future in (*running, *queued):
        future.result(5)
    pool.close()


def test_each_pool_thread_holds_its_own_connection():
    pool = ReadPool(
        lambda: sqlite3.connect(":memory:", check_same_thread=False), workers=2
    )
    barrier = threading.Barrier(2, timeout=5)

    def identity():
        barrier.wait()
        return id(pool.connection()), id(pool.connection())

    first, second = (pool.submit(identity) for _ in range(2))
    a, b = first.result(5), second.result(5)
    pool.close()
    assert a[0] == a[1] and b[0] == b[1] and a[0] != b[0]


@pytest.mark.asyncio
async def test_reads_run_concurrently_with_a_long_write(owner):
    entered, release = threading.Event(), threading.Event()

    def long_write():
        entered.set()
        assert release.wait(5)

    write = asyncio.create_task(owner.run_write(long_write, ExecutionContext()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (
            await owner.run_read(
                lambda tx: tx.workspace.policy_epoch(), ExecutionContext()
            )
            == 0
        )
    finally:
        release.set()
        await write


@pytest.mark.asyncio
async def test_a_transaction_left_open_on_a_pool_connection_is_rolled_back(owner):
    database = owner.path / "corpus.sqlite3"
    owner._reads.close()
    owner._reads = ReadPool(lambda: open_read_connection(database), workers=1)

    def leave_open():
        db = owner._reads.connection()
        db.execute("BEGIN")
        # The first read fixes the transaction's WAL snapshot.
        return db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0]

    stale = owner._reads.submit(leave_open).result(5)
    await owner.run_write(lambda: _bump(owner), ExecutionContext())

    def read(tx):
        return tx.workspace.policy_epoch()

    # The same (only) pool thread serves the read: it must not join the stale
    # transaction, so it sees the writer's commit.
    assert await owner.run_read(read, ExecutionContext()) == stale + 1
    assert not owner._reads.submit(
        lambda: owner._reads.connection().in_transaction
    ).result(5)
