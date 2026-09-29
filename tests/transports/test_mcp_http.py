"""Real loopback Streamable HTTP MCP protocol qualification."""

import asyncio
import json
import os
import socket
import sys
import time
from io import BytesIO
from typing import Annotated

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from pydantic import BaseModel

from tychora import Tychora
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import InspectRequest
from tychora.contracts.requests import RequestScope
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth
from tychora.interfaces.mcp.tools import _result, _serialized_structured
from tychora.ports.execution import ExecutionContext


class RecordingStream(httpx2.AsyncByteStream):
    def __init__(self, source, chunks):
        self.source = source
        self.chunks = chunks

    async def __aiter__(self):
        async for chunk in self.source:
            self.chunks.append(chunk)
            yield chunk

    async def aclose(self):
        await self.source.aclose()


class RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(self):
        self.source = httpx2.AsyncHTTPTransport()
        self.records = []

    async def handle_async_request(self, request):
        response = await self.source.handle_async_request(request)
        chunks = []
        self.records.append((request, chunks))
        response.stream = RecordingStream(response.stream, chunks)
        return response

    async def aclose(self):
        await self.source.aclose()


def _wire_structured(records, expected):
    for _, chunks in records:
        body = b"".join(chunks).decode("utf-8")
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            message = json.loads(line[6:])
            if message.get("result", {}).get("structuredContent") != expected:
                continue
            marker = '"structuredContent":'
            start = line.find(marker)
            assert start >= 0, line
            value, end = json.JSONDecoder().raw_decode(line[start + len(marker) :])
            return value, line[start + len(marker) :][:end].encode("utf-8")
    raise AssertionError("No structured MCP result on HTTP wire")


