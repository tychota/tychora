"""Query identity and orchestration guards."""

import json
import os
import sqlite3
import struct
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from PIL import Image

from tests.helpers.assets import asset_path
from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tests.helpers.eligibility import eligibility_view, legacy_attempt
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.providers.test_visual import CharacterCounter, RecordingRawTransport, _limits
from tests.retrieval.test_segments import config as base_config
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch
from tychora.adapters.voyage.query import (
    VoyageTextQueryProvider,
    VoyageVisualQueryProvider,
)
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.text import VoyageTextEmbedder
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import (
    QueryService,
    linked_visual_context,
    query_binding_from_owner,
)
from tychora.application.sources import SourceService
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.corpus import AssetRow, RevisionArtifactRow, UnitAssetRow
from tychora.contracts.embeddings import EmbeddingRecord, QueryEmbeddingProbe
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import ImageLocator
from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest, RequestScope, SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.query import result_cache_key, validate_query_text
from tychora.retrieval.segments import OutputTokenCounter


class Counter:
    model = "local"
    tokenizer_id = "provider@1"

    def count(self, text: str) -> int:
        return len(text.split())


def _assert_project_facets(owner: WorkspaceOwner) -> None:
    assert owner.db.execute(
        "SELECT value_type FROM facet_definitions WHERE facet_key='scope.project'"
    ).fetchone() == ("text",)
    rows = owner.db.execute(
        "SELECT r.metadata_json, f.text_value FROM revisions r "
        "JOIN facet_values f ON f.revision_id=r.revision_id "
        "WHERE f.facet_key='scope.project' AND f.ordinal=0"
    ).fetchall()
    assert sorted(
        (json.loads(metadata)["scope"]["project"], value) for metadata, value in rows
    ) == [("alpha", "alpha"), ("beta", "beta")]


class LocalTextQueryProvider:
    credential_binding_id = "synthetic-local-binding"
    remote = False
    available = True

    def __init__(self, owner, scope, capabilities, counter, caches):
        self.owner = owner
        self.scope = scope
        self.capabilities = capabilities
        self.counter = counter
        self.caches = caches

    def bind(self, owner, scope, policy_epoch):
        assert owner is self.owner
        assert scope == self.scope
        assert policy_epoch >= 0
        capabilities = self.capabilities
        owner = self.owner

        class LocalEmbedder:
            def __init__(self):
                self.capabilities = capabilities

            def probe_queries(self, queries, ctx):
                assert len(queries) == 1
                value = EmbeddingRecord.from_normalized_bytes(
                    record_id="synthetic-local-query",
                    input_sha=digest("synthetic-query-v1", queries),
                    space_id=capabilities.space_id,
                    stored=struct.pack("<ff", 1.0, 0.0),
                )
                return QueryEmbeddingProbe(
                    workspace_id=owner.workspace_id,
                    provider=capabilities.provider,
                    query_batch_sha=digest("query-embedding-probe-v1", queries),
                    input_count=1,
                    state="hit",
                    records=(value,),
                )

            def embed_queries(self, queries, ctx):
                raise AssertionError("Local hit must not call a remote provider")

            def embed_groups(self, groups, ctx):
                raise AssertionError("Query binding must not index documents")

        return LocalEmbedder()


def test_full_result_identity_changes_with_filters_context_and_lexical_epoch() -> None:
    base = QueryRequest.from_mapping({"question": "verification"})
    filtered = QueryRequest.from_mapping(
        {
            "question": "verification",
            "filters": {"op": "eq", "field": "project", "value": "beta"},
        }
    )
    contextual = QueryRequest.from_mapping(
        {"question": "verification", "context": {"code": "sensitive"}}
    )
    key = result_cache_key(base, "snapshot", 1, 5, "scope", "config")
    assert key != result_cache_key(filtered, "snapshot", 1, 5, "scope", "config")
    assert key != result_cache_key(contextual, "snapshot", 1, 5, "scope", "config")
    assert key != result_cache_key(base, "snapshot", 1, 6, "scope", "config")
    assert key != result_cache_key(base, "snapshot", 2, 5, "scope", "config")
    assert key != result_cache_key(
        base, "snapshot", 1, 5, "scope", "config", "new credential"
    )
    assert key != result_cache_key(
        base, "snapshot", 1, 5, "scope", "config", "", "new output"
    )


