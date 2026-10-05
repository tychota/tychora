"""Comparison admission drains publication without blocking query callbacks."""

import asyncio

import pytest

from tychora.contracts.operations import ConfigurationVariant
from tychora.contracts.requests import QueryRequest
from tychora.evaluation.dataset import GoldCase
from tychora.evaluation.runner import (
    ComparisonAdmission,
    EvaluationService,
    execution_plan,
    validate_retained_size,
    validate_workload,
    variant_query,
)


@pytest.mark.asyncio
async def test_comparison_drains_mutation_and_pauses_new_admission():
    gate = ComparisonAdmission()
    started = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def mutating():
        async with gate.mutation():
            started.set()
            await release.wait()

    first = asyncio.create_task(mutating())
    await started.wait()

    async def comparing():
        async with gate.comparison():
            entered.set()
            await release.wait()

    comparison = asyncio.create_task(comparing())
    await asyncio.sleep(0)
    assert not entered.is_set()
    release.set()
    await first
    await comparison
    assert entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_pending_comparison_releases_new_mutations():
    gate = ComparisonAdmission()
    held = asyncio.Event()
    release = asyncio.Event()

    async def mutating():
        async with gate.mutation():
            held.set()
            await release.wait()

    first = asyncio.create_task(mutating())
    await held.wait()

    async def comparing():
        async with gate.comparison():
            raise AssertionError("must drain before entry")

    comparison = asyncio.create_task(comparing())
    await asyncio.sleep(0)
    comparison.cancel()
    with pytest.raises(asyncio.CancelledError):
        await comparison
    release.set()
    await first
    async with gate.mutation():
        pass


@pytest.mark.asyncio
async def test_new_mutation_waits_during_comparison():
    gate = ComparisonAdmission()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def comparing():
        async with gate.comparison():
            entered.set()
            await release.wait()

    comparison = asyncio.create_task(comparing())
    await entered.wait()

    async def later_mutation():
        async with gate.mutation():
            return "entered"

    new_mutation = asyncio.create_task(later_mutation())
    await asyncio.sleep(0.03)
    assert not new_mutation.done()
    release.set()
    await comparison
    assert await new_mutation == "entered"


@pytest.mark.asyncio
async def test_second_comparison_and_direct_mutator_are_retryably_busy():
    from tychora.contracts.errors import TychoraError

    gate = ComparisonAdmission()
    async with gate.comparison():
        with pytest.raises(TychoraError) as second:
            async with gate.comparison():
                pass
        assert second.value.code == "WORKSPACE_BUSY" and second.value.retryable
        with pytest.raises(TychoraError) as direct, gate.mutation_nowait():
            pass
        assert direct.value.code == "WORKSPACE_BUSY" and direct.value.retryable


def test_variant_changes_only_declared_query_controls():
    case = GoldCase(
        case_id="case",
        family_key="family",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find evidence", "limit": 8}),
    )
    variant = ConfigurationVariant.from_mapping(
        {
            "name": "lexical",
            "profile": "test",
            "overrides": {"limit": 3, "exact_only": True},
        }
    )
    query = variant_query(case, variant, profile="test")
    assert query.limit == 3
    assert query.exact_only is True
    assert query.input == case.query.input


def test_variant_rejects_unsupported_override():
    case = GoldCase(
        case_id="case",
        family_key="family",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find evidence"}),
    )
    variant = ConfigurationVariant.from_mapping(
        {
            "name": "unsafe",
            "profile": "test",
            "overrides": {"question": "changed"},
        }
    )
    with pytest.raises(ValueError, match="override"):
        variant_query(case, variant, profile="test")


def test_six_ablation_plans_have_distinct_executed_channels():
    controls = {
        "lexical": {"channels": ["lexical"], "rerank": False},
        "hybrid": {"channels": ["lexical", "text"], "rerank": False},
        "reranked": {"channels": ["lexical", "text"], "rerank": True},
        "structural": {"channels": ["lexical", "structural_graph"], "rerank": False},
        "semantic": {"channels": ["lexical", "semantic_graph"], "rerank": False},
        "visual": {"channels": ["lexical", "image"], "rerank": False},
    }
    expected = {
        "lexical": (True, False, False, False, False, False),
        "hybrid": (True, True, False, False, False, False),
        "reranked": (True, True, False, False, False, True),
        "structural": (True, False, False, True, False, False),
        "semantic": (True, False, False, False, True, False),
        "visual": (True, False, True, False, False, False),
    }
    for name, overrides in controls.items():
        variant = ConfigurationVariant.from_mapping(
            {
                "name": name,
                "profile": "test",
                "overrides": overrides,
            }
        )
        plan = execution_plan(variant)
        assert (
            plan.lexical,
            plan.text,
            plan.image,
            plan.graph_structural,
            plan.graph_semantic,
            plan.rerank,
        ) == expected[name]


