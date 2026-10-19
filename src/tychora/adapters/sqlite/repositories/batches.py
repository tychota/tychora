"""Index batch rows, their lexical and vector rows, and their state."""

import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.publication import LexicalBatchReceipt, LexicalRecord, NewBatch
from tychora.contracts.vectors import BatchReceipt
from tychora.ports.repositories import BatchRow, VectorRecordRow

from .artifacts import SqliteArtifactRepository, artifact_ref
from .workspace import SqliteWorkspaceRepository, require_write

BATCH_SELECT = (
    "SELECT b.batch_id,b.family_id,f.channel,b.extraction_id,b.state,b.expected_count,"
    "b.actual_count,b.receipt_json,i.artifact_sha,i.size_bytes,i.media_type,i.storage_key,"
    "v.artifact_sha,v.size_bytes,v.media_type,v.storage_key "
    "FROM index_batches b JOIN index_families f USING(family_id) "
    "JOIN artifacts i ON i.artifact_sha=b.input_manifest_sha "
    "LEFT JOIN artifacts v ON v.artifact_sha=b.vectors_sha "
)
_IMMUTABLE_COLUMNS = (
    "batch_id,family_id,extraction_id,input_manifest_sha,expected_count,"
    "entry_ids_sha,receipt_json,vectors_sha"
)
_LEXICAL_COLUMNS = "batch_id,chunk_id,identifiers,title,headings,body,context"
_VECTOR_COLUMNS = "record_id,batch_id,chunk_id,asset_id,input_sha,row_index"


def _mismatch(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


def _receipt_digest(receipt_json: str | None) -> str | None:
    if receipt_json is None:
        return None
    return sha256(canonical_bytes(json.loads(receipt_json))).hexdigest()


def batch_from_row(row: Sequence[Any]) -> BatchRow:
    """Build a batch from a row selected by ``BATCH_SELECT``."""
    try:
        return BatchRow(
            batch_id=row[0],
            family_id=row[1],
            slot=row[2],
            extraction_id=row[3],
            state=row[4],
            expected_count=row[5],
            actual_count=row[6],
            receipt_digest=_receipt_digest(row[7]),
            input_manifest=artifact_ref(row[8:12]),
            vectors=None if row[12] is None else artifact_ref(row[12:16]),
        )
    except (TypeError, ValueError):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Index batch row is invalid.",
            reason="canonical.row_invalid",
        ) from None


def _entry_ids_sha(batch: NewBatch) -> str | None:
    receipt = batch.receipt
    if isinstance(receipt, LexicalBatchReceipt):
        return receipt.entry_ids_sha
    if isinstance(receipt, BatchReceipt):
        return digest("index-entries-v1", tuple(r.record_id for r in receipt.records))
    return None


def _immutable_values(batch: NewBatch) -> tuple[object, ...]:
    return (
        batch.batch_id,
        batch.family_id,
        batch.extraction_id,
        batch.input_manifest.sha256,
        batch.expected_count,
        _entry_ids_sha(batch),
        canonical_bytes(batch.receipt.model_dump(mode="json")).decode(),
        batch.vectors.sha256 if batch.vectors else None,
    )


