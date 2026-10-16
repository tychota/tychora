"""Real subprocess MCP protocol qualification."""

import asyncio
import base64
import os
import sys
from io import BytesIO

import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent, TextResourceContents
from PIL import Image

from tests.helpers.assets import asset_path
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.requests import RequestScope
from tychora.interfaces.mcp.tools import _serialized_structured
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def _text_content(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_stdio_sdk_reads_authorized_binary_asset_resource(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    picture = tmp_path / "synthetic.png"
    out = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(out, format="PNG")
    picture.write_bytes(out.getvalue())
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    settings = workspace / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n'
        '[configuration.extraction]\nparser="docling"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    with Tychora.open(
        workspace,
        config=resolve_config(settings, "test", {}),
        token_counter=Counter(),
    ) as kb:
        accepted = kb.sources.add(
            {
                "type": "file",
                "path": str(picture),
                "format": "png",
                "source_key": "synthetic.png",
                "collection_id": "default",
                "kind": "manual@1",
                "metadata": {
                    "title": "Synthetic image",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(accepted.id, timeout=20)
        assert status.state == "succeeded", status.error_code

        def retained_asset():
            row = kb._workspace.owner.db.execute(
                "SELECT x.asset_id,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM assets x JOIN artifacts a ON a.artifact_sha=x.artifact_sha"
            ).fetchone()
            ref = ArtifactRef(
                sha256=row[1],
                size_bytes=row[2],
                media_type=row[3],
                storage_key=row[4],
            )
            with kb._workspace.owner.artifacts.open_verified(ref) as stream:
                return row[0], stream.read()

        asset_id, asset_bytes = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                retained_asset,
                ExecutionContext(),
            )
        )
        principal = kb._workspace.owner.scope
        reader = principal.model_copy(update={"capabilities": frozenset({"read"})})
    script = """
import asyncio
import sys
from pathlib import Path
from tychora.contracts.requests import RequestScope
from tychora.application.inspection import InspectionService
from tychora.interfaces.mcp.server import serve_stdio
async def main():
    if len(sys.argv) > 3 and sys.argv[3] == 'oversize':
        InspectionService._artifact = lambda self, sha, *, original: (b'x' * (16 * 1024 * 1024 + 1), 'application/octet-stream')
    await serve_stdio(Path(sys.argv[1]), principal=RequestScope.model_validate_json(sys.argv[2]))
asyncio.run(main())
"""

    async def read_as(scope, *, denied=False, oversized=False):
        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                script,
                str(workspace),
                scope.model_dump_json(),
                *(["oversize"] if oversized else []),
            ],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        async with Client(process, mode="legacy") as client:
            if denied:
                with pytest.raises(Exception) as failure:
                    await client.read_resource(f"tychora://assets/{asset_id}")
                assert ("BUDGET_TOO_SMALL" if oversized else "FORBIDDEN_SCOPE") in str(
                    failure.value
                )
                return None
            return await client.read_resource(f"tychora://assets/{asset_id}")

    async with asyncio.timeout(20):
        result = await read_as(reader)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mime_type == "application/octet-stream"
    assert base64.b64decode(content.blob) == asset_bytes
    narrowed = reader.model_copy(update={"collection_ids": frozenset()})
    async with asyncio.timeout(20):
        await read_as(narrowed, denied=True)
    async with asyncio.timeout(20):
        await read_as(reader, denied=True, oversized=True)


@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_stdio_reader_lists_scoped_tools_and_discovery_resource(tmp_path):
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n[configuration.limits]\nmax_tokens=12000\n'
    )
    with Tychora.open(workspace, token_counter=Counter()) as kb:
        accepted = kb.sources.add(
            {
                "type": "text",
                "source_key": "unicode",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Café π scored 3.14.",
                "metadata": {
                    "title": "Unicode",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(accepted.id, timeout=5).state == "succeeded"
        principal = kb._workspace.owner.scope
        reader = RequestScope(
            principal_id=principal.principal_id,
            collection_ids=principal.collection_ids,
            access_ceiling=principal.access_ceiling,
            uses=principal.uses,
            capabilities=frozenset({"read"}),
        )
    script = """
import asyncio
import sys
from pathlib import Path
from tychora import Tychora
from tychora.contracts.requests import RequestScope
from tychora.interfaces.mcp.server import serve_stdio
class Counter:
    model = 'synthetic-output-test-counter'
    tokenizer_id = 'cl100k_base'
    def count(self, text): return len(text)
async def main():
    principal = RequestScope.model_validate_json(sys.argv[2])
    await serve_stdio(Path(sys.argv[1]), principal=principal, token_counter=Counter())
asyncio.run(main())
"""
    process = StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(workspace), reader.model_dump_json()],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with asyncio.timeout(20):
        async with Client(process, mode="legacy") as client:
            from tychora.interfaces.http.client import connect_local

            sibling = connect_local(workspace)
            try:
                assert sibling.discover({"subject": "kinds"}).subject == "kinds"
            finally:
                sibling.close()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"discover", "search", "inspect"} <= names
            assert "add_sources" not in names
            assert "manage_job" in names
            result = await client.call_tool(
                "discover", {"request": {"subject": "kinds"}}
            )
            assert result.structured_content["subject"] == "kinds"
            assert not result.content
            resource = await client.read_resource("tychora://kinds/manual@1")
            resource_content = resource.contents[0]
            assert isinstance(resource_content, TextResourceContents)
            assert "manual@1" in resource_content.text
            query = {"request": {"question": "Café", "modalities": ["lexical"]}}
            found = await client.call_tool("search", query)
            assert not found.is_error, found.content
            assert found.structured_content["schema_version"] == "1"
            assert not found.content
            wire = _serialized_structured(found)
            assert (
                found.structured_content["budget_contract"]["render_targets"][0][
                    "serialization"
                ]
                == "mcp-structured-json@2.2.0"
            )
            assert (
                len(wire)
                <= found.structured_content["budget_contract"]["max_response_bytes"]
            )
            assert (
                len(wire.decode())
                <= found.structured_content["budget_contract"]["max_tokens"]
            )
            rendered = await client.call_tool(
                "search",
                {
                    "request": {
                        **query["request"],
                        "budgets": {"max_tokens": 12000},
                    },
                    "render": "markdown@1",
                },
            )
            assert not rendered.is_error, rendered.content
            assert len(rendered.content) == 1
            text = _text_content(rendered)
            assert (
                len(_serialized_structured(rendered)) + len(text.encode())
                <= rendered.structured_content["budget_contract"]["max_response_bytes"]
            )
            assert (
                len(_serialized_structured(rendered).decode()) + len(text)
                <= rendered.structured_content["budget_contract"]["max_tokens"]
            )
            denied = await client.call_tool(
                "manage_job", {"action": "cancel", "job_id": accepted.id}
            )
            assert denied.is_error and "FORBIDDEN_SCOPE" in _text_content(denied)
            missing_replay = await client.call_tool(
                "search", {"replay_run_id": "missing"}
            )
            assert missing_replay.is_error and "REPLAY_UNAVAILABLE" in _text_content(
                missing_replay
            )
            templates = await client.list_resource_templates()
            assert {
                "tychora://schemas/{name}",
                "tychora://kinds/{id}",
                "tychora://evidence/{id}",
                "tychora://assets/{id}",
            } <= {str(item.uri_template) for item in templates.resource_templates}
            with pytest.raises(Exception) as missing_kind:
                await client.read_resource("tychora://kinds/unknown@1")
            assert "NOT_FOUND" in str(missing_kind.value)


@pytest.mark.asyncio
async def test_stdio_sdk_cancellation_releases_active_operation(tmp_path):
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, token_counter=Counter()):
        pass
    marker = tmp_path / "cancelled"
    script = """
import asyncio
import sys
from pathlib import Path
from tychora import Tychora
from tychora.interfaces.mcp.server import build_mcp
class Counter:
    model = 'synthetic-output-test-counter'
    tokenizer_id = 'cl100k_base'
    def count(self, text): return len(text)
async def main():
    with Tychora.open(Path(sys.argv[1]), token_counter=Counter()) as kb:
        server = build_mcp(kb._workspace.services, kb.scope)
        @server.tool()
        async def slow():
            try:
                await asyncio.sleep(3600)
            finally:
                Path(sys.argv[2]).write_text('cancelled')
        await server.run_stdio_async()
asyncio.run(main())
"""
    process = StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(workspace), str(marker)],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with asyncio.timeout(15):
        async with Client(process, mode="legacy") as client:
            pending = asyncio.create_task(client.call_tool("slow", {}))
            await asyncio.sleep(0.15)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.02)
            assert marker.read_text() == "cancelled"
            again = await client.call_tool(
                "discover", {"request": {"subject": "kinds"}}
            )
            assert again.structured_content["subject"] == "kinds"
