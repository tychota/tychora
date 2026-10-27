"""Strict synthetic reranker response tests."""

import inspect
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from tests.helpers.corpus import (
    COLLECTION,
    artifact,
    commit_with_facts,
    reader,
    registry,
    seed_bundle,
)
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteRepresentationRepository
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.rerank import (
    map_rerank_response,
    validate_indices,
    validate_rerank_request,
)
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_rerank
from tychora.application.owner import WorkspaceOwner
from tychora.application.reranking import (
    _create_injected_reranker,
    attest_ranking_input,
    create_reranker,
    rank_with_owner,
)
from tychora.application.setup import SetupService, load_voyage_tokenizer
from tychora.configuration.models import ProviderSettings, RerankerSettings
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import AssetRow, LocatorRow, UnitAssetRow
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import AnchorRange, ImageLocator
from tychora.contracts.identity import digest
from tychora.contracts.requests import RequestScope
from tychora.indexing.representations import prepare_extraction
from tychora.ingestion.assets import ProxyFactory
from tychora.ports.execution import ExecutionContext
from tychora.ports.reranking import RankingInput, RankingSpan, render_ranking_input


@pytest.mark.parametrize("indices", [(0, 0), (0, 2), (0,)])
def test_reranker_cannot_drop_duplicate_or_invent_candidates(indices):
    with pytest.raises(TychoraError):
        validate_indices(indices, 2)


def test_raw_rerank_mapping_preserves_original_indices_with_stable_score_order():
    body = json.dumps(
        {
            "data": [
                {"index": 2, "relevance_score": 0.8},
                {"index": 0, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.8},
            ]
        }
    ).encode()
    assert [(item.index, item.score) for item in map_rerank_response(body, 3)] == [
        (0, 0.9),
        (1, 0.8),
        (2, 0.8),
    ]


@pytest.mark.parametrize(
    "entries",
    [
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 2, "relevance_score": 0.2}],
        [
            {"index": 0, "relevance_score": float("nan")},
            {"index": 1, "relevance_score": 0.2},
        ],
        [
            {"index": 0, "relevance_score": 10**400},
            {"index": 1, "relevance_score": 0.2},
        ],
    ],
)
def test_raw_rerank_mapping_rejects_invalid_mapping_or_score(entries):
    with pytest.raises(TychoraError) as error:
        map_rerank_response(json.dumps({"data": entries}).encode(), 2)
    assert error.value.code == "PROVIDER_FAILURE"


def test_reranker_settings_are_disabled_by_default_and_require_explicit_model():
    assert ProviderSettings().rerank.provider == "disabled"
    with pytest.raises(ValidationError):
        RerankerSettings(provider="voyage", credential_ref="env:VOYAGE_KEY")
    with pytest.raises(ValidationError):
        RerankerSettings(provider="voyage", model="rerank-2.5")
    enabled = RerankerSettings(
        provider="voyage", model="rerank-2.5", credential_ref="env:VOYAGE_KEY"
    )
    assert enabled.model == "rerank-2.5"
    assert enabled.truncation is False


def test_preview_requires_its_own_qualified_capability():
    with pytest.raises(TychoraError) as error:
        qualify_voyage_rerank("rerank-3", "rerank-3:pinned")
    assert error.value.code == "CAPABILITY_MISSING"


def test_stable_capability_binds_model_tokenizer_and_engine_limits():
    with pytest.raises(TychoraError):
        qualify_voyage_rerank("rerank-2.5", "other:tokenizer")
    capability = qualify_voyage_rerank(
        "rerank-2.5", "rerank-2.5:5cf631991ecd82024c3dc64d8e34de27318d5b9e"
    )
    assert capability.max_query_tokens == 2000
    assert capability.max_representation_tokens == 1200
    assert capability.max_candidates == 64
    assert capability.max_pair_tokens == 32000
    assert capability.max_aggregate_tokens == 600000


def test_stable_tokenizer_has_own_pinned_selection_and_missing_asset_fails(
    tmp_path: Path,
):
    (asset,) = SetupService(tmp_path, offline=True).plan(("rerank-2.5",)).assets
    assert asset.revision == "5cf631991ecd82024c3dc64d8e34de27318d5b9e"
    assert (
        asset.sha256
        == "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"
    )
    with pytest.raises(TychoraError) as error:
        load_voyage_tokenizer(tmp_path, "rerank-2.5")
    assert error.value.code == "CAPABILITY_MISSING"