def test_over_2000_provider_tokens_fails_before_search() -> None:
    request = QueryRequest.from_mapping({"question": "word " * 2001})
    with pytest.raises(TychoraError) as error:
        validate_query_text(request, Counter())
    assert error.value.code == "INVALID_REQUEST"
    assert "2,000" in error.value.message


def test_local_text_replay_gate_degrades_reranker_but_strict_and_remote_still_fail() -> (
    None
):
    config = SimpleNamespace(
        values=SimpleNamespace(
            offline=True,
            index=SimpleNamespace(
                text=SimpleNamespace(backend="chroma"),
                image=SimpleNamespace(backend="disabled"),
            ),
            providers=SimpleNamespace(rerank=SimpleNamespace(provider="voyage")),
            retrieval=SimpleNamespace(allow_degraded=True),
        )
    )
    owner = SimpleNamespace(config=config, workspace_id="workspace")
    scope = RequestScope(principal_id="reader")
    caches = QueryCaches()
    counter = RecordingCounter()
    provider = LocalTextQueryProvider(
        owner, scope, RecordingEmbedder(counter).capabilities, counter, caches
    )
    assert (
        provider.bind(owner, scope, 0)
        .probe_queries(("verification",), ExecutionContext())
        .state
        == "hit"
    )
    service = QueryService.__new__(QueryService)
    vars(service)["owner"] = owner
    service.caches = caches
    service.text_provider = provider
    vars(service)["text_index"] = object()
    service.visual_provider = None
    service.visual_index = None
    service.reranker = None
    service.rerank_counter = None
    service.rerank_unavailable = None
    request = QueryRequest.from_mapping({"question": "verification"})
    assert service._replay_ready(request)
    config.values.retrieval.allow_degraded = False
    assert not service._replay_ready(request)
    config.values.retrieval.allow_degraded = True
    provider.remote = True
    assert not service._replay_ready(request)


def test_query_upload_binding_uses_actual_owner_scope_and_epoch(tmp_path) -> None:
    owner_scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({"alpha", "beta"}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    requested = owner_scope.model_copy(update={"collection_ids": frozenset({"alpha"})})
    with WorkspaceOwner.open(
        tmp_path / "data", base_config(tmp_path), owner_scope
    ) as owner:
        bound = query_binding_from_owner(owner, requested, 7)
        assert bound.collection_ids == frozenset({"alpha"})
        assert bound.policy_epoch == 7
        assert (
            bound.principal_upload_scope_sha
            != query_binding_from_owner(owner, requested, 8).principal_upload_scope_sha
        )
        with pytest.raises(TychoraError):
            query_binding_from_owner(
                owner,
                requested.model_copy(update={"principal_id": "other"}),
                7,
            )


def test_image_match_links_only_pinned_canonical_source_units() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE unit_assets(unit_id TEXT, asset_id TEXT, role TEXT)")
    db.executemany(
        "INSERT INTO unit_assets VALUES (?,?,?)",
        [
            ("eligible", "image", "source"),
            ("hidden", "image", "source"),
            ("caption", "image", "caption"),
        ],
    )
    linked = linked_visual_context(
        db,
        (ChannelCandidate(key=("asset", "image"), rank=1),),
        frozenset({"eligible"}),
    )
    assert [candidate.key for candidate in linked] == [("unit", "eligible")]
    db.close()


def test_text_vector_proxy_maps_to_eligible_canonical_asset() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE vector_records(record_id TEXT, chunk_id TEXT, asset_id TEXT)"
    )
    db.execute("CREATE TABLE chunks(chunk_id TEXT, unit_id TEXT)")
    db.execute("CREATE TABLE asset_proxies(proxy_unit_id TEXT, asset_id TEXT)")
    db.execute("INSERT INTO vector_records VALUES ('record', 'chunk', NULL)")
    db.execute("INSERT INTO chunks VALUES ('chunk', 'proxy')")
    db.execute("INSERT INTO asset_proxies VALUES ('proxy', 'asset')")
    service = QueryService.__new__(QueryService)
    vars(service)["owner"] = SimpleNamespace(db=db)
    view = eligibility_view().model_copy(
        update={
            "vector_record_ids": frozenset({"record"}),
            "proxy_unit_ids": frozenset({"proxy"}),
            "asset_ids": frozenset({"asset"}),
        }
    )
    attempt = legacy_attempt(view)
    assert service._vector_owners(attempt, "unit") == {"record": ("asset", "asset")}
    db.close()


