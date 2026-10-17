"""Real loopback owner process and public HTTP client qualification."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tychora import AsyncTychora, Tychora
from tychora.contracts.errors import TychoraError
from tychora.interfaces.http.client import connect_local


def _serve(path: Path):
    script = """
import asyncio
import sys
from pathlib import Path
from tychora.interfaces.http.server import serve
class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"
    def count(self, text):
        return len(text)
asyncio.run(serve(Path(sys.argv[1]), token_counter=Counter()))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    endpoint = path / ".runtime" / "endpoint.json"
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"Server exited early: {stdout} {stderr}")
        if endpoint.exists():
            record = json.loads(endpoint.read_text())
            if record.get("authority"):
                return process, record
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=5)
    raise AssertionError("Server did not publish its endpoint")


@pytest.mark.qualification(loopback=True)
def test_live_local_connect_handshake_and_busy_owner(tmp_path):
    workspace = tmp_path / "knowledge"
    process, record = _serve(workspace)
    try:
        client = connect_local(workspace)
        try:
            kinds = client.discover({"subject": "kinds"})
            assert any(item.id == "manual@1" for item in kinds.definitions)
            direct = Tychora.connect(
                f"http://{record['authority']}", record["token"], record["workspace_id"]
            )
            try:
                assert direct.discover({"subject": "kinds"}).subject == "kinds"
            finally:
                direct.close()
            with pytest.raises(TychoraError) as busy:
                Tychora.open(workspace)
            assert busy.value.code == "WORKSPACE_BUSY"
        finally:
            client.close()
    finally:
        process.terminate()
        process.wait(timeout=5)
    with pytest.raises(TychoraError) as stale:
        connect_local(workspace)
    assert stale.value.code in {"WORKSPACE_BUSY", "INTEGRITY_ERROR"}


@pytest.mark.qualification(loopback=True)
def test_mismatched_workspace_handshake_rejected(tmp_path):
    workspace = tmp_path / "knowledge"
    process, record = _serve(workspace)
    try:
        with pytest.raises(TychoraError) as mismatch:
            Tychora.connect(
                f"http://{record['authority']}",
                record["token"],
                "00000000-0000-4000-8000-000000000099",
            )
        assert mismatch.value.code == "INTEGRITY_ERROR"
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.qualification(loopback=True)
def test_two_public_clients_share_durable_source_job_and_async_discovery(tmp_path):
    workspace = tmp_path / "knowledge"
    process, record = _serve(workspace)
    try:
        first = connect_local(workspace)
        second = Tychora.connect(
            f"http://{record['authority']}", record["token"], record["workspace_id"]
        )
        try:
            source = {
                "type": "text",
                "source_key": "manual",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Synthetic local record.",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
            accepted = first.sources.add(source, idempotency_key="shared")
            same = second.sources.add(source, idempotency_key="shared")
            assert same.id == accepted.id
            status = second.jobs.wait(accepted.id, timeout=5)
            assert status.state == "succeeded", (
                status.error_code,
                list(second.jobs.events(accepted.id)),
            )
            assert first.jobs.get(accepted.id).state == "succeeded"
            result = second.search({"question": "synthetic", "modalities": ["lexical"]})
            assert result.segments
            evidence = first.evidence.get(result.segments[0].matched_unit_ids[0])
            assert evidence.evidence is not None
            assert "Synthetic local record." in evidence.evidence.text

            import asyncio

            async def async_check():
                async with AsyncTychora.connect(
                    f"http://{record['authority']}",
                    record["token"],
                    record["workspace_id"],
                ) as client:
                    assert client.scope.principal_id == "local-owner"
                    assert (await client.discover({"subject": "sources"})).definitions
                    assert (await client.jobs.get(accepted.id)).state == "succeeded"

            asyncio.run(async_check())
        finally:
            first.close()
            second.close()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_server_startup_failure_releases_owner(tmp_path, monkeypatch):
    import asyncio

    import uvicorn

    from tychora.interfaces.http.server import serve

    async def fail_startup(self, *, sockets):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(uvicorn.Server, "serve", fail_startup)
    workspace = tmp_path / "knowledge"
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        asyncio.run(serve(workspace))
    with Tychora.open(workspace) as reopened:
        assert reopened.discover({"subject": "kinds"}).definitions


@pytest.mark.qualification(loopback=True)
def test_cli_dashboard_link_and_startup_link_exchange_against_live_owner(tmp_path):
    import httpx
    from typer.testing import CliRunner

    from tychora.interfaces.cli import app

    workspace = tmp_path / "knowledge"
    process, record = _serve(workspace)
    try:
        result = CliRunner().invoke(
            app, ["--workspace", str(workspace), "manage", "dashboard-link"]
        )
        assert result.exit_code == 0, result.output
        url = result.stdout.strip()
        prefix = f"http://{record['authority']}/dashboard/#launch="
        assert url.startswith(prefix)
        assert record["token"] not in result.output
        origin = f"http://{record['authority']}"
        with httpx.Client(base_url=origin, trust_env=False) as client:
            exchanged = client.post(
                "/v1/session/launch",
                json={"code": url.removeprefix(prefix)},
                headers={"Origin": origin},
            )
            assert exchanged.status_code == 200
            assert exchanged.json()["token"] == record["token"]
    finally:
        process.terminate()
        stdout, _ = process.communicate(timeout=5)
    assert f"http://{record['authority']}/dashboard/#launch=" in stdout
    assert record["token"] not in stdout


def _hold_standalone(path: Path):
    """Open the workspace in a child process through the Python API, no server."""
    script = """
import sys
from pathlib import Path
from tychora import Tychora
class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"
    def count(self, text):
        return len(text)
with Tychora.open(Path(sys.argv[1]), token_counter=Counter()):
    print("ready", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready", process.stderr
    return process


def test_standalone_owner_without_endpoint_is_busy_not_integrity(tmp_path):
    workspace = tmp_path / "workspace"
    process = _hold_standalone(workspace)
    try:
        with pytest.raises(TychoraError) as error:
            connect_local(workspace)
        assert error.value.code == "WORKSPACE_BUSY"
        assert error.value.reason == "owner.live"
        assert error.value.retryable is True
    finally:
        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=30)
