"""Publication rewrites one page, verifies only new batches, and binds through SQL."""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from tests.indexing.test_publication import ProjectedCorpus, make_projected_corpus
from tychora.adapters.sqlite.repositories import SqliteSnapshotRepository
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import bucket_of


@pytest.fixture
def projected(tmp_path, config_factory) -> Iterator[ProjectedCorpus]:
    corpus = make_projected_corpus(tmp_path, config_factory)
    yield corpus
    corpus.store.db.close()


def _pages(store, snapshot_id):
    return dict(
        store.db.execute(
            "SELECT bucket,page_id FROM snapshot_pages WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
    )


def _source_id(store, source_key):
    return store.db.execute(
        "SELECT source_id FROM sources WHERE source_key=?", (source_key,)
    ).fetchone()[0]


def test_add_rewrites_only_its_bucket(projected):
    f = projected
    first = f.add_revision("first source text", source_key="a.md")
    second = f.add_revision("second source text", source_key="b.md")
    before, after = _pages(f.store, first), _pages(f.store, second)
    changed = {bucket for bucket in after if before.get(bucket) != after[bucket]}
    assert changed == {bucket_of(_source_id(f.store, "b.md"))}


def test_views_equal_the_plan(projected):
    f = projected
    base = f.store.active_snapshot_id()
    _, _, plan = f.prepare_revision("view equivalence text", source_key="v.md")
    snapshot_id = f.store.publish_snapshot(plan)
    members = set(
        f.store.db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default "
            "FROM snapshot_members WHERE snapshot_id=?",
            (snapshot_id,),
        )
    )
    repository = SqliteSnapshotRepository(f.store.db)
    pages = list(plan.new_pages)
    for bucket, page_id in _pages(f.store, base).items():
        if page_id in plan.reused_page_ids:
            page = repository.page(base, bucket)
            assert page is not None
            pages.append(page)
    assert sorted(page.page_id for page in pages) == list(plan.page_ids)
    expected = {
        (m.source_id, m.revision_id, m.extraction_id, int(m.is_default))
        for page in pages
        for m in page.members
    }
    assert members == expected
    assert plan.member_count == len(expected)
    batches = {
        row[0]
        for row in f.store.db.execute(
            "SELECT batch_id FROM snapshot_batches WHERE snapshot_id=?", (snapshot_id,)
        )
    }
    bound = {b.batch_id for page in pages for m in page.members for b in m.batches}
    assert batches == bound | {b.batch_id for b in plan.root_batches}
    assert {b.batch_id for b in plan.new_batches} <= batches


def test_reused_batches_are_never_reopened(projected):
    f = projected
    f.add_revision("earlier source text", source_key="early.md")
    _, _, plan = f.prepare_revision("later source text", source_key="late.md")
    new_ids = {b.batch_id for b in plan.new_batches}
    opened = []
    original = f.store.artifacts.open_verified

    def spy(ref):
        opened.append(ref.sha256)
        return original(ref)

    verified = []
    original_verify = f.vector.verify_batch

    def verify_spy(receipt):
        verified.append(receipt.digest)
        return original_verify(receipt)

    with (
        patch.object(f.store.artifacts, "open_verified", spy),
        patch.object(f.vector, "verify_batch", verify_spy),
    ):
        f.store.publish_snapshot(plan)
    new_vectors = {b.vectors.sha256 for b in plan.new_batches if b.vectors is not None}
    new_inputs = {b.input_manifest.sha256 for b in plan.new_batches}
    assert set(opened) <= new_vectors | new_inputs | {plan.manifest.sha256}
    assert len(verified) == sum(1 for b in plan.new_batches if b.slot == "text")
    assert new_ids


def test_new_edition_rewrites_only_its_bucket(projected):
    f = projected
    f.add_revision("neighbour text", source_key="n.md")
    first = f.add_revision("edition one text", source_key="e.md")
    second = f.add_edition("edition two text", source_key="e.md", edition="2")
    before, after = _pages(f.store, first), _pages(f.store, second)
    assert sum(1 for b in after if before.get(b) != after[b]) == 1
    rows = f.store.db.execute(
        "SELECT r.edition_key,m.is_default FROM snapshot_members m "
        "JOIN revisions r USING(revision_id) "
        "JOIN sources s ON s.source_id=m.source_id "
        "WHERE m.snapshot_id=? AND s.source_key='e.md'",
        (second,),
    ).fetchall()
    assert sorted(rows) == [("1", 0), ("2", 1)]


