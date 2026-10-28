"""One off-loop bridge for owner-aware service coroutines.

The bridge admits every caller while it is open. Work is bounded where it runs:
the owner's lanes refuse beyond their queue bounds with ``WORKSPACE_BUSY``
reason ``queue.full``, so a smaller cap here would refuse callers below them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import Future, wait
from threading import Event, Lock, Thread, current_thread
from typing import TypeVar

from tychora.application.runtime import workspace_closing
from tychora.ports.execution import ExecutionContext

T = TypeVar("T")


class ClientBridge:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = Event()
        self._closed = False
        self._pending: set[Future] = set()
        self._lock = Lock()
        self._thread = Thread(target=self._serve, name="tychora-client", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    def submit(self, operation: Coroutine[object, object, T]) -> Future[T]:
        with self._lock:
            if self._closed:
                operation.close()
                raise workspace_closing()
            try:
                future = asyncio.run_coroutine_threadsafe(operation, self.loop)
            except BaseException:
                operation.close()
                raise
            self._pending.add(future)

        def finished(done: Future) -> None:
            with self._lock:
                self._pending.discard(done)

        future.add_done_callback(finished)
        return future

    def call(self, operation: Coroutine[object, object, T]) -> T:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self.loop:
            operation.close()
            raise RuntimeError("A blocking call cannot run on the owned service loop")
        return self.submit(operation).result()

    async def acall(
        self,
        operation: Coroutine[object, object, T],
        ctx: ExecutionContext | None = None,
    ) -> T:
        future = self.submit(operation)
        wrapped = asyncio.wrap_future(future)
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(wrapped)
                if cancelled:
                    raise asyncio.CancelledError
                return value
            except asyncio.CancelledError:
                if cancelled and wrapped.done():
                    raise
                cancelled = True
                if ctx is not None:
                    ctx.cancelled.set()
                if wrapped.done():
                    raise
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise

    def close(self) -> None:
        if current_thread() is self._thread:
            raise RuntimeError("Owned service loop cannot close itself")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending)
        if pending:
            wait(pending)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
