"""Exact inspection admits registered, currently readable source ranges."""

import asyncio
import json
from contextlib import asynccontextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image

from tests.helpers.corpus import raw_review, refresh_facts
from tests.retrieval.graph_fixture import assertion, sealed_graph, unit
from tests.typed_json import json_array, json_object, json_string
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


def _extraction_of(owner, unit_id: str) -> str:
    """The extraction that holds one unit."""
    return owner.db.execute(
        "SELECT extraction_id FROM evidence_units WHERE unit_id=?", (unit_id,)
    ).fetchone()[0]


@asynccontextmanager
async def _published(tmp_path: Path, before_add=None):
    """A workspace with one published synthetic manual and its first unit id.

    ``before_add`` is called once the workspace is open and before the manual is added.
    """
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
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                (
                    COLLECTION,
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    json.dumps({"schema_version": 1, "upload_providers": []}),
                ),
            ),
            ExecutionContext(),
        )
        if before_add is not None:
            before_add()
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
        uid = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT unit_id FROM evidence_units ORDER BY ordinal LIMIT 1"
            ).fetchone()[0],
            ExecutionContext(),
        )
        yield owner, scope, uid


@pytest.fixture
async def published(tmp_path: Path):
    """A fresh workspace for tests that insert rows or patch services."""
    async with _published(tmp_path) as value:
        yield value


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def published_shared(tmp_path_factory):
    """One workspace per module for tests that only read."""
    async with _published(tmp_path_factory.mktemp("published")) as value:
        yield value


@pytest.mark.asyncio(loop_scope="module")
async def test_exact_range_and_unknown_id(published_shared):
    owner, scope, uid = published_shared
    service = InspectionService(owner)
    result = await service.inspect(
        InspectRequest(
            subject="evidence", id=uid, range=CodepointRange(char_start=0, char_end=5)
        ),
        scope,
        ExecutionContext(),
    )
    assert result.evidence is None
    assert result.partial_evidence is not None
    assert (
        result.partial_evidence.canonical_text_sha
        == sha256(b"Exact source evidence.").hexdigest()
    )
    assert result.partial_evidence.canonical_text_length == len(
        "Exact source evidence."
    )
    assert [
        (item.char_start, item.char_end, item.text) for item in result.excerpts
    ] == [(0, 5, "Exact")]
    with pytest.raises(TychoraError) as error:
        await service.inspect(
            InspectRequest(subject="evidence", id="unknown"), scope, ExecutionContext()
        )
    assert error.value.code in {"NOT_FOUND", "FORBIDDEN_SCOPE"}


