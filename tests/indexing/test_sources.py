"""Source service through the actual owner, capture, parser and canonical store."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from PIL import Image

from tests.code.conftest import make_repo
from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.providers.test_visual import CharacterCounter, RecordingRawTransport, _limits
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import EmbeddingCapabilities, VisualEmbeddingLimits
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import canonical_bytes, space_id
from tychora.contracts.publication import PublicationPlan
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.contracts.vectors import BatchReceipt
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext
from tychora.ports.jobs import JobRepository


class Counter:
    model = "local"
    tokenizer_id = "local-codepoints@1"

    def count(self, text: str) -> int:
        return len(text)


class EmptyVisualProvider:
    def __init__(self):
        self.capabilities = EmbeddingCapabilities(
            provider="voyage",
            endpoint="recording",
            model="voyage-multimodal-3.5",
            dimensions=2,
            input_types=frozenset({"document", "query"}),
            contextualization="independent",
            tokenizer_id="local-codepoints@1",
            space_id=space_id(
                "visual", "voyage", "voyage-multimodal-3.5", 2, "cosine", "b" * 64
            ),
            representation_fingerprint="b" * 64,
            modality="visual",
            max_group_tokens=24000,
            max_request_tokens=28000,
            visual_limits=VisualEmbeddingLimits(
                accounting_version="voyage-multimodal-accounting-v1",
                model_revision="test",
                tokenizer_sha256="a" * 64,
                client_config_sha256="b" * 64,
                accounting_image_pixels_min=1,
                accounting_image_pixels_max=100,
                image_pixels_per_token=1,
                source_pixels_max=100,
                image_encoded_bytes_max=1000,
                input_tokens_max=100,
                request_assets_max=1,
                request_tokens_max=100,
                request_wire_bytes_max=1000,
                decoded_image_bytes_max=300,
            ),
        )

    def embed_assets(self, items, ctx):
        raise AssertionError("Empty image batch must not dispatch a provider")

    def probe_query(self, query, ctx):
        raise AssertionError("Empty image batch must not probe a query")

    def embed_query(self, query, ctx):
        raise AssertionError("Empty image batch must not embed a query")

    @property
    def _access(self) -> "_NoVisualAccess":
        return _NoVisualAccess()


class _NoVisualAccess:
    @property
    def scope(self) -> RequestScope:
        raise AssertionError("Empty image batch must not inspect query scope")

    @property
    def epoch(self) -> int:
        raise AssertionError("Empty image batch must not inspect policy epoch")

    def binding(self, owner: object, scope: RequestScope) -> None:
        raise AssertionError("Empty image batch must not bind query access")

    def operation(self, query_or_items, mode):
        raise AssertionError("Empty image batch must not prepare visual operation")

    def authorize(self, query_or_items, mode, ctx, *, body_sha=None):
        raise AssertionError("Empty image batch must not authorize visual operation")

    def admitted(self):
        raise AssertionError("Empty image batch must not admit visual operation")


async def _slots(owner, view):
    """Read a snapshot's retained slots on the owner thread."""
    return await owner.run_write(
        lambda: owner.store.snapshot_slots(view), ExecutionContext()
    )


def _jobs(service: SourceService) -> JobRepository:
    jobs = service._jobs
    assert jobs is not None
    return jobs


@pytest.mark.asyncio
async def test_add_and_sync_native_source_reuse_canonical_extraction(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
        )
        service = SourceService(owner, coordinator)
        source = SourceInput.from_mapping(
            {
                "type": "text",
                "text": "A synthetic source.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {
                    "title": "Synthetic Manual",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "source_key": "manual.txt",
                "collection_id": collection,
            }
        )
        first = await service.add(source, scope)
        first_view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        second = await service.sync(first_view.members[0].source_id, scope)
        second_view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert first.id != second.id
        assert first_view.members == second_view.members
        assert first_view.snapshot_id != second_view.snapshot_id
        assert (
            await owner.run_write(
                lambda: owner.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[
                    0
                ],
                ExecutionContext(),
            )
            == 1
        )


_COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