class LengthCounter:
    model = "rerank-2.5"
    tokenizer_id = "rerank-2.5:5cf631991ecd82024c3dc64d8e34de27318d5b9e"

    def count(self, text: str) -> int:
        return len(text)


def _input(text: str, token_count: int) -> RankingInput:
    span = RankingSpan(
        unit_id="unit-a",
        char_start=0,
        char_end=len(text),
        text=text,
        dependency_ids=frozenset({"unit-a"}),
    )
    return RankingInput(
        key=("unit", "unit-a"),
        source_kind="manual@1",
        title="Synthetic",
        heading_path=("Example",),
        spans=(span,),
        text=render_ranking_input("manual@1", "Synthetic", ("Example",), (span,)),
        dependency_ids=frozenset({"unit-a"}),
        token_count=token_count,
    )


def test_rerank_request_validates_actual_token_counts_and_repeated_query_budget():
    caps = qualify_voyage_rerank("rerank-2.5", LengthCounter.tokenizer_id)
    good = _input("source", len(_input("source", 1).text))
    validate_rerank_request("query", (good,), LengthCounter(), caps)
    with pytest.raises(TychoraError):
        validate_rerank_request("query", (good, good), LengthCounter(), caps)
    with pytest.raises(TychoraError) as error:
        validate_rerank_request("query", (_input("source", 5),), LengthCounter(), caps)
    assert error.value.code == "INVALID_REQUEST"
    with pytest.raises(TychoraError):
        validate_rerank_request("q" * 2001, (good,), LengthCounter(), caps)
    with pytest.raises(TychoraError):
        validate_rerank_request("q", (_input("x" * 1201, 1201),), LengthCounter(), caps)
    with pytest.raises(TychoraError) as broken_counter:
        validate_rerank_request(
            "query",
            (good,),
            type("BrokenCounter", (LengthCounter,), {"count": lambda self, text: -1})(),
            caps,
        )
    assert broken_counter.value.code == "CAPABILITY_MISSING"


def test_ranking_input_rejects_caller_authored_rendered_text():
    with pytest.raises(ValidationError):
        _input("source", 5).model_copy(update={"text": "injected"}).model_validate(
            _input("source", 5).model_dump() | {"text": "injected"}
        )


def _slice(db, bundle):
    """The persisted slice of every unit of the bundle's extraction."""
    return SqliteRepresentationRepository(db).slice(
        bundle.extraction.extraction_id,
        unit_ids=tuple(unit.unit_id for unit in bundle.units),
    )


def test_retained_representation_reuses_canonical_ownership(tmp_path: Path):
    bundle = seed_bundle("Exact synthetic source span.")
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        commit_with_facts(
            CorpusStore(db, registry=registry(), artifacts=reader()), bundle
        )
        assert _slice(db, bundle).prepared == prepare_extraction(
            bundle.model_copy(update={"code_files": ()})
        )
        assert not db.in_transaction
    finally:
        db.close()


def test_retained_code_reuses_validated_overlap_and_context_ownership(tmp_path: Path):
    from tests.code.test_structure import _registration
    from tychora.adapters.code.normalize import normalize_code, retain_code_extraction

    artifacts, registration, parsed = _registration(tmp_path)
    bundle = normalize_code(
        retain_code_extraction(registration, parsed, registry(), artifacts), registry()
    )
    db = open_corpus(tmp_path / "code.sqlite3", initialize=True)
    try:
        commit_with_facts(
            CorpusStore(db, registry=registry(), artifacts=artifacts), bundle
        )
        assert _slice(db, bundle).prepared == prepare_extraction(
            bundle.model_copy(update={"code_files": ()})
        )
    finally:
        db.close()


