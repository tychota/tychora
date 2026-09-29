"""Bounded thread ownership with cancellation-safe actual-exit draining."""

import asyncio
import fcntl
import json
import multiprocessing
import os
import secrets
import stat
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Lock
from typing import cast
from uuid import uuid4

from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


class BoundedWorker:
    """A finite submission queue in front of a fixed-size thread pool."""

    def __init__(self, workers: int = 1, queued: int = 32) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="tychora"
        )
        self._bound = workers + queued
        self._pending = 0
        self._closed = False
        self._lock = Lock()

    def submit[T](self, call: Callable[[], T]) -> Future[T]:
        with self._lock:
            if self._closed or self._pending >= self._bound:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Workspace queue is full or closing; retry shortly",
                    retryable=True,
                )
            self._pending += 1
            try:
                future = self._pool.submit(call)
            except BaseException:
                self._pending -= 1
                raise
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future: Future) -> None:
        with self._lock:
            self._pending -= 1

    def stop_admission(self) -> None:
        with self._lock:
            self._closed = True

    def close(self, cleanup: Callable[[], None] | None = None) -> None:
        # Internal cleanup goes behind all accepted work, bypassing admission.
        with self._lock:
            self._closed = True
            final = self._pool.submit(cleanup) if cleanup is not None else None
        try:
            if final is not None:
                final.result()
        finally:
            self._pool.shutdown(wait=True)


async def drain_result[T](future: Future[T], ctx: ExecutionContext) -> T:
    """Client cancellation cannot cancel the submitted worker or its cleanup."""
    delivery = asyncio.wrap_future(future)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(delivery)
            break
        except asyncio.CancelledError:
            cancelled = True
            ctx.cancelled.set()
            if delivery.done():
                # A worker can itself raise CancelledError. Reawaiting that
                # completed future would otherwise spin without yielding.
                try:
                    delivery.result()
                finally:
                    raise asyncio.CancelledError from None
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    ctx.checkpoint()
    return result


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise TychoraError("INTEGRITY_ERROR", "Runtime directory has another owner")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


class RuntimeIdentity:
    """A lock and secret coordination record; this is not a live handshake."""

    def __init__(self, root: Path) -> None:
        self.directory = root / ".runtime"
        self._fd: int | None = None
        self._published = False

    def acquire(self) -> None:
        try:
            private_directory(self.directory)
            fd = os.open(
                self.directory / "owner.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                ):
                    raise TychoraError("INTEGRITY_ERROR", "Unsafe workspace lock")
                os.fchmod(fd, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise TychoraError(
                        "WORKSPACE_BUSY",
                        "Workspace already has an owner; retry when it exits",
                        retryable=True,
                    ) from None
                self._fd = fd
            except BaseException:
                os.close(fd)
                raise
        except OSError:
            raise TychoraError(
                "INTEGRITY_ERROR", "Runtime path is unavailable or unsafe"
            ) from None

    def publish(self, workspace_id: str) -> None:
        self.instance_id = str(uuid4())
        record = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "protocol_version": 1,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "token": secrets.token_urlsafe(32),
        }
        self.record = record
        self._write_record(record)

    def publish_endpoint(self, authority: str) -> None:
        """Publish the bound loopback authority after the socket is reserved."""
        if not self._published or not hasattr(self, "record"):
            raise TychoraError("INTEGRITY_ERROR", "Owner runtime is unpublished")
        record = {**self.record, "authority": authority}
        self._write_record(record)
        self.record = record

    def _write_record(self, record: dict) -> None:
        temporary = self.directory / (".endpoint-" + self.instance_id)
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / "endpoint.json")
            self._published = True
        finally:
            temporary.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if self._published:
                (self.directory / "endpoint.json").unlink(missing_ok=True)
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None


def _parser_entry(call: Callable, paths: tuple[Path, ...], output: Connection) -> None:
    try:
        output.send((True, call(paths)))
    except Exception:  # noqa: BLE001 - sanitize arbitrary parser failures at the process boundary
        output.send((False, None))
    finally:
        output.close()


