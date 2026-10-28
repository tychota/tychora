"""Rerank failure keeps typed fused evidence only when degradation is allowed."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.corpus import commit_with_facts, reader, seed_bundle
from tests.helpers.eligibility import revision_attempt, revision_view
from tests.helpers.legacy import OutputCounter
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.providers.test_reranker import (
    LengthCounter,
    RecordingRerankTransport,
    _online_rerank_config_scope,
)
from tests.retrieval.test_result_cache import _record_puts
from tychora import ProviderBindings, Tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite import lexical as lexical_module
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_rerank
from tychora.api import LocalTextBinding
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import QueryService
from tychora.application.reranking import (
    _create_injected_reranker,
    create_reranker,
    prepare_rerank_facts,
)
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Coverage, CoverageGap
from tychora.contracts.requests import QueryRequest, QuestionInput
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.admission import Admission
from tychora.retrieval.coverage import build_coverage, missing_batch_slots
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import (
    RerankContext,
    RerankFacts,
    RerankMatch,
    rerank_candidates,
)
from tychora.retrieval.response import _delivered_coverage


class CharacterCounter:
    model = "rerank-2.5"
    tokenizer_id = "synthetic-character-counter"

    def count(self, text: str) -> int:
        return len(text)


def _case(*, offline: bool, degraded: bool, disabled: bool = False):
    bundle = seed_bundle("Selected exact evidence")
    unit = bundle.units[0]
    candidate = FusedCandidate(
        ("unit", unit.unit_id),
        1,
        1.0,
        {"lexical": 1},
        matching_window_ids=(bundle.chunks[0].chunk_id,),
    )
    admission = Admission((candidate,), (), {}, {}, (), {}, 63)
    owned = frozenset({unit.unit_id, bundle.revision.revision_id})
    facts = RerankFacts(
        bundle,
        prepare_extraction(bundle),
        (
            RerankMatch(
                bundle.chunks[0].chunk_id,
                unit.unit_id,
                0,
                len(unit.text),
                "lexical",
                1,
                1.0,
            ),
        ),
        dependencies={unit.unit_id: owned, bundle.chunks[0].chunk_id: owned},
    )
    attempt = SimpleNamespace(
        view=revision_view(
            {bundle.revision.revision_id: bundle.extraction.extraction_id}
        )
    )
    owner = SimpleNamespace(
        config=SimpleNamespace(
            values=SimpleNamespace(
                offline=offline,
                providers=SimpleNamespace(
                    rerank=SimpleNamespace(
                        provider="disabled" if disabled else "voyage"
                    )
                ),
            )
        )
    )
    # These branch-isolating doubles expose only the state read before any owner/provider call.
    context = RerankContext(
        owner=owner,  # ty: ignore[invalid-argument-type]
        attempt=attempt,  # ty: ignore[invalid-argument-type]
        reranker=None,
        counter=CharacterCounter(),
        facts={candidate.key: facts},
        execution=ExecutionContext(),
        allow_degraded=degraded,
    )
    return admission, context


@pytest.mark.asyncio
async def test_offline_never_constructs_or_calls_remote_reranker():
    admission, context = _case(offline=True, degraded=True)
    context = replace(context, counter=None)
    result = await rerank_candidates(
        QueryRequest(input=QuestionInput(text="query")),
        admission,
        context,
    )
    assert result.items[0].candidate is admission.selected[0]
    assert result.items[0].status == "unavailable"
    assert result.failure is not None
    assert result.failure.code == "CAPABILITY_MISSING"
    assert result.items[0].window_ids == admission.selected[0].matching_window_ids
    assert result.items[0].representation is None
    assert (
        result.items[0].anchors[0].indexed_window_id
        == admission.selected[0].matching_window_ids[0]
    )


@pytest.mark.asyncio
async def test_preview_unavailable_degrades_or_fails_by_policy():
    admission, context = _case(offline=False, degraded=True)
    with pytest.raises(TychoraError) as preview:
        qualify_voyage_rerank("rerank-3", "rerank-3:unqualified")
    context = replace(
        context,
        unavailable=preview.value,
        counter=None,
    )
    result = await rerank_candidates(
        QueryRequest(input=QuestionInput(text="query")),
        admission,
        context,
    )
    assert result.failure is not None
    assert result.failure.code == "CAPABILITY_MISSING"
    assert result.items[0].representation is None
    assert result.items[0].anchors[0].char_start == 0
    with pytest.raises(TychoraError) as error:
        await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            replace(context, allow_degraded=False),
        )
    assert error.value.code == "CAPABILITY_MISSING"


@pytest.mark.asyncio
async def test_declared_disabled_reranker_keeps_canonical_facts_without_counter():
    admission, context = _case(offline=True, degraded=False, disabled=True)
    result = await rerank_candidates(
        QueryRequest(input=QuestionInput(text="query")),
        admission,
        replace(context, counter=None),
    )
    assert result.failure is None
    assert result.items[0].status == "disabled"
    assert result.items[0].representation is None
    assert (
        result.items[0].source_matches
        == context.facts[admission.selected[0].key].matches
    )


@pytest.mark.asyncio
async def test_missing_verified_factory_asset_degrades_without_transport(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path):
        runtime = VoyageRuntime(
            lambda transport=transport: transport,
            workspace_id=context.owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        with pytest.raises(TychoraError) as missing:
            await create_reranker(
                context.owner,
                runtime=runtime,
                scope=context.owner.scope,
                cache_root=tmp_path / "absent-model-cache",
                ctx=context.execution,
            )
        assert missing.value.code == "CAPABILITY_MISSING"
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            replace(context, reranker=None, counter=None, unavailable=missing.value),
        )
        assert result.failure is not None
        assert result.failure.code == "CAPABILITY_MISSING"
        assert all(item.representation is None for item in result.items)
        assert transport.calls == []


@pytest.mark.asyncio
async def test_offline_factory_does_not_construct_transport(tmp_path):
    async for admission, context, _ in _owner_case(tmp_path):
        config = context.owner.config
        context.owner.config = config.model_copy(
            update={"values": config.values.model_copy(update={"offline": True})}
        )
        constructed: list[str] = []

        def forbidden_transport(recorded: list[str] = constructed):
            recorded.append("constructed")
            raise AssertionError("offline mode must not construct a transport")

        runtime = VoyageRuntime(
            forbidden_transport,
            workspace_id=context.owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        with pytest.raises(TychoraError) as unavailable:
            await create_reranker(
                context.owner,
                runtime=runtime,
                scope=context.owner.scope,
                cache_root=tmp_path / "absent-model-cache",
                ctx=context.execution,
            )
        assert unavailable.value.code == "CAPABILITY_MISSING"
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            replace(
                context, reranker=None, counter=None, unavailable=unavailable.value
            ),
        )
        assert result.failure is not None
        assert result.failure.code == "CAPABILITY_MISSING"
        assert constructed == []


@pytest.mark.asyncio
async def test_attempt_dependency_lookup_denial_blocks_before_provider():
    admission, context = _case(offline=False, degraded=True)
    key = admission.selected[0].key
    context.facts[key] = replace(context.facts[key], dependencies={})
    with pytest.raises(TychoraError) as error:
        await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_attempt_unit_dependency_denial_blocks_before_provider():
    admission, context = _case(offline=False, degraded=True)
    candidate = admission.selected[0]
    facts = context.facts[candidate.key]
    context.facts[candidate.key] = replace(
        facts,
        dependencies={
            window: dependencies
            for window, dependencies in facts.dependencies.items()
            if window != candidate.key[1]
        },
    )
    with pytest.raises(TychoraError) as error:
        await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


async def _owner_case(tmp_path: Path, *, upload: bool = True):
    config, scope = _online_rerank_config_scope(tmp_path)
    if not upload:
        scope = scope.model_copy(update={"upload_providers": frozenset()})
    owner_cm = WorkspaceOwner.open(tmp_path / "workspace", config, scope)
    owner = owner_cm.__enter__()
    try:

        def install():
            bundles = []
            for index in range(2):
                bundle = seed_bundle(
                    f"Synthetic match {index}", source_key=f"source-{index}.md"
                )
                for ref in bundle.artifacts:
                    with reader().open_verified(ref) as stream:
                        owner.artifacts.put_stream(stream, ref.media_type)
                created = owner.db.execute(
                    "SELECT created_at FROM workspaces"
                ).fetchone()[0]
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
                                    {
                                        "schema_version": 1,
                                        "upload_providers": ["voyage"],
                                    }
                                ),
                            }
                        ),
                    }
                )
                commit_with_facts(owner.store, bundle)
                bundles.append(bundle)
            epoch = owner.db.execute(
                "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
                (owner.workspace_id,),
            ).fetchone()[0]
            return tuple(bundles), epoch

        bundles, epoch = await owner.run_write(install, ExecutionContext())
        candidates = tuple(
            FusedCandidate(
                ("unit", bundle.units[0].unit_id),
                rank,
                1.0 / rank,
                {"lexical": rank},
                matching_window_ids=(bundle.chunks[0].chunk_id,),
            )
            for rank, bundle in enumerate(bundles, 1)
        )
        admission = Admission(candidates, (), {}, {}, (), {}, 62)
        execution = ExecutionContext()
        view = revision_view(
            {
                bundle.revision.revision_id: bundle.extraction.extraction_id
                for bundle in bundles
            },
            snapshot_id="synthetic-snapshot",
        ).model_copy(update={"policy_epoch": epoch})
        attempt = revision_attempt(view, owner=owner, ctx=execution)
        dependencies = {
            representation_id: frozenset(
                {bundle.revision.revision_id, bundle.units[0].unit_id}
            )
            for bundle in bundles
            for representation_id in (
                bundle.chunks[0].chunk_id,
                bundle.units[0].unit_id,
            )
        }
        transport = RecordingRerankTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
        )
        reranker = await _create_injected_reranker(
            owner, runtime=runtime, scope=scope, counter=LengthCounter(), ctx=execution
        )
        facts: dict[tuple[str, str], RerankFacts] = {
            candidate.key: RerankFacts(
                bundle,
                prepare_extraction(bundle),
                (
                    RerankMatch(
                        bundle.chunks[0].chunk_id,
                        bundle.units[0].unit_id,
                        0,
                        len(bundle.units[0].text),
                        "lexical",
                        candidate.rank,
                        1.0,
                    ),
                ),
                dependencies=dependencies,
            )
            for candidate, bundle in zip(candidates, bundles, strict=True)
        }
        context = RerankContext(
            owner, attempt, reranker, reranker._counter, facts, execution, True
        )
        yield admission, context, transport
    finally:
        owner_cm.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_owner_adapter_mapping_failure_degrades_without_fabricated_ranks(
    tmp_path,
):
    async for admission, context, transport in _owner_case(tmp_path):
        transport.responses = [
            RawVoyageResponse(
                200,
                b'{"data":[{"index":0,"relevance_score":0.9},'
                b'{"index":0,"relevance_score":0.8}]}',
            )
        ]
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
        assert result.failure is not None
        assert result.failure.code == "PROVIDER_FAILURE"
        assert [item.candidate.key for item in result.items] == [
            candidate.key for candidate in admission.selected
        ]
        assert all(item.rerank_rank is None for item in result.items)
        assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_strict_required_provider_failure_propagates(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path):
        transport.responses = [RawVoyageResponse(200, b'{"data":[]}')]
        with pytest.raises(TychoraError) as error:
            await rerank_candidates(
                QueryRequest(input=QuestionInput(text="query")),
                admission,
                replace(context, allow_degraded=False),
            )
        assert error.value.code == "PROVIDER_FAILURE"
        assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_native_asset_stays_independent_of_text_reranker():
    admission, context = _case(offline=False, degraded=False)
    asset = FusedCandidate(("asset", "asset-synthetic"), 1, 1.0, {"visual": 1})
    admission = replace(admission, selected=(asset,))
    result = await rerank_candidates(
        QueryRequest(input=QuestionInput(text="query")),
        admission,
        context,
    )
    assert result.failure is None
    assert result.items[0].candidate is asset
    assert result.items[0].status == "independent"
    assert result.items[0].rerank_score is None


@pytest.mark.asyncio
async def test_owner_adapter_upload_denial_never_sends_subset(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path, upload=False):
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
        assert result.failure is not None
        assert result.failure.code == "FORBIDDEN_SCOPE"
        assert len(result.items) == 2
        assert transport.calls == []


@pytest.mark.asyncio
async def test_successful_owner_mapping_orders_text_and_keeps_source_facts(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path):
        transport.responses = [
            RawVoyageResponse(
                200,
                b'{"data":[{"index":1,"relevance_score":0.9},'
                b'{"index":0,"relevance_score":0.8}]}',
            )
        ]
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
        assert result.failure is None
        assert [item.candidate.key for item in result.items] == [
            admission.selected[1].key,
            admission.selected[0].key,
        ]
        assert [item.rerank_rank for item in result.items] == [1, 2]
        assert (
            result.items[0].source_matches
            == context.facts[result.items[0].candidate.key].matches
        )


@pytest.mark.asyncio
async def test_owner_transport_timeout_degrades_without_fallback(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path):

        def timeout(path, body, timeout_seconds, recording=transport):
            recording.calls.append((path, body))
            raise TimeoutError("synthetic timeout")

        transport.post_json = timeout
        result = await rerank_candidates(
            QueryRequest(input=QuestionInput(text="query")),
            admission,
            context,
        )
        assert result.failure is not None
        assert result.failure.code == "PROVIDER_FAILURE"
        assert len(transport.calls) == 3
        assert all(item.status == "unavailable" for item in result.items)


@pytest.mark.asyncio
async def test_owner_prepares_matches_from_retained_chunks_and_channel_rows(tmp_path):
    async for admission, context, _ in _owner_case(tmp_path):
        channels = {
            "lexical": tuple(
                ChannelCandidate(
                    key=candidate.key,
                    rank=candidate.rank,
                    matching_window_ids=candidate.matching_window_ids,
                )
                for candidate in admission.selected
            )
        }
        prepared = await prepare_rerank_facts(
            context.owner, context.attempt, admission, channels, context.execution
        )
        assert set(prepared) == {candidate.key for candidate in admission.selected}
        for candidate in admission.selected:
            match = prepared[candidate.key].matches[0]
            assert match.window_id == candidate.matching_window_ids[0]
            assert match.char_start == 0
            assert match.char_end == len(prepared[candidate.key].rows.units[0].text)


@pytest.mark.asyncio
async def test_owner_rejects_forged_index_window_before_transport(tmp_path):
    async for admission, context, transport in _owner_case(tmp_path):
        candidate = admission.selected[0]
        forged_id = "forged-window-id"
        facts = dict(context.facts)
        # The forged window carries a readable set: only the store refuses it.
        facts[candidate.key] = replace(
            facts[candidate.key],
            matches=(replace(facts[candidate.key].matches[0], window_id=forged_id),),
            dependencies={
                **facts[candidate.key].dependencies,
                forged_id: facts[candidate.key].dependencies[
                    candidate.matching_window_ids[0]
                ],
            },
        )
        with pytest.raises(TychoraError) as error:
            await rerank_candidates(
                QueryRequest(input=QuestionInput(text="query")),
                admission,
                replace(context, facts=facts),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        assert transport.calls == []


def _add_text(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def _drop_batches(kb, slot):
    owner = kb._workspace.owner
    owner.run_write_sync(
        lambda: owner.db.execute("DELETE FROM page_batches WHERE slot=?", (slot,)),
        ExecutionContext(),
    )


_LOCAL_PROFILE = (
    'schema_version = 1\n[profiles.local]\nbuiltin = "general-documents@1"\n'
    '[defaults]\nprofile = "local"\n'
)


def _open_text_workspace(root: Path, *, degraded: bool) -> Tychora:
    """A workspace with a local text slot; its query provider is never reached."""
    settings = root / "workspace.toml"
    settings.write_text(
        _LOCAL_PROFILE + "[configuration]\noffline = true\n"
        f"[configuration.retrieval]\nallow_degraded = {str(degraded).lower()}\n"
        '[configuration.providers.text]\nprovider = "voyage"\n'
        'model = "voyage-context-4"\ndimensions = 2\n'
        '[configuration.index.text]\nbackend = "chroma"\n'
    )
    counter = RecordingCounter()
    return Tychora.open(
        root / "ws",
        config=resolve_config(settings, "local", {}),
        providers=ProviderBindings(
            local_text=LocalTextBinding(embedder=RecordingEmbedder(counter)),
            text_index=ChromaVectorIndex(root / "vectors"),
            text_counter=counter,
        ),
        token_counter=OutputCounter(),
    )


def test_truncated_channel_is_not_incomplete_in_strict_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(lexical_module, "WINDOW_LIMIT", 1)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for index in range(3):
            _add_text(
                kb,
                f"{index}.md",
                f"# Report {index}\n\nThe verification outcome is final.\n",
            )
        result = kb.search({"question": "verification outcome"})
    assert result.coverage.status == "complete"
    assert result.coverage.truncated_channels == ("lexical",)
    assert "lexical" not in result.coverage.unavailable_channels


def _twenty_audits(kb):
    for index in range(20):
        _add_text(
            kb,
            f"{index}.md",
            f"# Audit {index}\n\nRecord the verification outcome for item {index}.\n",
        )


def test_verification_outcome_succeeds_strict_on_20_documents(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _twenty_audits(kb)
        result = kb.search({"question": "verification outcome"})
    assert result.segments
    assert result.coverage.status == "complete"


def test_output_budget_omission_is_a_delivery_truncation(tmp_path):
    # One token a character: twenty owners overflow the 6,000-token budget.
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _twenty_audits(kb)
        result = kb.search({"question": "verification outcome"})
    assert result.coverage.status == "complete"
    assert "delivery" in result.coverage.truncated_channels
    assert [(gap.code, gap.channel) for gap in result.coverage_gaps] == [
        ("output_budget_omission", "delivery")
    ]


def test_unmet_requirement_stays_partial_beside_a_delivery_truncation():
    complete = Coverage(status="complete", completed_channels=("lexical",))
    delivery = CoverageGap(
        code="output_budget_omission",
        channel="delivery",
        message="selected_evidence_omitted",
    )
    unmet = CoverageGap(
        code="required_lane",
        channel="standard",
        message="not_selected_in_bounded_search",
    )
    alone = _delivered_coverage(complete, [delivery], False)
    assert (alone.status, alone.truncated_channels) == ("complete", ("delivery",))
    both = _delivered_coverage(complete, [delivery, unmet], False)
    assert (both.status, both.truncated_channels) == ("partial", ("delivery",))
    required = _delivered_coverage(complete, [unmet], False)
    assert (required.status, required.truncated_channels) == ("partial", ())
    checks = _delivered_coverage(complete, [], True)
    assert checks.status == "partial"


def test_missing_required_batch_is_incomplete(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        _drop_batches(kb, "lexical")
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "verification outcome"})
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )


def test_strict_exact_only_search_needs_no_lexical_batch(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        _drop_batches(kb, "lexical")
        result = kb.search({"question": "verification outcome", "exact_only": True})
    assert result.coverage.status == "complete"
    assert result.coverage.completed_channels == ("exact",)
    assert result.coverage.unavailable_channels == ()


def test_degraded_mode_skips_a_channel_with_a_missing_batch(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "workspace.toml").write_text(
        _LOCAL_PROFILE + "[configuration.retrieval]\nallow_degraded = true\n"
    )
    with Tychora.open(root, token_counter=OutputCounter()) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        _drop_batches(kb, "lexical")
        result = kb.search({"question": "verification outcome"})
        kb.search({"question": "verification outcome"})
    # Exact reads no lexical batch, so it still runs; lexical alone is skipped.
    assert result.coverage.status == "partial"
    assert result.coverage.completed_channels == ("exact",)
    assert result.coverage.unavailable_channels == ("lexical",)
    assert "lexical" not in result.coverage.truncated_channels
    # A failure-degraded result is never cached.
    assert stored == []


def test_missing_text_batch_is_refused_in_strict_mode(tmp_path):
    with _open_text_workspace(tmp_path, degraded=False) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        _drop_batches(kb, "text")
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "verification outcome"})
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )


def test_missing_text_batch_is_unavailable_in_degraded_mode(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    with _open_text_workspace(tmp_path, degraded=True) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        _drop_batches(kb, "text")
        result = kb.search({"question": "verification outcome"})
    assert result.coverage.status == "partial"
    assert result.coverage.completed_channels == ("exact", "lexical")
    assert result.coverage.unavailable_channels == ("text",)
    assert stored == []


def _service(*, degraded: bool) -> QueryService:
    service = QueryService.__new__(QueryService)
    retrieval = SimpleNamespace(allow_degraded=degraded)
    vars(service)["owner"] = SimpleNamespace(
        config=SimpleNamespace(values=SimpleNamespace(retrieval=retrieval))
    )
    return service


def test_missing_image_batch_is_refused_or_reported_as_visual():
    # A visual slot needs a qualified multimodal embedder at ingestion, so the
    # image slot is checked on a synthetic view through the same helper.
    view = revision_view(
        {"r1": "e1"},
        batches={"lexical": ("b1",)},
        batch_extractions={"lexical": ("e1",)},
    )
    with pytest.raises(TychoraError) as error:
        _service(degraded=False)._missing_batches(view, ("image",), [])
    assert error.value.reason == "batch.missing"
    incomplete: list[str] = []
    assert _service(degraded=True)._missing_batches(view, ("image",), incomplete) == [
        "visual"
    ]
    assert incomplete == ["visual"]


def test_missing_batch_slots_checks_each_eligible_extraction():
    view = revision_view(
        {"r1": "e1", "r2": "e2"},
        batches={"lexical": ("b1", "b2"), "text": ("t1", "t1")},
        batch_extractions={"lexical": ("e1", "e2"), "text": ("e1",)},
    )
    # Two text batch rows serve one extraction: a count would miss the gap.
    assert missing_batch_slots(view, ("lexical", "text")) == ("text",)


def test_graph_budget_truncation_keeps_coverage_complete():
    coverage = build_coverage(
        completed=("lexical",),
        unavailable=(),
        truncated=(),
        rerank_failed=False,
        graph_exhausted=False,
    )
    assert coverage.status == "complete"
    assert coverage.truncated_channels == ("graph",)
    failed = build_coverage(
        completed=("lexical",),
        unavailable=("text",),
        truncated=("lexical",),
        rerank_failed=True,
        graph_exhausted=True,
    )
    assert failed.status == "partial"
    assert failed.unavailable_channels == ("text", "rerank")