def test_graph_variant_without_explicit_seed_channel_is_rejected():
    case = GoldCase(
        case_id="case",
        family_key="family",
        split="test",
        query=QueryRequest.from_mapping({"question": "Find evidence"}),
    )
    variant = ConfigurationVariant.from_mapping(
        {
            "name": "unseeded",
            "profile": "test",
            "overrides": {"channels": ["structural_graph"]},
        }
    )
    with pytest.raises(ValueError, match="seed"):
        variant_query(case, variant, profile="test")


def test_aggregate_workload_limit_has_exact_boundary():
    from tychora.contracts.errors import TychoraError

    assert validate_workload(128, 2, 4) == 1024
    with pytest.raises(TychoraError, match="1024") as excess:
        validate_workload(129, 2, 4)
    assert excess.value.code == "LIMIT_EXCEEDED"


def test_retained_artifact_limit_has_exact_boundary():
    from tychora.contracts.errors import TychoraError

    assert validate_retained_size(64 * 1024 * 1024 - 1, 1) == 64 * 1024 * 1024
    with pytest.raises(TychoraError, match="64 MiB") as excess:
        validate_retained_size(64 * 1024 * 1024, 1)
    assert excess.value.code == "LIMIT_EXCEEDED"


def test_limit_error_maps_consistently_to_http_and_cli():
    from tychora.contracts.errors import TychoraError

    error = TychoraError("LIMIT_EXCEEDED", "Evaluation exceeds its limit")
    assert error.http_status == 413
    assert error.cli_exit_code == 2


def test_disabled_lexical_votes_do_not_escape_graph_seed_path(tmp_path, monkeypatch):
    from copy import copy

    from tychora import Tychora
    from tychora.application.query import QueryExecutionPlan
    from tychora.ports.execution import ExecutionContext

    class Counter:
        model = "synthetic-output"
        tokenizer_id = "cl100k_base"

        def count(self, value):
            return len(value)

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "synthetic-note",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "The saffron marker identifies the selected note.",
                "metadata": {
                    "title": "Synthetic note",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id, timeout=10).state == "succeeded"
        request = QueryRequest.from_mapping(
            {"question": "saffron marker", "modalities": ["lexical"]}
        )
        assert kb.search(request).segments

        async def forbidden_text(*args, **kwargs):
            raise AssertionError("disabled text provider executed")

        monkeypatch.setattr(
            "tychora.application.query.QueryService._text_candidates", forbidden_text
        )
        lexical_only = copy(kb._workspace.services.query)
        lexical_only.execution_plan = QueryExecutionPlan(
            True, False, False, False, False, False
        )
        text_requested = QueryRequest.from_mapping(
            {"question": "saffron marker", "modalities": ["lexical", "text"]}
        )
        assert kb._workspace.bridge.call(
            lexical_only.search(
                text_requested, kb._workspace.owner.scope, ExecutionContext()
            )
        ).result.segments

        def forbidden_lexical(*args, **kwargs):
            raise AssertionError("disabled lexical channel executed")

        monkeypatch.setattr(
            "tychora.application.query.retrieve_exact_lexical", forbidden_lexical
        )
        isolated = copy(kb._workspace.services.query)
        isolated.execution_plan = QueryExecutionPlan(
            False, False, False, False, False, False
        )
        result = kb._workspace.bridge.call(
            isolated.search(request, kb._workspace.owner.scope, ExecutionContext())
        )
        assert result.result.segments == ()