def parser_process[T](
    call: Callable[[tuple[Path, ...]], T], paths: tuple[Path, ...]
) -> T:
    """Spawn with only independent copies; always reap before removing staging."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_parser_entry, args=(call, paths, sender))
    try:
        child.start()
        sender.close()
        try:
            succeeded, result = receiver.recv()
        except EOFError:
            succeeded, result = False, None
        child.join()
        if not succeeded or child.exitcode != 0:
            raise TychoraError("INVALID_REQUEST", "Parser process failed")
        return cast(T, result)
    finally:
        if child.pid is not None:
            # Unexpected supervisor failure fences a child before staged cleanup.
            if child.is_alive():
                child.terminate()
            child.join()
            child.close()
        receiver.close()
        sender.close()


def close_owned_runtimes(runtimes: tuple) -> bool:
    """Attempt every owned close without replacing a committed or primary result."""
    succeeded = True
    for runtime in runtimes:
        try:
            runtime.close()
        except BaseException:  # noqa: BLE001 - cleanup cannot hide the primary result
            succeeded = False
    return succeeded


class ServiceRuntime:
    """One current service binding and shared local-operation activation gate."""

    def __init__(self, owner, services) -> None:
        self.owner = owner
        self.services = services
        self._gate = Lock()
        self._active = 0
        self._activating = False
        self.cleanup_diagnostics: tuple[str, ...] = ()
        self._retired_schedulers: list = []
        self._workspace = None

    def attach_workspace(self, workspace) -> None:
        self._workspace = workspace

    @contextmanager
    def operation(self):
        """Pin one current binding for a Python or embedded transport call."""
        self.admit()
        try:
            yield self.services
        finally:
            self.release()

    @property
    def retired_count(self) -> int:
        with self._gate:
            return len(self._retired_schedulers)

    def record_cleanup_failure(self, scheduler=None) -> None:
        """Retain only bounded, public-safe post-commit cleanup information."""
        with self._gate:
            self.cleanup_diagnostics = (
                *self.cleanup_diagnostics[-15:],
                "previous source scheduler cleanup failed",
            )
            if scheduler is not None and scheduler not in self._retired_schedulers:
                self._retired_schedulers.append(scheduler)

    def record_provider_cleanup_failure(self) -> None:
        """Keep a bounded, sanitized diagnostic for failed owned-provider cleanup."""
        with self._gate:
            self.cleanup_diagnostics = (
                *self.cleanup_diagnostics[-15:],
                "owned provider runtime cleanup failed",
            )

    def close_retired_sync(self) -> None:
        """Retry owned old bridges before the final owner release."""
        with self._gate:
            schedulers = tuple(self._retired_schedulers)
        for scheduler in schedulers:
            try:
                scheduler.close()
            except Exception:  # noqa: BLE001 - retained for the next retry/host teardown
                self.record_cleanup_failure()
            else:
                with self._gate:
                    if scheduler in self._retired_schedulers:
                        self._retired_schedulers.remove(scheduler)

    async def aclose_retired(self) -> None:
        """Retry old bridges without blocking the host event loop."""
        await asyncio.to_thread(self.close_retired_sync)

    def admit(self, *, activation: bool = False) -> None:
        with self._gate:
            if self._activating or (activation and self._active):
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Workspace configuration is busy; retry shortly.",
                    retryable=True,
                )
            if activation:
                self._activating = True
            else:
                self._active += 1

    def release(self, *, activation: bool = False) -> None:
        with self._gate:
            if activation:
                self._activating = False
            else:
                self._active -= 1

    def swap(self, services) -> None:
        with self._gate:
            if not self._activating:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Settings swap requires exclusive admission."
                )
            self.services = services
            if self._workspace is not None:
                self._workspace.services = services
                self._workspace.bridge = services.source_jobs.bridge
                self._workspace.config = self.owner.config
