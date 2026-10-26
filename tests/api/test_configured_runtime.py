"""Configured public runtime composition with synthetic settings."""

import pytest

from tests.helpers.assets import asset_path
from tychora import Tychora
from tychora.configuration.resolve import resolve_config


class OutputCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def test_public_open_composes_configured_text_and_native_index(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        bindings = kb._workspace.services.provider_bindings
        assert bindings.text_query is not None
        assert bindings.text_embedder is not None
        assert bindings.text_index is not None
        assert bindings.text_embedder.capabilities.model == "voyage-context-4"


def test_public_open_composes_image_rerank_and_graph_without_network(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.image]\nbackend="chroma"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        bindings = kb._workspace.services.provider_bindings
        assert bindings.visual_query is not None
        assert bindings.visual_embedder is not None
        assert bindings.visual_index is not None
        assert bindings.reranker is not None
        assert bindings.graph is not None
        assert bindings.graph_factory is not None


def test_public_add_persists_native_graph_generation(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    config = resolve_config(settings, "test", {})
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic record of valve testing.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "valve-test",
                "collection_id": "default",
                "metadata": {
                    "title": "Valve test",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(job.id)
        assert status.state == "succeeded", status
        second = kb.sources.add(
            {
                "type": "text",
                "text": "A second synthetic valve measurement.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "valve-second",
                "collection_id": "default",
                "metadata": {
                    "title": "Second valve",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(second.id).state == "succeeded"
        view = kb._workspace.bridge.call(
            kb._workspace.owner.run_write(
                lambda: kb._workspace.owner.store.resolve_snapshot(None),
                __import__(
                    "tychora.ports.execution", fromlist=["ExecutionContext"]
                ).ExecutionContext(),
            )
        )
        assert any(slot.slot == "graph" and slot.required for slot in view.families)
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        assert kb._workspace.services.provider_bindings.graph is not None
        assert kb.search(
            {"question": "valve testing", "modalities": ["lexical"]}
        ).segments


@pytest.mark.asyncio
async def test_settings_activation_recomposes_credential_without_changing_space(
    tmp_path, monkeypatch
):
    from tychora.application.settings import SettingsService
    from tychora.contracts.operations import SettingsActivation

    keys = []

    class Transport:
        def __init__(self, *, api_key):
            keys.append(api_key)
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-old")
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    settings = workspace / "workspace.toml"
    content = (
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n[configuration]\noffline=false\n'
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    settings.write_text(content)
    config = resolve_config(settings, "test", {})
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        ws = kb._workspace
        old = ws.services.provider_bindings.text_query
        assert old is not None
        old_id = old.credential_binding_id
        old_space = old.capabilities.space_id
        old_transport = old._base._runtime.transport()
        assert keys == ["synthetic-old"]
        monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-new")
        await SettingsService(ws.owner, ws.runtime).activate(
            SettingsActivation(
                profile="test",
                workspace_toml=content + '\n[configuration.output]\ntitle="Rotated"\n',
            )
        )
        current = ws.services.provider_bindings.text_query
        assert current is not None
        assert current.credential_binding_id != old_id
        assert current.capabilities.space_id == old_space
        assert current._base._runtime.transport() is not old_transport
        assert keys == ["synthetic-old", "synthetic-new"]
        assert old_transport.closed
        native_index = ws.services.provider_bindings.text_index
        current_transport = current._base._runtime.transport()
        monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-third")
        await SettingsService(ws.owner, ws.runtime).activate(
            SettingsActivation(
                profile="test",
                workspace_toml=content + '\n[configuration.output]\ntitle="Rotated"\n',
            )
        )
        latest = ws.services.provider_bindings.text_query
        assert latest is not None
        assert latest.credential_binding_id != current.credential_binding_id
        assert latest.capabilities.space_id == old_space
        assert ws.services.provider_bindings.text_index is native_index
        latest._base._runtime.transport()
        assert keys == ["synthetic-old", "synthetic-new", "synthetic-third"]
        assert current_transport.closed


def _two_provider_config(tmp_path):
    settings = tmp_path / "workspace.toml"
    content = (
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
    )
    settings.write_text(content)
    return content, resolve_config(settings, "test", {})


@pytest.mark.asyncio
async def test_committed_activation_settles_each_owned_runtime_close(
    tmp_path, monkeypatch
):
    from tychora.application.settings import SettingsService
    from tychora.contracts.operations import SettingsActivation

    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    closed = []
    transports = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"
            self.position = len(transports)
            transports.append(self)

        def close(self):
            closed.append(self.position)
            if self.position == 0:
                raise RuntimeError("private transport close detail")

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    content, config = _two_provider_config(tmp_path)
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(content)
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        ws = kb._workspace
        old = ws.services
        for runtime in old.owned_runtimes:
            runtime.transport()
        assert len(transports) == 2
        candidate = content + '\n[configuration.output]\ntitle="Committed"\n'
        accepted = await SettingsService(ws.owner, ws.runtime).activate(
            SettingsActivation(profile="test", workspace_toml=candidate)
        )
        assert accepted.config_sha == ws.owner.config.config_sha
        assert ws.runtime.services is not old
        assert (workspace / "workspace.toml").read_text() == candidate
        assert closed == [0, 1]
        assert ws.runtime.cleanup_diagnostics == (
            "owned provider runtime cleanup failed",
        )
        assert "private transport close detail" not in repr(
            ws.runtime.cleanup_diagnostics
        )


def test_workspace_close_settles_each_owned_runtime_close(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    closed = []
    transports = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"
            self.position = len(transports)
            transports.append(self)

        def close(self):
            closed.append(self.position)
            if self.position == 0:
                raise RuntimeError("private transport close detail")

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    _, config = _two_provider_config(tmp_path)
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        ws = kb._workspace
        for runtime in ws.services.owned_runtimes:
            runtime.transport()
    assert closed == [0, 1]
    assert ws.runtime.cleanup_diagnostics == ("owned provider runtime cleanup failed",)


def test_secret_reference_uses_explicit_resolver_only_on_operation(
    tmp_path, monkeypatch
):
    keys = []

    class Transport:
        def __init__(self, *, api_key):
            keys.append(api_key)

        def close(self):
            pass

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="secret:synthetic"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        token_counter=OutputCounter(),
        credential_resolver=lambda name: "synthetic-key" if name == "synthetic" else "",
    ) as kb:
        assert keys == []
        query = kb._workspace.services.provider_bindings.text_query
        assert query is not None
        query._base._runtime.transport()
        assert keys == ["synthetic-key"]


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_public_add_dispatches_configured_text_only_after_policy_grant(
    tmp_path, monkeypatch
):
    import json

    from tychora.adapters.voyage.request_types import RawVoyageResponse
    from tychora.contracts.requests import RequestScope
    from tychora.ports.execution import ExecutionContext

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    posts = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"

        def post_json(self, path, body, timeout):
            posts.append((path, body))
            batches = json.loads(body)["inputs"]
            vector = [1.0] + [0.0] * 1023
            data = [
                {
                    "index": outer,
                    "data": [
                        {"index": inner, "embedding": vector}
                        for inner, _ in enumerate(batch)
                    ],
                }
                for outer, batch in enumerate(batches)
            ]
            return RawVoyageResponse(200, json.dumps({"data": data}).encode())

        def close(self):
            pass

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({"00000000-0000-4000-8000-000000000001"}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        scope=scope,
        token_counter=OutputCounter(),
    ) as kb:
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    'UPDATE collections SET policy_json=? WHERE slug="default"',
                    ('{"schema_version":1,"upload_providers":["voyage"]}',),
                ),
                ExecutionContext(),
            )
        )
        source = {
            "type": "text",
            "text": "Synthetic record of valve testing.",
            "format": "text",
            "kind": "manual@1",
            "source_key": "valve-test",
            "collection_id": "default",
            "metadata": {
                "title": "Valve test",
                "publisher": "Example Team",
                "edition": "1",
            },
        }
        job = kb.sources.add(source)
        status = kb.jobs.wait(job.id)
        assert status.state == "succeeded", status
        assert posts and posts[0][0] == "/v1/contextualizedembeddings"
        sent = len(posts)
        narrow = scope.model_copy(update={"upload_providers": frozenset()})
        source["source_key"] = "valve-test-narrow"
        from tychora.contracts.errors import TychoraError

        with pytest.raises(TychoraError) as denied:
            kb._workspace.bridge.call(
                kb._workspace.services.sources.add(
                    kb._workspace.parse_source(source),
                    narrow,
                )
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"
        assert len(posts) == sent
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        scope=scope,
        token_counter=OutputCounter(),
    ) as reopened:
        result = reopened.search({"question": "valve testing"})
        assert result.segments


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_configured_text_provider_cli_api_mcp_grant_then_revoke(tmp_path, monkeypatch):
    import asyncio
    import json

    from typer.testing import CliRunner

    from tychora.adapters.voyage.request_types import RawVoyageResponse
    from tychora.contracts.errors import TychoraError
    from tychora.interfaces.cli import app as cli_app
    from tychora.interfaces.mcp.server import _current, build_mcp

    monkeypatch.setenv(
        "TYCHORA_MODEL_CACHE", str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    )
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    posts = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"

        def post_json(self, path, body, timeout):
            posts.append(path)
            batches = json.loads(body)["inputs"]
            vector = [1.0] + [0.0] * 1023
            return RawVoyageResponse(
                200,
                json.dumps(
                    {
                        "data": [
                            {
                                "index": outer,
                                "data": [
                                    {"index": inner, "embedding": vector}
                                    for inner, _ in enumerate(batch)
                                ],
                            }
                            for outer, batch in enumerate(batches)
                        ]
                    }
                ).encode(),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    settings = workspace / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    source = {
        "type": "text",
        "text": "Synthetic valve inspection record.",
        "format": "text",
        "kind": "manual@1",
        "source_key": "valve-inspection",
        "collection_id": "default",
        "metadata": {
            "title": "Valve",
            "publisher": "Example Team",
            "edition": "1",
        },
    }
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        denied = kb.sources.add(source)
        assert kb.jobs.wait(denied.id).state == "failed"
        assert posts == []
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "voyage",
                "action": "grant",
            }
        )
    with Tychora.open(workspace, token_counter=OutputCounter()) as kb:
        added = kb.sources.add(source)
        assert kb.jobs.wait(added.id).state == "succeeded"
        assert posts == ["/v1/contextualizedembeddings"]
        api_result = kb.search({"question": "valve api", "modalities": ["text"]})
        assert api_result.segments
        assert posts[-1] == "/v1/contextualizedembeddings"
    cli_request = tmp_path / "cli-query.json"
    cli_request.write_text(
        json.dumps({"question": "valve cli", "modalities": ["text"]})
    )
    cli = CliRunner().invoke(
        cli_app,
        [
            "--workspace",
            str(workspace),
            "search",
            "--request-file",
            str(cli_request),
            "--json",
        ],
    )
    assert cli.exit_code == 0, cli.stderr
    assert json.loads(cli.stdout)["segments"]
    assert len(posts) == 3
    with Tychora.open(workspace, token_counter=OutputCounter()) as kb:
        server = build_mcp(kb._workspace.services, kb.scope)

        async def mcp_search():
            marker = _current.set((kb._workspace.services, kb.scope))
            try:
                return await server.call_tool(
                    "search",
                    {
                        "request": {
                            "question": "valve mcp",
                            "modalities": ["text"],
                        }
                    },
                )
            finally:
                _current.reset(marker)

        mcp = asyncio.run(mcp_search())
        assert not mcp.is_error and mcp.structured_content["segments"]
        assert len(posts) == 4
        kb.management.run(
            {
                "operation": "provider-upload-consent",
                "collection_id": "default",
                "provider": "voyage",
                "action": "revoke",
            }
        )
    rejected_cli = CliRunner().invoke(
        cli_app,
        ["--workspace", str(workspace), "search", "revoked valve", "--json"],
    )
    assert rejected_cli.exit_code != 0
    assert json.loads(rejected_cli.stderr)["code"] == "FORBIDDEN_SCOPE"
    with Tychora.open(workspace, token_counter=OutputCounter()) as kb:
        with pytest.raises(TychoraError) as rejected_api:
            kb.search({"question": "revoked valve api", "modalities": ["text"]})
        assert rejected_api.value.code == "FORBIDDEN_SCOPE"
    assert len(posts) == 4


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_public_visual_query_uses_default_binding_and_narrow_scope(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace

    from tychora.adapters.voyage.request_types import RawVoyageResponse
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.requests import RequestScope
    from tychora.ports.execution import ExecutionContext

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    monkeypatch.setattr(
        "tychora.application.default_bindings.load_voyage_tokenizer",
        lambda cache_root, model: SimpleNamespace(
            encode=lambda value: SimpleNamespace(ids=list(value))
        ),
    )
    monkeypatch.setattr(
        "tychora.application.default_bindings.load_voyage_multimodal_accounting",
        lambda cache_root: None,
    )
    posts = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"

        def post_json(self, path, body, timeout):
            posts.append((path, json.loads(body)))
            return RawVoyageResponse(
                200,
                json.dumps(
                    {"data": [{"index": 0, "embedding": [1.0] * 1024}]}
                ).encode(),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.image]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({"00000000-0000-4000-8000-000000000001"}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        scope=scope,
        token_counter=OutputCounter(),
    ) as kb:
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    'UPDATE collections SET policy_json=? WHERE slug="default"',
                    ('{"schema_version":1,"upload_providers":["voyage"]}',),
                ),
                ExecutionContext(),
            )
        )
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic valve inspection.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "valve-inspection",
                "collection_id": "default",
                "metadata": {
                    "title": "Valve",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        kb.search({"question": "valve", "modalities": ["image"]})
        assert len(posts) == 1
        assert posts[0][0] == "/v1/multimodalembeddings"
        assert posts[0][1]["inputs"][0]["content"] == [
            {"type": "text", "text": "valve"}
        ]
        narrow = scope.model_copy(update={"upload_providers": frozenset()})
        with pytest.raises(TychoraError) as denied:
            kb.search(
                {"question": "different valve", "modalities": ["image"]},
                scope=narrow,
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"
        assert len(posts) == 1


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_public_rerank_uses_default_binding_and_narrow_scope(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from tychora.adapters.voyage.request_types import RawVoyageResponse
    from tychora.application.query import QueryExecutionPlan, QueryService
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.requests import QueryRequest, RequestScope
    from tychora.ports.execution import ExecutionContext

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    monkeypatch.setattr(
        "tychora.application.default_bindings.load_voyage_tokenizer",
        lambda cache_root, model: SimpleNamespace(
            encode=lambda value: SimpleNamespace(ids=list(value))
        ),
    )
    posts = []

    class Transport:
        def __init__(self, *, api_key):
            assert api_key == "synthetic-test-key"

        def post_json(self, path, body, timeout):
            request = json.loads(body)
            posts.append((path, request))
            return RawVoyageResponse(
                200,
                json.dumps(
                    {
                        "data": [
                            {
                                "index": index,
                                "relevance_score": 0.9
                                if index == len(request["documents"]) - 1
                                else 0.1,
                            }
                            for index in range(len(request["documents"]))
                        ]
                    }
                ).encode(),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", Transport
    )
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({"00000000-0000-4000-8000-000000000001"}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        scope=scope,
        token_counter=OutputCounter(),
    ) as kb:
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    'UPDATE collections SET policy_json=? WHERE slug="default"',
                    ('{"schema_version":1,"upload_providers":["voyage"]}',),
                ),
                ExecutionContext(),
            )
        )
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic valve inspection.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "valve-inspection",
                "collection_id": "default",
                "metadata": {
                    "title": "Valve",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        second = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic valve alternate inspection.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "valve-alternate",
                "collection_id": "default",
                "metadata": {
                    "title": "Valve alternate",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(second.id).state == "succeeded"
        request = QueryRequest.from_mapping(
            {"question": "valve", "modalities": ["lexical"], "limit": 2}
        )
        plain_service = QueryService(
            owner,
            output_counter=kb._workspace.services.query.counter,
            execution_plan=QueryExecutionPlan(
                lexical=True,
                text=False,
                image=False,
                graph_structural=False,
                graph_semantic=False,
                rerank=False,
            ),
        )
        plain = kb._workspace.bridge.call(
            plain_service.search(request, scope, ExecutionContext())
        ).result
        assert len(plain.segments) == 2
        assert posts == []
        reranked = kb.search(request)
        assert len(reranked.segments) == 2
        assert len(posts) == 1
        assert posts[0][0] == "/v1/rerank"
        assert len(posts[0][1]["documents"]) == 2
        assert [s.matched_unit_ids for s in reranked.segments] == [
            s.matched_unit_ids for s in reversed(plain.segments)
        ]
        narrow = scope.model_copy(update={"upload_providers": frozenset()})
        with pytest.raises(TychoraError) as denied:
            kb.search(
                {"question": "inspection", "modalities": ["lexical"]},
                scope=narrow,
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"
        assert len(posts) == 1


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_cli_add_and_search_use_configured_native_graph(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from tychora.interfaces.cli import app

    cache = str(asset_path("TYCHORA_TEST_MODEL_CACHE"))
    monkeypatch.setenv("TYCHORA_MODEL_CACHE", cache)
    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    (workspace / "workspace.toml").write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="local"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "type": "text",
                "text": "Synthetic check of pump output.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "pump-check",
                "collection_id": "00000000-0000-4000-8000-000000000001",
                "metadata": {
                    "title": "Pump check",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
    )
    runner = CliRunner()
    added = runner.invoke(
        app,
        ["--workspace", str(workspace), "add", "--request-file", str(source), "--json"],
    )
    assert added.exit_code == 0, added.stderr
    assert json.loads(added.stdout)["state"] == "succeeded"
    found = runner.invoke(
        app, ["--workspace", str(workspace), "search", "pump output", "--json"]
    )
    assert found.exit_code == 0, found.stderr
    assert json.loads(found.stdout)["segments"]


@pytest.mark.asyncio
async def test_http_query_uses_configured_graph_owner(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Synthetic pressure record.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "pressure",
                "collection_id": "default",
                "metadata": {
                    "title": "Pressure",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth(
                {"owner-token": owner.scope},
                authority="127.0.0.1:8765",
            ),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            response = await client.post(
                "/v1/queries",
                json={"question": "pressure", "modalities": ["lexical"]},
            )
        assert response.status_code == 200, response.text
        assert response.json()["segments"]


def test_offline_retains_native_index_without_remote_binding(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        bindings = kb._workspace.services.provider_bindings
        assert bindings.text_index is not None
        assert bindings.text_query is None
        assert bindings.text_embedder is None


def test_missing_model_asset_is_typed_at_first_token_count(tmp_path, monkeypatch):
    from tychora.contracts.errors import TychoraError

    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(tmp_path / "empty-cache"))
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        counter = kb._workspace.services.provider_bindings.text_counter
        assert counter is not None
        with pytest.raises(TychoraError) as missing:
            counter.count("synthetic")
        assert missing.value.code == "CAPABILITY_MISSING"
        assert "manage setup" in missing.value.message


def test_configured_text_and_image_use_separate_native_spaces(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
        '[configuration.index.image]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        bindings = kb._workspace.services.provider_bindings
        assert bindings.text_index is not None and bindings.visual_index is not None
        assert bindings.text_index.path != bindings.visual_index.path
        assert (
            bindings.text_embedder is not None and bindings.visual_embedder is not None
        )
        assert (
            bindings.text_embedder.capabilities.space_id
            != bindings.visual_embedder.capabilities.space_id
        )


def test_unresolved_credential_is_typed_and_never_constructs_transport(
    tmp_path, monkeypatch
):
    from tychora.contracts.errors import TychoraError

    monkeypatch.delenv("UNSET_TEST_KEY", raising=False)
    constructed = []
    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport",
        lambda **kwargs: constructed.append(kwargs),
    )
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        query = kb._workspace.services.provider_bindings.text_query
        assert query is not None
        with pytest.raises(TychoraError) as missing:
            query._base._runtime.transport()
        assert missing.value.code == "CAPABILITY_MISSING"
        assert constructed == []


@pytest.mark.asyncio
async def test_failed_settings_prepare_restores_native_graph_policy(tmp_path):
    from tychora.application.settings import SettingsService
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.operations import SettingsActivation

    workspace = tmp_path / "knowledge"
    workspace.mkdir()
    content = (
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[defaults]\nprofile="test"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    (workspace / "workspace.toml").write_text(content)
    config = resolve_config(workspace / "workspace.toml", "test", {})
    with Tychora.open(workspace, config=config, token_counter=OutputCounter()) as kb:
        ws = kb._workspace
        graph = ws.services.provider_bindings.graph
        assert graph is not None
        previous_policy = graph._traversal.policy
        candidate = content + (
            '\n[configuration.retrieval]\ntable_continuations="none"\n'
            "[configuration]\noffline=false\n"
            '[configuration.providers.text]\nprovider="voyage"\n'
            'model="unsupported"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        )
        with pytest.raises(TychoraError) as rejected:
            await SettingsService(ws.owner, ws.runtime).activate(
                SettingsActivation(profile="test", workspace_toml=candidate)
            )
        assert rejected.value.code == "CAPABILITY_MISSING"
        assert ws.services.provider_bindings.graph is graph
        assert graph._traversal.policy == previous_policy
        assert (workspace / "workspace.toml").read_text() == content


def test_failed_default_composition_closes_prepared_provider_runtime(
    tmp_path, monkeypatch
):
    from tychora.adapters.voyage.runtime import VoyageRuntime
    from tychora.contracts.errors import TychoraError

    closed = []
    original_close = VoyageRuntime.close

    def counted_close(self):
        closed.append(self)
        original_close(self)

    monkeypatch.setattr(VoyageRuntime, "close", counted_close)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="unsupported"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    with pytest.raises(TychoraError) as failed:
        Tychora.open(
            tmp_path / "knowledge", config=config, token_counter=OutputCounter()
        )
    assert failed.value.code == "CAPABILITY_MISSING"
    assert len(closed) == 1


def test_failed_service_build_closes_composed_provider_runtime(tmp_path, monkeypatch):
    from tychora.adapters.voyage.runtime import VoyageRuntime

    closed = []
    original_close = VoyageRuntime.close

    def counted_close(self):
        closed.append(self)
        if len(closed) == 1:
            raise RuntimeError("private cleanup detail")
        original_close(self)

    monkeypatch.setattr(VoyageRuntime, "close", counted_close)

    def failed_build(*args, **kwargs):
        raise RuntimeError("synthetic service construction failure")

    monkeypatch.setattr("tychora.api.build_services", failed_build)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    with pytest.raises(RuntimeError, match="synthetic service construction"):
        Tychora.open(
            tmp_path / "knowledge", config=config, token_counter=OutputCounter()
        )
    assert len(closed) == 2


def test_empty_visual_context_still_requires_verified_accounting_asset(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tychora.contracts.errors import TychoraError

    monkeypatch.setenv("TYCHORA_MODEL_CACHE", str(tmp_path / "empty-cache"))
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    with Tychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        embedder = kb._workspace.services.provider_bindings.visual_embedder
        assert embedder is not None
        with pytest.raises(TychoraError) as missing:
            embedder._tokens(
                "",
                SimpleNamespace(
                    width=100,
                    height=100,
                    resize=SimpleNamespace(source_width=100, source_height=100),
                ),
            )
        assert missing.value.code == "CAPABILITY_MISSING"


def test_missing_native_index_package_is_typed(tmp_path, monkeypatch):
    import builtins

    from tychora.contracts.errors import TychoraError

    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "tychora.adapters.chroma.index":
            raise ImportError("synthetic absent optional package")
        return original_import(name, *args, **kwargs)

    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(TychoraError) as missing:
        Tychora.open(
            tmp_path / "knowledge", config=config, token_counter=OutputCounter()
        )
    assert missing.value.code == "CAPABILITY_MISSING"
    assert "native index" in missing.value.message


@pytest.mark.asyncio
async def test_async_public_open_composes_configured_text(tmp_path):
    from tychora import AsyncTychora

    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
    )
    config = resolve_config(settings, "test", {})
    async with AsyncTychora.open(
        tmp_path / "knowledge", config=config, token_counter=OutputCounter()
    ) as kb:
        assert kb._workspace.services.provider_bindings.text_query is not None