class WireMetrics(BaseModel):
    schema_version: str = "1"
    name: str
    runtime_ms: float
    score: float


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_mounted_http_reader_auth_and_sdk_client(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        reader = RequestScope(
            principal_id=owner.scope.principal_id,
            collection_ids=owner.scope.collection_ids,
            access_ceiling=owner.scope.access_ceiling,
            uses=owner.scope.uses,
            capabilities=frozenset({"read"}),
        )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(32)
            sock.setblocking(False)
            authority = f"127.0.0.1:{sock.getsockname()[1]}"
            app = create_app(
                owner,
                kb._workspace.services,
                LocalAuth(
                    {
                        "reader": reader,
                        "writer": owner.scope,
                        "narrow": reader.model_copy(
                            update={"collection_ids": frozenset()}
                        ),
                    },
                    authority=authority,
                ),
            )
            entered = asyncio.Event()
            release = asyncio.Event()

            @app.state.mcp_server.tool(name="slow_read")
            async def slow_read() -> str:
                entered.set()
                await release.wait()
                return "released"

            @app.state.mcp_server.tool(name="wire_metrics")
            async def wire_metrics() -> Annotated[CallToolResult, WireMetrics]:
                return _result(
                    WireMetrics(name="Café π", runtime_ms=1.2e-7, score=0.1234)
                )

            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=sock.getsockname()[1],
                    log_level="error",
                    lifespan="on",
                )
            )
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.02)
                assert server.started
                url = f"http://{authority}/mcp/"
                async with httpx2.AsyncClient(
                    headers={"Authorization": "Bearer reader"}, timeout=5
                ) as http:
                    async with Client(
                        streamable_http_client(url, http_client=http), mode="legacy"
                    ) as client:
                        tools = await client.list_tools()
                        assert {"discover", "search", "inspect"} <= {
                            t.name for t in tools.tools
                        }
                        assert "add_sources" not in {t.name for t in tools.tools}
                        malformed = await client.call_tool(
                            "discover", {"request": {"subject": "secret-example"}}
                        )
                        assert malformed.is_error
                        assert (
                            json.loads(malformed.content[0].text)["code"]
                            == "INVALID_REQUEST"
                        )
                        assert "secret-example" not in malformed.content[0].text
                        result = await client.call_tool(
                            "discover", {"request": {"subject": "kinds"}}
                        )
                        assert result.structured_content["subject"] == "kinds"
                        denied_write = await client.call_tool(
                            "add_sources", {"action": "add", "source": {}}
                        )
                        assert denied_write.is_error
                        assert "FORBIDDEN_SCOPE" in denied_write.content[0].text
                        async with httpx2.AsyncClient(
                            headers={"Authorization": "Bearer writer"}, timeout=5
                        ) as manager:
                            activated = await manager.put(
                                f"http://{authority}/v1/settings",
                                json={
                                    "profile": "local",
                                    "workspace_toml": (
                                        tmp_path / "knowledge" / "workspace.toml"
                                    ).read_text(),
                                },
                            )
                            assert activated.status_code == 200, activated.text
                        assert (
                            await client.call_tool(
                                "discover", {"request": {"subject": "kinds"}}
                            )
                        ).structured_content["subject"] == "kinds"
                    recording = RecordingTransport()
                    async with (
                        httpx2.AsyncClient(
                            headers={"Authorization": "Bearer writer"},
                            timeout=5,
                            transport=recording,
                        ) as writing_http,
                        Client(
                            streamable_http_client(url, http_client=writing_http),
                            mode="legacy",
                        ) as writer,
                    ):
                        writer_tools = await writer.list_tools()
                        add_schema = next(
                            tool.input_schema
                            for tool in writer_tools.tools
                            if tool.name == "add_sources"
                        )
                        source_schema = add_schema["properties"]["source"]
                        assert {"$ref": "#/$defs/SourceVariant"} in source_schema[
                            "anyOf"
                        ]
                        assert {"source_key", "collection_id", "kind", "text"} <= set(
                            add_schema["$defs"]["TextSource"]["required"]
                        )
                        assert {"const": "default", "type": "string"} in add_schema[
                            "$defs"
                        ]["TextSource"]["properties"]["collection_id"]["anyOf"]
                        malformed_action = await writer.call_tool(
                            "add_sources", {"action": "secret-example"}
                        )
                        assert malformed_action.is_error
                        assert (
                            json.loads(malformed_action.content[0].text)["code"]
                            == "INVALID_REQUEST"
                        )
                        assert "secret-example" not in malformed_action.content[0].text
                        accepted = await writer.call_tool(
                            "add_sources",
                            {
                                "action": "add",
                                "source": {
                                    "type": "text",
                                    "source_key": "http-mcp",
                                    "collection_id": "default",
                                    "kind": "manual@1",
                                    "format": "text",
                                    "text": "Example Café 3.14.",
                                    "metadata": {
                                        "title": "Example",
                                        "publisher": "Example",
                                        "edition": "1",
                                    },
                                },
                            },
                        )
                        assert not accepted.is_error, accepted.content
                        assert (
                            kb.jobs.wait(
                                accepted.structured_content["id"], timeout=5
                            ).state
                            == "succeeded"
                        )
                        status = await writer.call_tool(
                            "manage_job",
                            {
                                "action": "get",
                                "job_id": accepted.structured_content["id"],
                            },
                        )
                        assert (
                            status.structured_content["result"]["state"] == "succeeded"
                        )
                        listed = await writer.call_tool(
                            "manage_job", {"action": "list", "limit": 10}
                        )
                        assert not listed.is_error, listed.content
                        assert any(
                            row["id"] == accepted.structured_content["id"]
                            for row in listed.structured_content["result"]["jobs"]
                        )
                        sources = await writer.call_tool(
                            "discover", {"request": {"subject": "sources"}}
                        )
                        source_id = sources.structured_content["definitions"][0]["id"]
                        coverage = await writer.call_tool(
                            "inspect",
                            {
                                "request": {
                                    "subject": "source_revisions",
                                    "source_id": source_id,
                                    "limit": 1,
                                }
                            },
                        )
                        assert not coverage.is_error, coverage.content
                        assert (
                            coverage.structured_content["result"]["source_id"]
                            == source_id
                        )
                        found = await writer.call_tool(
                            "search",
                            {
                                "request": {
                                    "question": "Café",
                                    "modalities": ["lexical"],
                                }
                            },
                        )
                        assert not found.is_error, found.content
                        wire_value, wire_bytes = _wire_structured(
                            recording.records, found.structured_content
                        )
                        assert wire_value == found.structured_content
                        assert (
                            found.structured_content["budget_contract"][
                                "render_targets"
                            ][0]["serialization"]
                            == "mcp-structured-json@2.2.0"
                        )
                        assert wire_bytes == _serialized_structured(found)
                        assert b"Caf\xc3\xa9" in wire_bytes
                        assert (
                            len(wire_bytes)
                            <= found.structured_content["budget_contract"][
                                "max_response_bytes"
                            ]
                        )
                        assert (
                            len(wire_bytes.decode())
                            <= found.structured_content["budget_contract"]["max_tokens"]
                        )
                        probe = await writer.call_tool("wire_metrics", {})
                        assert not probe.content
                        _, numeric_wire = _wire_structured(
                            recording.records, probe.structured_content
                        )
                        assert numeric_wire == _serialized_structured(probe)
                        assert b"Caf\xc3\xa9 \xcf\x80" in numeric_wire
                        assert b"1.2e-7" in numeric_wire
                        assert numeric_wire != canonical_bytes(probe.structured_content)
                        tiny = await writer.call_tool(
                            "search",
                            {
                                "request": {
                                    "question": "Café",
                                    "modalities": ["lexical"],
                                    "budgets": {"max_tokens": 1},
                                }
                            },
                        )
                        assert tiny.is_error
                        assert "BUDGET_TOO_SMALL" in tiny.content[0].text
                        http_query = await writing_http.post(
                            f"http://{authority}/v1/queries",
                            json={"question": "Café", "modalities": ["lexical"]},
                        )
                        assert http_query.status_code == 200, http_query.text
                        assert (
                            http_query.json()["budget_contract"]["render_targets"][0][
                                "serialization"
                            ]
                            == "canonical-json-utf8"
                        )
                        assert http_query.content == canonical_bytes(http_query.json())
                        app.state.runtime.admit(activation=True)
                        try:
                            busy = await writer.call_tool(
                                "discover", {"request": {"subject": "kinds"}}
                            )
                            assert (
                                busy.is_error
                                and "WORKSPACE_BUSY" in busy.content[0].text
                            )
                        finally:
                            app.state.runtime.release(activation=True)
                        active = asyncio.create_task(writer.call_tool("slow_read", {}))
                        await asyncio.wait_for(entered.wait(), 5)
                        try:
                            denied_activation = await writing_http.put(
                                f"http://{authority}/v1/settings",
                                json={
                                    "profile": "local",
                                    "workspace_toml": (
                                        tmp_path / "knowledge" / "workspace.toml"
                                    ).read_text(),
                                },
                            )
                            assert denied_activation.status_code == 409
                            assert denied_activation.json()["code"] == "WORKSPACE_BUSY"
                        finally:
                            release.set()
                        assert not (await asyncio.wait_for(active, 5)).is_error
                        revision_id = coverage.structured_content["result"][
                            "revisions"
                        ][0]["revision_id"]
                        original = await writer.call_tool(
                            "inspect",
                            {
                                "request": {
                                    "subject": "revisions",
                                    "id": revision_id,
                                    "original": True,
                                }
                            },
                        )
                        assert not original.is_error, original.content

                        def enlarge_original():
                            payload = b"x" * (4 * 1024 * 1024 + 256)
                            ref = owner.artifacts.put_stream(
                                BytesIO(payload), "text/plain"
                            )
                            owner.db.execute(
                                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) "
                                "VALUES (?,?,?,?,?)",
                                (
                                    ref.sha256,
                                    ref.size_bytes,
                                    ref.media_type,
                                    ref.storage_key,
                                    "2026-01-01T00:00:00Z",
                                ),
                            )
                            old_sha = owner.db.execute(
                                "SELECT original_sha FROM revisions WHERE revision_id=?",
                                (revision_id,),
                            ).fetchone()[0]
                            owner.db.execute(
                                "UPDATE revisions SET original_sha=? WHERE revision_id=?",
                                (ref.sha256, revision_id),
                            )
                            owner.db.execute(
                                "UPDATE revision_artifacts SET artifact_sha=? "
                                "WHERE revision_id=? AND role='original'",
                                (ref.sha256, revision_id),
                            )
                            owner.db.execute(
                                "UPDATE locators SET artifact_sha=?, "
                                "details_json=json_set(details_json,'$.artifact_ref',?) "
                                "WHERE artifact_sha=?",
                                (ref.sha256, ref.sha256, old_sha),
                            )

                        await owner.run_blocking(enlarge_original, ExecutionContext())
                        direct_original = (
                            await kb._workspace.services.inspection.inspect(
                                InspectRequest(
                                    subject="revisions", id=revision_id, original=True
                                ),
                                owner.scope,
                                ExecutionContext(),
                            )
                        )
                        assert len(direct_original.payload) > 4 * 1024 * 1024
                        assert (
                            len(
                                _result(direct_original, wrapped=True)
                                .model_dump_json(by_alias=True, exclude_unset=True)
                                .encode("utf-8")
                            )
                            > 4 * 1024 * 1024
                        )
                        oversized_original = await writer.call_tool(
                            "inspect",
                            {
                                "request": {
                                    "subject": "revisions",
                                    "id": revision_id,
                                    "original": True,
                                }
                            },
                        )
                        assert oversized_original.is_error
                        assert (
                            json.loads(oversized_original.content[0].text)["code"]
                            == "OUTPUT_BUDGET_EXCEEDED"
                        ), oversized_original.content[0].text
                    async with Client(
                        streamable_http_client(url, http_client=http), mode="legacy"
                    ) as reconnected:
                        assert (
                            await reconnected.call_tool(
                                "discover", {"request": {"subject": "sources"}}
                            )
                        ).structured_content["definitions"]
                    async with (
                        httpx2.AsyncClient(
                            headers={"Authorization": "Bearer narrow"}, timeout=5
                        ) as narrow_http,
                        Client(
                            streamable_http_client(url, http_client=narrow_http),
                            mode="legacy",
                        ) as narrowed,
                    ):
                        hidden = await narrowed.call_tool(
                            "inspect",
                            {
                                "request": {
                                    "subject": "source_revisions",
                                    "source_id": source_id,
                                }
                            },
                        )
                        assert hidden.is_error and "NOT_FOUND" in hidden.content[0].text
                async with httpx2.AsyncClient(timeout=5) as anonymous:
                    denied = await anonymous.post(
                        url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                    )
                    assert denied.status_code == 403
                    bad_origin = await anonymous.post(
                        url,
                        headers={
                            "Authorization": "Bearer reader",
                            "Origin": "http://example.invalid",
                        },
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    )
                    assert bad_origin.status_code == 403
                    oversized = await anonymous.post(
                        url,
                        headers={"Authorization": "Bearer reader"},
                        content=b"x" * (1024 * 1024 + 1),
                    )
                    assert oversized.status_code == 400
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)


