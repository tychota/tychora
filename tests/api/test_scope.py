"""Public composition and provider identity boundaries."""

from types import SimpleNamespace

import pytest

from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.retrieval.test_query import LocalTextQueryProvider
from tychora import ProviderBindings, Tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.api import LocalTextBinding
from tychora.application.query import QueryService
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import space_id
from tychora.retrieval.cache import QueryCaches


class OutputCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def test_document_embedding_requires_its_own_model_counter(tmp_path):
    embedder = RecordingEmbedder(RecordingCounter())
    with pytest.raises(TychoraError) as missing:
        Tychora.open(
            tmp_path / "knowledge",
            providers=ProviderBindings(local_text=LocalTextBinding(embedder=embedder)),
            token_counter=OutputCounter(),
        )
    assert missing.value.code == "INVALID_REQUEST"


def test_pinned_runtime_compatibility_is_narrower_than_config_identity(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ndimensions=2\ncredential_ref="env:OLD"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    original = resolve_config(settings, "test", {})
    values = original.values
    providers = values.providers
    rotated_providers = providers.model_copy(
        update={"text": providers.text.model_copy(update={"credential_ref": "env:NEW"})}
    )
    rotated = original.model_copy(
        update={"values": values.model_copy(update={"providers": rotated_providers})}
    )
    assert pinned_runtime_compatible(original, rotated)
    for changed in (
        providers.text.model_copy(update={"model": "different-model"}),
        providers.text.model_copy(update={"dimensions": 3}),
    ):
        candidate = original.model_copy(
            update={
                "values": values.model_copy(
                    update={"providers": providers.model_copy(update={"text": changed})}
                )
            }
        )
        assert not pinned_runtime_compatible(original, candidate)
    for field, changed in (
        (
            "index",
            values.index.model_copy(
                update={
                    "text": values.index.text.model_copy(update={"backend": "disabled"})
                }
            ),
        ),
        (
            "extraction",
            values.extraction.model_copy(update={"representation_version": "2"}),
        ),
        ("limits", values.limits.model_copy(update={"limit": 1})),
    ):
        candidate = original.model_copy(
            update={"values": values.model_copy(update={field: changed})}
        )
        assert not pinned_runtime_compatible(original, candidate)


def test_future_operations_check_their_own_capability_before_reporting_unavailable(
    tmp_path,
):
    with Tychora.open(tmp_path / "knowledge", token_counter=OutputCounter()) as initial:
        read_only = initial.scope.model_copy(
            update={"capabilities": frozenset({"read"})}
        )
    with Tychora.open(
        tmp_path / "knowledge", scope=read_only, token_counter=OutputCounter()
    ) as kb:
        health = kb.management.run({"operation": "health"})
        assert health.plan.operation == "health"
        assert health.recovery is not None
        with pytest.raises(TychoraError) as resume:
            kb.management.run({"operation": "health", "resume_pending_deletions": True})
        assert resume.value.code == "FORBIDDEN_SCOPE"
        with pytest.raises(TychoraError) as remove:
            kb.management.run({"operation": "remove", "source_ids": ["source"]})
        assert remove.value.code == "FORBIDDEN_SCOPE"


def test_offline_public_local_text_add_and_query_uses_declared_local_binding(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\ndimensions=2\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    model_counter = RecordingCounter()
    embedder = RecordingEmbedder(model_counter)
    caches = QueryCaches()
    query_provider = LocalTextQueryProvider(
        None, None, embedder.capabilities, model_counter, caches
    )
    bindings = ProviderBindings(
        local_text=LocalTextBinding(embedder=embedder),
        text_query=query_provider,
        text_index=ChromaVectorIndex(tmp_path / "vectors"),
        text_counter=model_counter,
    )
    with Tychora.open(
        tmp_path / "knowledge",
        config=config,
        providers=bindings,
        token_counter=OutputCounter(),
    ) as kb:
        query_provider.owner = kb._workspace.owner
        query_provider.scope = kb.scope
        job = kb.sources.add(
            {
                "type": "text",
                "text": "Record verification outcomes.",
                "format": "text",
                "kind": "manual@1",
                "source_key": "manual",
                "collection_id": "default",
                "metadata": {
                    "title": "Verification",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        result = kb.search(
            {"question": "verification", "modalities": ["lexical", "text"]}
        )
        assert result.segments
        assert "text" in result.coverage.completed_channels
        query_provider.capabilities = embedder.capabilities.model_copy(
            update={
                "representation_fingerprint": "f" * 64,
                "space_id": space_id(
                    "text",
                    embedder.capabilities.provider,
                    embedder.capabilities.model,
                    embedder.capabilities.dimensions,
                    embedder.capabilities.metric,
                    "f" * 64,
                ),
            }
        )
        with pytest.raises(TychoraError) as mismatch:
            kb.search({"question": "verification", "modalities": ["lexical", "text"]})
        assert mismatch.value.code in {"INTEGRITY_ERROR", "PROFILE_INCOMPATIBLE"}


@pytest.mark.asyncio
async def test_pinned_vector_family_survives_credential_rotation_and_new_add(tmp_path):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    root = tmp_path / "knowledge"
    root.mkdir()
    settings = root / "workspace.toml"
    original = (
        'schema_version=1\n[defaults]\nprofile="test"\n'
        '[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ndimensions=2\n'
        'credential_ref="env:ORIGINAL"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    settings.write_text(original)
    config = resolve_config(settings, "test", {})
    counter = RecordingCounter()
    embedder = RecordingEmbedder(counter)
    caches = QueryCaches()
    query_provider = LocalTextQueryProvider(
        None, None, embedder.capabilities, counter, caches
    )
    bindings = ProviderBindings(
        local_text=LocalTextBinding(embedder=embedder),
        text_query=query_provider,
        text_index=ChromaVectorIndex(tmp_path / "vectors"),
        text_counter=counter,
    )
    with Tychora.open(
        root,
        config=config,
        providers=bindings,
        token_counter=OutputCounter(),
    ) as kb:
        query_provider.owner = kb._workspace.owner
        query_provider.scope = kb.scope

        def source(key):
            return {
                "type": "text",
                "text": f"Verification record {key}.",
                "format": "text",
                "kind": "manual@1",
                "source_key": key,
                "collection_id": "default",
                "metadata": {"title": key, "publisher": "Example", "edition": "1"},
            }

        first = await asyncio.to_thread(kb.sources.add, source("before"))
        assert (await asyncio.to_thread(kb.jobs.wait, first.id)).state == "succeeded"
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"owner-token": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer owner-token"},
        ) as client:
            rotated = original.replace("env:ORIGINAL", "env:ROTATED")
            response = await client.put(
                "/v1/settings",
                json={"profile": "test", "workspace_toml": rotated},
            )
            assert response.status_code == 200, response.text
            old_result = await asyncio.to_thread(
                kb.search,
                {"question": "verification", "modalities": ["lexical", "text"]},
            )
            assert "text" in old_result.coverage.completed_channels
            second = await asyncio.to_thread(kb.sources.add, source("after"))
            status = await asyncio.to_thread(kb.jobs.wait, second.id)
            assert status.state == "succeeded", (status.state, status.error_code)
            new_result = await asyncio.to_thread(
                kb.search,
                {"question": "verification", "modalities": ["lexical", "text"]},
            )
            assert "text" in new_result.coverage.completed_channels
            assert new_result.segments
            from tychora.ports.execution import ExecutionContext

            current_config = owner.config
            snapshot_id = await owner.run_write(
                owner.store.active_snapshot_id, ExecutionContext()
            )
            policy = rotated + "\n[configuration.limits]\nlimit=4\n"
            changed_policy = await client.put(
                "/v1/settings", json={"profile": "test", "workspace_toml": policy}
            )
            assert changed_policy.status_code == 409, changed_policy.text
            assert changed_policy.json()["code"] == "PROFILE_INCOMPATIBLE"
            assert settings.read_text() == rotated and owner.config is current_config
            assert (
                await owner.run_write(
                    owner.store.active_snapshot_id, ExecutionContext()
                )
                == snapshot_id
            )
            preserved = await asyncio.to_thread(
                kb.search,
                {
                    "question": "verification",
                    "limit": 1,
                    "modalities": ["lexical", "text"],
                },
            )
            assert "text" in preserved.coverage.completed_channels
            assert preserved.segments
        await app.state.runtime.services.source_jobs.aclose()


def test_actual_factory_space_and_locality_change_result_identity(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    capabilities = RecordingEmbedder(RecordingCounter()).capabilities
    cache = object()
    provider = SimpleNamespace(
        capabilities=capabilities,
        credential_binding_id="same-binding",
        remote=False,
        available=True,
        caches=cache,
    )
    service = QueryService.__new__(QueryService)
    vars(service)["owner"] = SimpleNamespace(config=config)
    vars(service)["text_provider"] = provider
    service.text_index = None
    service.visual_provider = None
    service.visual_index = None
    service.reranker = None
    service.execution_plan = None
    first = service._provider_fingerprint()
    provider.capabilities = capabilities.model_copy(
        update={
            "representation_fingerprint": "f" * 64,
            "space_id": space_id(
                "text",
                capabilities.provider,
                capabilities.model,
                capabilities.dimensions,
                capabilities.metric,
                "f" * 64,
            ),
        }
    )
    assert service._provider_fingerprint() != first
    provider.capabilities = capabilities
    provider.remote = True
    assert service._provider_fingerprint() != first
