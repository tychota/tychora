"""Atomic snapshot publication against the real canonical SQLite schema."""

import struct
from dataclasses import dataclass
from io import BytesIO

import pytest

from tests.helpers.corpus import COLLECTION, reader, registry, seed_bundle
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import publication_manifest
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import EmbeddingCapabilities, EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest, space_id
from tychora.contracts.publication import (
    IndexFamily,
    LexicalBatchReceipt,
    PublicationBatch,
    PublicationPlan,
    PublicationSlot,
    SnapshotMember,
)
from tychora.contracts.requests import RequestScope
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext


class RecordingEmbedder:
    def __init__(self, counter):
        self.capabilities = EmbeddingCapabilities(
            provider="voyage",
            endpoint="recording",
            model="voyage-context-4",
            dimensions=2,
            input_types=frozenset({"document", "query"}),
            contextualization="joint",
            tokenizer_id=counter.tokenizer_id,
            space_id=space_id(
                "text", "voyage", "voyage-context-4", 2, "cosine", "a" * 64
            ),
            representation_fingerprint="a" * 64,
            max_group_tokens=24000,
            max_request_tokens=28000,
            options=FrozenJsonMap(
                {
                    "input_type": "document",
                    "enable_auto_chunking": False,
                    "truncation": False,
                }
            ),
        )
        self.calls = 0

    def embed_groups(self, groups, ctx):
        self.calls += 1
        return tuple(
            EmbeddingRecord.from_normalized_bytes(
                record_id=digest(
                    "embedding-record-v1",
                    window_id,
                    group.input_sha,
                    self.capabilities.space_id,
                ),
                input_sha=group.input_sha,
                space_id=self.capabilities.space_id,
                stored=struct.pack("<ff", 1.0, 0.0),
            )
            for group in groups
            for window_id in group.window_ids
        )


class RecordingCounter:
    model = "voyage-context-4"
    tokenizer_id = "recording-codepoint@1"

    def count(self, text):
        return len(text)


class RecordingVector:
    def __init__(self, path):
        self.inner = ChromaVectorIndex(path)
        self.fail_next_batch = False
        self.upserts = 0

    def create_family(self, spec):
        return self.inner.create_family(spec)

    def upsert_batch(self, batch, ctx):
        self.upserts += 1
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise TychoraError(
                "PROVIDER_FAILURE",
                "Injected native vector write failure",
                retryable=True,
            )
        return self.inner.upsert_batch(batch, ctx)

    def verify_batch(self, receipt):
        return self.inner.verify_batch(receipt)


@dataclass
class ProjectedCorpus:
    store: CorpusStore
    jobs: JobRepository
    config: ArtifactRef
    scope: RequestScope
    coordinator: ProjectionCoordinator
    vector: RecordingVector
    embedder: RecordingEmbedder

    def prepare_revision(self, text: str, *, source_key: str = "manual.md"):
        bundle = seed_bundle(text, source_key=source_key).model_copy(
            update={"chunks": ()}
        )
        job = self.jobs.accept(
            "add", self.scope, None, {"sha": bundle.revision.original_sha}, self.config
        )
        self.jobs.start(job.id)
        plan = self.coordinator.build(
            bundle, self.store.active_snapshot_id(), job, ExecutionContext()
        )
        return bundle, job, plan

    def add_revision(self, text: str, *, source_key: str = "manual.md") -> str:
        _, _, plan = self.prepare_revision(text, source_key=source_key)
        return self.store.publish_snapshot(plan)


