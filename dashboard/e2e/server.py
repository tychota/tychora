"""Real HTTP application over a disposable synthetic corpus; no providers or mocks."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import threading
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tychora import Tychora
from tychora.application.acquired_source import AcquiredSource
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import JobHandle
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.segments import OutputTokenCounter

try:
    from dashboard.e2e.qualification import prepare
except ModuleNotFoundError:
    from qualification import prepare


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


class SyntheticIndexCounter:
    """Distinct test tokenizer makes stored windows larger than returned excerpts."""

    model = "synthetic-index-characters-4"
    tokenizer_id = "synthetic-index-characters-4"

    def count(self, text: str) -> int:
        return (len(text) + 3) // 4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tychora-dashboard-e2e-") as temporary:
        workspace = (Path(temporary) / "knowledge").resolve()
        # Initialize the standard workspace before resolving its explicit offline configuration.
        with Tychora.open(workspace, token_counter=SyntheticCounter()):
            pass
        configuration = workspace / "workspace.toml"
        with configuration.open("a") as stream:
            stream.write("\n[configuration]\noffline = true\n")
        fixtures = Path(os.environ.get("TYCHORA_E2E_FIXTURE_DIR", temporary))
        scenario = os.environ.get("TYCHORA_E2E_SCENARIO", "local")
        prepare(scenario, workspace, fixtures)
        config = resolve_config(configuration, "local", {})
        counter = (
            OutputTokenCounter(Path(os.environ["TYCHORA_MODEL_CACHE"]))
            if scenario in {"remote", "ocr"}
            else SyntheticCounter()
        )
        with Tychora.open(workspace, config=config, token_counter=counter) as kb:
            assert kb._workspace.owner.config.values.offline
            if scenario == "local":
                kb._workspace.services.sources.coordinator.counter = (
                    SyntheticIndexCounter()
                )
            gate = StreamGate()
            original_extract = kb._workspace.services.sources._extract
            failed_recovery = set()

            async def transient_extract(registration, capture, ctx, source, scope):
                key = source.root.source_key
                if (
                    key in {"synthetic-recovery", "synthetic-pdf-recovery"}
                    and key not in failed_recovery
                ):
                    failed_recovery.add(key)
                    raise TychoraError(
                        "PROVIDER_FAILURE",
                        "Synthetic transient fixture failure",
                        retryable=True,
                    )
                if key == "synthetic-pdf-partial":
                    raise TychoraError(
                        "DEADLINE_EXCEEDED", "Synthetic partial-conversion timeout"
                    )
                return await original_extract(registration, capture, ctx, source, scope)

            kb._workspace.services.sources._extract = transient_extract
            execute = kb._workspace.services.sources.execute_accepted

            async def execute_after_stream(
                handle: JobHandle,
                source: SourceInput,
                scope: RequestScope,
                *,
                ctx: ExecutionContext | None = None,
                retained_capture_sha: str | None = None,
                acquired: AcquiredSource | None = None,
            ) -> JobHandle:
                if source.root.source_key == "synthetic-browser-manual":
                    await gate.wait_for_stream(handle.id)
                return await execute(
                    handle,
                    source,
                    scope,
                    ctx=ctx,
                    retained_capture_sha=retained_capture_sha,
                    acquired=acquired,
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
