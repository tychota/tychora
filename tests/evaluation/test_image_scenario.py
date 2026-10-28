"""Declared synthetic PNG and proxy traverse a nonempty native image batch."""

from hashlib import sha256
from io import BytesIO

import pytest

from tests.evaluation.conftest import SyntheticCounter
from tests.helpers.corpus import reader, seed_bundle
from tests.helpers.publication import slots_of
from tests.providers.test_visual import (
    CharacterCounter,
    RecordingRawTransport,
    _image_bytes,
    _limits,
)
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.voyage.query import VoyageVisualQueryProvider
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import QueryService
from tychora.application.query_plan import QueryBindings
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    AssetRow,
    LocatorRow,
    RevisionArtifactRow,
    UnitAssetRow,
)
from tychora.contracts.evidence import Geometry, ImageLocator
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.operations import ConfigurationVariant, EvaluationRequest
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.contracts.vectors import BatchReceipt
from tychora.evaluation.runner import EvaluationService
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ingestion.assets import ProxyFactory
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


@pytest.mark.asyncio
async def test_declared_png_proxy_enters_visual_query_and_evaluation(tmp_path):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ndimensions=1024\n'
        '[configuration.index.image]\nbackend="chroma"\n'
    )
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="synthetic-reader",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "workspace", config, scope) as owner:
        ctx = ExecutionContext()
        await owner.run_write(
            lambda: owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,0,?)",
                (
                    COLLECTION,
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ctx,
        )
        text = "Synthetic violet diagram."
        bundle = seed_bundle(text, source_key="violet.txt")
        for ref in bundle.artifacts:
            with reader().open_verified(ref) as stream:
                await owner.run_write(
                    lambda stream=stream, ref=ref: owner.artifacts.put_stream(
                        stream, ref.media_type
                    ),
                    ctx,
                )
        png = _image_bytes((80, 20, 120))
        image_ref = await owner.run_write(
            lambda: owner.artifacts.put_stream(BytesIO(png), "image/png"), ctx
        )
        asset = AssetRow(
            asset_id=digest(
                "asset-v1",
                bundle.extraction.extraction_id,
                "figure:violet",
                image_ref.sha256,
            ),
            extraction_id=bundle.extraction.extraction_id,
            artifact_sha=image_ref.sha256,
            asset_type="picture",
            native_ref="figure:violet",
            locator=ImageLocator(
                artifact_ref=image_ref.sha256,
                native_ref="figure:violet",
                geometry=Geometry(domain="pixels", x0=0, y0=0, x1=256, y1=256),
            ),
            caption=text,
        )
        derivation = await owner.run_write(
            lambda: owner.artifacts.put_stream(
                BytesIO(canonical_bytes({"algorithm": "synthetic-caption-copy@1"})),
                "application/json",
            ),
            ctx,
        )
        created_at = await owner.run_write(
            lambda: owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0],
            ctx,
        )
        bundle = bundle.model_copy(
            update={
                "workspace": bundle.workspace.model_copy(
                    update={
                        "workspace_id": owner.workspace_id,
                        "created_at": created_at,
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
                "artifacts": (*bundle.artifacts, image_ref),
                "assets": (asset,),
                "unit_assets": (
                    UnitAssetRow(
                        unit_id=bundle.units[0].unit_id,
                        asset_id=asset.asset_id,
                        role="source",
                    ),
                    UnitAssetRow(
                        unit_id=bundle.units[0].unit_id,
                        asset_id=asset.asset_id,
                        role="caption",
                    ),
                ),
                "revision_artifacts": (
                    *bundle.revision_artifacts,
                    RevisionArtifactRow(
                        revision_id=bundle.revision.revision_id,
                        role="sidecar",
                        source_ref="figure:violet",
                        artifact_sha=image_ref.sha256,
                    ),
                ),
            }
        )
        proxy = ProxyFactory(bundle).make_proxy(
            asset.asset_id,
            "caption_copy",
            text,
            (bundle.units[0].unit_id,),
            derivation,
        )
        bundle = bundle.model_copy(
            update={
                "artifacts": (*bundle.artifacts, derivation),
                "chunks": (),
                "units": (*bundle.units, proxy.unit),
                "locators": (
                    *bundle.locators,
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
        visual_counter = CharacterCounter()
        transport = RecordingRawTransport()
        caches = QueryCaches()
        runtime = VoyageRuntime(
            lambda: transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=caches,
        )
        embedder = await create_visual_embedder(
            owner,
            capabilities=qualify_voyage_visual_capabilities(
                counter=visual_counter, limits=_limits()
            ),
            counter=visual_counter,
            runtime=runtime,
            scope=scope,
            ctx=ctx,
            priority="background",
        )
        image_index = ChromaVectorIndex(tmp_path / "vectors")
        coordinator = ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=config,
            counter=SyntheticCounter(),
            visual_embedder=embedder,
            image_index=image_index,
        )
        config_ref = await owner.run_write(
            lambda: owner.artifacts.put_stream(
                BytesIO(
                    canonical_bytes(
                        {
                            "type": "registry",
                            "schema_version": "1",
                            "data": config.model_dump(mode="json"),
                        }
                    )
                ),
                "application/json",
            ),
            ctx,
        )

        def prepare():
            jobs = JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            )
            job = jobs.accept("add", scope, None, {"source": "violet.txt"}, config_ref)
            jobs.start(job.id)
            return coordinator.prepare(
                bundle, owner.store.active_snapshot_id(), job, ctx
            )

        prepared = await owner.run_write(prepare, ctx)
        visual_records = await owner.embed_visual_assets(
            embedder, prepared.visual_inputs, ctx, scope=scope
        )
        plan = await owner.run_write(
            lambda: coordinator.finalize(prepared, (), ctx, visual_records), ctx
        )
        snapshot_id = await owner.run_write(
            lambda: owner.store.publish_snapshot(plan), ctx
        )
        snapshot = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ctx
        )
        slots = await owner.run_write(lambda: slots_of(owner.store, snapshot), ctx)
        image_slot = next(slot for slot in slots if slot.slot == "image")
        assert image_slot.batches[0].expected_count == 1
        image_receipt = image_slot.batches[0].receipt
        assert isinstance(image_receipt, BatchReceipt)
        assert image_receipt.count == 1
        assert len(transport.calls) == 1
        retained_proxy = await owner.run_write(
            lambda: owner.db.execute(
                "SELECT asset_id,origin_type,derivation_sha FROM asset_proxies "
                "WHERE proxy_unit_id=?",
                (proxy.unit.unit_id,),
            ).fetchone(),
            ctx,
        )
        assert retained_proxy == (asset.asset_id, "caption_copy", derivation.sha256)
        query = QueryService(
            owner,
            output_counter=SyntheticCounter(),
            caches=caches,
            bindings=QueryBindings(
                visual_provider=VoyageVisualQueryProvider(embedder),
                visual_index=image_index,
            ),
        )
        delivered = await query.search(
            QueryRequest.from_mapping(
                {
                    "question": "violet",
                    "modalities": ["image"],
                    "mode": "lookup",
                }
            ),
            scope,
            ctx,
        )
        assert delivered.result.snapshot_id == snapshot_id
        assert any(
            visual.asset_id == asset.asset_id for visual in delivered.result.visuals
        )
        assert any(
            visual.locator.artifact_ref == image_ref.sha256
            and visual.locator.native_ref == "figure:violet"
            for visual in delivered.result.visuals
        )
        assert len(transport.calls) == 2
        case = {
            "case_id": "proxy-image",
            "family_key": "proxy-image",
            "split": "test",
            "benchmark": "integrity",
            "label_provenance": "declared PNG, caption proxy and canonical retained asset binding",
            "query": {"question": "violet", "modalities": ["image"], "mode": "lookup"},
            "expected_source_ids": [bundle.source.source_id],
            "expected_revision_ids": [bundle.revision.revision_id],
            "expected_asset_ids": [asset.asset_id],
        }
        cases = canonical_bytes(case) + b"\n"
        manifest = canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": "synthetic-image-v1",
                "name": "Synthetic proxy image",
                "version_sha": sha256(cases).hexdigest(),
                "case_count": 1,
            }
        )
        jobs = await owner.run_write(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ctx,
        )
        evaluation = EvaluationService(owner, query, jobs)
        await evaluation.register_dataset_bytes(manifest, cases, scope, ctx)
        handle = await evaluation.run(
            EvaluationRequest(
                dataset_id="synthetic-image-v1",
                split="test",
                snapshot_id=snapshot_id,
                offline=False,
                variants=(
                    ConfigurationVariant.from_mapping(
                        {
                            "name": "lexical",
                            "profile": "test",
                            "overrides": {"channels": ["lexical"]},
                        }
                    ),
                    ConfigurationVariant.from_mapping(
                        {
                            "name": "image",
                            "profile": "test",
                            "overrides": {"channels": ["image"]},
                        }
                    ),
                ),
            ),
            scope,
            ctx,
        )
        assert handle.state == "succeeded"
        report = await evaluation.get(handle.id, scope, ctx)
        runs = report["cases"]["proxy-image"]["runs"]
        assert [run["variant"] for run in runs] == ["lexical", "image"]
        metrics = [
            {item["name"]: item["value"] for item in run["metrics"]["metrics"]}
            for run in runs
        ]
        lexical_reasons = {
            reason
            for visual in runs[0]["result"]["visuals"]
            for reason in visual["selection_reasons"]
        }
        image_reasons = {
            reason
            for visual in runs[1]["result"]["visuals"]
            for reason in visual["selection_reasons"]
        }
        assert "proxy_match" in lexical_reasons
        assert proxy.binding.required_unit_ids == frozenset({bundle.units[0].unit_id})
        assert "multimodal_match" not in lexical_reasons
        assert "multimodal_match" in image_reasons
        assert metrics[0]["asset_recall"] == metrics[1]["asset_recall"] == 1.0
        assert metrics[1]["asset_recall"] == 1.0
        assert metrics[1]["forbidden_leakage"] == 0.0
        assert metrics[1]["locator_validity"] == 1.0