def test_installed_sdk_serializer_exponent_and_unicode_fragment():
    from mcp.types import CallToolResult

    block = CallToolResult(
        content=[],
        structured_content={
            "name": "Café π",
            "runtime_ms": 1.2e-7,
            "score": 0.1234,
        },
    )
    fragment = _serialized_structured(block)
    assert b"Caf\xc3\xa9 \xcf\x80" in fragment
    assert b"1.2e-7" in fragment
    assert fragment != canonical_bytes(block.structured_content)


@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_streamable_http_restarts_with_fresh_owner_session_manager(tmp_path):
    workspace = tmp_path / "knowledge"
    script = """
import asyncio
import sys
from pathlib import Path
from tychora.interfaces.http.server import serve
class Counter:
    model = 'synthetic-output-test-counter'
    tokenizer_id = 'cl100k_base'
    def count(self, text): return len(text)
asyncio.run(serve(Path(sys.argv[1]), token_counter=Counter()))
"""
    previous_instance = None
    for _ in range(2):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(workspace),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            endpoint = workspace / ".runtime" / "endpoint.json"
            deadline = time.monotonic() + 10
            record = None
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    assert process.stderr is not None
                    raise AssertionError(
                        f"HTTP owner exited early: {(await process.stderr.read()).decode()}"
                    )
                if endpoint.exists():
                    candidate = json.loads(endpoint.read_text())
                    if (
                        candidate.get("authority")
                        and candidate.get("instance_id") != previous_instance
                    ):
                        record = candidate
                        break
                await asyncio.sleep(0.05)
            assert record is not None, "HTTP owner did not publish a fresh endpoint"
            previous_instance = record["instance_id"]
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {record['token']}"},
                timeout=5,
            ) as http:
                async with asyncio.timeout(10):
                    async with Client(
                        streamable_http_client(
                            f"http://{record['authority']}/mcp/",
                            http_client=http,
                        ),
                        mode="legacy",
                    ) as client:
                        result = await client.call_tool(
                            "discover", {"request": {"subject": "kinds"}}
                        )
                        assert result.structured_content["subject"] == "kinds"
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
