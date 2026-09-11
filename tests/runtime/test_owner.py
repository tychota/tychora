import asyncio
import threading
import time

import pytest

from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


@pytest.fixture
def scope():
    return RequestScope(principal_id="owner")


def test_checkpoint_stops_cancelled_and_expired_work():
    ctx = ExecutionContext()
    ctx.cancelled.set()
    with pytest.raises(TychoraError, match="cancelled") as exc:
        ctx.checkpoint()
    assert exc.value.code == "CANCELLED"
    with pytest.raises(TychoraError) as exc:
        ExecutionContext(deadline=time.monotonic() - 1).checkpoint()
    assert exc.value.code == "DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_owner_worker_preserves_sqlite_affinity_and_heartbeat(
    tmp_path, settings, scope
):
    started, release = threading.Event(), threading.Event()
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:

        def work():
            started.set()
            assert release.wait(5)
            return owner.db.execute("SELECT workspace_id FROM workspaces").fetchone()[0]

        pending = asyncio.create_task(owner.run_blocking(work, ExecutionContext()))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            await asyncio.sleep(0.01)
            release.set()
            assert await pending == owner.workspace_id
        finally:
            release.set()
            await pending


@pytest.mark.asyncio
async def test_cancelled_delivery_keeps_worker_lease_until_actual_exit(
    tmp_path, settings, scope
):
    started, release = threading.Event(), threading.Event()
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:

        def work():
            with owner.leases.pin(frozenset({"snapshot-a"})):
                started.set()
                assert release.wait(5)

        pending = asyncio.create_task(owner.run_blocking(work, ExecutionContext()))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            pending.cancel()
            await asyncio.sleep(0.02)
            assert owner.leases.is_pinned("snapshot-a")
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not owner.leases.is_pinned("snapshot-a")


@pytest.mark.asyncio
async def test_queue_bound_rejects_excess_work(tmp_path, settings, scope):
    from tychora.application.runtime import BoundedWorker, drain_result

    worker = BoundedWorker(workers=1, queued=32)
    release, started = threading.Event(), threading.Event()

    def held():
        started.set()
        assert release.wait(5)

    first = worker.submit(held)
    assert started.wait(1)
    queued = [worker.submit(lambda: 1) for _ in range(32)]
    try:
        with pytest.raises(TychoraError) as exc:
            worker.submit(lambda: 2)
        assert exc.value.code == "WORKSPACE_BUSY"
        assert exc.value.retryable
    finally:
        release.set()
        await drain_result(first, ExecutionContext())
        for future in queued:
            assert await drain_result(future, ExecutionContext()) == 1
        worker.close()
    with pytest.raises(TychoraError):
        worker.submit(lambda: 3)


