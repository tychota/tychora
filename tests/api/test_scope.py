"""Public composition and provider identity boundaries."""

from types import SimpleNamespace

import pytest

from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.retrieval.test_query import LocalTextQueryProvider
from tychora import ProviderBindings, RequestScope, Tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.api import LocalTextBinding
from tychora.application.query import QueryService
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


def test_future_operations_check_their_own_capability_before_reporting_unavailable(
    tmp_path,
):
    read_only = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({"00000000-0000-4000-8000-000000000001"}),
        capabilities=frozenset({"read"}),
        uses=frozenset({"lookup"}),
    )
    with Tychora.open(
        tmp_path / "knowledge", scope=read_only, token_counter=OutputCounter()
    ) as kb:
        with pytest.raises(TychoraError) as health:
            kb.management.run({"operation": "health"})
        assert health.value.code == "CAPABILITY_MISSING"
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
    service.owner = SimpleNamespace(config=config)
    service.text_provider = provider
    service.text_index = None
    service.visual_provider = None
    service.visual_index = None
    service.reranker = None
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
