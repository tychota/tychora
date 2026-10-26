"""A declared text source traverses both configured native query channels."""

import asyncio
from hashlib import sha256

import pytest

from tests.indexing.test_publication import RecordingCounter, RecordingEmbedder
from tests.performance.engine_probe import Counter
from tests.providers.test_visual import CharacterCounter, RecordingRawTransport, _limits
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.voyage.query import (
    VoyageTextQueryProvider,
    VoyageVisualQueryProvider,
)
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.text import VoyageTextEmbedder
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.query import QueryService
from tychora.application.sources import SourceService
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import ConfigurationVariant, EvaluationRequest
from tychora.contracts.requests import QueryRequest, RequestScope, SourceInput
from tychora.evaluation.runner import EvaluationService
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches

COLLECTION = "55825936-6a6a-4d29-945a-327d771a76b7"


class _TextTransport:
    calls = 0

    def post_json(self, path, body, timeout):
        self.calls += 1
        return RawVoyageResponse(
            200,
            b'{"data":[{"index":0,"data":[{"index":0,"embedding":[1.0,0.0]}]}]}',
        )

    def close(self):
        pass


@pytest.mark.asyncio
async def test_real_text_and_visual_channels_overlap_on_one_pinned_query(
    tmp_path, monkeypatch
):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\nmodel="voyage-context-4"\ndimensions=2\n'
        '[configuration.providers.image]\nprovider="voyage"\nmodel="voyage-multimodal-3.5"\ndimensions=1024\n'
        '[configuration.index.text]\nbackend="chroma"\n'
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
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
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
            ExecutionContext(),
        )
        caches = QueryCaches()
        text_counter = RecordingCounter()
        visual_counter = CharacterCounter()
        text_transport = _TextTransport()
        visual_transport = RecordingRawTransport()
        text_runtime = VoyageRuntime(
            lambda: text_transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=caches,
        )
        visual_runtime = VoyageRuntime(
            lambda: visual_transport,
            workspace_id=owner.workspace_id,
            credential_binding_id="synthetic-credential",
            query_caches=caches,
        )
        text_capabilities = RecordingEmbedder(text_counter).capabilities
        visual_capabilities = qualify_voyage_visual_capabilities(
            counter=visual_counter, limits=_limits()
        )
        visual_embedder = await create_visual_embedder(
            owner,
            capabilities=visual_capabilities,
            counter=visual_counter,
            runtime=visual_runtime,
            scope=scope,
            ctx=ExecutionContext(),
        )
        text_index = ChromaVectorIndex(tmp_path / "text-vectors")
        visual_index = ChromaVectorIndex(tmp_path / "image-vectors")
        source_embedder = RecordingEmbedder(text_counter)
        sources = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=text_counter,
                text_embedder=source_embedder,
                text_index=text_index,
                visual_embedder=visual_embedder,
                image_index=visual_index,
            ),
        )
        await sources.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Synthetic amber evidence.",
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic note",
                        "publisher": "Example Team",
                        "edition": "1",
                    },
                    "source_key": "amber.txt",
                    "collection_id": COLLECTION,
                }
            ),
            scope,
        )
        service = QueryService(
            owner,
            output_counter=Counter(),
            caches=caches,
            text_provider=VoyageTextQueryProvider(
                VoyageTextEmbedder(
                    capabilities=text_capabilities,
                    counter=text_counter,
                    runtime=text_runtime,
                    authorize_document=lambda *_: None,
                )
            ),
            text_index=text_index,
            visual_provider=VoyageVisualQueryProvider(visual_embedder),
            visual_index=visual_index,
        )
        entered = set()
        both = asyncio.Event()
        original_text = owner.embed_queries
        original_visual = owner.embed_visual_query

        async def text_query(*args, **kwargs):
            entered.add("text")
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return await original_text(*args, **kwargs)

        async def visual_query(*args, **kwargs):
            entered.add("visual")
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            return await original_visual(*args, **kwargs)

        monkeypatch.setattr(owner, "embed_queries", text_query)
        monkeypatch.setattr(owner, "embed_visual_query", visual_query)
        delivered = await service.search(
            QueryRequest.from_mapping(
                {"question": "amber", "modalities": ["lexical", "text", "image"]}
            ),
            scope,
            ExecutionContext(),
        )
        assert entered == {"text", "visual"}
        assert text_transport.calls == 1
        assert len(visual_transport.calls) == 1
        assert any("amber" in segment.text for segment in delivered.result.segments)
        snapshot = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        slots = await owner.run_write(
            lambda: owner.store.snapshot_slots(snapshot), ExecutionContext()
        )
        assert {slot.slot for slot in slots} >= {"text", "image"}
        assert (
            next(slot for slot in slots if slot.slot == "image")
            .batches[0]
            .expected_count
            == 0
        )

        def labels():
            return owner.db.execute(
                "SELECT s.source_id,r.revision_id,u.unit_id FROM evidence_units u "
                "JOIN extractions e USING(extraction_id) JOIN revisions r USING(revision_id) "
                "JOIN sources s USING(source_id) WHERE s.source_key=? AND instr(u.text,?)>0",
                ("amber.txt", "amber"),
            ).fetchall()

        ((source_id, revision_id, unit_id),) = await owner.run_write(
            labels, ExecutionContext()
        )
        case = {
            "case_id": "combined-synthetic-amber",
            "family_key": "amber",
            "split": "test",
            "benchmark": "integrity",
            "label_provenance": "declared synthetic source and canonical SQL anchor",
            "query": {"question": "amber", "modalities": ["text", "image"]},
            "expected_source_ids": [source_id],
            "expected_revision_ids": [revision_id],
            "expected_unit_ids": [unit_id],
        }
        cases = canonical_bytes(case) + b"\n"
        manifest = canonical_bytes(
            {
                "format": "tychora-gold@1",
                "dataset_id": "synthetic-combined-v1",
                "name": "Synthetic combined channel mechanics",
                "version_sha": sha256(cases).hexdigest(),
                "case_count": 1,
            }
        )
        jobs = await owner.run_write(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ExecutionContext(),
        )
        evaluation = EvaluationService(owner, service, jobs)
        await evaluation.register_dataset_bytes(
            manifest, cases, scope, ExecutionContext()
        )
        run = await evaluation.run(
            EvaluationRequest(
                dataset_id="synthetic-combined-v1",
                split="test",
                snapshot_id=snapshot.snapshot_id,
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
                            "name": "combined",
                            "profile": "test",
                            "overrides": {"channels": ["lexical", "text", "image"]},
                        }
                    ),
                ),
            ),
            scope,
            ExecutionContext(),
        )
        assert run.state == "succeeded"
        stored = await evaluation.get(run.id, scope, ExecutionContext())
        runs = stored["cases"]["combined-synthetic-amber"]["runs"]
        assert [entry["variant"] for entry in runs] == ["lexical", "combined"]
        assert [entry["execution_plan"]["image"] for entry in runs] == [False, True]
        assert all(
            next(
                metric
                for metric in entry["metrics"]["metrics"]
                if metric["name"] == "unit_recall"
            )["value"]
            == 1.0
            for entry in runs
        )
        old_text = next(slot for slot in slots if slot.slot == "text")
        old_image = next(slot for slot in slots if slot.slot == "image")
        old_embed_calls = source_embedder.calls
        await sources.add(
            SourceInput.from_mapping(
                {
                    "type": "text",
                    "text": "Synthetic amber edition two.",
                    "format": "text",
                    "kind": "manual@1",
                    "metadata": {
                        "title": "Synthetic note",
                        "publisher": "Example Team",
                        "edition": "2",
                    },
                    "source_key": "amber.txt",
                    "collection_id": COLLECTION,
                }
            ),
            scope,
        )
        refreshed = await owner.run_write(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert len(refreshed.members) == 2
        assert source_embedder.calls == old_embed_calls + 1
        refreshed_slots = await owner.run_write(
            lambda: owner.store.snapshot_slots(refreshed), ExecutionContext()
        )
        for slot_name, earlier in (("text", old_text), ("image", old_image)):
            current = next(slot for slot in refreshed_slots if slot.slot == slot_name)
            assert current.family == earlier.family
            assert len(current.batches) == 2
            assert earlier.batches[0] in current.batches