def test_process_lock_and_stale_endpoint(tmp_path, settings, scope):
    import json
    import os
    import subprocess
    import sys

    root = tmp_path / "data"
    code = """
import sys
from pathlib import Path
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.requests import RequestScope
from tychora.contracts.errors import TychoraError
try:
    with WorkspaceOwner.open(Path(sys.argv[1]), resolve_config(Path(sys.argv[2]), "test", {}), RequestScope(principal_id="owner")):
        print("OPEN")
except TychoraError as error:
    print(error.code)
"""

    def child():
        return subprocess.run(
            [sys.executable, "-c", code, str(root), str(tmp_path / "workspace.toml")],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    with WorkspaceOwner.open(root, settings, scope) as owner:
        endpoint = root / ".runtime/endpoint.json"
        record = json.loads(endpoint.read_text())
        assert record["workspace_id"] == owner.workspace_id
        assert record["protocol_version"] == 1
        assert len(record["token"]) >= 32
        assert child() == "WORKSPACE_BUSY"
        inode = (root / ".runtime/owner.lock").stat().st_ino
        assert (root / ".runtime").stat().st_mode & 0o777 == 0o700
        assert endpoint.stat().st_mode & 0o777 == 0o600
        assert (root / ".runtime/owner.lock").stat().st_mode & 0o777 == 0o600
    assert not endpoint.exists()
    endpoint.write_text(json.dumps(record))
    os.chmod(endpoint, 0o600)
    with WorkspaceOwner.open(root, settings, scope) as successor:
        replacement = json.loads(endpoint.read_text())
        assert successor.workspace_id == record["workspace_id"]
        assert replacement["token"] != record["token"]
        assert replacement["instance_id"] != record["instance_id"]
        assert (root / ".runtime/owner.lock").stat().st_ino == inode
    assert child() == "OPEN"


def test_failed_initialization_releases_lock(tmp_path, settings, scope):
    root = tmp_path / "data"
    root.mkdir()
    (root / "corpus.sqlite3").write_bytes(b"not a database")
    with pytest.raises(TychoraError), WorkspaceOwner.open(root, settings, scope):
        pytest.fail("Invalid database opened")
    (root / "corpus.sqlite3").unlink()
    with WorkspaceOwner.open(root, settings, scope):
        pass


def test_runtime_symlink_is_rejected_without_touching_target(tmp_path, settings, scope):
    root, outside = tmp_path / "data", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(TychoraError) as exc, WorkspaceOwner.open(root, settings, scope):
        pytest.fail("Unsafe runtime opened")
    assert exc.value.code == "INTEGRITY_ERROR"
    assert not list(outside.iterdir())


def _parse_copy(paths):
    import os
    import time

    data = paths[0].read_bytes()
    (paths[0].parent / "child-started").write_text(str(os.getpid()))
    deadline = time.monotonic() + 8
    while not (paths[0].parent / "release-child").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Synthetic parser was not released")
        time.sleep(0.01)
    return os.getpid(), data, str(paths[0])


@pytest.mark.asyncio
async def test_parser_receives_independent_copies_and_retains_staging_through_cancel(
    tmp_path, settings, scope
):
    from io import BytesIO

    from tychora.adapters.files.cas import LocalArtifacts

    root = tmp_path / "data"
    with WorkspaceOwner.open(root, settings, scope) as owner:
        ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(
                BytesIO(b"synthetic input"), "text/plain"
            ),
            ExecutionContext(),
        )
        other_cas = LocalArtifacts(root.resolve() / "artifacts")
        pending = asyncio.create_task(
            owner.run_parser(_parse_copy, (ref,), ExecutionContext())
        )
        staging = None
        try:
            for _ in range(300):
                matches = list((root / "staging").glob("*/child-started"))
                if matches:
                    staging = matches[0].parent
                    break
                await asyncio.sleep(0.01)
            assert staging is not None
            assert not other_cas.payload_is_pinned(ref.sha256)
            (other_cas.root / ref.storage_key).unlink()
            pending.cancel()
            await asyncio.sleep(0.02)
            assert staging.exists()
        finally:
            if staging is not None:
                (staging / "release-child").touch()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not staging.exists()


@pytest.mark.asyncio
async def test_cancelled_queued_operation_never_enters_call(tmp_path, settings, scope):
    started, release = threading.Event(), threading.Event()
    called = []
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:

        def held():
            started.set()
            assert release.wait(5)

        first = asyncio.create_task(owner.run_blocking(held, ExecutionContext()))
        while not started.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.create_task(
            owner.run_blocking(lambda: called.append(True), ExecutionContext())
        )
        await asyncio.sleep(0.02)
        second.cancel()
        await asyncio.sleep(0.01)
        release.set()
        await first
        with pytest.raises(asyncio.CancelledError):
            await second
        assert called == []


def test_shutdown_drains_before_close_and_releases_lock_last(tmp_path, settings, scope):
    import sqlite3

    root = tmp_path / "data"
    started, release, closing = threading.Event(), threading.Event(), threading.Event()
    observations = []

    def lifetime():
        with WorkspaceOwner.open(root, settings, scope) as owner:
            owner._worker.submit(
                lambda: owner.register_close(
                    lambda: observations.append(
                        owner.db.execute("SELECT 1").fetchone()[0]
                    )
                )
            ).result()

            def held():
                started.set()
                assert release.wait(5)
                observations.append(owner.db.execute("SELECT 2").fetchone()[0])

            owner._worker.submit(held)
            closing.set()
        observations.append(3)
        with pytest.raises(sqlite3.ProgrammingError):
            owner.db.execute("SELECT 1")

    thread = threading.Thread(target=lifetime)
    thread.start()
    try:
        assert started.wait(3) and closing.wait(3)
        time.sleep(0.02)
        assert thread.is_alive()
        assert (root / ".runtime/endpoint.json").exists()
        with (
            pytest.raises(TychoraError) as exc,
            WorkspaceOwner.open(root, settings, scope),
        ):
            pass
        assert exc.value.code == "WORKSPACE_BUSY"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert observations == [2, 1, 3]
    assert not (root / ".runtime/endpoint.json").exists()