@pytest.mark.asyncio
async def test_runner_persists_each_variant_and_repetition(tmp_path):
    from io import BytesIO

    from tychora.adapters.sqlite.evaluations import EvaluationRepository
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.evidence import Coverage, Excerpt, QueryResult, Segment
    from tychora.contracts.identity import canonical_bytes
    from tychora.contracts.operations import EvaluationRequest
    from tychora.contracts.requests import RequestScope
    from tychora.evaluation.dataset import EvaluationDataset
    from tychora.ports.execution import ExecutionContext
    from tychora.retrieval.response import DeliveredResult

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="owner", capabilities=frozenset({"manage", "read"})
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:

        def seed():
            ref = owner.artifacts.put_stream(
                BytesIO(canonical_bytes({"type": "snapshot", "schema_version": "1"})),
                "application/json",
            )
            owner.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            owner.db.execute(
                "INSERT INTO snapshots (snapshot_id,workspace_id,membership_sha,config_sha,manifest_sha,published_at) VALUES (?,?,?,?,?,?)",
                (
                    "snapshot",
                    owner.workspace_id,
                    "membership",
                    "config",
                    ref.sha256,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            owner.db.execute("UPDATE workspaces SET active_snapshot_id='snapshot'")
            EvaluationRepository(owner).register(
                EvaluationDataset(
                    dataset_id="one-case",
                    name="Synthetic mechanics",
                    version_sha="a" * 64,
                    cases=(
                        GoldCase(
                            case_id="one",
                            family_key="one",
                            split="test",
                            query=QueryRequest.from_mapping(
                                {"question": "Find selected"}
                            ),
                            expected_unit_ids=frozenset({"selected"}),
                        ),
                    ),
                )
            )

        await owner.run_blocking(seed, ExecutionContext())

        class LocalQuery:
            counter = type(
                "Counter", (), {"model": "synthetic", "tokenizer_id": "synthetic@1"}
            )()
            text_provider = None
            visual_provider = None
            reranker = None

            async def search(self, query, grant, ctx):
                return DeliveredResult(
                    result=QueryResult(
                        run_id="query-run",
                        snapshot_id="snapshot",
                        applied_scope=grant,
                        coverage=Coverage(status="complete"),
                        segments=(
                            Segment(
                                segment_id="match",
                                source_id="source",
                                revision_id="revision",
                                extraction_id="extraction",
                                kind="text",
                                role="match",
                                lane="lexical",
                                text="selected",
                                matched_unit_ids=("selected",),
                                excerpts=(
                                    Excerpt(
                                        unit_id="selected",
                                        char_start=0,
                                        char_end=8,
                                        text="selected",
                                    ),
                                ),
                            ),
                        ),
                    ),
                    payloads=(),
                    token_count=1,
                    byte_count=8,
                )

        jobs = await owner.run_blocking(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ExecutionContext(),
        )
        service = EvaluationService(owner, LocalQuery(), jobs)
        request = EvaluationRequest(
            dataset_id="one-case",
            split="test",
            snapshot_id="snapshot",
            variants=(
                ConfigurationVariant.from_mapping(
                    {"name": "first", "profile": "test", "overrides": {"limit": 3}}
                ),
                ConfigurationVariant.from_mapping(
                    {"name": "second", "profile": "test", "overrides": {"limit": 5}}
                ),
            ),
            repetitions=2,
        )
        handle = await service.run(request, scope, ExecutionContext())
        assert handle.state == "succeeded"

        def inspect():
            row = owner.db.execute(
                "SELECT evaluation_id FROM evaluation_runs"
            ).fetchone()
            stored = EvaluationRepository(owner).get(row[0], scope, jobs)
            assert len(stored["cases"]["one"]["runs"]) == 4
            assert {r["variant"] for r in stored["cases"]["one"]["runs"]} == {
                "first",
                "second",
            }
            assert {r["repetition"] for r in stored["cases"]["one"]["runs"]} == {0, 1}
            assert stored["report"]["aggregate"]["metrics"]

        await owner.run_blocking(inspect, ExecutionContext())

        class PolicyChangingQuery(LocalQuery):
            async def search(self, query, grant, ctx):
                delivered = await super().search(query, grant, ctx)
                await owner.run_blocking(
                    lambda: owner.db.execute(
                        "UPDATE workspaces SET policy_epoch=policy_epoch+1"
                    ),
                    ctx,
                )
                return delivered

        changed = EvaluationService(owner, PolicyChangingQuery(), jobs)
        from tychora.contracts.errors import TychoraError

        with pytest.raises(TychoraError, match="epochs changed"):
            await changed.run(request, scope, ExecutionContext())
        assert (
            await owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT COUNT(*) FROM evaluation_runs"
                ).fetchone()[0],
                ExecutionContext(),
            )
            == 1
        )
