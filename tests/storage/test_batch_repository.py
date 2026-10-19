"""Batch rows move building -> ready; ready counts are one statement."""

import pytest

from tychora.contracts.errors import TychoraError
from tychora.contracts.publication import LexicalRecord
from tychora.ports.repositories import VectorRecordRow


def _extra_chunk(db, chunk_id: str) -> None:
    unit_id, extraction_id = db.execute(
        "SELECT unit_id,extraction_id FROM chunks"
    ).fetchone()
    db.execute(
        "INSERT INTO chunks (chunk_id,unit_id,extraction_id,ordinal_in_unit,char_start,"
        "char_end,context_group_key,chunker_fingerprint,token_count) "
        "VALUES (?,?,?,1,1,4,'same','other',1)",
        (chunk_id, unit_id, extraction_id),
    )


def _epoch(db) -> int:
    return db.execute("SELECT lexical_statistics_epoch FROM workspaces").fetchone()[0]


def test_batch_moves_from_building_to_ready_and_bumps_epoch_once(seeded_batch):
    db, uow, batch, entries = seeded_batch
    before = _epoch(db)
    with uow.write() as tx:
        tx.batches.begin(batch)
        row = tx.batches.get(batch.batch_id)
        assert row is not None and row.state == "building"
        tx.batches.write_lexical(
            batch.batch_id, (LexicalRecord(chunk_id=entries[0], body="Record"),)
        )
        tx.batches.mark_ready(batch.batch_id, 1)
    assert _epoch(db) == before + 1
    with uow.read() as tx:
        row = tx.batches.find(batch.family_id, batch.extraction_id)
        assert row is not None and row.state == "ready" and row.actual_count == 1
        assert tx.batches.count_ready((batch.batch_id, "missing")) == 1
        assert row.page_batch() == batch.page_batch()
        assert tx.batches.find(batch.family_id, "other") is None
        assert tx.batches.get("missing") is None


def test_lexical_epoch_bumps_once_per_batch_not_per_row(seeded_batch):
    db, uow, batch, entries = seeded_batch
    with uow.write():
        _extra_chunk(db, "extra-chunk")
    before = _epoch(db)
    records = (
        LexicalRecord(chunk_id=entries[0], body="Record"),
        LexicalRecord(chunk_id="extra-chunk", body="Other"),
    )
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.write_lexical(batch.batch_id, records)
        assert _epoch(db) == before
        tx.batches.mark_ready(batch.batch_id, 2)
        tx.batches.mark_ready(batch.batch_id, 2)
    assert _epoch(db) == before + 1
    hits = db.execute(
        "SELECT COUNT(*) FROM lexical_fts WHERE lexical_fts MATCH 'record OR other'"
    ).fetchone()[0]
    assert hits == 2


def test_write_lexical_refuses_a_different_immutable_row(seeded_batch):
    _, uow, batch, entries = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.write_lexical(
            batch.batch_id, (LexicalRecord(chunk_id=entries[0], body="Record"),)
        )
        tx.batches.write_lexical(
            batch.batch_id, (LexicalRecord(chunk_id=entries[0], body="Record"),)
        )
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.write_lexical(
            batch.batch_id, (LexicalRecord(chunk_id=entries[0], body="Changed"),)
        )
    assert error.value.reason == "canonical.binding_mismatch"


def test_write_vectors_is_idempotent_and_refuses_a_different_row(seeded_batch):
    db, uow, batch, entries = seeded_batch
    row = VectorRecordRow(
        record_id="r" * 64,
        chunk_id=entries[0],
        asset_id=None,
        input_sha="a" * 64,
        row_index=0,
    )
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.write_vectors(batch.batch_id, (row,))
        tx.batches.write_vectors(batch.batch_id, (row,))
    assert db.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0] == 1
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.write_vectors(
            batch.batch_id, (row.model_copy(update={"input_sha": "b" * 64}),)
        )
    assert error.value.reason == "canonical.binding_mismatch"


def test_begin_is_idempotent_for_the_same_batch(seeded_batch):
    _, uow, batch, _ = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.begin(batch)


def test_begin_refuses_a_different_row_under_the_same_batch_id(seeded_batch):
    _, uow, batch, _ = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
    forged = batch.model_copy(update={"expected_count": 2})
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.begin(forged)
    assert error.value.reason == "canonical.binding_mismatch"


def test_mark_ready_requires_a_known_batch(seeded_batch):
    _, uow, _, _ = seeded_batch
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.mark_ready("unknown", 0)
    assert error.value.reason == "canonical.binding_mismatch"


def test_mark_ready_refuses_a_different_count_once_ready(seeded_batch):
    _, uow, batch, _ = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
        tx.batches.mark_ready(batch.batch_id, 1)
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.batches.mark_ready(batch.batch_id, 2)
    assert error.value.reason == "canonical.binding_mismatch"


def test_a_building_batch_is_not_publishable(seeded_batch):
    _, uow, batch, _ = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
        row = tx.batches.get(batch.batch_id)
        assert row is not None
        assert tx.batches.count_ready((batch.batch_id,)) == 0
        with pytest.raises(TychoraError) as error:
            row.page_batch()
    assert error.value.reason == "batch.missing"


@pytest.mark.parametrize("write", ["begin", "lexical", "vectors", "ready"])
def test_read_transaction_cannot_write_batches(seeded_batch, write):
    db, uow, batch, entries = seeded_batch
    with uow.write() as tx:
        tx.batches.begin(batch)
    calls = {
        "begin": lambda tx: tx.batches.begin(batch),
        "lexical": lambda tx: tx.batches.write_lexical(
            batch.batch_id, (LexicalRecord(chunk_id=entries[0]),)
        ),
        "vectors": lambda tx: tx.batches.write_vectors(batch.batch_id, ()),
        "ready": lambda tx: tx.batches.mark_ready(batch.batch_id, 1),
    }
    with pytest.raises(TychoraError) as error, uow.read() as tx:
        calls[write](tx)
    assert error.value.reason == "invariant.unexpected"
    assert db.execute("SELECT state FROM index_batches").fetchone()[0] == "building"