def test_image_query_descriptor_uses_registered_pixels_and_source_links(tmp_path):
    bundle = seed_bundle("", source_key="image.png")
    encoded = BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(encoded, format="PNG")
    pixels = artifact(encoded.getvalue(), "image/png")
    locator = ImageLocator(artifact_ref=pixels.sha256, native_ref="image.png")
    asset_id = digest(
        "asset-v1", bundle.extraction.extraction_id, "image.png", pixels.sha256
    )
    asset = AssetRow(
        asset_id=asset_id,
        extraction_id=bundle.extraction.extraction_id,
        artifact_sha=pixels.sha256,
        asset_type="image",
        native_ref="image.png",
        locator=locator,
    )
    unit = bundle.units[0].model_copy(
        update={"unit_type": "image_original", "locators": (locator,)}
    )
    bundle = bundle.model_copy(
        update={
            "units": (unit,),
            "locators": (bundle.locators[0].model_copy(update={"locator": locator}),),
            "assets": (asset,),
            "artifacts": (*bundle.artifacts, pixels),
            "revision_artifacts": (
                *bundle.revision_artifacts,
                RevisionArtifactRow(
                    revision_id=bundle.revision.revision_id,
                    role="sidecar",
                    source_ref="image.png",
                    artifact_sha=pixels.sha256,
                ),
            ),
            "unit_assets": (
                UnitAssetRow(unit_id=unit.unit_id, asset_id=asset_id, role="source"),
            ),
        }
    )
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    CorpusStore(db, registry=registry(), artifacts=reader()).commit_extraction(bundle)
    service = QueryService.__new__(QueryService)
    vars(service)["owner"] = SimpleNamespace(db=db, artifacts=reader())
    view = eligibility_view(
        revision_ids=frozenset({bundle.revision.revision_id}),
        context_unit_ids=frozenset({unit.unit_id}),
    ).model_copy(update={"asset_ids": frozenset({asset_id})})
    attempt = legacy_attempt(
        view,
        dependencies={
            asset_id: frozenset({asset_id, unit.unit_id, bundle.revision.revision_id})
        },
    )
    query = service._registered_image_query(asset_id, attempt)
    assert query.image.dependency_ids == frozenset({asset_id, unit.unit_id})
    assert query.image.pixel_sha256 == sha256(bytes((255, 0, 0)) * 4).hexdigest()
    with pytest.raises(TychoraError) as error:
        service._registered_image_query("unselected", attempt)
    assert error.value.code == "FORBIDDEN_SCOPE"
    db.close()


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_filter_change_retrieves_again_from_real_fts(tmp_path: Path) -> None:
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        source_service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        for project in ("alpha", "beta"):
            await source_service.add(
                SourceInput.from_mapping(
                    {
                        "type": "text",
                        "text": f"Verification evidence for {project}.",
                        "format": "text",
                        "kind": "manual@1",
                        "metadata": {
                            "title": f"{project} manual",
                            "publisher": "Example Team",
                            "edition": "1",
                            "scope": {"project": project},
                        },
                        "source_key": f"{project}.txt",
                        "collection_id": collection,
                    }
                ),
                scope,
            )

        await owner.run_write(lambda: _assert_project_facets(owner), ExecutionContext())
        service = QueryService(
            owner,
            output_counter=OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE")),
        )
        outputs = []
        for project in ("alpha", "beta"):
            result = await service.search(
                QueryRequest.from_mapping(
                    {
                        "question": "verification",
                        "filters": {
                            "op": "eq",
                            "field": "project",
                            "value": project,
                        },
                    }
                ),
                scope,
                ExecutionContext(),
            )
            outputs.append(result.result)
        assert outputs[0].run_id != outputs[1].run_id
        assert outputs[0].segments and outputs[1].segments
        assert not (
            {segment.source_id for segment in outputs[0].segments}
            & {segment.source_id for segment in outputs[1].segments}
        )
        limited = await service.search(
            QueryRequest.from_mapping({"question": "verification", "limit": 1}),
            scope,
            ExecutionContext(),
        )
        assert len(limited.result.segments) == 1
        limited_hit = await service.search(
            QueryRequest.from_mapping({"question": "verification", "limit": 1}),
            scope,
            ExecutionContext(),
        )
        assert len(limited_hit.result.segments) == 1
        assert limited_hit.result.run_id != limited.result.run_id
        with pytest.raises(TychoraError) as error:
            await service.replay(outputs[0].run_id, scope, ExecutionContext())
        assert error.value.code == "REPLAY_UNAVAILABLE"
        from tychora.adapters.sqlite.history import HistoryRepository

        opted = QueryService(
            owner,
            output_counter=service.counter,
            history=HistoryRepository(
                owner.db, artifacts=owner.artifacts, retain_payloads=True
            ),
        )
        retained = await opted.search(
            QueryRequest.from_mapping({"question": "verification", "exact_only": True}),
            scope,
            ExecutionContext(),
        )
        replayed = await opted.replay(retained.result.run_id, scope, ExecutionContext())
        assert replayed.result.run_id != retained.result.run_id
        assert replayed.result.snapshot_id == retained.result.snapshot_id
        assert replayed.result.segments == retained.result.segments
        common = QueryRequest.from_mapping({"question": "verification"})
        warmed = await service.search(common, scope, ExecutionContext())
        removed_source = outputs[0].segments[0].source_id
        assert removed_source in {s.source_id for s in warmed.result.segments}
        await owner.run_write(
            lambda: owner.store.withdraw_source(removed_source), ExecutionContext()
        )
        after_withdrawal = await service.search(common, scope, ExecutionContext())
        assert removed_source not in {
            segment.source_id for segment in after_withdrawal.result.segments
        }
        assert after_withdrawal.result.run_id != warmed.result.run_id
        remaining_source = outputs[1].segments[0].source_id

        class WithdrawingHistory(HistoryRepository):
            def __init__(self):
                super().__init__(owner.db, artifacts=owner.artifacts)
                self.did_withdraw = False

            def record(self, result, metrics, retained_payload):
                super().record(result, metrics, retained_payload)
                if not self.did_withdraw:
                    self.did_withdraw = True
                    owner.store.withdraw_source(remaining_source)

        timed_history = WithdrawingHistory()
        timed_service = QueryService(
            owner, output_counter=service.counter, history=timed_history
        )
        during_history = await timed_service.search(common, scope, ExecutionContext())
        assert remaining_source not in {
            segment.source_id for segment in during_history.result.segments
        }
        states = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT state FROM query_runs WHERE run_id=? OR "
                "(principal_id=? AND state='superseded') ORDER BY created_at",
                (during_history.result.run_id, scope.principal_id),
            ).fetchall(),
            ExecutionContext(),
        )
        assert ("superseded",) in states
        assert timed_history.did_withdraw


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_offline_degraded_retained_lexical_replay_does_not_require_reranker(
    tmp_path: Path,
) -> None:
    from tychora.adapters.sqlite.history import HistoryRepository

    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=true\n"
        "[configuration.retrieval]\nallow_degraded=true\n"
        '[configuration.providers.rerank]\nprovider="voyage"\n'
        'model="rerank-2.5"\ncredential_ref="env:UNUSED_TEST_CREDENTIAL"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                'VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        source_service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        await source_service.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Verification evidence for offline replay.",
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Offline manual",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "offline.txt",
                    "collection_id": collection,
                }
            ),
            scope,
        )
        history = HistoryRepository(
            owner.db, artifacts=owner.artifacts, retain_payloads=True
        )
        service = QueryService(
            owner,
            output_counter=OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE")),
            history=history,
        )
        request = QueryRequest.from_mapping({"question": "verification"})
        original = await service.search(request, scope, ExecutionContext())
        assert original.result.segments
        assert original.result.coverage.status == "partial"
        assert "rerank" in original.result.coverage.unavailable_channels
        replayed = await service.replay(
            original.result.run_id, scope, ExecutionContext()
        )
        assert replayed.result.segments
        assert replayed.result.run_id != original.result.run_id
        assert replayed.result.coverage.status == "partial"
        assert "rerank" in replayed.result.coverage.unavailable_channels


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_filter_change_reuses_authorized_query_vector_but_retrieves_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\ndimensions=2\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                (
                    collection,
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        counter = RecordingCounter()
        index = ChromaVectorIndex(tmp_path / "vectors")
        sources = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=counter,
                text_embedder=RecordingEmbedder(counter),
                text_index=index,
            ),
        )
        for project in ("alpha", "beta"):
            await sources.add(
                SourceInput.from_mapping(
                    {
                        "type": "text",
                        "text": f"Verification evidence for {project}.",
                        "format": "text",
                        "kind": "manual@1",
                        "metadata": {
                            "title": f"{project} manual",
                            "publisher": "Example Team",
                            "edition": "1",
                            "scope": {"project": project},
                        },
                        "source_key": f"{project}.txt",
                        "collection_id": collection,
                    }
                ),
                scope,
            )

        await owner.run_write(lambda: _assert_project_facets(owner), ExecutionContext())

        class Transport:
            def __init__(self) -> None:
                self.calls = 0

            def post_json(self, path, body, timeout):
                self.calls += 1
                return RawVoyageResponse(
                    200,
                    b'{"data":[{"index":0,"data":[{"index":0,"embedding":[1.0,0.0]}]}]}',
                )

            def close(self):
                pass

        class CountingLexical:
            def __init__(self):
                self.calls = 0
                self.original = SqliteLexicalSearch.search

            def search(self, adapter, view, query, limit, ctx):
                self.calls += 1
                return self.original(adapter, view, query, limit, ctx)

        transport = Transport()
        caches = QueryCaches()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=caches,
        )
        embedder = VoyageTextEmbedder(
            capabilities=RecordingEmbedder(counter).capabilities,
            counter=counter,
            runtime=runtime,
            authorize_document=lambda payload_sha, dependencies, ctx: None,
        )
        lexical = CountingLexical()
        monkeypatch.setattr(
            SqliteLexicalSearch,
            "search",
            lambda adapter, view, query, limit, ctx: lexical.search(
                adapter, view, query, limit, ctx
            ),
        )
        service = QueryService(
            owner,
            output_counter=OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE")),
            caches=caches,
            text_provider=VoyageTextQueryProvider(embedder),
            text_index=index,
        )
        results = []
        for project in ("alpha", "beta"):
            results.append(
                await service.search(
                    QueryRequest.from_mapping(
                        {
                            "question": "verification",
                            "filters": {
                                "op": "eq",
                                "field": "project",
                                "value": project,
                            },
                        }
                    ),
                    scope,
                    ExecutionContext(),
                )
            )
        assert transport.calls == 1
        assert lexical.calls == 2
        assert results[0].result.run_id != results[1].result.run_id
        assert not (
            {s.source_id for s in results[0].result.segments}
            & {s.source_id for s in results[1].result.segments}
        )
        repeated = await service.search(
            QueryRequest.from_mapping(
                {
                    "question": "verification",
                    "filters": {"op": "eq", "field": "project", "value": "alpha"},
                }
            ),
            scope,
            ExecutionContext(),
        )
        assert repeated.result.run_id != results[0].result.run_id
        assert repeated.byte_count == results[0].byte_count
        assert lexical.calls == 2
        assert "text" in results[0].result.coverage.completed_channels

        local = QueryService(
            owner,
            output_counter=service.counter,
            text_provider=LocalTextQueryProvider(
                owner, scope, embedder.capabilities, counter, caches
            ),
            text_index=index,
            caches=caches,
        )
        local_result = await local.search(
            QueryRequest.from_mapping(
                {
                    "question": "verification",
                    "filters": {"op": "eq", "field": "project", "value": "alpha"},
                }
            ),
            scope,
            ExecutionContext(),
        )
        assert local_result.result.segments
        assert transport.calls == 1

        from tychora.adapters.sqlite.history import HistoryRepository

        retained_service = QueryService(
            owner,
            output_counter=service.counter,
            text_provider=VoyageTextQueryProvider(embedder),
            text_index=index,
            caches=caches,
            history=HistoryRepository(
                owner.db, artifacts=owner.artifacts, retain_payloads=True
            ),
        )
        retained = await retained_service.search(
            QueryRequest.from_mapping({"question": "verification"}),
            scope,
            ExecutionContext(),
        )
        caches.purge()
        owner.config = config.model_copy(
            update={"values": config.values.model_copy(update={"offline": True})}
        )
        calls_before = transport.calls
        lexical_before = lexical.calls
        with pytest.raises(TychoraError) as unavailable:
            await retained_service.replay(
                retained.result.run_id, scope, ExecutionContext()
            )
        assert unavailable.value.code == "REPLAY_UNAVAILABLE"
        assert transport.calls == calls_before
        assert lexical.calls == lexical_before


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_image_only_query_uses_registered_original_and_visual_channel(
    tmp_path: Path,
) -> None:
    model_root = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    if model_root is None:
        pytest.skip("requires the provisioned Docling cache")
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\nmodel="voyage-multimodal-3.5"\ndimensions=1024\n'
        '[configuration.index.image]\nbackend="chroma"\n'
        '[configuration.extraction]\nparser="docling"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    image = BytesIO()
    Image.new("RGB", (256, 256), (20, 40, 60)).save(image, format="PNG")
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                (
                    collection,
                    owner.workspace_id,
                    "figures",
                    "Figures",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        caches = QueryCaches()
        transport = RecordingRawTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=caches,
        )
        provider = await create_visual_embedder(
            owner,
            capabilities=qualify_voyage_visual_capabilities(
                counter=CharacterCounter(), limits=_limits()
            ),
            counter=CharacterCounter(),
            runtime=runtime,
            scope=scope,
            ctx=ExecutionContext(),
        )
        index = ChromaVectorIndex(tmp_path / "images")
        sources = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
                visual_embedder=provider,
                image_index=index,
            ),
            docling_models=Path(model_root),
        )
        await sources.add(
            SourceInput.model_validate(
                {
                    "type": "bytes",
                    "data": image.getvalue(),
                    "format": "png",
                    "media_type": "image/png",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic Figure",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "figure.png",
                    "collection_id": UUID(collection),
                }
            ),
            scope,
        )
        asset_id = await owner.run_write(
            lambda: owner.db.execute("SELECT asset_id FROM assets").fetchone()[0],
            ExecutionContext(),
        )
        service = QueryService(
            owner,
            output_counter=OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE")),
            visual_provider=VoyageVisualQueryProvider(provider),
            visual_index=index,
            caches=caches,
        )
        before = len(transport.calls)
        result = await service.search(
            QueryRequest.from_mapping(
                {
                    "input": {"type": "image", "asset_ref": asset_id},
                    "mode": "lookup",
                    "modalities": ["image"],
                }
            ),
            scope,
            ExecutionContext(),
        )
        assert len(transport.calls) == before + 1
        assert [item.asset_id for item in result.result.visuals] == [asset_id]
        assert "visual" in result.result.coverage.completed_channels


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.asyncio
async def test_vector_only_tail_window_reaches_reranker_and_delivery(
    tmp_path, monkeypatch
):
    from tests.providers.test_reranker import LengthCounter, RecordingRerankTransport
    from tychora.application.reranking import _create_injected_reranker
    from tychora.contracts.vectors import CandidatePage, DenseCandidate

    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\ndimensions=2\n'
        '[configuration.providers.rerank]\nprovider="voyage"\nmodel="rerank-2.5"\ncredential_ref="env:VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                (
                    collection,
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        counter = RecordingCounter()
        index = ChromaVectorIndex(tmp_path / "vectors")
        sources = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=counter,
                text_embedder=RecordingEmbedder(counter),
                text_index=index,
            ),
        )

        text = "Unrelated prefix. " * 1200 + "TAIL_EVIDENCE"
        handle = await sources.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": text,
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic",
                        "publisher": "Example",
                        "edition": "1",
                    },
                    "source_key": "long",
                    "collection_id": collection,
                }
            ),
            scope,
        )
        assert handle.state == "succeeded"
        record_id, chunk_id, unit_id, start, _end = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT v.record_id,c.chunk_id,c.unit_id,c.char_start,c.char_end "
                "FROM vector_records v JOIN chunks c USING(chunk_id) ORDER BY c.char_start DESC LIMIT 1"
            ).fetchone(),
            ExecutionContext(),
        )
        assert start > 0
        prefix_record = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT v.record_id FROM vector_records v JOIN chunks c USING(chunk_id) ORDER BY c.char_start LIMIT 1"
            ).fetchone()[0],
            ExecutionContext(),
        )

        def tail_search(query, mapping, limit, work, ctx):
            assert mapping[record_id] == ("unit", unit_id)
            return CandidatePage(
                candidates=(
                    DenseCandidate(
                        key=("unit", unit_id),
                        distance=0.0,
                        record_ids=(record_id, prefix_record),
                    ),
                ),
                missing_record_ids=(),
                coverage_complete=True,
                truncated=False,
                exhausted=True,
                returned_records=1,
                queried_partitions=1,
            )

        monkeypatch.setattr(index, "search", tail_search)
        caches = QueryCaches()
        transport = RecordingRerankTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic",
        )
        rerank_counter = LengthCounter()
        reranker = await _create_injected_reranker(
            owner,
            runtime=runtime,
            scope=scope,
            counter=rerank_counter,
            ctx=ExecutionContext(),
        )
        service = QueryService(
            owner,
            output_counter=OutputTokenCounter(asset_path("TYCHORA_TEST_MODEL_CACHE")),
            text_provider=LocalTextQueryProvider(
                owner, scope, RecordingEmbedder(counter).capabilities, counter, caches
            ),
            text_index=index,
            reranker=reranker,
            rerank_counter=rerank_counter,
            caches=caches,
        )
        result = await service.search(
            QueryRequest.from_mapping({"question": "quasar", "modalities": ["text"]}),
            scope,
            ExecutionContext(),
        )
        assert len(transport.calls) == 1
        assert "TAIL_EVIDENCE" in transport.calls[0][1]["documents"][0]
        assert result.result.segments
        segment = result.result.segments[0]
        assert "TAIL_EVIDENCE" in segment.text
        assert any(
            anchor.indexed_window_id == chunk_id and anchor.char_start >= start
            for anchor in segment.anchors
        )

        # Shared ANN work exhaustion must remain an explicit strict-coverage
        # failure, even when it has already returned a usable source candidate.
        owner.config = owner.config.model_copy(
            update={
                "values": owner.config.values.model_copy(
                    update={
                        "retrieval": owner.config.values.retrieval.model_copy(
                            update={"allow_degraded": False}
                        )
                    }
                )
            }
        )

        def truncated_search(*args):
            return tail_search(*args).model_copy(
                update={"truncated": True, "exhausted": False}
            )

        monkeypatch.setattr(index, "search", truncated_search)
        with pytest.raises(TychoraError) as incomplete:
            await service.search(
                QueryRequest.from_mapping(
                    {"question": "nebula", "modalities": ["text"]}
                ),
                scope,
                ExecutionContext(),
            )
        assert incomplete.value.code == "INDEX_INCOMPLETE"
