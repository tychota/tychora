"""Exact inspection admits registered, currently readable source ranges."""

import asyncio
from pathlib import Path

import pytest

from tests.retrieval.graph_fixture import assertion, sealed_graph, unit
from tychora.adapters.ladybug.traversal import resolve_graph_traversal_policy
from tychora.application.discovery import DiscoveryService
from tychora.application.inspection import InspectionService
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts import (
    DiscoverRequest,
    InspectRequest,
    RelationRequest,
    RequestScope,
    SourceInput,
)
from tychora.contracts.base import CodepointRange
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphPredicatePolicy, GraphTraversalPolicy
from tychora.contracts.identity import digest
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


class Counter:
    model = "local"
    tokenizer_id = "provider@1"

    def count(self, text: str) -> int:
        return len(text.split())


@pytest.fixture
async def published(tmp_path: Path):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="reader",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
                (COLLECTION, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        await service.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Exact source evidence.",
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic manual",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "manual.txt",
                    "collection_id": COLLECTION,
                }
            ),
            scope,
        )
        uid = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT unit_id FROM evidence_units ORDER BY ordinal LIMIT 1"
            ).fetchone()[0],
            ExecutionContext(),
        )
        yield owner, scope, uid


@pytest.mark.asyncio
async def test_exact_range_and_unknown_id(published):
    owner, scope, uid = published
    service = InspectionService(owner)
    result = await service.inspect(
        InspectRequest(
            subject="evidence", id=uid, range=CodepointRange(char_start=0, char_end=5)
        ),
        scope,
        ExecutionContext(),
    )
    assert result.evidence is not None
    assert result.evidence.text == ""
    assert [
        (item.char_start, item.char_end, item.text) for item in result.excerpts
    ] == [(0, 5, "Exact")]
    with pytest.raises(TychoraError) as error:
        await service.inspect(
            InspectRequest(subject="evidence", id="unknown"), scope, ExecutionContext()
        )
    assert error.value.code in {"NOT_FOUND", "FORBIDDEN_SCOPE"}


@pytest.mark.asyncio
async def test_lookup_only_scope_can_inspect_its_source(published):
    owner, scope, uid = published
    lookup = scope.model_copy(update={"uses": frozenset({"lookup"})})
    result = await InspectionService(owner).inspect(
        InspectRequest(subject="evidence", id=uid, view="full"),
        lookup,
        ExecutionContext(),
    )
    assert result.evidence is not None
    assert "Exact source evidence." in result.evidence.text


@pytest.mark.asyncio
async def test_denied_scope_cannot_inspect_registered_unit(published):
    owner, scope, uid = published
    denied = scope.model_copy(update={"collection_ids": frozenset()})
    with pytest.raises(TychoraError) as error:
        await InspectionService(owner).inspect(
            InspectRequest(subject="evidence", id=uid, view="full"),
            denied,
            ExecutionContext(),
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


def test_relation_view_comes_from_registered_structural_semantics(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    config = resolve_config(settings, "test", {})
    policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
    categories = {item.predicate: item.view for item in policy.inspection_predicates}
    assert categories["contains@1"] == "structural"
    assert categories["cites@1"] == "semantic"


def test_disabled_structural_policy_does_not_advertise_structural_inspection(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    policy = resolve_graph_traversal_policy(config, Registry.from_config(config))
    assert not any(item.view == "structural" for item in policy.inspection_predicates)


def test_native_explicit_relation_view_and_direction_narrow_policy(tmp_path):
    policy = GraphTraversalPolicy(
        predicates=(),
        inspection_predicates=(
            GraphPredicatePolicy(
                predicate="cites@1", view="semantic", directions=("outgoing",)
            ),
            GraphPredicatePolicy(
                predicate="contains@1",
                view="structural",
                directions=("outgoing", "incoming"),
            ),
        ),
        semantic=True,
        structural="code_context@1",
    )
    case = sealed_graph(
        tmp_path,
        (unit("a", 0), unit("b", 1), unit("c", 2)),
        (
            assertion("a", "b", predicate="cites@1"),
            assertion("b", "c", predicate="contains@1"),
        ),
        policy,
    )
    view = case.view(context={"a", "b", "c"}, candidates={"a", "b", "c"})
    semantic = case.graph.get_assertions(
        view,
        ("a",),
        ("cites@1", "contains@1"),
        case.budget(),
        case.ctx,
        direction="both",
        relation_view="semantic",
    )
    assert [path.predicate for path in semantic.assertions] == ["cites@1"]
    incoming = case.graph.get_assertions(
        view,
        ("b",),
        ("cites@1",),
        case.budget(),
        case.ctx,
        direction="incoming",
        relation_view="semantic",
    )
    assert incoming.assertions == ()


@pytest.mark.asyncio
async def test_explicit_relations_require_authorized_seed_before_graph(published):
    owner, scope, uid = published
    service = InspectionService(owner)
    with pytest.raises(TychoraError) as denied:
        await service.relations(
            RelationRequest(unit_ids=("unknown",)), scope, ExecutionContext()
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"
    with pytest.raises(TychoraError) as missing:
        await service.relations(
            RelationRequest(unit_ids=(uid,)), scope, ExecutionContext()
        )
    assert missing.value.code == "INDEX_INCOMPLETE"


@pytest.mark.asyncio
async def test_removed_original_binding_blocks_read_even_while_hash_exists(published):
    owner, scope, _ = published
    revision, sha, job = await owner.run_blocking(
        lambda: owner.db.execute(
            "SELECT r.revision_id,r.original_sha,j.job_id FROM revisions r CROSS JOIN jobs j LIMIT 1"
        ).fetchone(),
        ExecutionContext(),
    )
    service = InspectionService(owner)
    request = InspectRequest(subject="revisions", id=revision, original=True)
    assert (
        await service.inspect(request, scope, ExecutionContext())
    ).payload == b"Exact source evidence."
    await owner.run_blocking(
        lambda: owner.db.execute(
            "INSERT INTO original_payload_tombstones VALUES (?,?,?,?)",
            (revision, sha, job, "2026-01-01T00:00:00Z"),
        ),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as removed:
        await service.inspect(request, scope, ExecutionContext())
    assert removed.value.code == "ORIGINAL_REMOVED"


@pytest.mark.asyncio
async def test_full_original_cannot_expose_rejected_source_unit(published):
    owner, scope, uid = published
    revision = await owner.run_blocking(
        lambda: owner.db.execute(
            "SELECT revision_id FROM revisions LIMIT 1"
        ).fetchone()[0],
        ExecutionContext(),
    )
    await owner.run_blocking(
        lambda: owner.db.execute(
            "INSERT INTO review_events(unit_id,state,actor_id,created_at) VALUES (?,'rejected','reviewer','2026-01-01T00:00:00Z')",
            (uid,),
        ),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as denied:
        await InspectionService(owner).inspect(
            InspectRequest(subject="revisions", id=revision, original=True),
            scope,
            ExecutionContext(),
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_discovery_counts_only_readable_collection_sources(published):
    owner, scope, _ = published
    result = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="kinds"),
        scope,
    )
    assert next(item.count for item in result.counts if item.name == "manual@1") == 1
    sources = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="sources"),
        scope,
    )
    assert len(sources.definitions) == 1
    assert sources.definitions[0].definition["stages"]["extraction"] == "retained"
    denied = scope.model_copy(update={"collection_ids": frozenset()})
    hidden = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="kinds"),
        denied,
    )
    assert next(item.count for item in hidden.counts if item.name == "manual@1") == 0
    hidden_sources = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="sources"),
        denied,
    )
    assert hidden_sources.definitions == ()
    no_read = scope.model_copy(update={"capabilities": frozenset({"write"})})
    no_read_result = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="kinds"),
        no_read,
    )
    assert (
        next(item.count for item in no_read_result.counts if item.name == "manual@1")
        == 0
    )


