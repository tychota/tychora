"""Source service through the actual owner, capture, parser and canonical store."""

import asyncio
import json
import os
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
from tychora.contracts.identity import canonical_bytes, space_id
from tychora.contracts.publication import PublicationPlan
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext


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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        first_view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        second = await service.sync(first_view.members[0].source_id, scope)
        second_view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert first.id != second.id
        assert first_view.members == second_view.members
        assert first_view.snapshot_id != second_view.snapshot_id
        assert (
            await owner.run_blocking(
                lambda: owner.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[
                    0
                ],
                ExecutionContext(),
            )
            == 1
        )


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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        image = next(slot for slot in view.slots if slot.slot == "image")
        assert image.required and len(image.batches) == 1
        assert image.batches[0].expected_count == 0
        assert image.batches[0].receipt.count == 0
        if legacy:
            assert "capabilities" not in image.family.config_json
            with pytest.raises(TychoraError) as reuse_error:
                await owner.run_blocking(
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
                await owner.run_blocking(
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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        graph_slot = next(slot for slot in view.slots if slot.slot == "graph")
        graph_batch = graph_slot.batches[0]
        assert graph_slot.required and len(view.members) == 2
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
            await owner.run_blocking(owner.store.active_snapshot_id, ExecutionContext())
            == view.snapshot_id
        )
        job_id = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='sealed-third'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        await owner.run_blocking(
            lambda: service._jobs.retry(job_id), ExecutionContext()
        )
        await service.add(third, scope, "sealed-third")
        resumed = await owner.run_blocking(
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
        job_id = await owner.run_blocking(
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
                root = json.load(stream)
            return PublicationPlan.model_validate_json(
                canonical_bytes(
                    {**root["data"], "manifest": ref.model_dump(mode="json")}
                )
            )

        plan = await owner.run_blocking(stale_plan, ExecutionContext())
        old_watermark = next(
            slot.batches[0].receipt.decision_watermark
            for slot in plan.slots
            if slot.slot == "graph"
        )
        assert old_watermark == 0
        await owner.run_blocking(
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
        await owner.run_blocking(
            lambda: service._jobs.retry(job_id), ExecutionContext()
        )
        await owner.run_blocking(
            lambda: service._jobs.start(job_id), ExecutionContext()
        )
        with pytest.raises(TychoraError) as stale_error:
            await owner.run_blocking(
                lambda: owner.store.publish_snapshot(plan), ExecutionContext()
            )
        assert stale_error.value.code == "WORKSPACE_BUSY"
        assert (
            await owner.run_blocking(owner.store.active_snapshot_id, ExecutionContext())
            == resumed.snapshot_id
        )
        await owner.run_blocking(
            lambda: service._jobs.fail(job_id, stale_error.value), ExecutionContext()
        )
        await owner.run_blocking(
            lambda: service._jobs.retry(job_id), ExecutionContext()
        )
        await service.add(fourth, scope, "watermark-fourth")
        refreshed = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert (
            next(
                slot.batches[0].receipt.decision_watermark
                for slot in refreshed.slots
                if slot.slot == "graph"
            )
            == 1
        )
        from tychora.application.recovery import RecoveryService

        active_graph = next(slot for slot in refreshed.slots if slot.slot == "graph")
        graph_batch = active_graph.batches[0]
        retained_seal = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT receipt_json FROM index_batches WHERE batch_id=?",
                (graph_batch.batch_id,),
            ).fetchone()[0],
            ExecutionContext(),
        )
        fresh_graph = LadybugGraphStore(tmp_path / "rebuilt-graph")
        await owner.run_blocking(
            lambda: owner.register_close(fresh_graph.close), ExecutionContext()
        )

        def mark_graph_pending():
            owner.db.execute(
                "UPDATE index_batches SET state='pending' WHERE batch_id=?",
                (graph_batch.batch_id,),
            )
            owner.db.commit()
            owner.store.graph_indexes[graph_batch.receipt.generation_id] = fresh_graph

        await owner.run_blocking(mark_graph_pending, ExecutionContext())
        report = await owner.run_blocking(
            lambda: RecoveryService(owner)._rebuild_native_on_owner(
                scope, ExecutionContext(), ("graph",)
            ),
            ExecutionContext(),
        )
        assert report.native_pending_batches == 0
        assert await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT state,receipt_json FROM index_batches WHERE batch_id=?",
                (graph_batch.batch_id,),
            ).fetchone(),
            ExecutionContext(),
        ) == ("ready", retained_seal)
        assert list((tmp_path / "data" / ".physical-verification").glob("*.json"))
        recovery = RecoveryService(owner)
        old_graph_batch = next(
            slot.batches[0].batch_id for slot in view.slots if slot.slot == "graph"
        )
        previous_graph_batch = next(
            slot.batches[0].batch_id for slot in resumed.slots if slot.slot == "graph"
        )
        active_graph_batch = active_graph.batches[0].batch_id
        with owner.leases.pin(frozenset({view.snapshot_id})):
            leased = await owner.run_blocking(
                lambda: recovery._prune_native_on_owner(scope, ExecutionContext()),
                ExecutionContext(),
            )
            assert leased.admission_busy and not leased.pruned_batches
        from tychora.application.restore import RESTORE_SUSPENSION

        marker = tmp_path / "data" / RESTORE_SUSPENSION
        marker.write_bytes(
            canonical_bytes(
                {
                    "type": "restore-suspension",
                    "schema_version": 1,
                    "workspace_id": owner.workspace_id,
                    "pending_deletion_suspended": True,
                }
            )
        )
        owner.restore_deletion_suspended = True
        suspended = await owner.run_blocking(
            lambda: recovery._prune_native_on_owner(scope, ExecutionContext()),
            ExecutionContext(),
        )
        assert suspended.deletion_suspended and not suspended.pruned_batches
        marker.unlink()
        owner.restore_deletion_suspended = False
        pruned = await owner.run_blocking(
            lambda: recovery._prune_native_on_owner(scope, ExecutionContext()),
            ExecutionContext(),
        )
        assert old_graph_batch in pruned.pruned_batches
        assert previous_graph_batch not in pruned.pruned_batches
        assert active_graph_batch not in pruned.pruned_batches
        assert (
            await owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT state FROM index_batches WHERE batch_id=?",
                    (old_graph_batch,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            == "pending"
        )
        assert not (
            await owner.run_blocking(
                lambda: recovery._prune_native_on_owner(scope, ExecutionContext()),
                ExecutionContext(),
            )
        ).pruned_batches
        await owner.run_blocking(
            lambda: recovery._rebuild_native_on_owner(
                scope, ExecutionContext(), ("graph",), view.snapshot_id
            ),
            ExecutionContext(),
        )
        assert (
            await owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT state FROM index_batches WHERE batch_id=?",
                    (old_graph_batch,),
                ).fetchone()[0],
                ExecutionContext(),
            )
            == "ready"
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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        before = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        await service.add(source(second_path), scope, "relocated")
        after = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert before.members == after.members
        assert before.slots[0].batches == after.slots[0].batches
        assert (
            await owner.run_blocking(
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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        original = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        first_path.write_text("Intervening revision.")
        await service.add(source(first_path), scope)
        changed = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert changed.members != original.members

        async def parser_must_not_run(*_args):
            raise AssertionError("Retained extraction should replay without parsing")

        monkeypatch.setattr(service, "_extract", parser_must_not_run)
        await service.add(source(second_path), scope)
        restored = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert restored.members == original.members
        assert (
            await owner.run_blocking(
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
        await owner.run_blocking(
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
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=Counter(),
            visual_embedder=provider,
            image_index=ChromaVectorIndex(tmp_path / "images"),
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
        view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        image = next(slot for slot in view.slots if slot.slot == "image")
        assert image.batches[0].expected_count >= 1
        assert len(transport.calls) == image.batches[0].expected_count
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
        restored = await owner.run_blocking(
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
        await owner.run_blocking(
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
            job_id = await owner.run_blocking(
                lambda: owner.db.execute("SELECT job_id FROM jobs").fetchone()[0],
                ExecutionContext(),
            )
            await owner.run_blocking(
                lambda: service._jobs.cancel(job_id), ExecutionContext()
            )
        finally:
            release.set()
        with pytest.raises(TychoraError) as cancelled:
            await task
        assert cancelled.value.code == "CANCELLED"
        assert (
            await owner.run_blocking(owner.store.active_snapshot_id, ExecutionContext())
            is None
        )
        assert (
            await owner.run_blocking(
                lambda: service._jobs.get(job_id).state, ExecutionContext()
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
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,'{}')",
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
        view = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert len(view.members) == 1
        assert (
            await owner.run_blocking(
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
        changed = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert changed.members != view.members
        (repository / "module.py").write_bytes(original_bytes)

        async def parser_must_not_run(*_args):
            raise AssertionError("Retained repository must replay without parsing")

        monkeypatch.setattr(service, "_extract", parser_must_not_run)
        await service.add(source, scope)
        restored = await owner.run_blocking(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert restored.members == view.members
