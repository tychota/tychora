"""Real HTTP application over a disposable synthetic corpus; no providers or mocks."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import threading
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.operations import JobHandle
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext


class StreamGate:
    """Hold a real scheduler task until its authenticated SSE response starts."""

    def __init__(self) -> None:
        self._waiting: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    async def wait_for_stream(self, job_id: str, *, timeout: float = 30) -> None:
        subscribed = threading.Event()
        with self._lock:
            self._waiting[job_id] = subscribed
        try:
            if not await asyncio.to_thread(subscribed.wait, timeout):
                raise TimeoutError(
                    "SSE subscription did not start within fixture bound"
                )
        finally:
            with self._lock:
                self._waiting.pop(job_id, None)

    def observe(self, path: str, message: Message) -> None:
        if message["type"] != "http.response.start" or message["status"] != 200:
            return
        if not any(
            name.lower() == b"content-type" and value.startswith(b"text/event-stream")
            for name, value in message.get("headers", [])
        ):
            return
        with self._lock:
            for job_id, subscribed in self._waiting.items():
                if path == f"/v1/jobs/{job_id}/events":
                    subscribed.set()


class ObserveStream:
    """Observe responses only; the ordinary app still authorizes and emits SSE."""

    def __init__(self, app: ASGIApp, gate: StreamGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observed_send(message: Message) -> None:
            await send(message)
            self.gate.observe(scope.get("path", ""), message)

        await self.app(scope, receive, observed_send)


class SyntheticCounter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tychora-dashboard-e2e-") as temporary:
        workspace = Path(temporary) / "knowledge"
        # Initialize the standard workspace before resolving its explicit offline configuration.
        with Tychora.open(workspace, token_counter=SyntheticCounter()):
            pass
        configuration = workspace / "workspace.toml"
        with configuration.open("a") as stream:
            stream.write("\n[configuration]\noffline = true\n")
        config = resolve_config(configuration, "local", {})
        with Tychora.open(
            workspace, config=config, token_counter=SyntheticCounter()
        ) as kb:
            assert kb._workspace.owner.config.values.offline
            gate = StreamGate()
            execute = kb._workspace.services.sources.execute_accepted

            async def execute_after_stream(
                handle: JobHandle,
                source: SourceInput,
                scope: RequestScope,
                *,
                ctx: ExecutionContext | None = None,
                retained_capture_sha: str | None = None,
            ) -> JobHandle:
                if source.root.source_key == "synthetic-browser-manual":
                    await gate.wait_for_stream(handle.id)
                return await execute(
                    handle,
                    source,
                    scope,
                    ctx=ctx,
                    retained_capture_sha=retained_capture_sha,
                )

            kb._workspace.services.sources.execute_accepted = execute_after_stream
            reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
            app = create_app(
                kb._workspace.owner,
                kb._workspace.services,
                LocalAuth(
                    {
                        "synthetic-dashboard-owner": kb.scope,
                        "synthetic-dashboard-reader": reader,
                    },
                    authority=f"127.0.0.1:{args.port}",
                    managed_owner_token="synthetic-dashboard-owner",
                ),
            )
            app.add_middleware(ObserveStream, gate=gate)
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
            )


if __name__ == "__main__":
    main()