def _text_source(key: str) -> SourceInput:
    """A one-paragraph text source in the test collection."""
    return SourceInput.from_mapping(
        {
            "type": "text",
            "text": f"Synthetic source {key}.",
            "format": "text",
            "kind": "manual@1",
            "metadata": {"title": key, "publisher": "Example Team", "edition": "1"},
            "source_key": f"{key}.txt",
            "collection_id": _COLLECTION,
        }
    )


@asynccontextmanager
async def _text_workspace(tmp_path):
    """An owner with one collection and a lexical-only source service."""
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({_COLLECTION}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (_COLLECTION, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
        )
        yield owner, SourceService(owner, coordinator), coordinator, scope


@pytest.mark.asyncio
async def test_projection_leases_its_base_and_bound_batches_until_publication(
    tmp_path, monkeypatch
):
    text = _text_source
    async with _text_workspace(tmp_path) as (owner, service, coordinator, scope):
        service = SourceService(owner, coordinator)
        await service.add(text("first"), scope)
        base = await owner.run_write(owner.store.active_snapshot_id, ExecutionContext())
        assert base is not None
        observed = {}
        original_finalize = coordinator.finalize
        original_publish = owner.store.publish_snapshot

        def finalize(prepared, *args):
            observed["base_at_finalize"] = owner.leases.is_pinned(base)
            return original_finalize(prepared, *args)

        def publish(plan):
            bound = {
                batch.batch_id
                for page in plan.new_pages
                for member in page.members
                for batch in member.batches
            }
            observed["bound_before_publish"] = all(
                owner.leases.is_pinned(batch_id) for batch_id in bound
            )
            observed["base_before_publish"] = owner.leases.is_pinned(base)
            return original_publish(plan)

        monkeypatch.setattr(coordinator, "finalize", finalize)
        monkeypatch.setattr(owner.store, "publish_snapshot", publish)
        await service.add(text("second"), scope)
        assert observed == {
            "base_at_finalize": True,
            "bound_before_publish": True,
            "base_before_publish": True,
        }
        assert not owner.leases.any_pinned()


@pytest.mark.asyncio
async def test_interrupted_activation_resumes_the_checkpointed_plan_unchanged(
    tmp_path, monkeypatch
):
    async with _text_workspace(tmp_path) as (owner, service, coordinator, scope):
        await service.add(_text_source("first"), scope)
        original_publish = owner.store.publish_snapshot

        def killed(plan):
            monkeypatch.setattr(owner.store, "publish_snapshot", original_publish)
            raise TychoraError(
                "PROVIDER_FAILURE", "Killed before activation", retryable=True
            )

        monkeypatch.setattr(owner.store, "publish_snapshot", killed)
        with pytest.raises(TychoraError, match="Killed before activation"):
            await service.add(_text_source("second"), scope, "resume-second")

        def checkpointed():
            job_id, sha = owner.db.execute(
                "SELECT s.job_id,s.output_artifact_sha FROM job_steps s JOIN jobs j "
                "USING(job_id) WHERE j.idempotency_key='resume-second' "
                "AND s.step_key LIKE 'projection/plan/%'"
            ).fetchone()
            row = owner.db.execute(
                "SELECT size_bytes,media_type,storage_key FROM artifacts "
                "WHERE artifact_sha=?",
                (sha,),
            ).fetchone()
            ref = ArtifactRef(
                sha256=sha, size_bytes=row[0], media_type=row[1], storage_key=row[2]
            )
            with owner.artifacts.open_verified(ref) as stream:
                document = json.load(stream)
            assert document["type"] == "publication_plan"
            return job_id, PublicationPlan.model_validate_json(
                canonical_bytes(document["data"])
            )

        job_id, plan = await owner.run_write(checkpointed, ExecutionContext())
        await owner.run_write(lambda: _jobs(service).retry(job_id), ExecutionContext())

        def no_prepare(*args):
            raise AssertionError("a resumed job must not prepare again")

        monkeypatch.setattr(coordinator, "prepare", no_prepare)
        resumed = await service.add(_text_source("second"), scope, "resume-second")
        assert (resumed.id, resumed.state) == (job_id, "succeeded")
        active = await owner.run_write(
            owner.store.active_snapshot_id, ExecutionContext()
        )
        assert active == plan.snapshot_id


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_image_slot_publishes_required_empty_batch(tmp_path, monkeypatch, legacy):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\nmodel="voyage-multimodal-3.5"\ndimensions=2\n'
        '[configuration.index.image]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        image_index = ChromaVectorIndex(tmp_path / "images")
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
            visual_embedder=EmptyVisualProvider(),
            image_index=image_index,
        )
        if legacy:
            original_family = coordinator._image_family

            def preproof_family(*args):
                family, spec = original_family(*args)
                old_json = {
                    key: value
                    for key, value in family.config_json.items()
                    if key != "capabilities"
                }
                # A pre-proof family row was stored without its capability proof.
                owner.store.db.execute(
                    "UPDATE index_families SET config_json=? WHERE family_id=?",
                    (canonical_bytes(old_json).decode(), family.family_id),
                )
                return family.model_copy(
                    update={"config_json": FrozenJsonMap(old_json)}
                ), spec

            monkeypatch.setattr(coordinator, "_image_family", preproof_family)
        source_data = {
            "type": "text",
            "text": "No images here.",
            "format": "text",
            "kind": "manual@1",
            "metadata": {
                "title": "Synthetic Manual",
                "publisher": "Example Team",
                "edition": "1",
            },
            "source_key": "manual.txt",
            "collection_id": collection,
        }
        source = SourceInput.from_mapping(source_data)
        service = SourceService(owner, coordinator)
        await service.add(source, scope)
        if legacy:
            monkeypatch.setattr(coordinator, "_image_family", original_family)
        view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        image = next(
            slot for slot in (await _slots(owner, view)) if slot.slot == "image"
        )
        assert image.required and len(image.batches) == 1
        assert image.batches[0].expected_count == 0
        receipt = image.batches[0].receipt
        assert isinstance(receipt, BatchReceipt)
        assert receipt.count == 0
        if legacy:
            assert "capabilities" not in image.family.config_json
            with pytest.raises(TychoraError) as reuse_error:
                await owner.run_write(
                    lambda: coordinator._retained_vector_spec(image, "image"),
                    ExecutionContext(),
                )
            assert reuse_error.value.code == "CAPABILITY_MISSING"

            def forbidden_native(*_args, **_kwargs):
                pytest.fail("Legacy family publication reached native mutation")

            monkeypatch.setattr(image_index, "create_family", forbidden_native)
            monkeypatch.setattr(image_index, "upsert_batch", forbidden_native)
            new_source = SourceInput.from_mapping(
                source_data | {"text": "No images there.", "source_key": "manual-2.txt"}
            )
            with pytest.raises(TychoraError) as error:
                await service.add(new_source, scope)
            assert error.value.code == "CAPABILITY_MISSING"
            assert "new workspace" in str(error.value)
            assert (
                await owner.run_write(
                    owner.store.active_snapshot_id, ExecutionContext()
                )
                == view.snapshot_id
            )


