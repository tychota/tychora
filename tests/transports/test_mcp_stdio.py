"""Real subprocess MCP protocol qualification."""

import asyncio
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from tychora import Tychora
from tychora.contracts.requests import RequestScope
from tychora.interfaces.mcp.tools import _serialized_structured


class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


@pytest.mark.asyncio
@pytest.mark.qualification(loopback=True)
async def test_stdio_reader_lists_scoped_tools_and_discovery_resource(tmp_path):
    workspace = tmp_path / "knowledge"
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
            assert "manual@1" in resource.contents[0].text
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
                "search", {**query, "render": "markdown@1"}
            )
            assert not rendered.is_error, rendered.content
            assert len(rendered.content) == 1
            text = rendered.content[0].text
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
            assert denied.is_error and "FORBIDDEN_SCOPE" in denied.content[0].text
            missing_replay = await client.call_tool(
                "search", {"replay_run_id": "missing"}
            )
            assert (
                missing_replay.is_error
                and "REPLAY_UNAVAILABLE" in missing_replay.content[0].text
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
