"""Writer priorities, queue bounds and the CPU lane of the workspace owner."""

import ast
import asyncio
import threading
import time
from pathlib import Path

import pytest

from tychora.application.owner import WorkspaceOwner
from tychora.application.runtime import (
    BACKGROUND_BOUND,
    CPU_THREAD_PREFIX,
    INTERACTIVE_BOUND,
    WRITER_THREAD_PREFIX,
    BoundedWorker,
    PriorityWorker,
    drain_result,
)
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


def _hold(worker: PriorityWorker):
    started, release = threading.Event(), threading.Event()

    def held():
        started.set()
        assert release.wait(5)

    future = worker.submit(held, "background")
    assert started.wait(1)
    return future, release


def test_interactive_task_runs_before_queued_background_tasks():
    worker = PriorityWorker()
    order: list[str] = []
    held, release = _hold(worker)
    try:
        background = [
            worker.submit(lambda index=index: order.append(f"b{index}"), "background")
            for index in range(3)
        ]
        interactive = worker.submit(lambda: order.append("i"), "interactive")
    finally:
        release.set()
    held.result(5)
    interactive.result(5)
    for future in background:
        future.result(5)
    worker.close()
    assert order == ["i", "b0", "b1", "b2"]


@pytest.mark.parametrize(
    ("priority", "bound"),
    [("interactive", INTERACTIVE_BOUND), ("background", BACKGROUND_BOUND)],
)
def test_queue_bounds_raise_queue_full(priority, bound):
    worker = PriorityWorker()
    held, release = _hold(worker)
    queued = []
    try:
        queued = [worker.submit(lambda: 1, priority) for _ in range(bound)]
        with pytest.raises(TychoraError) as error:
            worker.submit(lambda: 2, priority)
        assert (error.value.code, error.value.reason) == (
            "WORKSPACE_BUSY",
            "queue.full",
        )
        assert error.value.retryable
        other = "background" if priority == "interactive" else "interactive"
        queued.append(worker.submit(lambda: 1, other))
    finally:
        release.set()
    held.result(5)
    assert [future.result(5) for future in queued] == [1] * len(queued)
    worker.close()


@pytest.mark.asyncio
async def test_cpu_lane_runs_two_tasks_concurrently(tmp_path, settings):
    barrier = threading.Barrier(2, timeout=5)

    def meet():
        barrier.wait()
        return threading.current_thread().name

    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as owner:
        ctx = ExecutionContext()
        names = await asyncio.gather(owner.run_cpu(meet, ctx), owner.run_cpu(meet, ctx))
    assert all(name.startswith(CPU_THREAD_PREFIX) for name in names)


@pytest.mark.asyncio
async def test_writes_run_on_the_writer_thread(tmp_path, settings):
    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as owner:
        ctx = ExecutionContext()
        names = {
            await owner.run_write(lambda: threading.current_thread().name, ctx),
            await owner.run_write(
                lambda: threading.current_thread().name, ctx, "interactive"
            ),
        }
    assert len(names) == 1
    assert names.pop().startswith(WRITER_THREAD_PREFIX)


def test_native_handles_stay_on_writer_thread():
    """The query service never hands Chroma or Ladybug work to an unowned thread."""
    tree = ast.parse((SOURCE / "application/query.py").read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"to_thread", "run_in_executor"}
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module == "asyncio"
            and any(alias.name == "to_thread" for alias in node.names)
        )
    ]
    assert offenders == []


def test_run_blocking_is_gone():
    hits = [
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*.py")
        if "run_" + "blocking" in path.read_text()
    ]
    assert hits == []


@pytest.mark.asyncio
async def test_drain_result_delivers_a_priority_future():
    worker = PriorityWorker()
    try:
        assert (
            await drain_result(
                worker.submit(lambda: 7, "interactive"), ExecutionContext()
            )
            == 7
        )
    finally:
        worker.close()


@pytest.mark.parametrize(
    "make", [PriorityWorker, lambda: BoundedWorker(workers=1, queued=1)]
)
def test_closed_lane_refuses_as_closing_not_queue_full(make):
    worker = make()
    worker.close()
    with pytest.raises(TychoraError) as error:
        worker.submit(lambda: 1)
    assert error.value.code == "WORKSPACE_BUSY"
    assert error.value.retryable
    assert error.value.reason != "queue.full"
    assert error.value.message == "Workspace is closing; retry shortly"


def test_close_runs_every_queued_call_before_cleanup():
    worker = PriorityWorker()
    order: list[str] = []
    held, release = _hold(worker)
    queued = [
        worker.submit(lambda: order.append("b0"), "background"),
        worker.submit(lambda: order.append("b1"), "background"),
        worker.submit(lambda: order.append("i0"), "interactive"),
    ]
    closer = threading.Thread(
        target=worker.close, args=(lambda: order.append("cleanup"),)
    )
    closer.start()
    deadline = time.monotonic() + 5
    while not worker._closed:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    release.set()
    closer.join(5)
    assert not closer.is_alive()
    held.result(5)
    assert all(future.done() for future in queued)
    assert order == ["i0", "b0", "b1", "cleanup"]


def test_failing_call_delivers_its_exception_and_the_worker_continues():
    worker = PriorityWorker()
    try:
        failure = ValueError("boom")

        def fail():
            raise failure

        failed = worker.submit(fail, "interactive")
        assert failed.exception(5) is failure
        assert worker.submit(lambda: 3).result(5) == 3
    finally:
        worker.close()


def test_cancelled_queued_future_is_skipped_and_the_next_call_runs():
    worker = PriorityWorker()
    order: list[str] = []
    held, release = _hold(worker)
    try:
        skipped = worker.submit(lambda: order.append("skipped"), "background")
        following = worker.submit(lambda: order.append("next"), "background")
        assert skipped.cancel()
    finally:
        release.set()
    held.result(5)
    following.result(5)
    worker.close()
    assert skipped.cancelled()
    assert order == ["next"]