@pytest.mark.asyncio(loop_scope="module")
async def test_empty_selected_range_is_invalid_even_for_retained_unit(published_shared):
    owner, scope, uid = published_shared
    with pytest.raises(TychoraError) as error:
        await InspectionService(owner).inspect(
            InspectRequest(
                subject="evidence",
                id=uid,
                range=CodepointRange(char_start=0, char_end=0),
            ),
            scope,
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_native_overlap_denies_child_slice_and_allows_disjoint_parent_ranges(
    published,
):
    owner, scope, uid = published

    def add_rejected_child():
        owner.db.execute(
            "INSERT INTO evidence_units SELECT 'rejected-child',extraction_id,unit_id,"
            "ordinal+100,'paragraph','child',boundary_key,heading_path_json,"
            "'source',?,text_origin,citable,'unreviewed','{}' "
            "FROM evidence_units WHERE unit_id=?",
            (sha256(b"source").hexdigest(), uid),
        )
        owner.db.execute(
            "INSERT INTO locators SELECT 'child-locator','rejected-child',0,"
            "locator_type,artifact_sha,source_ref,physical_page,page_label,"
            "line_start,line_end,byte_start,byte_end,geometry_json,"
            "json_set(details_json,'$.char_start',6,'$.char_end',12) "
            "FROM locators WHERE unit_id=?",
            (uid,),
        )
        raw_review(owner.db, "unit", "rejected-child")
        refresh_facts(owner.store, _extraction_of(owner, uid))

    await owner.run_write(add_rejected_child, ExecutionContext())
    service = InspectionService(owner)
    with pytest.raises(TychoraError) as denied:
        await service.inspect(
            InspectRequest(
                subject="evidence",
                id=uid,
                range=CodepointRange(char_start=6, char_end=12),
            ),
            scope,
            ExecutionContext(),
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"
    allowed = await service.inspect(
        InspectRequest(
            subject="evidence",
            id=uid,
            ranges=(
                CodepointRange(char_start=0, char_end=6),
                CodepointRange(char_start=12, char_end=22),
            ),
        ),
        scope,
        ExecutionContext(),
    )
    assert [item.text for item in allowed.excerpts] == ["Exact ", " evidence."]
    assert allowed.partial_evidence is not None
    assert allowed.partial_evidence.canonical_text_sha is None
    assert allowed.partial_evidence.canonical_text_length is None
    assert (
        sha256(b"Exact source evidence.").hexdigest() not in allowed.model_dump_json()
    )


@pytest.mark.asyncio
async def test_retained_docling_source_parts_denies_rejected_child_overlap(published):
    owner, scope, uid = published

    def install_parts():
        extraction, revision, source_key, original_sha = owner.db.execute(
            "SELECT e.extraction_id,r.revision_id,s.source_key,r.original_sha "
            "FROM evidence_units u JOIN extractions e USING(extraction_id) "
            "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
            "WHERE u.unit_id=?",
            (uid,),
        ).fetchone()
        domain = digest(
            "document-source-domain-v1",
            revision,
            extraction,
            "#/texts/0",
            "text",
            None,
        )
        common = {
            "document_representation": "docling-source-parts@1",
            "source_refs": ["#/texts/0"],
            "structural_separators": [],
            "overlapping_owner_ids": [uid, "doc-child"],
        }

        def attributes(unit_start, unit_end, source_start, source_end):
            return json.dumps(
                {
                    **common,
                    "source_parts": [
                        {
                            "native_ref": "#/texts/0",
                            "field": "text",
                            "cell_index": None,
                            "unit_start": unit_start,
                            "unit_end": unit_end,
                            "source_start": source_start,
                            "source_end": source_end,
                            "source_domain": domain,
                            "provenance": [],
                        }
                    ],
                }
            )

        def locator(end):
            return json.dumps(
                {
                    "type": "document",
                    "artifact_ref": original_sha,
                    "native_ref": source_key,
                    "char_domain": "unit",
                    "char_start": 0,
                    "char_end": end,
                }
            )

        owner.db.execute(
            "UPDATE evidence_units SET attributes_json=? WHERE unit_id=?",
            (attributes(0, 22, 0, 22), uid),
        )
        owner.db.execute(
            "UPDATE locators SET locator_type='document',details_json=? WHERE unit_id=?",
            (locator(22), uid),
        )
        owner.db.execute(
            "INSERT INTO evidence_units SELECT 'doc-child',extraction_id,unit_id,"
            "ordinal+100,'paragraph','doc-child',boundary_key,heading_path_json,"
            "'source',?,text_origin,citable,'unreviewed',? "
            "FROM evidence_units WHERE unit_id=?",
            (sha256(b"source").hexdigest(), attributes(0, 6, 6, 12), uid),
        )
        owner.db.execute(
            "INSERT INTO locators SELECT 'doc-child-locator','doc-child',0,"
            "'document',artifact_sha,source_ref,physical_page,page_label,"
            "line_start,line_end,byte_start,byte_end,geometry_json,? "
            "FROM locators WHERE unit_id=?",
            (locator(6), uid),
        )
        raw_review(owner.db, "unit", "doc-child")
        refresh_facts(owner.store, _extraction_of(owner, uid))

    await owner.run_write(install_parts, ExecutionContext())
    service = InspectionService(owner)
    with pytest.raises(TychoraError) as denied:
        await service.inspect(
            InspectRequest(
                subject="evidence",
                id=uid,
                range=CodepointRange(char_start=6, char_end=12),
            ),
            scope,
            ExecutionContext(),
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"
    allowed = await service.inspect(
        InspectRequest(
            subject="evidence",
            id=uid,
            ranges=(
                CodepointRange(char_start=0, char_end=6),
                CodepointRange(char_start=12, char_end=22),
            ),
        ),
        scope,
        ExecutionContext(),
    )
    assert [item.text for item in allowed.excerpts] == ["Exact ", " evidence."]
    assert allowed.partial_evidence is not None
    assert allowed.partial_evidence.canonical_text_sha is None
    assert allowed.partial_evidence.canonical_text_length is None
    assert (
        sha256(b"Exact source evidence.").hexdigest() not in allowed.model_dump_json()
    )
    assert "doc-child" not in allowed.model_dump_json()


@pytest.mark.asyncio(loop_scope="module")
async def test_lookup_only_scope_can_inspect_its_source(published_shared):
    owner, scope, uid = published_shared
    lookup = scope.model_copy(update={"uses": frozenset({"lookup"})})
    result = await InspectionService(owner).inspect(
        InspectRequest(subject="evidence", id=uid, view="full"),
        lookup,
        ExecutionContext(),
    )
    assert result.evidence is not None
    assert "Exact source evidence." in result.evidence.text


@pytest.mark.asyncio(loop_scope="module")
async def test_denied_scope_cannot_inspect_registered_unit(published_shared):
    owner, scope, uid = published_shared
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


@pytest.mark.asyncio(loop_scope="module")
async def test_explicit_relations_require_authorized_seed_before_graph(
    published_shared,
):
    owner, scope, uid = published_shared
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
async def test_ready_native_relation_cursor_rejects_changed_query(
    published, tmp_path, monkeypatch
):
    owner, scope, _ = published
    case = sealed_graph(
        tmp_path,
        (unit("a", 0), unit("b", 1), unit("c", 2)),
        (
            assertion("a", "b", predicate="cites@1"),
            assertion("a", "c", predicate="cites@1"),
        ),
        resolve_graph_traversal_policy(owner.config, owner.store.registry),
    )
    service = InspectionService(owner, case.graph)
    view = case.view(context={"a", "b", "c"}, candidates={"a", "b", "c"})
    monkeypatch.setattr(
        service, "_attempt", lambda _snapshot, _scope: SimpleNamespace(view=view)
    )
    monkeypatch.setattr(
        service, "compiler", SimpleNamespace(revalidate_epoch=lambda *_: None)
    )
    first = await service.relations(
        RelationRequest(
            unit_ids=("a",), predicates=frozenset({"cites@1"}), max_hops=1, max_edges=1
        ),
        scope,
        ExecutionContext(),
    )
    assert len(first.paths) == 1
    assert first.next_cursor
    with pytest.raises(TychoraError) as changed:
        await service.relations(
            RelationRequest(
                unit_ids=("b",),
                predicates=frozenset({"cites@1"}),
                max_hops=1,
                max_edges=1,
                cursor=first.next_cursor,
            ),
            scope,
            ExecutionContext(),
        )
    assert changed.value.code == "INVALID_REQUEST"
    monkeypatch.setattr(
        service,
        "_attempt",
        lambda _snapshot, _scope: SimpleNamespace(
            view=view.model_copy(update={"graph_generation_id": "later-generation"})
        ),
    )
    with pytest.raises(TychoraError) as generation:
        await service.relations(
            RelationRequest(
                unit_ids=("a",),
                predicates=frozenset({"cites@1"}),
                max_hops=1,
                max_edges=1,
                cursor=first.next_cursor,
            ),
            scope,
            ExecutionContext(),
        )
    assert generation.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_ready_native_relation_inspection_stops_at_100_edges(
    published, tmp_path, monkeypatch
):
    owner, scope, _ = published
    targets = tuple(f"target-{i:03d}" for i in range(101))
    case = sealed_graph(
        tmp_path,
        (unit("seed", 0), *(unit(target, i + 1) for i, target in enumerate(targets))),
        tuple(assertion("seed", target, predicate="cites@1") for target in targets),
        resolve_graph_traversal_policy(owner.config, owner.store.registry),
    )
    service = InspectionService(owner, case.graph)
    eligible = {"seed", *targets}
    monkeypatch.setattr(
        service,
        "_attempt",
        lambda _snapshot, _scope: SimpleNamespace(
            view=case.view(context=eligible, candidates=eligible)
        ),
    )
    monkeypatch.setattr(
        service, "compiler", SimpleNamespace(revalidate_epoch=lambda *_: None)
    )
    result = await service.relations(
        RelationRequest(
            unit_ids=("seed",),
            predicates=frozenset({"cites@1"}),
            max_hops=1,
            max_edges=100,
        ),
        scope,
        ExecutionContext(),
    )
    assert len(result.paths) == 100
    assert result.truncated is True
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_removed_original_binding_blocks_read_even_while_hash_exists(published):
    owner, scope, _ = published
    revision, sha, job = await owner.run_write(
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
    await owner.run_write(
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
@pytest.mark.parametrize("historical", [False, True])
async def test_registered_zero_text_asset_and_unregistered_paths(published, historical):
    owner, scope, uid = published

    def register_image():
        image = Image.new("RGB", (1, 1), (20, 40, 60))
        stream = BytesIO()
        image.save(stream, format="PNG")
        payload = stream.getvalue()
        ref = owner.artifacts.put_stream(BytesIO(payload), "image/png")
        owner.db.execute(
            "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
                ref.storage_key,
                "2026-01-01T00:00:00Z",
            ),
        )
        extraction, revision = owner.db.execute(
            "SELECT e.extraction_id,e.revision_id FROM evidence_units u "
            "JOIN extractions e USING(extraction_id) WHERE u.unit_id=?",
            (uid,),
        ).fetchone()
        locator = json.dumps(
            {
                "type": "image",
                "artifact_ref": ref.sha256,
                "native_ref": "image.png",
            }
        )
        owner.db.execute(
            "INSERT INTO evidence_units SELECT 'image-unit',extraction_id,NULL,"
            "ordinal+100,'image_original','image.png',boundary_key,heading_path_json,"
            "'',?,text_origin,1,'unreviewed','{}' FROM evidence_units WHERE unit_id=?",
            (sha256(b"").hexdigest(), uid),
        )
        owner.db.execute(
            "INSERT INTO locators(locator_id,unit_id,fragment_ordinal,locator_type,"
            "artifact_sha,source_ref,details_json) VALUES (?,?,?,?,?,?,?)",
            (
                "image-locator",
                "image-unit",
                0,
                "image",
                ref.sha256,
                "image.png",
                locator,
            ),
        )
        owner.db.execute(
            "INSERT INTO assets(asset_id,extraction_id,artifact_sha,asset_type,"
            "native_ref,locator_json,declared_review_state) VALUES (?,?,?,?,?,?,?)",
            (
                "image-asset",
                extraction,
                ref.sha256,
                "image",
                "image.png",
                locator,
                "unreviewed",
            ),
        )
        owner.db.execute(
            "INSERT INTO unit_assets(unit_id,asset_id,role) VALUES (?,?,?)",
            ("image-unit", "image-asset", "source"),
        )
        refresh_facts(owner.store, extraction)
        return payload, revision, ref.sha256

    payload, revision, sha = await owner.run_write(register_image, ExecutionContext())
    if historical:
        service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=owner.config,
                counter=Counter(),
            ),
        )
        handle = await service.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Replacement text.",
                    "format": "text",
                    "kind": "manual@1",
                    "source_key": "manual.txt",
                    "collection_id": COLLECTION,
                    "metadata": {
                        "title": "Synthetic manual",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                }
            ),
            scope,
        )
        assert handle.state == "succeeded"
    service = InspectionService(owner)
    unit_result = await service.inspect(
        InspectRequest(subject="evidence", id="image-unit", view="full"),
        scope,
        ExecutionContext(),
    )
    assert unit_result.evidence is not None
    assert unit_result.evidence.text == ""
    asset_result = await service.inspect(
        InspectRequest(subject="assets", id="image-asset"), scope, ExecutionContext()
    )
    assert asset_result.payload == payload
    assert asset_result.media_type == "image/png"
    for forged in ("../image.png", sha):
        with pytest.raises(TychoraError) as missing:
            await service.inspect(
                InspectRequest(subject="assets", id=forged), scope, ExecutionContext()
            )
        assert missing.value.code == "NOT_FOUND"
    original = await service.inspect(
        InspectRequest(subject="revisions", id=revision, original=True),
        scope,
        ExecutionContext(),
    )
    assert original.payload == b"Exact source evidence."
    await owner.run_write(
        lambda: owner.db.execute(
            "INSERT INTO original_payload_tombstones VALUES (?,?,?,?)",
            (
                revision,
                sha,
                owner.db.execute("SELECT job_id FROM jobs LIMIT 1").fetchone()[0],
                "2026-01-01T00:00:00Z",
            ),
        ),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as removed:
        await service.inspect(
            InspectRequest(subject="assets", id="image-asset"),
            scope,
            ExecutionContext(),
        )
    assert removed.value.code == "ORIGINAL_REMOVED"


@pytest.mark.asyncio
async def test_full_original_cannot_expose_rejected_source_unit(published):
    owner, scope, uid = published
    revision = await owner.run_write(
        lambda: owner.db.execute(
            "SELECT revision_id FROM revisions LIMIT 1"
        ).fetchone()[0],
        ExecutionContext(),
    )
    await owner.run_write(
        lambda: raw_review(owner.db, "unit", uid),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as denied:
        await InspectionService(owner).inspect(
            InspectRequest(subject="revisions", id=revision, original=True),
            scope,
            ExecutionContext(),
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio(loop_scope="module")
async def test_discovery_counts_only_readable_collection_sources(published_shared):
    owner, scope, _ = published_shared
    discovery = DiscoveryService(owner.config, owner.store.registry, owner)
    result = await asyncio.to_thread(
        discovery.discover,
        DiscoverRequest(subject="kinds"),
        scope,
    )
    assert next(item.count for item in result.counts if item.name == "manual@1") == 1
    sources = await asyncio.to_thread(
        discovery.discover,
        DiscoverRequest(subject="sources"),
        scope,
    )
    assert len(sources.definitions) == 1
    revisions = json_array(sources.definitions[0].definition["revisions"])
    extractions = json_array(json_object(revisions[0])["extractions"])
    assert json_object(extractions[0], "stages")["extraction"] == "retained"
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
async def test_source_discovery_preserves_unextracted_source_and_revision_stages(
    published,
):
    owner, scope, _ = published

    def stage():
        owner.db.execute(
            "INSERT INTO sources SELECT 'awaiting',collection_id,'awaiting.txt',"
            "source_type,registration_json,access_floor,NULL,NULL,created_at "
            "FROM sources LIMIT 1"
        )
        owner.db.execute(
            "INSERT INTO revisions SELECT 'second-revision',source_id,kind_id,"
            "original_sha,'second-edition',metadata_sha,title,published_on,"
            "effective_from,effective_until,access_rank,declared_review_state,"
            "allowed_uses_json,metadata_json,captured_at FROM revisions LIMIT 1"
        )
        owner.db.execute(
            "INSERT INTO extractions SELECT 'alternate-extraction',revision_id,"
            "parser_fingerprint || '-alternate',normalizer_fingerprint,"
            "document_artifact_sha,assertions_artifact_sha,diagnostics_artifact_sha,"
            "created_at FROM extractions LIMIT 1"
        )

    await owner.run_write(stage, ExecutionContext())
    discovery = DiscoveryService(owner.config, owner.store.registry, owner)
    result = await asyncio.to_thread(
        discovery.discover,
        DiscoverRequest(subject="sources"),
        scope,
    )
    definitions = {entry.id: entry.definition for entry in result.definitions}
    assert (
        json_object(definitions["awaiting"], "stages")["registration"] == "registered"
    )
    assert definitions["awaiting"]["revisions"] == ()
    original_id = next(key for key in definitions if key != "awaiting")
    original = definitions[original_id]
    preview = json_array(original["revisions"])
    assert len(preview) == 1
    cursor = json_string(original["next_revision_cursor"])
    next_page = await asyncio.to_thread(
        discovery.source_revisions, original_id, scope, cursor=cursor, limit=1
    )
    assert len(next_page.revisions) == 1
    assert next_page.revisions[0].revision_id != json_object(preview[0])["revision_id"]
    full_page = await asyncio.to_thread(discovery.source_revisions, original_id, scope)
    revisions = full_page.revisions
    assert len(revisions) == 2
    assert [item.revision_id for item in revisions] == sorted(
        item.revision_id for item in revisions
    )
    assert (
        next(
            item for item in revisions if item.revision_id == "second-revision"
        ).extractions
        == ()
    )
    original_extractions = next(
        item for item in revisions if item.revision_id != "second-revision"
    ).extractions
    assert len(original_extractions) == 2
    assert [item.extraction_id for item in original_extractions] == sorted(
        item.extraction_id for item in original_extractions
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_job_inspection_uses_durable_authorization(published_shared):
    owner, scope, _ = published_shared
    job = await owner.run_write(
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
                '{"candidate_count":8675309421}',
                '{"unit_ids":["hidden"]}',
                None,
                "2026-01-01T00:00:00Z",
                "2027-01-01T00:00:00Z",
            ),
        )

    await owner.run_write(insert_run, ExecutionContext())
    result = await InspectionService(owner).inspect(
        InspectRequest(subject="runs", id="run-inspect"), scope, ExecutionContext()
    )
    assert result.records[0].value["state"] == "partial"
    # A long sentinel: short numbers can occur by chance inside random identifiers.
    assert "8675309421" not in result.model_dump_json()
    assert "hidden" not in result.model_dump_json()
    assert result.payload is None
    assert result.records[0].value["request_retained"] is False
    listing = await asyncio.to_thread(
        DiscoveryService(owner.config, owner.store.registry, owner).discover,
        DiscoverRequest(subject="runs"),
        scope,
    )
    assert [entry.id for entry in listing.definitions] == ["run-inspect"]
    assert listing.definitions[0].definition["state"] == "partial"
    assert "8675309421" not in listing.model_dump_json()
    assert "hidden" not in listing.model_dump_json()
    denied = scope.model_copy(update={"principal_id": "other"})
    with pytest.raises(TychoraError) as error:
        await asyncio.to_thread(
            DiscoveryService(owner.config, owner.store.registry, owner).discover,
            DiscoverRequest(subject="runs"),
            denied,
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


@pytest.mark.asyncio(loop_scope="module")
async def test_diagnostic_summary_is_scope_limited(published_shared):
    owner, scope, _ = published_shared
    extraction = await owner.run_write(
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