def test_publication_refuses_reused_batch_that_is_not_ready(projected):
    f = projected
    f.add_revision("first", source_key="same.md")
    _, _, plan = f.prepare_revision("first", source_key="same.md")
    page_batches = {
        b.batch_id for page in plan.new_pages for m in page.members for b in m.batches
    } - {b.batch_id for b in plan.new_batches}
    assert page_batches
    f.store.db.execute(
        "UPDATE index_batches SET state='pending' WHERE batch_id=?",
        (min(page_batches),),
    )
    f.store.db.commit()
    active = f.store.active_snapshot_id()
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(plan)
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "batch.missing",
    )
    assert f.store.active_snapshot_id() == active


def test_resolve_snapshot_never_reads_the_root_manifest(projected):
    f = projected
    snapshot_id = f.add_revision("resolve from SQL", source_key="r.md")
    opened = []
    original = f.store.artifacts.open_verified

    def spy(ref):
        opened.append(ref.sha256)
        return original(ref)

    with patch.object(f.store.artifacts, "open_verified", spy):
        view = f.store.resolve_snapshot(snapshot_id)
        with f.store.pin_snapshot(snapshot_id):
            pass
    assert opened == []
    assert view.member_count == len(view.members)


@pytest.mark.parametrize("point", ["insert_page", "insert_snapshot", "set_active"])
def test_crash_inside_activation_keeps_the_previous_snapshot(projected, point):
    f = projected
    active = f.store.active_snapshot_id()
    before = f.store.resolve_snapshot(active)
    before_slots = f.store.snapshot_slots(before)
    _, _, plan = f.prepare_revision("crash during activation", source_key="crash.md")
    with (
        patch.object(
            SqliteSnapshotRepository, point, side_effect=RuntimeError("killed")
        ),
        pytest.raises(RuntimeError, match="killed"),
    ):
        f.store.publish_snapshot(plan)
    assert f.store.active_snapshot_id() == active
    # Writing the new lexical batch moves only the corpus-wide statistics epoch.
    after = f.store.resolve_snapshot(None)
    assert after == before.model_copy(
        update={"lexical_statistics_epoch": after.lexical_statistics_epoch}
    )
    assert f.store.snapshot_slots(before) == before_slots
    new_pages = [page.page_id for page in plan.new_pages]
    assert (
        f.store.db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE snapshot_id=?", (plan.snapshot_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        f.store.db.execute(
            "SELECT COUNT(*) FROM membership_pages WHERE page_id IN (?)", new_pages
        ).fetchone()[0]
        == 0
    )
    assert f.store.publish_snapshot(plan) == plan.snapshot_id
    assert f.store.active_snapshot_id() == plan.snapshot_id


def test_crash_after_batches_are_ready_resumes_without_rebuilding(projected):
    f = projected
    f.prepare_revision("resume after a crash", source_key="resume.md")
    upserts, calls = f.vector.upserts, f.embedder.calls
    _, _, plan = f.prepare_revision("resume after a crash", source_key="resume.md")
    assert {batch.slot for batch in plan.new_batches} <= {"graph"}
    assert (f.vector.upserts, f.embedder.calls) == (upserts, calls)
    assert f.store.publish_snapshot(plan) == plan.snapshot_id


def test_stale_base_is_a_retryable_rebase(projected):
    f = projected
    _, _, stale = f.prepare_revision("stale base text", source_key="stale.md")
    f.add_revision("another publication", source_key="other.md")
    with pytest.raises(TychoraError) as error:
        f.store.publish_snapshot(stale)
    assert (error.value.code, error.value.reason) == (
        "WORKSPACE_BUSY",
        "publication.rebase",
    )
    assert error.value.retryable


def test_unknown_snapshot_is_absent(projected):
    with pytest.raises(TychoraError) as error:
        projected.store.resolve_snapshot("0" * 64)
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "snapshot.absent")


def test_retained_slots_rebuild_the_published_batches(projected):
    f = projected
    _, _, plan = f.prepare_revision("retained slot text", source_key="slot.md")
    snapshot_id = f.store.publish_snapshot(plan)
    view = f.store.resolve_snapshot(snapshot_id)
    slots = {slot.slot: slot for slot in f.store.snapshot_slots(view)}
    assert set(slots) == {item.slot for item in plan.families}
    for batch in plan.new_batches:
        retained = next(
            item
            for item in slots[batch.slot].batches
            if item.batch_id == batch.batch_id
        )
        assert (retained.receipt, retained.input_manifest, retained.vectors) == (
            batch.receipt,
            batch.input_manifest,
            batch.vectors,
        )
        if batch.slot == "lexical":
            assert len(retained.lexical_records) == batch.expected_count
        if batch.slot == "text":
            assert len(retained.vector_records) == batch.expected_count