def test_exact_span_attestation_derives_complete_canonical_dependencies():
    bundle = seed_bundle("Exact synthetic source span.")
    prepared = prepare_extraction(bundle)
    unit = bundle.units[0]
    dependencies = frozenset({bundle.revision.revision_id, unit.unit_id})
    span = RankingSpan(
        unit_id=unit.unit_id,
        char_start=6,
        char_end=15,
        text=unit.text[6:15],
        dependency_ids=dependencies,
    )
    text = render_ranking_input(
        bundle.revision.kind_id, bundle.revision.title, (), (span,)
    )
    item = RankingInput(
        key=("unit", unit.unit_id),
        text=text,
        source_kind=bundle.revision.kind_id,
        title=bundle.revision.title,
        spans=(span,),
        dependency_ids=dependencies,
        token_count=len(text),
    )
    assert attest_ranking_input(item, bundle, prepared) == dependencies
    with pytest.raises(TychoraError):
        attest_ranking_input(
            item.model_copy(
                update={"spans": (span.model_copy(update={"text": "forged"}),)}
            ),
            bundle,
            prepared,
        )
    with pytest.raises(TychoraError) as nested:
        attest_ranking_input(
            item.model_copy(update={"spans": ({"unit_id": unit.unit_id},)}),
            bundle,
            prepared,
        )
    assert nested.value.code == "FORBIDDEN_SCOPE"


def test_anchor_must_be_fully_inside_an_uploaded_match_span():
    bundle = seed_bundle("An exact selected passage with surrounding source.")
    prepared = prepare_extraction(bundle)
    unit = bundle.units[0]
    span = RankingSpan(
        unit_id=unit.unit_id,
        char_start=9,
        char_end=17,
        text=unit.text[9:17],
        dependency_ids=frozenset({bundle.revision.revision_id, unit.unit_id}),
    )
    rendered = render_ranking_input(
        bundle.revision.kind_id, bundle.revision.title, (), (span,)
    )
    item = RankingInput(
        key=("unit", unit.unit_id),
        text=rendered,
        source_kind=bundle.revision.kind_id,
        title=bundle.revision.title,
        spans=(span,),
        dependency_ids=span.dependency_ids,
        token_count=len(rendered),
        anchors=(
            AnchorRange(
                unit_id=unit.unit_id,
                char_start=0,
                char_end=len(unit.text),
                origin="index_window",
            ),
        ),
    )
    with pytest.raises(TychoraError) as outside:
        attest_ranking_input(item, bundle, prepared)
    assert outside.value.code == "FORBIDDEN_SCOPE"
    contained = item.model_copy(
        update={
            "anchors": (
                AnchorRange(
                    unit_id=unit.unit_id,
                    char_start=10,
                    char_end=15,
                    origin="index_window",
                ),
            )
        }
    )
    assert attest_ranking_input(contained, bundle, prepared) == span.dependency_ids


def test_standard_reranker_factory_has_no_counter_injection_parameter():
    assert "counter" not in inspect.signature(create_reranker).parameters


