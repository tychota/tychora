"""Atomic snapshot publication against the real canonical SQLite schema."""

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from unittest.mock import patch
from uuid import UUID, uuid5

import pytest

from tests.helpers.corpus import COLLECTION, reader, registry, seed_bundle
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import publication_manifest
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import ExtractionBundle
from tychora.contracts.embeddings import EmbeddingCapabilities, EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import (
    canonical_bytes,
    digest,
    family_id,
    new_family_nonce,
    space_id,
    vector_record_id,
)
from tychora.contracts.identity import family_id as derive_family_id
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.publication import (
    IndexFamily,
    LexicalBatchReceipt,
    LexicalRecord,
    PublicationBatch,
    PublicationPlan,
    PublicationSlot,
    SnapshotMember,
)
from tychora.contracts.requests import RequestScope
from tychora.contracts.vectors import BatchReceipt
from tychora.evaluation.runner import ComparisonAdmission
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

    def embed_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]:
        raise AssertionError("document recording embedder received query embedding")

    def probe_queries(self, queries: tuple[str, ...], ctx: ExecutionContext):
        raise AssertionError("document recording embedder received query probe")


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
        self.creations = 0

    def create_family(self, spec):
        self.creations += 1
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

    def search(
        self,
        query_vector,
        eligible_records_and_owners,
        candidate_limit,
        work_limits,
        ctx,
    ):
        return self.inner.search(
            query_vector,
            eligible_records_and_owners,
            candidate_limit,
            work_limits,
            ctx,
        )

    def drop_records(self, record_ids, ctx):
        return self.inner.drop_records(record_ids, ctx)


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
        uow = SqliteUnitOfWork(self.store.db)
        with uow.read() as tx:
            nonce = tx.families.nonce("lexical", self.config.sha256)
        nonce = nonce or new_family_nonce()
        family_id = derive_family_id(
            bundle.workspace.workspace_id, self.config.sha256, nonce
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
        with uow.write() as tx:
            tx.families.create(family, nonce, self.config.sha256)
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
            lexical_records=(LexicalRecord(chunk_id=entries[0], body=text),),
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
def published_corpus(tmp_path) -> Iterator[PublishedCorpus]:
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


def make_projected_corpus(tmp_path, config_factory, *, legacy=False) -> ProjectedCorpus:
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
        "INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES (?,2,?)",
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
            canonical_bytes(old.collection.policy_json).decode(),
        ),
    )
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({COLLECTION}),
        capabilities=frozenset({"write", "read"}),
    )
    config_ref = reader().put_stream(
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
    if legacy:
        original_family = coordinator._text_family

        def preproof_family(*args):
            family, spec = original_family(*args)
            old_json = {
                key: value
                for key, value in family.config_json.items()
                if key != "capabilities"
            }
            # A pre-proof family row was stored without its capability proof.
            store.db.execute(
                "UPDATE index_families SET config_json=? WHERE family_id=?",
                (canonical_bytes(old_json).decode(), family.family_id),
            )
            return family.model_copy(
                update={"config_json": FrozenJsonMap(old_json)}
            ), spec

        with patch.object(coordinator, "_text_family", side_effect=preproof_family):
            result.add_revision("old source text")
    else:
        result.add_revision("old source text")
    return result


@pytest.fixture
def projected_corpus(tmp_path, config_factory) -> Iterator[ProjectedCorpus]:
    result = make_projected_corpus(tmp_path, config_factory)
    yield result
    result.store.db.close()


def test_native_health_reads_ready_vector_bytes_and_rejects_deleted_entry(
    projected_corpus,
):
    from types import SimpleNamespace

    from tychora.application.recovery import RecoveryService

    f = projected_corpus
    view = f.store.resolve_snapshot(None)
    slot = next(slot for slot in view.slots if slot.slot == "text")
    assert isinstance(slot.batches[0].receipt, BatchReceipt)
    service = RecoveryService(SimpleNamespace(store=f.store, db=f.store.db))
    before = service._verify_native_on_owner(ExecutionContext())
    assert before == (view.snapshot_id, slot.batches[0].receipt.count, 0, 0)
    record_id = slot.batches[0].receipt.records[0].record_id
    f.vector.inner._collection.delete(ids=[record_id])
    with pytest.raises(TychoraError, match="Ready native vector bytes differ"):
        service._verify_native_on_owner(ExecutionContext())


def test_read_only_chroma_family_open_does_not_create_missing_collection(
    projected_corpus, tmp_path
):
    slot = next(
        slot
        for slot in projected_corpus.store.resolve_snapshot(None).slots
        if slot.slot == "text"
    )
    spec = FamilySpec.model_validate_json(
        canonical_bytes(slot.family.config_json["spec"])
    )
    unopened = ChromaVectorIndex(tmp_path / "missing-family")
    assert unopened.open_family(spec) is None
    assert unopened._client.list_collections() == []


def test_preproof_text_family_rejects_new_publication_before_native_mutation(
    tmp_path, config_factory
):
    f = make_projected_corpus(tmp_path, config_factory, legacy=True)
    try:
        old = f.store.resolve_snapshot(None)
        slot = next(slot for slot in old.slots if slot.slot == "text")
        assert "capabilities" not in slot.family.config_json
        before = (f.vector.creations, f.vector.upserts, f.embedder.calls)
        with pytest.raises(TychoraError) as reuse_error:
            f.coordinator._retained_vector_spec(slot, "text")
        assert reuse_error.value.code == "CAPABILITY_MISSING"
        with pytest.raises(TychoraError) as error:
            f.add_revision("new source text", source_key="new.md")
        assert error.value.code == "CAPABILITY_MISSING"
        assert "new workspace" in str(error.value)
        assert (f.vector.creations, f.vector.upserts, f.embedder.calls) == before
        assert f.store.resolve_snapshot(None).snapshot_id == old.snapshot_id
    finally:
        f.store.db.close()


def test_semantic_recovery_uses_exact_retained_capability_order(projected_corpus):
    from tychora.application.recovery import verify_semantic_vector_family

    f = projected_corpus
    view = f.store.resolve_snapshot(None)
    family = next(slot.family for slot in view.slots if slot.slot == "text")
    config = f.store.retained_config_sha(f.config.sha256)
    origin_sha = family.config_json["effective_config_sha"]
    for modes in (["document", "query"], ["query", "document"]):
        capabilities = dict(family.config_json["capabilities"])
        capabilities["input_types"] = modes
        semantic_sha = digest(
            "text-family-config-v1",
            origin_sha,
            config.values.providers.text.model_dump(mode="json"),
            capabilities,
        )
        creation_uuid = str(uuid5(UUID(view.workspace_id), "text:" + semantic_sha))
        identity = family_id(view.workspace_id, semantic_sha, creation_uuid)
        spec = dict(family.config_json["spec"])
        spec.update(
            family_id=identity,
            config_sha=semantic_sha,
            creation_uuid=creation_uuid,
        )
        family_json = dict(family.config_json)
        family_json.update(spec=spec, capabilities=capabilities)
        retained = family.model_copy(
            update={"family_id": identity, "config_json": FrozenJsonMap(family_json)}
        )
        verify_semantic_vector_family(retained, config, view.workspace_id)

        capabilities["input_types"] = ["document", "document", "query"]
        family_json["capabilities"] = capabilities
        with pytest.raises(TychoraError, match="semantic proof differs"):
            verify_semantic_vector_family(
                retained.model_copy(update={"config_json": FrozenJsonMap(family_json)}),
                config,
                view.workspace_id,
            )


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


def test_native_recovery_replays_retained_vector_bytes_and_preserves_seal(
    projected_corpus, tmp_path
):
    from types import SimpleNamespace

    from tychora.application.recovery import RecoveryReport, RecoveryService

    f = projected_corpus
    view = f.store.resolve_snapshot(None)
    slot = next(slot for slot in view.slots if slot.slot == "text")
    batch = slot.batches[0]
    before_seal = f.store.db.execute(
        "SELECT receipt_json FROM index_batches WHERE batch_id=?", (batch.batch_id,)
    ).fetchone()[0]
    f.store.db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?", (batch.batch_id,)
    )
    f.store.db.commit()
    rebuilt = RecordingVector(tmp_path / "rebuilt-vectors")
    f.store.vector_indexes[slot.family.family_id] = rebuilt
    owner_scope = f.scope.model_copy(
        update={"capabilities": frozenset({"manage", "read", "write"})}
    )
    owner = SimpleNamespace(
        comparison_admission=ComparisonAdmission(),
        scope=owner_scope,
        store=f.store,
        db=f.store.db,
        artifacts=f.store.artifacts,
        path=tmp_path,
        workspace_id=view.workspace_id,
    )

    class IsolatedRecovery(RecoveryService):
        def _reconcile_on_owner(
            self, ctx, *, verify_payloads=False, verify_native=False
        ):
            pending = f.store.db.execute(
                "SELECT COUNT(*) FROM index_batches WHERE state='pending'"
            ).fetchone()[0]
            return RecoveryReport(view.workspace_id, 0, pending, 0, False)

    class InterruptedRecovery(IsolatedRecovery):
        def _write_physical_proof(self, batch_id, proof):
            super()._write_physical_proof(batch_id, proof)
            raise RuntimeError("interrupted after durable physical proof")

    before_provider = f.embedder.calls
    with pytest.raises(RuntimeError, match="interrupted after durable physical proof"):
        InterruptedRecovery(owner)._rebuild_native_on_owner(
            owner_scope, ExecutionContext(), ("text",)
        )
    assert (
        f.store.db.execute(
            "SELECT state FROM index_batches WHERE batch_id=?", (batch.batch_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert list((tmp_path / ".physical-verification").glob("*.json"))
    report = IsolatedRecovery(owner)._rebuild_native_on_owner(
        owner_scope, ExecutionContext(), ("text",)
    )
    assert report.native_pending_batches == 0
    assert f.embedder.calls == before_provider
    assert rebuilt.verify_batch(batch.receipt).ready
    assert f.store.db.execute(
        "SELECT state,receipt_json FROM index_batches WHERE batch_id=?",
        (batch.batch_id,),
    ).fetchone() == ("ready", before_seal)
    assert list((tmp_path / ".physical-verification").glob("*.json"))


@pytest.mark.parametrize(
    "alteration", ["semantic_family", "semantic_config", "physical_config", "namespace"]
)
def test_native_recovery_rejects_independent_family_identity_corruption(
    projected_corpus, alteration
):
    from tychora.application.recovery import vector_batch_records

    f = projected_corpus
    slot = next(
        slot for slot in f.store.resolve_snapshot(None).slots if slot.slot == "text"
    )
    family, batch = slot.family, slot.batches[0]
    config_json = dict(family.config_json)
    if alteration == "semantic_family":
        config_json["spec"] = dict(config_json["spec"], family_id="0" * 64)
    elif alteration == "semantic_config":
        config_json["spec"] = dict(config_json["spec"], config_sha="0" * 64)
    elif alteration == "physical_config":
        family = family.model_copy(update={"config_sha": "0" * 64})
    else:
        config_json["namespace_hash"] = "0" * 64
    family = family.model_copy(update={"config_json": config_json})
    with pytest.raises(
        TychoraError, match="Vector family (or receipt|specification) differs"
    ):
        vector_batch_records(family, batch, f.store.artifacts)


def test_native_recovery_rejects_backend_handle_namespace_corruption(
    projected_corpus, tmp_path
):
    from tychora.application.recovery import (
        vector_batch_records,
        verify_native_vector,
    )

    f = projected_corpus
    slot = next(
        slot for slot in f.store.resolve_snapshot(None).slots if slot.slot == "text"
    )

    class MisboundVector(RecordingVector):
        def create_family(self, spec):
            handle = super().create_family(spec)
            return handle.model_copy(update={"namespace_hash": "0" * 64})

    with pytest.raises(TychoraError, match="Native vector family identity differs"):
        verify_native_vector(
            slot.family,
            slot.batches[0],
            lambda: vector_batch_records(
                slot.family, slot.batches[0], f.store.artifacts
            ),
            MisboundVector(tmp_path / "misbound"),
            ExecutionContext(),
        )


def test_native_recovery_requires_retained_semantic_capabilities(projected_corpus):
    from tychora.application.recovery import verify_semantic_vector_family

    f = projected_corpus
    snapshot = f.store.resolve_snapshot(None)
    family = next(slot.family for slot in snapshot.slots if slot.slot == "text")
    origin = f.store.retained_config_sha(family.config_json["effective_config_sha"])
    missing = family.model_copy(
        update={
            "config_json": {
                key: value
                for key, value in family.config_json.items()
                if key != "capabilities"
            },
        }
    )
    with pytest.raises(TychoraError) as absent:
        verify_semantic_vector_family(missing, origin, snapshot.workspace_id)
    assert absent.value.code == "CAPABILITY_MISSING"
    altered_json = dict(family.config_json)
    altered_json["capabilities"] = dict(altered_json["capabilities"], endpoint="forged")
    with pytest.raises(TychoraError, match="semantic proof differs"):
        verify_semantic_vector_family(
            family.model_copy(update={"config_json": altered_json}),
            origin,
            snapshot.workspace_id,
        )


@pytest.mark.parametrize("alteration", ["family", "batch", "entries", "vector"])
def test_native_recovery_rejects_sql_drift_before_native_write(
    projected_corpus, tmp_path, alteration
):
    from types import SimpleNamespace

    from tychora.application.recovery import RecoveryReport, RecoveryService

    f = projected_corpus
    snapshot = f.store.resolve_snapshot(None)
    slot = next(slot for slot in snapshot.slots if slot.slot == "text")
    batch = slot.batches[0]
    db = f.store.db
    db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?", (batch.batch_id,)
    )
    if alteration == "family":
        db.execute(
            "UPDATE index_families SET backend_namespace='forged' WHERE family_id=?",
            (slot.family.family_id,),
        )
    elif alteration == "batch":
        db.execute(
            "UPDATE index_batches SET expected_count=expected_count+1 WHERE batch_id=?",
            (batch.batch_id,),
        )
    elif alteration == "entries":
        db.execute(
            "UPDATE index_batches SET entry_ids_sha=? WHERE batch_id=?",
            ("0" * 64, batch.batch_id),
        )
    else:
        db.execute(
            "UPDATE vector_records SET input_sha=? WHERE batch_id=?",
            ("0" * 64, batch.batch_id),
        )
    db.commit()
    rebuilt = RecordingVector(tmp_path / "unwritten")
    f.store.vector_indexes[slot.family.family_id] = rebuilt
    grant = f.scope.model_copy(update={"capabilities": frozenset({"manage"})})
    owner = SimpleNamespace(
        comparison_admission=ComparisonAdmission(),
        scope=grant,
        store=f.store,
        db=db,
        artifacts=f.store.artifacts,
        path=tmp_path,
        workspace_id=snapshot.workspace_id,
    )

    class IsolatedRecovery(RecoveryService):
        def _reconcile_on_owner(
            self, ctx, *, verify_payloads=False, verify_native=False
        ):
            return RecoveryReport(snapshot.workspace_id, 0, 1, 0, False)

    with pytest.raises(TychoraError, match="SQL differs"):
        IsolatedRecovery(owner)._rebuild_native_on_owner(
            grant, ExecutionContext(), ("text",)
        )
    assert rebuilt.upserts == 0
    assert (
        db.execute(
            "SELECT state FROM index_batches WHERE batch_id=?", (batch.batch_id,)
        ).fetchone()[0]
        == "pending"
    )


def test_native_gc_prunes_only_unprotected_vector_batch_then_rebuilds(
    projected_corpus, tmp_path
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from tychora.application.leases import LeaseSet
    from tychora.application.recovery import RecoveryReport, RecoveryService

    f = projected_corpus
    oldest = f.store.resolve_snapshot(None)
    old_batch = next(slot.batches[0] for slot in oldest.slots if slot.slot == "text")
    f.add_revision("second version")
    previous = f.store.resolve_snapshot(None)
    f.add_revision("third version")
    active = f.store.resolve_snapshot(None)
    for job_id, snapshot_id in f.store.db.execute(
        "SELECT job_id,result_snapshot_id FROM jobs WHERE state='running'"
    ).fetchall():
        f.jobs.complete(job_id, result_snapshot_id=snapshot_id)
    grant = f.scope.model_copy(
        update={"capabilities": frozenset({"manage", "read", "write"})}
    )
    owner = SimpleNamespace(
        comparison_admission=ComparisonAdmission(),
        scope=grant,
        store=f.store,
        db=f.store.db,
        artifacts=f.store.artifacts,
        path=tmp_path,
        workspace_id=active.workspace_id,
        leases=LeaseSet(),
        restore_deletion_suspended=False,
        maintenance_barrier=nullcontext,
    )

    class IsolatedRecovery(RecoveryService):
        def _reconcile_on_owner(
            self, ctx, *, verify_payloads=False, verify_native=False
        ):
            pending = f.store.db.execute(
                "SELECT COUNT(*) FROM index_batches WHERE state='pending'"
            ).fetchone()[0]
            return RecoveryReport(active.workspace_id, 0, pending, 0, False)

    recovery = IsolatedRecovery(owner)
    pruned = recovery._prune_native_on_owner(grant, ExecutionContext())
    assert old_batch.batch_id in pruned.pruned_batches
    for protected in (previous, active):
        batch = next(slot.batches[0] for slot in protected.slots if slot.slot == "text")
        assert batch.batch_id not in pruned.pruned_batches
        assert f.vector.verify_batch(batch.receipt).ready
    assert not f.vector.verify_batch(old_batch.receipt).ready
    assert (
        f.store.db.execute(
            "SELECT state FROM index_batches WHERE batch_id=?", (old_batch.batch_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert not recovery._prune_native_on_owner(grant, ExecutionContext()).pruned_batches
    recovery._rebuild_native_on_owner(
        grant, ExecutionContext(), ("text",), oldest.snapshot_id
    )
    assert f.vector.verify_batch(old_batch.receipt).ready


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


def test_archive_after_preparation_blocks_canonical_publication(projected_corpus):
    from datetime import UTC, datetime

    from tychora.application.collection_policy import parse_collection_policy

    corpus = projected_corpus
    bundle, _, plan = corpus.prepare_revision("Archive during publication")
    raw = corpus.store.db.execute(
        "SELECT policy_json FROM collections WHERE collection_id=?",
        (bundle.collection.collection_id,),
    ).fetchone()[0]
    policy = parse_collection_policy(raw).model_copy(
        update={"archived_at": datetime.now(UTC)}
    )
    active = corpus.store.active_snapshot_id()
    corpus.store.db.execute(
        "UPDATE collections SET policy_json=? WHERE collection_id=?",
        (policy.model_dump_json(), bundle.collection.collection_id),
    )
    corpus.store.db.execute("UPDATE workspaces SET policy_epoch=policy_epoch+1")
    corpus.store.db.commit()
    with pytest.raises(TychoraError) as error:
        corpus.store.publish_snapshot(plan)
    assert error.value.code == "FORBIDDEN_SCOPE"
    assert corpus.store.active_snapshot_id() == active


def test_vector_record_ids_are_bound_to_the_family(projected_corpus):
    f = projected_corpus
    rows = f.store.db.execute(
        "SELECT v.record_id,v.chunk_id,v.input_sha,b.family_id FROM vector_records v "
        "JOIN index_batches b USING(batch_id)"
    ).fetchall()
    assert rows
    for record_id, chunk_id, input_sha, family in rows:
        assert record_id == vector_record_id(family, chunk_id, input_sha)
    nonce = f.store.db.execute(
        "SELECT creation_nonce FROM index_families WHERE channel='text'"
    ).fetchone()[0]
    assert len(nonce) == 36


def test_large_text_batch_is_one_artifact(projected_corpus):
    f = projected_corpus
    text = "\n\n".join(
        f"Paragraph {index} records one outcome." for index in range(400)
    )
    f.add_revision(text, source_key="large.md")
    media = dict(
        f.store.db.execute(
            "SELECT media_type,COUNT(*) FROM artifacts GROUP BY media_type"
        ).fetchall()
    )
    assert "application/vnd.tychora.float32-le" not in media
    batches = f.store.db.execute(
        "SELECT b.batch_id,b.vectors_sha,COUNT(v.record_id) FROM index_batches b "
        "JOIN vector_records v USING(batch_id) GROUP BY b.batch_id"
    ).fetchall()
    assert batches and all(sha is not None for _, sha, _ in batches)
    assert media["application/vnd.tychora.vector-batch.v1"] == len(batches)
    indexes = [
        row[0]
        for row in f.store.db.execute(
            "SELECT row_index FROM vector_records WHERE batch_id=? ORDER BY record_id",
            (batches[-1][0],),
        )
    ]
    assert indexes == list(range(len(indexes)))


def test_missing_vector_batch_artifact_reports_cache_absent(projected_corpus):
    from tychora.application.recovery import vector_batch_records

    f = projected_corpus
    view = f.store.resolve_snapshot(None)
    slot = next(slot for slot in view.slots if slot.slot == "text")
    batch = slot.batches[0]
    missing = batch.model_copy(
        update={
            "vectors": batch.vectors.model_copy(
                update={"sha256": "0" * 64, "storage_key": "0" * 64}
            )
        }
    )
    with pytest.raises(TychoraError) as error:
        vector_batch_records(slot.family, missing, f.store.artifacts)
    assert error.value.code == "CAPABILITY_MISSING"
    assert error.value.reason == "vectors.cache_absent"


@pytest.mark.parametrize("state", ["delete_pending", "deleted"])
def test_readding_content_revives_a_released_vector_batch(projected_corpus, state):
    f = projected_corpus
    (sha,) = f.store.db.execute(
        "SELECT vectors_sha FROM index_batches WHERE vectors_sha IS NOT NULL"
    ).fetchone()
    stamp = "2026-01-01T00:00:00+00:00" if state == "deleted" else None
    f.store.db.execute(
        "UPDATE artifacts SET payload_state=?,payload_deleted_at=? WHERE artifact_sha=?",
        (state, stamp, sha),
    )
    f.store.db.commit()
    f.add_revision("old source text")
    assert f.store.db.execute(
        "SELECT payload_state,payload_deleted_at FROM artifacts WHERE artifact_sha=?",
        (sha,),
    ).fetchone() == ("present", None)


def _text_slot_batch(f):
    slot = next(s for s in f.store.resolve_snapshot(None).slots if s.slot == "text")
    return slot, slot.batches[0]


def _altered_vectors(f, batch, *, swap_ids=False):
    """A valid vector batch artifact whose rows differ from the sealed receipt."""
    from tychora.contracts.vector_batches import (
        VECTOR_BATCH_MEDIA_TYPE,
        VectorBatchRow,
        decode_vector_batch,
        encode_vector_batch,
    )

    with f.store.artifacts.open_verified(batch.vectors) as stream:
        decoded = decode_vector_batch(stream.read())
    rows = list(decoded.rows)
    if swap_ids:
        rows[0], rows[-1] = (
            rows[0].model_copy(update={"values": rows[-1].values}),
            rows[-1].model_copy(update={"values": rows[0].values}),
        )
        if rows[0].values == rows[-1].values:
            rows[0] = rows[0].model_copy(update={"values": (0.0, 1.0)})
    else:
        rows[0] = VectorBatchRow(
            record_id=rows[0].record_id, input_sha=rows[0].input_sha, values=(0.0, 1.0)
        )
        if rows[0].values == decoded.rows[0].values:
            rows[0] = rows[0].model_copy(update={"values": (1.0, 0.0)})
    return f.store.artifacts.put_stream(
        BytesIO(encode_vector_batch(decoded.family_id, decoded.dimension, rows)),
        VECTOR_BATCH_MEDIA_TYPE,
    )


def test_publication_rejects_a_valid_vector_artifact_with_altered_rows(
    projected_corpus,
):
    f = projected_corpus
    _, _, plan = f.prepare_revision("a second source text", source_key="b.md")
    slot = next(s for s in plan.slots if s.slot == "text")
    batch = slot.batches[-1]
    altered = batch.model_copy(update={"vectors": _altered_vectors(f, batch)})
    slots = tuple(
        s.model_copy(update={"batches": (*s.batches[:-1], altered)})
        if s.slot == "text"
        else s
        for s in plan.slots
    )
    forged = plan.model_copy(update={"slots": slots})
    forged = forged.model_copy(
        update={
            "manifest": f.store.artifacts.put_stream(
                BytesIO(publication_manifest(forged)), "application/json"
            )
        }
    )
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(forged)
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "payload.hash_mismatch",
    )


def test_vector_batch_records_reject_an_artifact_that_differs_from_the_receipt(
    projected_corpus,
):
    from tychora.application.recovery import vector_batch_records

    f = projected_corpus
    slot, batch = _text_slot_batch(f)
    forged = batch.model_copy(update={"vectors": _altered_vectors(f, batch)})
    with pytest.raises(TychoraError) as error:
        vector_batch_records(slot.family, forged, f.store.artifacts)
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "canonical.binding_mismatch",
    )


def test_receipt_mismatch_is_reported_before_an_absent_vector_batch(projected_corpus):
    from tychora.application.recovery import vector_batch_records

    f = projected_corpus
    slot, batch = _text_slot_batch(f)
    tampered = (
        batch.vector_records[0].model_copy(update={"value_sha": "1" * 64}),
        *batch.vector_records[1:],
    )
    broken = batch.model_copy(
        update={
            "vector_records": tampered,
            "vectors": batch.vectors.model_copy(
                update={"sha256": "0" * 64, "storage_key": "0" * 64}
            ),
        }
    )
    with pytest.raises(TychoraError) as error:
        vector_batch_records(slot.family, broken, f.store.artifacts)
    assert error.value.code == "INTEGRITY_ERROR"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_revival_is_refused_without_verified_fresh_bytes(projected_corpus, damage):
    from tychora.adapters.sqlite.publication import _register_artifact

    f = projected_corpus
    _, batch = _text_slot_batch(f)
    ref = batch.vectors
    f.store.db.execute(
        "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=?",
        (ref.sha256,),
    )
    path = f.store.artifacts.root / ref.storage_key
    assert path.exists()
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    with pytest.raises(TychoraError) as error:
        _register_artifact(f.store, ref, revive=True)
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason in {"payload.missing", "payload.hash_mismatch"}
    assert f.store.db.execute(
        "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (ref.sha256,)
    ).fetchone() == ("delete_pending",)


def _chroma_metadata(vector: RecordingVector, record_ids: list[str]) -> list[dict]:
    collection, _ = vector.inner._require()
    return collection.get(ids=record_ids, include=["metadatas"])["metadatas"]


def test_text_batches_record_revision_extraction_and_unit_in_chroma(projected_corpus):
    f = projected_corpus
    slot = next(
        slot for slot in f.store.resolve_snapshot(None).slots if slot.slot == "text"
    )
    batch = slot.batches[0]
    units = dict(
        f.store.db.execute(
            "SELECT chunk_id, unit_id FROM chunks WHERE extraction_id=?",
            (batch.extraction_id,),
        ).fetchall()
    )
    revision_id = f.store.db.execute(
        "SELECT revision_id FROM extractions WHERE extraction_id=?",
        (batch.extraction_id,),
    ).fetchone()[0]
    stored = _chroma_metadata(f.vector, [row.record_id for row in batch.vector_records])
    assert len(stored) == len(batch.vector_records) > 0
    for binding, metadata in zip(batch.vector_records, stored, strict=True):
        assert metadata["owner_kind"] == "unit"
        assert metadata["owner_id"] == units[binding.chunk_id]
        assert metadata["extraction_id"] == batch.extraction_id
        assert metadata["revision_id"] == revision_id


def test_native_rebuild_restores_owner_metadata(projected_corpus, tmp_path):
    from tychora.adapters.sqlite.vector_owners import vector_owner_rows
    from tychora.application.recovery import (
        vector_batch_records,
        verify_native_vector,
    )

    f = projected_corpus
    slot = next(
        slot for slot in f.store.resolve_snapshot(None).slots if slot.slot == "text"
    )
    batch = slot.batches[0]
    ids = [row.record_id for row in batch.vector_records]
    original = _chroma_metadata(f.vector, ids)
    fresh = RecordingVector(tmp_path / "rebuilt")
    verify_native_vector(
        slot.family,
        batch,
        lambda: vector_batch_records(
            slot.family,
            batch,
            f.store.artifacts,
            owner_rows=vector_owner_rows(f.store.db, batch.batch_id),
        ),
        fresh,
        ExecutionContext(),
    )
    assert _chroma_metadata(fresh, ids) == original


@pytest.mark.parametrize("damage", ["no_owner", "partial", "unknown_record"])
def test_native_rebuild_rejects_owner_rows_that_differ_from_the_batch(
    projected_corpus, tmp_path, damage
):
    from tychora.adapters.sqlite.vector_owners import vector_owner_rows
    from tychora.application.recovery import (
        vector_batch_records,
        verify_native_vector,
    )

    f = projected_corpus
    slot = next(
        slot for slot in f.store.resolve_snapshot(None).slots if slot.slot == "text"
    )
    batch = slot.batches[0]
    rows = vector_owner_rows(f.store.db, batch.batch_id)
    if damage == "no_owner":
        rows = (rows[0]._replace(unit=None, asset=None), *rows[1:])
    elif damage == "partial":
        rows = rows[:-1] if len(rows) > 1 else (rows[0]._replace(record="x"),)
    else:
        rows = (rows[0]._replace(record="0" * 64), *rows[1:])
    with pytest.raises(TychoraError) as raised:
        verify_native_vector(
            slot.family,
            batch,
            lambda: vector_batch_records(
                slot.family, batch, f.store.artifacts, owner_rows=rows
            ),
            RecordingVector(tmp_path / "damaged"),
            ExecutionContext(),
        )
    assert raised.value.code == "INTEGRITY_ERROR"
    assert raised.value.reason == "canonical.binding_mismatch"


def test_asset_rows_rebuild_asset_owners():
    from tychora.adapters.sqlite.vector_owners import OwnerRow
    from tychora.application.recovery import _vector_owners

    (owner,) = _vector_owners((OwnerRow("rec", "rev", "ext", None, "asset-1"),))
    assert (owner.owner_kind, owner.owner_id) == ("asset", "asset-1")
    assert (owner.revision_id, owner.extraction_id) == ("rev", "ext")


def test_image_batches_own_each_record_by_its_asset():
    from types import SimpleNamespace
    from typing import cast

    from tychora.indexing.coordinator import _asset_owners

    bundle = SimpleNamespace(
        revision=SimpleNamespace(revision_id="r" * 64),
        extraction=SimpleNamespace(extraction_id="x" * 64),
    )
    (owner,) = _asset_owners(cast(ExtractionBundle, bundle), {"rec": "a" * 64})
    assert (owner.owner_kind, owner.owner_id) == ("asset", "a" * 64)
    assert (owner.revision_id, owner.extraction_id) == ("r" * 64, "x" * 64)