@dataclass
class PublishedCorpus:
    store: CorpusStore
    jobs: JobRepository
    config: ArtifactRef
    scope: RequestScope

    def plan(self, text: str, *, base: str | None = None) -> PublicationPlan:
        bundle = seed_bundle(text)
        self.store.commit_extraction(bundle)
        job = self.jobs.accept(
            "add",
            self.scope,
            None,
            {"text_sha": bundle.revision.original_sha},
            self.config,
        )
        self.jobs.start(job.id)
        member = SnapshotMember(
            source_id=bundle.source.source_id,
            revision_id=bundle.revision.revision_id,
            extraction_id=bundle.extraction.extraction_id,
        )
        family_id = digest(
            "lexical-family-v1", bundle.workspace.workspace_id, self.config.sha256
        )
        family = IndexFamily(
            family_id=family_id,
            channel="lexical",
            backend="sqlite-fts5",
            config_sha=self.config.sha256,
            config_json=FrozenJsonMap(
                {"tokenizer": "unicode61", "effective_config_sha": self.config.sha256}
            ),
            backend_namespace="lexical",
            created_at=bundle.workspace.created_at,
        )
        entries = (bundle.chunks[0].chunk_id,)
        input_manifest = reader().put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "index_input",
                        "schema_version": "1",
                        "data": {"entries": entries},
                    }
                )
            ),
            "application/json",
        )
        batch = PublicationBatch(
            batch_id=digest(
                "index-batch-v1",
                family_id,
                bundle.extraction.extraction_id,
                input_manifest.sha256,
            ),
            family_id=family_id,
            extraction_id=bundle.extraction.extraction_id,
            input_manifest=input_manifest,
            expected_count=1,
            receipt=LexicalBatchReceipt(
                family_id=family_id,
                count=1,
                entry_ids_sha=digest("index-entries-v1", entries),
            ),
            lexical_records=({"chunk_id": entries[0], "body": text},),
        )
        slot = PublicationSlot(slot="lexical", family=family, batches=(batch,))
        snapshot_id = digest(
            "snapshot-v1",
            bundle.workspace.workspace_id,
            base,
            job.id,
            self.config.sha256,
            [member.model_dump(mode="json")],
            [
                {
                    "slot": "lexical",
                    "family_id": family_id,
                    "required": True,
                    "batch_ids": [batch.batch_id],
                }
            ],
        )
        placeholder = reader().put_stream(BytesIO(b"placeholder"), "application/json")
        plan = PublicationPlan(
            job_id=job.id,
            snapshot_id=snapshot_id,
            workspace_id=bundle.workspace.workspace_id,
            base_snapshot_id=base,
            config_sha=self.config.sha256,
            manifest=placeholder,
            members=(member,),
            slots=(slot,),
        )
        manifest = reader().put_stream(
            BytesIO(publication_manifest(plan)),
            "application/json",
        )
        return plan.model_copy(update={"manifest": manifest})


@pytest.fixture
def published_corpus(tmp_path) -> PublishedCorpus:
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    initial = seed_bundle("old source text")
    store.commit_extraction(initial)
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"write", "read"}),
    )
    config = reader().put_stream(
        BytesIO(b'{"type":"registry","schema_version":"1","data":{"offline":true}}'),
        "application/json",
    )
    jobs = JobRepository(
        db, artifacts=reader(), workspace_id=initial.workspace.workspace_id
    )
    corpus = PublishedCorpus(store, jobs, config, scope)
    plan = corpus.plan("old source text")
    store.publish_snapshot(plan)
    yield corpus
    db.close()


def test_partial_new_projection_does_not_replace_active(
    published_corpus: PublishedCorpus,
) -> None:
    f = published_corpus
    old = f.store.resolve_snapshot(None).snapshot_id
    plan = f.plan("new source text", base=old)
    bad = plan.model_copy(
        update={
            "slots": (),
            "snapshot_id": digest(
                "snapshot-v1",
                plan.workspace_id,
                plan.base_snapshot_id,
                plan.job_id,
                plan.config_sha,
                [member.model_dump(mode="json") for member in plan.members],
                [],
            ),
        }
    )
    bad = bad.model_copy(
        update={
            "manifest": reader().put_stream(
                BytesIO(publication_manifest(bad)), "application/json"
            )
        }
    )
    with pytest.raises(TychoraError, match="required"):
        f.store.publish_snapshot(bad)
    assert f.store.resolve_snapshot(None).snapshot_id == old


@pytest.fixture
def projected_corpus(tmp_path, config_factory) -> ProjectedCorpus:
    config = config_factory(dimensions=2)
    counter = RecordingCounter()
    db = open_corpus(tmp_path / "projected.sqlite3", initialize=True)
    vector = RecordingVector(tmp_path / "vectors")
    store = CorpusStore(
        db,
        registry=registry(),
        artifacts=reader(),
        required_slots=frozenset({"lexical", "text"}),
    )
    old = seed_bundle("old source text")
    db.execute(
        "INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES (?,1,?)",
        (old.workspace.workspace_id, old.workspace.created_at),
    )
    db.execute(
        "INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,?,?)",
        (
            old.collection.collection_id,
            old.workspace.workspace_id,
            old.collection.slug,
            old.collection.title,
            old.collection.access_floor,
            "{}",
        ),
    )
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"write", "read"}),
    )
    config_ref = reader().put_stream(
        BytesIO(
            b'{"type":"registry","schema_version":"1","data":{"profile":"synthetic"}}'
        ),
        "application/json",
    )
    jobs = JobRepository(
        db, artifacts=reader(), workspace_id=old.workspace.workspace_id
    )
    embedder = RecordingEmbedder(counter)
    coordinator = ProjectionCoordinator(
        store=store,
        artifacts=reader(),
        config=config,
        counter=counter,
        text_embedder=embedder,
        local_text_embedder=embedder,
        text_index=vector,
    )
    result = ProjectedCorpus(
        store, jobs, config_ref, scope, coordinator, vector, embedder
    )
    result.add_revision("old source text")
    yield result
    db.close()