def test_asset_candidate_requires_canonical_source_link_and_asset_dependency():
    bundle = seed_bundle("Exact linked caption.")
    unit = bundle.units[0]
    asset = AssetRow(
        asset_id="synthetic-asset",
        extraction_id=bundle.extraction.extraction_id,
        artifact_sha=bundle.revision.original_sha,
        asset_type="figure",
        native_ref="figure:1",
        locator=ImageLocator(
            artifact_ref=bundle.revision.original_sha, native_ref="figure:1"
        ),
        caption=unit.text,
    )
    linked = bundle.model_copy(
        update={
            "assets": (asset,),
            "unit_assets": (
                UnitAssetRow(
                    unit_id=unit.unit_id, asset_id=asset.asset_id, role="caption"
                ),
            ),
        }
    )
    prepared = prepare_extraction(linked)
    span_deps = frozenset({bundle.revision.revision_id, unit.unit_id})
    span = RankingSpan(
        unit_id=unit.unit_id,
        char_start=0,
        char_end=len(unit.text),
        text=unit.text,
        dependency_ids=span_deps,
    )
    rendered = render_ranking_input(
        bundle.revision.kind_id, bundle.revision.title, (), (span,)
    )
    item = RankingInput(
        key=("asset", asset.asset_id),
        text=rendered,
        source_kind=bundle.revision.kind_id,
        title=bundle.revision.title,
        spans=(span,),
        dependency_ids=span_deps | {asset.asset_id},
        token_count=len(rendered),
    )
    assert attest_ranking_input(item, linked, prepared) == item.dependency_ids
    with pytest.raises(TychoraError):
        attest_ranking_input(item, bundle, prepare_extraction(bundle))

    proxy = ProxyFactory(linked).make_proxy(
        asset.asset_id,
        "caption_copy",
        asset.caption,
        (unit.unit_id,),
        artifact(b"synthetic caption derivation", "text/plain"),
    )
    with_proxy = linked.model_copy(
        update={
            "artifacts": (*linked.artifacts, proxy.derivation),
            "units": (*linked.units, proxy.unit),
            "locators": (
                *linked.locators,
                LocatorRow(
                    locator_id=digest("locator-v1", proxy.unit.unit_id, 0),
                    unit_id=proxy.unit.unit_id,
                    fragment_ordinal=0,
                    locator=proxy.unit.locators[0],
                ),
            ),
            "proxies": (proxy.binding,),
        }
    )
    proxy_prepared = prepare_extraction(with_proxy)
    proxy_deps = frozenset(
        {bundle.revision.revision_id, unit.unit_id, proxy.unit.unit_id, asset.asset_id}
    )
    proxy_span = RankingSpan(
        unit_id=proxy.unit.unit_id,
        char_start=0,
        char_end=len(proxy.unit.text),
        text=proxy.unit.text,
        dependency_ids=proxy_deps,
    )
    proxy_rendered = render_ranking_input(
        bundle.revision.kind_id, bundle.revision.title, (), (proxy_span,)
    )
    proxy_item = RankingInput(
        key=("asset", asset.asset_id),
        text=proxy_rendered,
        source_kind=bundle.revision.kind_id,
        title=bundle.revision.title,
        spans=(proxy_span,),
        dependency_ids=proxy_deps,
        token_count=len(proxy_rendered),
    )
    assert attest_ranking_input(proxy_item, with_proxy, proxy_prepared) == proxy_deps


class RecordingRerankTransport:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.on_call = None

    def post_json(self, path, body, timeout):
        self.calls.append((path, json.loads(body)))
        if self.on_call is not None:
            self.on_call()
        if self.responses:
            return self.responses.pop(0)
        return RawVoyageResponse(200, b'{"data":[{"index":0,"relevance_score":0.75}]}')

    def close(self):
        pass