@pytest.mark.asyncio
async def test_copy_cancellation_retains_shared_cas_pin(
    tmp_path, settings, scope, monkeypatch
):
    import shutil
    from io import BytesIO

    from tychora.adapters.files.cas import LocalArtifacts

    started, release = threading.Event(), threading.Event()
    original = shutil.copyfileobj

    def held_copy(source, destination, length):
        started.set()
        assert release.wait(5)
        original(source, destination, length)

    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(
                BytesIO(b"synthetic bytes"), "text/plain"
            ),
            ExecutionContext(),
        )
        peer = LocalArtifacts(owner.artifacts.root)
        monkeypatch.setattr(shutil, "copyfileobj", held_copy)
        task = asyncio.create_task(
            owner.run_parser(_parse_copy, (ref,), ExecutionContext())
        )
        try:
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.02)
            assert peer.payload_is_pinned(ref.sha256)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not peer.payload_is_pinned(ref.sha256)
        assert list((owner.path / "staging").iterdir()) == []


@pytest.mark.asyncio
async def test_parser_process_count_is_bounded_and_unknown_staging_is_retained(
    tmp_path, settings, scope
):
    from io import BytesIO

    root = tmp_path / "data"
    with WorkspaceOwner.open(root, settings, scope) as owner:
        unknown = root / "staging/unknown-old-child"
        unknown.mkdir()
        (unknown / "retained-input").write_bytes(b"unknown old input")
        ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(BytesIO(b"bounded"), "text/plain"),
            ExecutionContext(),
        )
        tasks = [
            asyncio.create_task(
                owner.run_parser(_parse_copy, (ref,), ExecutionContext())
            )
            for _ in range(3)
        ]
        seen = set()
        released = set()
        try:
            for _ in range(300):
                active = list((root / "staging").glob("parser-*/child-started"))
                if len(active) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(active) == 2
            await asyncio.sleep(0.05)
            assert len(list((root / "staging").glob("parser-*/child-started"))) == 2
            for _ in range(400):
                for marker in (root / "staging").glob("parser-*/child-started"):
                    if marker in released:
                        continue
                    seen.add(marker.read_text())
                    released.add(marker)
                    (marker.parent / "release-child").touch()
                if all(task.done() for task in tasks):
                    break
                await asyncio.sleep(0.01)
            results = await asyncio.gather(*tasks)
            assert len(seen) == 3
            assert all(result[1] == b"bounded" for result in results)
            assert unknown.exists()
        finally:
            for task in tasks:
                task.cancel()
            for marker in (root / "staging").glob("parser-*/child-started"):
                if marker not in released:
                    (marker.parent / "release-child").touch()
            await asyncio.gather(*tasks, return_exceptions=True)
    with WorkspaceOwner.open(root, settings, scope):
        assert (unknown / "retained-input").read_bytes() == b"unknown old input"


def test_canonical_database_symlink_cannot_create_a_second_owner(
    tmp_path, settings, scope
):
    first, second = tmp_path / "first", tmp_path / "second"
    with WorkspaceOwner.open(first, settings, scope):
        second.mkdir()
        (second / "corpus.sqlite3").symlink_to(first / "corpus.sqlite3")
        with pytest.raises(TychoraError), WorkspaceOwner.open(second, settings, scope):
            pytest.fail("Canonical database alias accepted")


