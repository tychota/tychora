"""The read pool: WAL snapshots, read-only connections and the boundary guard."""

import asyncio
import logging
import sqlite3
import threading

import pytest

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
    assert (
        await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ExecutionContext())
        == 0
    )


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