def _online_rerank_config_scope(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    settings = RerankerSettings(
        provider="voyage", model="rerank-2.5", credential_ref="env:VOYAGE_KEY"
    )
    providers = config.values.providers.model_copy(update={"rerank": settings})
    config = config.model_copy(
        update={
            "values": config.values.model_copy(
                update={
                    "offline": False,
                    "providers": providers,
                }
            )
        }
    )
    scope = RequestScope(
        principal_id="synthetic-principal",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    return config, scope


@pytest.mark.asyncio
async def test_standard_factory_rejects_absent_or_corrupt_pinned_asset(tmp_path: Path):
    config, scope = _online_rerank_config_scope(tmp_path)
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        constructed = []

        def transport_factory():
            constructed.append(True)
            return RecordingRerankTransport()

        runtime = VoyageRuntime(
            transport_factory,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        with pytest.raises(TypeError):
            await cast(Any, create_reranker)(
                owner,
                runtime=runtime,
                scope=scope,
                cache_root=tmp_path / "model-cache",
                counter=LengthCounter(),
                ctx=ExecutionContext(),
            )
        cache = tmp_path / "model-cache"
        with pytest.raises(TychoraError) as absent:
            await create_reranker(
                owner,
                runtime=runtime,
                scope=scope,
                cache_root=cache,
                ctx=ExecutionContext(),
            )
        assert absent.value.code == "CAPABILITY_MISSING"
        (asset,) = SetupService(cache, offline=True).plan(("rerank-2.5",)).assets
        wrong = cache / asset.path
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b"wrong tokenizer bytes")
        with pytest.raises(TychoraError) as corrupt:
            await create_reranker(
                owner,
                runtime=runtime,
                scope=scope,
                cache_root=cache,
                ctx=ExecutionContext(),
            )
        assert corrupt.value.code == "CAPABILITY_MISSING"
        assert constructed == []


@pytest.mark.asyncio
async def test_standard_factory_dispatches_with_verified_local_tokenizer(
    tmp_path: Path,
):
    root = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if root is None:
        pytest.skip("Set TYCHORA_TEST_MODEL_CACHE to verified public tokenizer assets")
    cache = Path(root)
    # An explicit test cache is a qualification promise. Missing or corrupt
    # selected reranker assets fail; runtime offline availability stays separate.
    load_voyage_tokenizer(cache, "rerank-2.5")
    config, scope = _online_rerank_config_scope(tmp_path)
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:

        def install():
            bundle = seed_bundle("Exact synthetic source span.")
            for ref in bundle.artifacts:
                with reader().open_verified(ref) as stream:
                    owner.artifacts.put_stream(stream, ref.media_type)
            created = owner.db.execute("SELECT created_at FROM workspaces").fetchone()[
                0
            ]
            bundle = bundle.model_copy(
                update={
                    "workspace": bundle.workspace.model_copy(
                        update={
                            "workspace_id": owner.workspace_id,
                            "created_at": created,
                        }
                    ),
                    "collection": bundle.collection.model_copy(
                        update={
                            "workspace_id": owner.workspace_id,
                            "policy_json": FrozenJsonMap(
                                {"schema_version": 1, "upload_providers": ["voyage"]}
                            ),
                        }
                    ),
                }
            )
            commit_with_facts(owner.store, bundle)
            return bundle

        bundle = await owner.run_write(install, ExecutionContext())
        unit = bundle.units[0]
        deps = frozenset({unit.unit_id, bundle.revision.revision_id})
        span = RankingSpan(
            unit_id=unit.unit_id,
            char_start=0,
            char_end=len(unit.text),
            text=unit.text,
            dependency_ids=deps,
        )
        text = render_ranking_input(
            bundle.revision.kind_id, bundle.revision.title, (), (span,)
        )
        tokenizer = load_voyage_tokenizer(cache, "rerank-2.5")
        item = RankingInput(
            key=("unit", unit.unit_id),
            text=text,
            source_kind=bundle.revision.kind_id,
            title=bundle.revision.title,
            spans=(span,),
            dependency_ids=deps,
            token_count=len(tokenizer.encode(text).ids),
        )
        transport = RecordingRerankTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        ctx = ExecutionContext()
        reranker = await create_reranker(
            owner, runtime=runtime, scope=scope, cache_root=cache, ctx=ctx
        )
        assert reranker._counter.count(text) == item.token_count
        assert item.token_count != len(text)
        ranked = await rank_with_owner(
            owner, reranker, "Find the café notes.", (item,), ctx
        )
        assert [(result.index, result.score) for result in ranked] == [(0, 0.75)]
        assert transport.calls[0][0] == "/v1/rerank"
        assert transport.calls[0][1]["documents"] == [text]
        wrong_count = item.model_copy(update={"token_count": len(text)})
        with pytest.raises(TychoraError) as count_error:
            await rank_with_owner(
                owner, reranker, "Find the café notes.", (wrong_count,), ctx
            )
        assert count_error.value.code == "INVALID_REQUEST"
        assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_owner_rerank_dispatches_only_attested_exact_text(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    providers = config.values.providers.model_copy(
        update={
            "rerank": RerankerSettings(
                provider="voyage", model="rerank-2.5", credential_ref="env:VOYAGE_KEY"
            )
        }
    )
    config = config.model_copy(
        update={
            "values": config.values.model_copy(
                update={"offline": False, "providers": providers}
            )
        }
    )
    scope = RequestScope(
        principal_id="synthetic-principal",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:

        def install():
            bundle = seed_bundle("Exact synthetic source span.")
            for ref in bundle.artifacts:
                with reader().open_verified(ref) as stream:
                    owner.artifacts.put_stream(stream, ref.media_type)
            created = owner.db.execute("SELECT created_at FROM workspaces").fetchone()[
                0
            ]
            bundle = bundle.model_copy(
                update={
                    "workspace": bundle.workspace.model_copy(
                        update={
                            "workspace_id": owner.workspace_id,
                            "created_at": created,
                        }
                    ),
                    "collection": bundle.collection.model_copy(
                        update={
                            "workspace_id": owner.workspace_id,
                            "policy_json": FrozenJsonMap(
                                {"schema_version": 1, "upload_providers": ["voyage"]}
                            ),
                        }
                    ),
                }
            )
            commit_with_facts(owner.store, bundle)
            return bundle

        bundle = await owner.run_write(install, ExecutionContext())
        unit = bundle.units[0]
        deps = frozenset({unit.unit_id, bundle.revision.revision_id})
        span = RankingSpan(
            unit_id=unit.unit_id,
            char_start=0,
            char_end=len(unit.text),
            text=unit.text,
            dependency_ids=deps,
        )
        text = render_ranking_input(
            bundle.revision.kind_id, bundle.revision.title, (), (span,)
        )
        item = RankingInput(
            key=("unit", unit.unit_id),
            text=text,
            source_kind=bundle.revision.kind_id,
            title=bundle.revision.title,
            spans=(span,),
            dependency_ids=deps,
            token_count=len(text),
        )
        transport = RecordingRerankTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        ctx = ExecutionContext()
        reranker = await _create_injected_reranker(
            owner, runtime=runtime, scope=scope, counter=LengthCounter(), ctx=ctx
        )
        result = await rank_with_owner(owner, reranker, "query", (item,), ctx)
        assert [(entry.index, entry.score) for entry in result] == [(0, 0.75)]
        assert transport.calls == [
            (
                "/v1/rerank",
                {
                    "model": "rerank-2.5",
                    "query": "query",
                    "documents": [text],
                    "top_k": 1,
                    "truncation": False,
                },
            )
        ]
        forged_context = RankingSpan(
            unit_id=unit.unit_id,
            char_start=0,
            char_end=5,
            text=unit.text[:5],
            role="context",
            dependency_ids=deps,
        )
        forged_text = render_ranking_input(
            bundle.revision.kind_id, bundle.revision.title, (), (span, forged_context)
        )
        forged = item.model_copy(
            update={
                "text": forged_text,
                "spans": (span, forged_context),
                "token_count": len(forged_text),
            }
        )
        with pytest.raises(TychoraError) as context_error:
            await rank_with_owner(owner, reranker, "query", (forged,), ctx)
        assert context_error.value.code == "FORBIDDEN_SCOPE"
        assert len(transport.calls) == 1

        transport.responses = [
            RawVoyageResponse(429, b"{}"),
            RawVoyageResponse(200, b'{"data":[{"index":0,"relevance_score":0.6}]}'),
        ]
        retried = await rank_with_owner(owner, reranker, "query", (item,), ctx)
        assert retried[0].score == 0.6
        assert len(transport.calls) == 3

        transport.responses = [
            RawVoyageResponse(200, b'{"data":[{"index":0,"relevance_score":null}]}')
        ]
        with pytest.raises(TychoraError) as bad_score:
            await rank_with_owner(owner, reranker, "query", (item,), ctx)
        assert bad_score.value.code == "PROVIDER_FAILURE"
        assert len(transport.calls) == 4

        transport.responses = [RawVoyageResponse(429, b"{}")]
        transport.on_call = lambda: owner.run_write_sync(
            lambda: owner.store.withdraw_source(bundle.source.source_id), ctx
        )
        with pytest.raises(TychoraError) as changed:
            await rank_with_owner(owner, reranker, "query", (item,), ctx)
        assert changed.value.code == "SCOPE_CHANGED"
        assert len(transport.calls) == 5


@pytest.mark.asyncio
async def test_offline_retains_explicit_remote_identity_without_runtime_construction(
    tmp_path: Path,
):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    settings = RerankerSettings(
        provider="voyage", model="rerank-2.5", credential_ref="env:VOYAGE_KEY"
    )
    config = config.model_copy(
        update={
            "values": config.values.model_copy(
                update={
                    "providers": config.values.providers.model_copy(
                        update={"rerank": settings}
                    )
                }
            )
        }
    )
    assert config.values.offline
    assert config.values.providers.rerank.model == "rerank-2.5"
    scope = RequestScope(
        principal_id="synthetic-principal",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        constructed = []

        def transport_factory():
            constructed.append(True)
            return RecordingRerankTransport()

        runtime = VoyageRuntime(
            transport_factory,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        with pytest.raises(TychoraError) as error:
            await create_reranker(
                owner,
                runtime=runtime,
                scope=scope,
                cache_root=tmp_path,
                ctx=ExecutionContext(),
            )
        assert error.value.code == "CAPABILITY_MISSING"
        assert constructed == []