class SqliteBatchRepository:
    """Batch rows in ``index_batches`` with their lexical and vector rows."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable
        self._artifacts = SqliteArtifactRepository(db, writable=writable)
        self._workspace = SqliteWorkspaceRepository(db, writable=writable)

    def _insert_once(
        self, table: str, columns: str, key_width: int, values: tuple[object, ...]
    ) -> None:
        # Table and column names come only from this module's constants; the
        # first ``key_width`` columns identify the row.
        self._db.execute(
            f"INSERT INTO {table} ({columns}) VALUES "
            f"({','.join('?' for _ in values)}) ON CONFLICT DO NOTHING",
            values,
        )
        key = " AND ".join(f"{name}=?" for name in columns.split(",")[:key_width])
        stored = self._db.execute(
            f"SELECT {columns} FROM {table} WHERE {key}", values[:key_width]
        ).fetchone()
        if stored != values:
            raise _mismatch(f"A {table} row differs from its immutable value")

    def begin(self, batch: NewBatch) -> None:
        """Store a building batch once; an identical repeat is a no-op."""
        require_write(self._db, self._writable)
        self._artifacts.register(batch.input_manifest)
        if batch.vectors is not None:
            self._artifacts.register(batch.vectors)
        values = _immutable_values(batch)
        self._db.execute(
            f"INSERT INTO index_batches ({_IMMUTABLE_COLUMNS},state) "
            "VALUES (?,?,?,?,?,?,?,?,'building') ON CONFLICT DO NOTHING",
            values,
        )
        stored = self._db.execute(
            f"SELECT {_IMMUTABLE_COLUMNS} FROM index_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
        if stored != values:
            raise _mismatch("Index batch differs from its immutable row")

    def write_lexical(self, batch_id: str, records: tuple[LexicalRecord, ...]) -> None:
        """Store a batch's lexical records; stored records never change."""
        require_write(self._db, self._writable)
        for r in records:
            values = (
                batch_id,
                r.chunk_id,
                r.identifiers,
                r.title,
                r.headings,
                r.body,
                r.context,
            )
            self._insert_once("lexical_records", _LEXICAL_COLUMNS, 2, values)

    def write_vectors(self, batch_id: str, rows: tuple[VectorRecordRow, ...]) -> None:
        """Store a batch's vector record rows; stored rows never change."""
        require_write(self._db, self._writable)
        for r in rows:
            values = (
                r.record_id,
                batch_id,
                r.chunk_id,
                r.asset_id,
                r.input_sha,
                r.row_index,
            )
            self._insert_once("vector_records", _VECTOR_COLUMNS, 1, values)

    def mark_ready(self, batch_id: str, actual_count: int) -> None:
        """Mark a building batch ready; a batch with lexical rows bumps the epoch once."""
        require_write(self._db, self._writable)
        moved = self._db.execute(
            "UPDATE index_batches SET state='ready',actual_count=? "
            "WHERE batch_id=? AND state='building'",
            (actual_count, batch_id),
        ).rowcount
        if moved == 1:
            if self._db.execute(
                "SELECT EXISTS(SELECT 1 FROM lexical_records WHERE batch_id=?)",
                (batch_id,),
            ).fetchone()[0]:
                self._workspace.bump_lexical_epoch()
            return
        stored = self._db.execute(
            "SELECT state,actual_count FROM index_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if stored != ("ready", actual_count):
            raise _mismatch("Only a building batch can become ready")

    def count_ready(self, batch_ids: tuple[str, ...]) -> int:
        """Return how many of these batches are ready, in one statement."""
        return self._db.execute(
            "SELECT COUNT(*) FROM index_batches WHERE state='ready' "
            "AND batch_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(set(batch_ids))),),
        ).fetchone()[0]

    def get(self, batch_id: str) -> BatchRow | None:
        """Return the stored batch, or ``None`` when absent."""
        row = self._db.execute(
            BATCH_SELECT + "WHERE b.batch_id=?", (batch_id,)
        ).fetchone()
        return None if row is None else batch_from_row(row)

    def find(self, family_id: str, extraction_id: str) -> BatchRow | None:
        """Return the batch of one family for one extraction, or ``None``."""
        row = self._db.execute(
            BATCH_SELECT + "WHERE b.family_id=? AND b.extraction_id=?",
            (family_id, extraction_id),
        ).fetchone()
        return None if row is None else batch_from_row(row)

    def for_snapshot(self, snapshot_id: str, slot: str) -> tuple[BatchRow, ...]:
        """Return the batches a snapshot serves for one slot, by batch ID."""
        rows = self._db.execute(
            BATCH_SELECT + "JOIN snapshot_batches s ON s.batch_id=b.batch_id "
            "WHERE s.snapshot_id=? AND s.slot=? ORDER BY b.batch_id",
            (snapshot_id, slot),
        ).fetchall()
        return tuple(batch_from_row(row) for row in rows)