def test_worker_cancellation_exception_does_not_spin_delivery_forever():
    import subprocess
    import sys

    code = """
import asyncio
from tychora.application.runtime import BoundedWorker, drain_result
from tychora.ports.execution import ExecutionContext
async def main():
    worker = BoundedWorker()
    def cancelled():
        raise asyncio.CancelledError
    try:
        await drain_result(worker.submit(cancelled), ExecutionContext())
    except asyncio.CancelledError:
        print('cancelled')
    finally:
        worker.close()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert result.stdout.strip() == "cancelled"


def test_existing_corpus_without_identity_is_not_assigned_a_new_workspace(
    tmp_path, settings, scope
):
    from tychora.adapters.sqlite.connection import open_corpus

    root = tmp_path / "data"
    root.mkdir()
    db = open_corpus(root / "corpus.sqlite3", initialize=True)
    db.execute(
        "INSERT INTO artifacts VALUES (?,1,'text/plain','synthetic','present',NULL,'2024')",
        ("a" * 64,),
    )
    db.close()
    with pytest.raises(TychoraError), WorkspaceOwner.open(root, settings, scope):
        pytest.fail("Existing anonymous corpus accepted")


def _write_parser_output(paths):
    from tychora.application.owner import StagedOutput

    output = paths[0].parent / "result.json"
    output.write_bytes(b'{"retained":true}')
    return (StagedOutput("result.json", "application/json", 17),)


def test_sync_owner_collects_after_child_exit_and_retains_output(
    tmp_path, settings, scope
):
    from io import BytesIO
    from threading import get_ident

    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        ctx = ExecutionContext()
        ref = owner.run_blocking_sync(
            lambda: owner.artifacts.put_stream(BytesIO(b"original"), "text/plain"), ctx
        )

        def collect(outputs):
            assert get_ident() == owner._thread_id
            return owner.artifacts.put_stream(outputs[0][1], outputs[0][0].media_type)

        result = owner.run_parser_sync(
            _write_parser_output, (ref,), ctx, collector=collect
        )
        assert owner.run_blocking_sync(
            lambda: owner.artifacts.stat(result).available, ctx
        )
        assert not list((owner.path / "staging").iterdir())
        with pytest.raises(TychoraError, match="owner thread"):
            owner.run_blocking_sync(
                lambda: owner.run_blocking_sync(lambda: None, ctx), ctx
            )


@pytest.mark.asyncio
async def test_sync_owner_bridge_rejects_event_loop(tmp_path, settings, scope):
    with (
        WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner,
        pytest.raises(TychoraError, match="async"),
    ):
        owner.run_blocking_sync(lambda: None, ExecutionContext())


def test_collector_failure_removes_private_outputs(tmp_path, settings, scope):
    from io import BytesIO

    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        ctx = ExecutionContext()
        ref = owner.run_blocking_sync(
            lambda: owner.artifacts.put_stream(BytesIO(b"original"), "text/plain"), ctx
        )

        def fail(outputs):
            raise ValueError("collector failure")

        with pytest.raises(ValueError, match="collector failure"):
            owner.run_parser_sync(_write_parser_output, (ref,), ctx, collector=fail)
        assert not list((owner.path / "staging").iterdir())


@pytest.mark.asyncio
async def test_cancelled_collector_drains_before_staging_cleanup(
    tmp_path, settings, scope
):
    from io import BytesIO

    started, release = threading.Event(), threading.Event()
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        ctx = ExecutionContext()
        ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(BytesIO(b"original"), "text/plain"), ctx
        )

        def collect(outputs):
            started.set()
            assert release.wait(5)
            return outputs[0][1].read()

        pending = asyncio.create_task(
            owner.run_parser(_write_parser_output, (ref,), ctx, collector=collect)
        )
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            pending.cancel()
            await asyncio.sleep(0.02)
            assert not pending.done()
            assert list((owner.path / "staging").iterdir())
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not list((owner.path / "staging").iterdir())


def _fifo_collection_probe(directory, settings, scope, sender):
    import os

    from tychora.application.owner import StagedOutput

    with WorkspaceOwner.open(directory / "data", settings, scope) as owner:
        staging = owner.path / "staging" / "probe"
        staging.mkdir()
        os.mkfifo(staging / "output", 0o600)

        def unexpected_collection(outputs):
            raise AssertionError("FIFO reached result collector")

        try:
            owner.run_blocking_sync(
                lambda: owner._collect_outputs(
                    staging,
                    (StagedOutput("output", "application/json", 0),),
                    unexpected_collection,
                ),
                ExecutionContext(),
            )
        except TychoraError as exc:
            sender.send((exc.code, exc.message))
        finally:
            sender.close()


def test_fifo_output_is_rejected_without_blocking_owner(tmp_path, settings, scope):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_fifo_collection_probe, args=(tmp_path, settings, scope, sender)
    )
    try:
        child.start()
        sender.close()
        assert receiver.poll(5), (
            "FIFO open blocked the canonical owner before rejection"
        )
        code, message = receiver.recv()
        assert code == "INTEGRITY_ERROR"
        assert message == "Unsafe parser output file"
        child.join(2)
        assert child.exitcode == 0
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()
        receiver.close()
        sender.close()
