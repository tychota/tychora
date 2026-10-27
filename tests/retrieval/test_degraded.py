"""Rerank failure keeps typed fused evidence only when degradation is allowed."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.corpus import commit_with_facts, reader, seed_bundle
from tests.helpers.eligibility import legacy_attempt, revision_view
from tests.providers.test_reranker import (
    LengthCounter,
    RecordingRerankTransport,
    _online_rerank_config_scope,
)
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_rerank
from tychora.application.owner import WorkspaceOwner
from tychora.application.reranking import (
    _create_injected_reranker,
    create_reranker,
    prepare_rerank_facts,
)
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest, QuestionInput
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.admission import Admission
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import (
    RerankContext,
    RerankFacts,
    RerankMatch,
    rerank_candidates,
)


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
        view = LegacyEligibilityView(
            snapshot_id="synthetic-snapshot",
            policy_epoch=epoch,
            scope_digest=digest("synthetic-scope"),
            eligibility_policy_digest=digest("synthetic-policy"),
            eligibility_request_digest=digest("synthetic-request"),
            selected_batch_ids=(),
            revision_ids=frozenset(bundle.revision.revision_id for bundle in bundles),
            source_unit_ids=frozenset(bundle.units[0].unit_id for bundle in bundles),
            context_unit_ids=frozenset(bundle.units[0].unit_id for bundle in bundles),
            structural_unit_ids=frozenset(),
            proxy_unit_ids=frozenset(),
            asset_ids=frozenset(),
            chunk_ids=frozenset(bundle.chunks[0].chunk_id for bundle in bundles),
            vector_record_ids=frozenset(),
            blocked_assertion_ids=frozenset(),
        )
        execution = ExecutionContext()
        attempt = legacy_attempt(
            view,
            grants={
                bundle.revision.revision_id: bundle.extraction.extraction_id
                for bundle in bundles
            },
            dependencies={
                representation_id: frozenset(
                    {bundle.revision.revision_id, bundle.units[0].unit_id}
                )
                for bundle in bundles
                for representation_id in (
                    bundle.chunks[0].chunk_id,
                    bundle.units[0].unit_id,
                )
            },
            owner=owner,
            ctx=execution,
        )
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
                dependencies=attempt.legacy_dependencies,
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
