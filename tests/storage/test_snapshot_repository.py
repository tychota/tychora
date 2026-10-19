"""Pages are content-addressed rows; inserting one twice is a no-op."""

import sqlite3
from hashlib import sha256
from io import BytesIO

import pytest

from tests.helpers.corpus import reader
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.errors import TychoraError
from tychora.contracts.publication import LexicalRecord
from tychora.contracts.snapshots import (
    MembershipPage,
    PageMember,
    bucket_of,
    compute_page_id,
    membership_sha,
)


def _page(db, uow, batch):
    chunk_id = db.execute("SELECT chunk_id FROM chunks").fetchone()[0]
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.write_lexical(batch.batch_id, (LexicalRecord(chunk_id=chunk_id),))
        tx.batches.mark_ready(batch.batch_id, 1)
    with uow.read() as tx:
        row = tx.batches.get(batch.batch_id)
        assert row is not None
        page_batch = row.page_batch()
    source_id, revision_id = db.execute(
        "SELECT source_id,revision_id FROM revisions"
    ).fetchone()
    member = PageMember(
        source_id=source_id,
        revision_id=revision_id,
        extraction_id=batch.extraction_id,
        batches=(page_batch,),
    )
    bucket = bucket_of(source_id)
    page = MembershipPage(
        bucket=bucket, members=(member,), page_id=compute_page_id(bucket, (member,))
    )
    manifest = reader().put_stream(BytesIO(page.manifest_bytes()), "application/json")
    return page, manifest


def _snapshot(db, page: MembershipPage, manifest) -> str:
    snapshot_id = "s" * 64
    db.execute(
        "INSERT INTO snapshots (snapshot_id,workspace_id,membership_sha,config_sha,"
        "manifest_sha,published_at,job_id,member_count) "
        "SELECT ?,workspace_id,?,?,?,'2026-10-16T00:00:00+00:00','job',1 FROM workspaces",
        (snapshot_id, membership_sha((page.page_id,)), "c" * 64, manifest.sha256),
    )
    db.execute(
        "INSERT INTO snapshot_pages (snapshot_id,bucket,page_id) VALUES (?,?,?)",
        (snapshot_id, page.bucket, page.page_id),
    )
    return snapshot_id


def test_insert_page_round_trips_and_is_idempotent(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        tx.snapshots.insert_page(page, manifest)
    with uow.read() as tx:
        assert tx.snapshots.page_manifests((page.page_id,)) == {page.page_id: manifest}
    assert db.execute("SELECT COUNT(*) FROM page_members").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM page_batches").fetchone()[0] == 1
    assert manifest.sha256 == sha256(page.manifest_bytes()).hexdigest()


def test_page_is_read_back_through_its_snapshot(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        snapshot_id = _snapshot(db, page, manifest)
    with uow.read() as tx:
        assert tx.snapshots.page(snapshot_id, page.bucket) == page
        assert tx.snapshots.page(snapshot_id, (page.bucket + 1) % 256) is None


def test_page_rows_that_differ_from_their_identity_are_refused(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        snapshot_id = _snapshot(db, page, manifest)
    with uow.write():
        db.execute("UPDATE page_members SET is_default=0")
    with pytest.raises(TychoraError) as error, uow.read() as tx:
        tx.snapshots.page(snapshot_id, page.bucket)
    assert error.value.reason == "canonical.binding_mismatch"


def test_insert_page_refuses_a_different_manifest(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    other = reader().put_stream(BytesIO(b'{"other":1}'), "application/json")
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.artifacts.register(other)
        tx.snapshots.insert_page(page, manifest)
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.snapshots.insert_page(page, other)
    assert error.value.reason == "canonical.binding_mismatch"


def test_unknown_page_manifest_is_refused(seeded_batch):
    _, uow, _, _ = seeded_batch
    with pytest.raises(TychoraError) as error, uow.read() as tx:
        tx.snapshots.page_manifests(("missing",))
    assert error.value.reason == "canonical.binding_mismatch"


def test_active_snapshot_switch(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.read() as tx:
        assert tx.snapshots.active_id() is None
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        snapshot_id = _snapshot(db, page, manifest)
        tx.snapshots.set_active(snapshot_id)
    with uow.read() as tx:
        assert tx.snapshots.active_id() == snapshot_id


@pytest.mark.parametrize("write", ["page", "active"])
def test_read_transaction_cannot_write_snapshots(seeded_batch, write):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.write() as tx:
        tx.artifacts.register(manifest)
    with pytest.raises(TychoraError) as error, uow.read() as tx:
        if write == "page":
            tx.snapshots.insert_page(page, manifest)
        else:
            tx.snapshots.set_active("s" * 64)
    assert error.value.reason == "invariant.unexpected"
    assert db.execute("SELECT COUNT(*) FROM membership_pages").fetchone()[0] == 0


def test_snapshot_page_bucket_must_match_the_page(seeded_batch):
    db, uow, batch, _ = seeded_batch
    page, manifest = _page(db, uow, batch)
    with uow.write() as tx:
        tx.artifacts.register(manifest)
        tx.snapshots.insert_page(page, manifest)
        _snapshot(db, page, manifest)
    with pytest.raises(sqlite3.IntegrityError), uow.write():
        db.execute(
            "INSERT INTO snapshot_pages (snapshot_id,bucket,page_id) VALUES (?,?,?)",
            ("s" * 64, (page.bucket + 1) % 256, page.page_id),
        )
    indexes = {
        row[0]
        for row in db.execute(
            "SELECT name FROM pragma_index_list('snapshot_root_batches')"
        )
    }
    assert "snapshot_root_batches_batch" in indexes


def test_missing_workspace_row_is_an_invalid_row(tmp_path):
    db = open_corpus(tmp_path / "w.sqlite3", initialize=True)
    uow = SqliteUnitOfWork(db)
    with pytest.raises(TychoraError) as error, uow.read() as tx:
        tx.snapshots.active_id()
    assert error.value.reason == "canonical.row_invalid"
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.snapshots.set_active("s" * 64)
    assert error.value.reason == "canonical.row_invalid"
    db.close()