@pytest.mark.asyncio
async def test_job_inspection_uses_durable_authorization(published):
    owner, scope, _ = published
    job = await owner.run_blocking(
        lambda: owner.db.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()[0],
        ExecutionContext(),
    )
    service = InspectionService(owner)
    result = await service.inspect(
        InspectRequest(subject="jobs", id=job), scope, ExecutionContext()
    )
    assert result.records[0].record_type == "job_summary"
    denied = scope.model_copy(update={"collection_ids": frozenset()})
    with pytest.raises(TychoraError) as error:
        await service.inspect(
            InspectRequest(subject="jobs", id=job), denied, ExecutionContext()
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio
async def test_metadata_only_run_remains_inspectable_without_replay(published):
    owner, scope, _ = published

    def insert_run():
        snapshot, config_sha = owner.db.execute(
            "SELECT snapshot_id,config_sha FROM snapshots LIMIT 1"
        ).fetchone()
        owner.db.execute(
            "INSERT INTO query_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-inspect",
                snapshot,
                scope.principal_id,
                digest("query-run-scope-v1", scope.model_dump(mode="json")),
                0,
                0,
                config_sha,
                "partial",
                '{"candidate_count":999}',
                '{"unit_ids":["hidden"]}',
                None,
                "2026-01-01T00:00:00Z",
                "2027-01-01T00:00:00Z",
            ),
        )

    await owner.run_blocking(insert_run, ExecutionContext())
    result = await InspectionService(owner).inspect(
        InspectRequest(subject="runs", id="run-inspect"), scope, ExecutionContext()
    )
    assert result.records[0].value["state"] == "partial"
    assert "999" not in result.model_dump_json()
    assert "hidden" not in result.model_dump_json()
    assert result.payload is None
    assert result.records[0].value["request_retained"] is False


@pytest.mark.asyncio
async def test_diagnostic_summary_is_scope_limited(published):
    owner, scope, _ = published
    extraction = await owner.run_blocking(
        lambda: owner.db.execute(
            "SELECT extraction_id FROM extractions LIMIT 1"
        ).fetchone()[0],
        ExecutionContext(),
    )
    result = await InspectionService(owner).inspect(
        InspectRequest(subject="diagnostics", id=extraction), scope, ExecutionContext()
    )
    assert result.records[0].value["scope_limited"] is True
    denied = scope.model_copy(update={"collection_ids": frozenset()})
    with pytest.raises(TychoraError) as error:
        await InspectionService(owner).inspect(
            InspectRequest(subject="diagnostics", id=extraction),
            denied,
            ExecutionContext(),
        )
    assert error.value.code == "FORBIDDEN_SCOPE"