def test_failed_vector_batch_preserves_active_snapshot(
    projected_corpus: ProjectedCorpus,
) -> None:
    f = projected_corpus
    old = f.store.resolve_snapshot(None).snapshot_id
    f.vector.fail_next_batch = True
    with pytest.raises(TychoraError) as error:
        f.add_revision("new source text")
    assert error.value.code == "PROVIDER_FAILURE"
    assert f.store.resolve_snapshot(None).snapshot_id == old


def test_identical_source_reuses_canonical_and_native_batches(
    projected_corpus: ProjectedCorpus,
) -> None:
    f = projected_corpus
    before = (f.embedder.calls, f.vector.upserts)
    old = f.store.resolve_snapshot(None)
    new_id = f.add_revision("old source text")
    new = f.store.resolve_snapshot(None)
    assert new_id != old.snapshot_id
    assert new.members == old.members
    assert new.slots[1].batches == old.slots[1].batches
    assert (f.embedder.calls, f.vector.upserts) == before


def test_cancelled_job_cannot_publish(projected_corpus: ProjectedCorpus) -> None:
    f = projected_corpus
    old = f.store.resolve_snapshot(None).snapshot_id
    _, job, plan = f.prepare_revision("cancelled source text")
    f.jobs.cancel(job.id)
    with pytest.raises(TychoraError, match="cancelled"):
        f.store.publish_snapshot(plan)
    assert f.store.resolve_snapshot(None).snapshot_id == old


def test_concurrent_source_additions_rebase_without_losing_membership(
    projected_corpus: ProjectedCorpus,
) -> None:
    f = projected_corpus
    base = f.store.resolve_snapshot(None).snapshot_id
    f.add_revision("first addition", source_key="first.md")
    second_bundle = seed_bundle("second addition", source_key="second.md").model_copy(
        update={"chunks": ()}
    )
    job = f.jobs.accept(
        "add", f.scope, None, {"sha": second_bundle.revision.original_sha}, f.config
    )
    f.jobs.start(job.id)
    stale = f.coordinator.build(second_bundle, base, job, ExecutionContext())
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(stale)
    assert error.value.code == "WORKSPACE_BUSY"
    rebased = f.coordinator.build(
        second_bundle, f.store.active_snapshot_id(), job, ExecutionContext()
    )
    f.store.publish_snapshot(rebased)
    selected = {member.source_id for member in f.store.resolve_snapshot(None).members}
    assert selected == {
        seed_bundle("old source text").source.source_id,
        seed_bundle("first addition", source_key="first.md").source.source_id,
        second_bundle.source.source_id,
    }


def test_old_reader_pin_survives_new_publication(
    projected_corpus: ProjectedCorpus,
) -> None:
    f = projected_corpus
    old = f.store.active_snapshot_id()
    assert old is not None
    with f.store.pin_snapshot(old) as reading:
        old_batch = reading.slots[1].batches[0]
        assert f.store.leases.is_pinned(old)
        assert f.store.leases.is_pinned(old_batch.batch_id)
        assert f.store.artifacts.payload_is_pinned(reading.manifest.sha256)
        new = f.add_revision("new source while reading", source_key="new.md")
        assert new != old
        assert f.store.resolve_snapshot(old).snapshot_id == old
        assert f.store.leases.is_pinned(old)
    assert not f.store.leases.is_pinned(old)


def test_retained_vector_plan_rebinds_native_index(
    projected_corpus: ProjectedCorpus,
) -> None:
    f = projected_corpus
    _, _, plan = f.prepare_revision(
        "rebind retained native vectors", source_key="rebind.md"
    )
    f.store.vector_indexes.clear()
    f.coordinator.attach(plan, ExecutionContext())
    assert f.store.publish_snapshot(plan) == plan.snapshot_id
