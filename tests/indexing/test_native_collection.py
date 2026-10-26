"""Snapshot collection drops native records only of batches no snapshot keeps."""

from types import SimpleNamespace

import pytest

from tests.indexing.test_publication import ProjectedCorpus, make_projected_corpus
from tychora.adapters.sqlite.repositories import snapshots as snapshot_rows
from tychora.application.snapshot_gc import SnapshotGc
from tychora.retrieval.catalog import SnapshotCatalog
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def corpus(tmp_path, config_factory, monkeypatch):
    f = make_projected_corpus(tmp_path, config_factory)
    for text in ("second version", "third version", "fourth version"):
        f.add_revision(text)
    for job_id, snapshot_id in f.store.db.execute(
        "SELECT job_id,result_snapshot_id FROM jobs WHERE state='running'"
    ).fetchall():
        f.jobs.complete(job_id, result_snapshot_id=snapshot_id)
    monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
    yield f
    f.store.db.close()


def _owner(f: ProjectedCorpus):
    return SimpleNamespace(
        store=f.store,
        db=f.store.db,
        artifacts=f.store.artifacts,
        leases=f.store.leases,
        workspace_id=f.store.resolve_snapshot(None).workspace_id,
        restore_deletion_suspended=False,
        catalog=SnapshotCatalog(),
    )


def _text_batches(f: ProjectedCorpus, snapshot_id: str):
    view = f.store.resolve_snapshot(snapshot_id)
    return next(s for s in f.store.snapshot_slots(view) if s.slot == "text").batches


def _snapshot_ids(f: ProjectedCorpus) -> list[str]:
    return [
        row[0]
        for row in f.store.db.execute(
            "SELECT snapshot_id FROM snapshots ORDER BY published_at,snapshot_id"
        )
    ]


def test_collection_drops_native_records_of_collected_batches_only(corpus):
    f = corpus
    ids = _snapshot_ids(f)
    collected = [b for sid in ids[:2] for b in _text_batches(f, sid)]
    kept = [b for sid in ids[2:] for b in _text_batches(f, sid)]
    dropped: list[str] = []
    drop = f.vector.drop_records

    def recording(record_ids, ctx):
        dropped.extend(record_ids)
        return drop(record_ids, ctx)

    f.vector.drop_records = recording
    summary = SnapshotGc(_owner(f)).collect(ExecutionContext())
    # The lexical and text batches of both collected snapshots go.
    assert summary.snapshots == 2 and summary.batches == 4
    assert summary.deferred_batches == 0
    assert set(dropped) == {r.record_id for b in collected for r in b.vector_records}
    for batch in kept:
        assert f.vector.verify_batch(batch.receipt).ready
    for batch in collected:
        assert not f.vector.verify_batch(batch.receipt).ready
        assert f.store.batch_cache.get(batch.batch_id) is None


def test_batches_of_a_detached_index_wait_and_can_be_built_again(corpus):
    f = corpus
    ids = _snapshot_ids(f)
    first, second = (_text_batches(f, sid)[0] for sid in ids[:2])
    attached = dict(f.store.vector_indexes)
    f.store.vector_indexes.clear()
    summary = SnapshotGc(_owner(f)).collect(ExecutionContext())
    assert summary.snapshots == 2
    # Lexical batches need no native store; text batches wait for theirs.
    assert summary.batches == 2 and summary.deferred_batches == 2
    states = dict(
        f.store.db.execute(
            "SELECT batch_id,state FROM index_batches WHERE batch_id IN (?,?)",
            (first.batch_id, second.batch_id),
        ).fetchall()
    )
    assert states == {first.batch_id: "removed", second.batch_id: "removed"}
    assert f.vector.verify_batch(first.receipt).ready
    assert f.store.batch_cache.get(first.batch_id) is None
    # A removed batch builds again when its content returns.
    f.store.vector_indexes.update(attached)
    f.add_revision("old source text")
    assert f.store.db.execute(
        "SELECT state FROM index_batches WHERE batch_id=?", (first.batch_id,)
    ).fetchone() == ("ready",)
    for job_id, snapshot_id in f.store.db.execute(
        "SELECT job_id,result_snapshot_id FROM jobs WHERE state='running'"
    ).fetchall():
        f.jobs.complete(job_id, result_snapshot_id=snapshot_id)
    retry = SnapshotGc(_owner(f)).collect(ExecutionContext(), include_unpublished=True)
    assert retry.deferred_batches == 0
    assert (
        f.store.db.execute(
            "SELECT 1 FROM index_batches WHERE batch_id=?", (second.batch_id,)
        ).fetchone()
        is None
    )
    assert f.vector.verify_batch(first.receipt).ready
    assert not f.vector.verify_batch(second.receipt).ready


def test_published_only_pass_retries_batches_deferred_for_a_closed_store(corpus):
    f = corpus
    ids = _snapshot_ids(f)
    deferred = [_text_batches(f, sid)[0] for sid in ids[:2]]
    attached = dict(f.store.vector_indexes)
    f.store.vector_indexes.clear()
    first = SnapshotGc(_owner(f)).collect(ExecutionContext())
    assert first.deferred_batches == 2
    f.store.vector_indexes.update(attached)
    retry = SnapshotGc(_owner(f)).collect(ExecutionContext())
    assert retry.deferred_batches == 0 and retry.batches == 2
    for batch in deferred:
        assert not f.vector.verify_batch(batch.receipt).ready
        assert (
            f.store.db.execute(
                "SELECT 1 FROM index_batches WHERE batch_id=?", (batch.batch_id,)
            ).fetchone()
            is None
        )