@pytest.mark.asyncio
async def test_graph_generation_attests_selected_membership(tmp_path, monkeypatch):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )

        def graph_factory(build):
            graph = LadybugGraphStore(
                tmp_path / "graphs" / build.spec.generation_id, build=build
            )
            owner.register_close(graph.close)
            return graph

        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
            graph_factory=graph_factory,
        )
        service = SourceService(owner, coordinator)
        for key in ("first", "second"):
            await service.add(
                SourceInput.from_mapping(
                    {
                        "type": "text",
                        "text": f"{key} evidence.",
                        "format": "text",
                        "kind": "manual@1",
                        "metadata": {
                            "title": key,
                            "publisher": "Example Team",
                            "edition": "1",
                        },
                        "source_key": f"{key}.txt",
                        "collection_id": collection,
                    }
                ),
                scope,
            )
        view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        graph_slot = next(
            slot for slot in (await _slots(owner, view)) if slot.slot == "graph"
        )
        graph_batch = graph_slot.batches[0]
        assert graph_slot.required and len(view.members) == 2
        assert graph_batch.graph_manifest is not None
        assert {
            m.extraction_id for m in graph_batch.graph_manifest.spec.memberships
        } == {m.extraction_id for m in view.members}

        original_publish = owner.store.publish_snapshot

        def fail_once(plan):
            monkeypatch.setattr(owner.store, "publish_snapshot", original_publish)
            raise TychoraError(
                "PROVIDER_FAILURE", "Injected post-seal interruption", retryable=True
            )

        monkeypatch.setattr(owner.store, "publish_snapshot", fail_once)
        third = SourceInput.from_mapping(
            {
                "type": "text",
                "text": "third evidence.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {
                    "title": "third",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "source_key": "third.txt",
                "collection_id": collection,
            }
        )
        with pytest.raises(TychoraError) as interrupted:
            await service.add(third, scope, "sealed-third")
        assert interrupted.value.code == "PROVIDER_FAILURE"
        assert (
            await owner.run_write(owner.store.active_snapshot_id, ExecutionContext())
            == view.snapshot_id
        )
        job_id = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='sealed-third'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        await owner.run_write(lambda: _jobs(service).retry(job_id), ExecutionContext())
        await service.add(third, scope, "sealed-third")
        resumed = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert len(resumed.members) == 3
        assert resumed.snapshot_id != view.snapshot_id

        fourth = SourceInput.from_mapping(
            {
                "type": "text",
                "text": "fourth evidence.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {
                    "title": "fourth",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "source_key": "fourth.txt",
                "collection_id": collection,
            }
        )

        def interrupt_before_activation(plan):
            monkeypatch.setattr(owner.store, "publish_snapshot", original_publish)
            raise TychoraError(
                "PROVIDER_FAILURE", "Injected late interruption", retryable=True
            )

        monkeypatch.setattr(
            owner.store, "publish_snapshot", interrupt_before_activation
        )
        with pytest.raises(TychoraError, match="Injected late interruption"):
            await service.add(fourth, scope, "watermark-fourth")
        job_id = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='watermark-fourth'"
            ).fetchone()[0],
            ExecutionContext(),
        )

        def stale_plan():
            row = owner.db.execute(
                "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM job_steps s JOIN artifacts a ON a.artifact_sha=s.output_artifact_sha "
                "WHERE s.job_id=? AND s.step_key LIKE 'projection/plan/%'",
                (job_id,),
            ).fetchone()
            ref = ArtifactRef(
                sha256=row[0], size_bytes=row[1], media_type=row[2], storage_key=row[3]
            )
            with owner.artifacts.open_verified(ref) as stream:
                document = json.load(stream)
            assert document["type"] == "publication_plan"
            return PublicationPlan.model_validate_json(
                canonical_bytes(document["data"])
            )

        plan = await owner.run_write(stale_plan, ExecutionContext())
        old_watermark = plan.graph_watermark
        assert old_watermark == 0
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO assertion_decisions "
                "(assertion_id,provenance_artifact_sha,provenance_record_id,state,actor_id,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "unselected-observation",
                    owner.db.execute(
                        "SELECT original_sha FROM revisions WHERE revision_id=?",
                        (resumed.members[0].revision_id,),
                    ).fetchone()[0],
                    "unselected-observation",
                    "recorded",
                    "reviewer",
                    "2026-09-23T00:00:00+00:00",
                ),
            ),
            ExecutionContext(),
        )
        await owner.run_write(lambda: _jobs(service).retry(job_id), ExecutionContext())
        await owner.run_write(lambda: _jobs(service).start(job_id), ExecutionContext())
        with pytest.raises(TychoraError) as stale_error:
            await owner.run_write(
                lambda: owner.store.publish_snapshot(plan), ExecutionContext()
            )
        assert stale_error.value.code == "WORKSPACE_BUSY"
        assert (
            await owner.run_write(owner.store.active_snapshot_id, ExecutionContext())
            == resumed.snapshot_id
        )
        await owner.run_write(
            lambda: _jobs(service).fail(job_id, stale_error.value), ExecutionContext()
        )
        await owner.run_write(lambda: _jobs(service).retry(job_id), ExecutionContext())
        await service.add(fourth, scope, "watermark-fourth")
        refreshed = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert (
            next(
                slot.batches[0].receipt.decision_watermark
                for slot in (await _slots(owner, refreshed))
                if slot.slot == "graph"
                and isinstance(slot.batches[0].receipt, GraphBatchReceipt)
            )
            == 1
        )
        from tychora.application.recovery import RecoveryService

        active_graph = next(
            slot for slot in (await _slots(owner, refreshed)) if slot.slot == "graph"
        )
        graph_batch = active_graph.batches[0]
        graph_receipt = graph_batch.receipt
        assert isinstance(graph_receipt, GraphBatchReceipt)
        retained_seal = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT receipt_json FROM index_batches WHERE batch_id=?",
                (graph_batch.batch_id,),
            ).fetchone()[0],
            ExecutionContext(),
        )
        fresh_graph = LadybugGraphStore(tmp_path / "rebuilt-graph")
        await owner.run_write(
            lambda: owner.register_close(fresh_graph.close), ExecutionContext()
        )

        def mark_graph_pending():
            owner.db.execute(
                "UPDATE index_batches SET state='pending' WHERE batch_id=?",
                (graph_batch.batch_id,),
            )
            owner.db.commit()
            owner.store.graph_indexes[graph_receipt.generation_id] = fresh_graph

        await owner.run_write(mark_graph_pending, ExecutionContext())
        pending_health = await owner.run_write(
            lambda: RecoveryService(owner)._reconcile_on_owner(
                ExecutionContext(), verify_native=True
            ),
            ExecutionContext(),
        )
        assert pending_health.native_skipped_pending_batches == 1
        assert pending_health.native_verified_generations == 0
        assert (
            await owner.run_write(
                lambda: owner.db.execute(
                    "SELECT state FROM index_batches WHERE batch_id=?",
                    (graph_batch.batch_id,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            == "pending"
        )
        assert not fresh_graph._state_path(graph_receipt.generation_id).exists()
        report = await owner.run_write(
            lambda: RecoveryService(owner)._rebuild_native_on_owner(
                scope, ExecutionContext(), ("graph",)
            ),
            ExecutionContext(),
        )
        assert report.native_pending_batches == 0
        verified_health = await owner.run_write(
            lambda: RecoveryService(owner)._reconcile_on_owner(
                ExecutionContext(), verify_native=True
            ),
            ExecutionContext(),
        )
        assert verified_health.native_verified_snapshot_id == refreshed.snapshot_id
        assert verified_health.native_verified_generations == 1
        assert await owner.run_write(
            lambda: owner.db.execute(
                "SELECT state,receipt_json FROM index_batches WHERE batch_id=?",
                (graph_batch.batch_id,),
            ).fetchone(),
            ExecutionContext(),
        ) == ("ready", retained_seal)
        assert list((tmp_path / "data" / ".physical-verification").glob("*.json"))
        recovery = RecoveryService(owner)
        old_graph_batch = next(
            slot.batches[0].batch_id
            for slot in (await _slots(owner, view))
            if slot.slot == "graph"
        )
        await owner.run_write(
            lambda: owner.db.execute(
                "UPDATE index_batches SET state='pending' WHERE batch_id=?",
                (old_graph_batch,),
            ).connection.commit(),
            ExecutionContext(),
        )
        await owner.run_write(
            lambda: recovery._rebuild_native_on_owner(
                scope, ExecutionContext(), ("graph",), view.snapshot_id
            ),
            ExecutionContext(),
        )
        assert (
            await owner.run_write(
                lambda: owner.db.execute(
                    "SELECT state FROM index_batches WHERE batch_id=?",
                    (old_graph_batch,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            == "ready"
        )
        await owner.run_write(
            lambda: fresh_graph._connection.execute(
                "MATCH (e:Evidence) WHERE e.generation_id=$gid "
                "SET e.content_hash='tampered'",
                {"gid": graph_receipt.generation_id},
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError, match="Graph evidence content differs"):
            await owner.run_write(
                lambda: recovery._reconcile_on_owner(
                    ExecutionContext(), verify_native=True
                ),
                ExecutionContext(),
            )


@pytest.mark.asyncio
async def test_relocated_file_with_identical_bytes_reuses_immutable_registration(
    tmp_path,
):
    first_path = tmp_path / "left" / "manual.txt"
    second_path = tmp_path / "right" / "manual.txt"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_text("Same retained content.")
    second_path.write_bytes(first_path.read_bytes())
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
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

        def source(path):
            return SourceInput.from_mapping(
                {
                    "type": "file",
                    "path": str(path),
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic Manual",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "manual.txt",
                    "collection_id": collection,
                }
            )

        await service.add(source(first_path), scope, "first")
        before = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        await service.add(source(second_path), scope, "relocated")
        after = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert before.members == after.members
        assert await _slots(owner, before) == await _slots(owner, after)
        assert (
            await owner.run_write(
                lambda: owner.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[
                    0
                ],
                ExecutionContext(),
            )
            == 1
        )


@pytest.mark.asyncio
async def test_reselects_retained_extraction_after_intervening_revision(
    tmp_path, monkeypatch
):
    first_path = tmp_path / "left" / "manual.txt"
    second_path = tmp_path / "right" / "manual.txt"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_text("First retained content.")
    second_path.write_bytes(first_path.read_bytes())
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
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

        def source(path):
            return SourceInput.from_mapping(
                {
                    "type": "file",
                    "path": str(path),
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic Manual",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "manual.txt",
                    "collection_id": collection,
                }
            )

        await service.add(source(first_path), scope)
        original = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        first_path.write_text("Intervening revision.")
        await service.add(source(first_path), scope)
        changed = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert changed.members != original.members

        async def parser_must_not_run(*_args):
            raise AssertionError("Retained extraction should replay without parsing")

        monkeypatch.setattr(service, "_extract", parser_must_not_run)
        await service.add(source(second_path), scope)
        restored = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert restored.members == original.members
        assert (
            await owner.run_write(
                lambda: owner.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[
                    0
                ],
                ExecutionContext(),
            )
            == 2
        )


@pytest.mark.asyncio
async def test_png_source_uses_owner_authorized_visual_provider(tmp_path, monkeypatch):
    model_root = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    if model_root is None:
        pytest.skip("requires the explicitly provisioned Docling model cache")
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
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    image_stream = BytesIO()
    Image.new("RGB", (256, 256), (20, 40, 60)).save(image_stream, format="PNG")
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
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
        transport = RecordingRawTransport()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
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
        image_index = ChromaVectorIndex(tmp_path / "images")
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
            visual_embedder=provider,
            image_index=image_index,
        )
        source = SourceInput.model_validate(
            {
                "type": "bytes",
                "data": image_stream.getvalue(),
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
        )
        service = SourceService(owner, coordinator, docling_models=Path(model_root))
        await service.add(source, scope)
        view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        image = next(
            slot for slot in (await _slots(owner, view)) if slot.slot == "image"
        )
        assert image.batches[0].expected_count >= 1
        assert len(transport.calls) == image.batches[0].expected_count
        collection, _ = image_index._require()
        stored = collection.get(
            ids=[row.record_id for row in image.batches[0].vector_records],
            include=["metadatas"],
        )["metadatas"]
        extraction_id = image.batches[0].extraction_id
        revision_id = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT revision_id FROM extractions WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()[0],
            ExecutionContext(),
        )
        for binding, metadata in zip(
            image.batches[0].vector_records, stored, strict=True
        ):
            assert metadata["owner_kind"] == "asset"
            assert metadata["owner_id"] == binding.asset_id
            assert metadata["extraction_id"] == image.batches[0].extraction_id
            assert metadata["revision_id"] == revision_id
        before_calls = len(transport.calls)
        await service.sync(view.members[0].source_id, scope)
        assert len(transport.calls) == before_calls
        changed_stream = BytesIO()
        Image.new("RGB", (256, 256), (60, 40, 20)).save(changed_stream, format="PNG")
        changed = SourceInput.model_validate(
            source.model_dump() | {"data": changed_stream.getvalue()}
        )
        await service.add(changed, scope)

        async def parser_must_not_run(*_args):
            raise AssertionError("Retained document should replay without parsing")

        monkeypatch.setattr(service, "_extract", parser_must_not_run)
        await service.add(source, scope)
        restored = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert restored.members == view.members


@pytest.mark.asyncio
async def test_cancellation_drains_active_provider_before_publication(
    tmp_path, config_factory
):
    config = config_factory(dimensions=2)
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    started, release = Event(), Event()

    class WaitingEmbedder(RecordingEmbedder):
        def embed_groups(self, groups, ctx):
            started.set()
            if not release.wait(5):
                raise AssertionError("Provider test release timed out")
            return super().embed_groups(groups, ctx)

    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
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
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=counter,
            text_embedder=WaitingEmbedder(counter),
            text_index=ChromaVectorIndex(tmp_path / "vectors"),
        )
        service = SourceService(owner, coordinator)
        source = SourceInput.from_mapping(
            {
                "type": "text",
                "text": "Delayed provider work.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {
                    "title": "Delayed",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "source_key": "delayed.txt",
                "collection_id": collection,
            }
        )
        task = asyncio.create_task(service.add(source, scope))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), 6)
            job_id = await owner.run_write(
                lambda: owner.db.execute("SELECT job_id FROM jobs").fetchone()[0],
                ExecutionContext(),
            )
            await owner.run_write(
                lambda: _jobs(service).cancel(job_id), ExecutionContext()
            )
        finally:
            release.set()
        with pytest.raises(TychoraError) as cancelled:
            await task
        assert cancelled.value.code == "CANCELLED"
        assert (
            await owner.run_write(owner.store.active_snapshot_id, ExecutionContext())
            is None
        )
        assert (
            await owner.run_write(
                lambda: _jobs(service).get(job_id).state, ExecutionContext()
            )
            == "cancelled"
        )


@pytest.mark.asyncio
async def test_repository_source_uses_spawned_language_adapters(tmp_path, monkeypatch):
    repository = make_repo(tmp_path)
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
        '[configuration.extraction]\nparser="code"\n'
    )
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "code", "Code"),
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
        source = SourceInput.from_mapping(
            {
                "type": "repository",
                "path": str(repository),
                "kind": "code_file@1",
                "metadata": {"title": "Synthetic Repository"},
                "source_key": "repository",
                "collection_id": collection,
            }
        )
        await service.add(source, scope)
        view = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert len(view.members) == 1
        assert (
            await owner.run_write(
                lambda: owner.db.execute(
                    "SELECT COUNT(*) FROM evidence_units WHERE extraction_id=?",
                    (view.members[0].extraction_id,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            >= 1
        )
        original_bytes = (repository / "module.py").read_bytes()
        (repository / "module.py").write_text("def changed(): return 2\n")
        await service.add(source, scope)
        changed = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert changed.members != view.members
        (repository / "module.py").write_bytes(original_bytes)

        async def parser_must_not_run(*_args):
            raise AssertionError("Retained repository must replay without parsing")

        monkeypatch.setattr(service, "_extract", parser_must_not_run)
        await service.add(source, scope)
        restored = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert restored.members == view.members


@pytest.mark.asyncio
async def test_registry_artifact_is_written_once_per_retained_sha(
    tmp_path, monkeypatch
):
    from tests.api.test_public_sync import CharacterCounter
    from tychora import SourceInput, Tychora

    writes = []
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        artifacts = kb._workspace.owner.artifacts
        original = artifacts.put_stream

        def recording(stream, media_type):
            ref = original(stream, media_type)
            writes.append(ref.sha256)
            return ref

        monkeypatch.setattr(artifacts, "put_stream", recording)
        for index in range(2):
            job = kb.sources.add(
                SourceInput(
                    type="text",
                    source_key=f"manual-{index}",
                    collection_id=kb.default_collection_id,
                    kind="manual@1",
                    format="text",
                    metadata={"title": "T", "publisher": "P", "edition": "1"},
                    text=f"Record verification outcome {index}.",
                )
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        retained = kb._workspace.owner.config.retained_sha
    assert writes.count(retained) == 1
