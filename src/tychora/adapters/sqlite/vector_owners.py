"""Read the revision, extraction and owner of each record of one vector batch."""

from __future__ import annotations

import sqlite3
from typing import NamedTuple


class OwnerRow(NamedTuple):
    """One vector record with its revision, extraction and owning unit or asset."""

    record: str
    revision: str
    extraction: str
    unit: str | None
    asset: str | None


_OWNER_ROWS = (
    "SELECT v.record_id,e.revision_id,b.extraction_id,c.unit_id,v.asset_id "
    "FROM vector_records v JOIN index_batches b USING(batch_id) "
    "JOIN extractions e ON e.extraction_id=b.extraction_id "
    "LEFT JOIN chunks c ON c.chunk_id=v.chunk_id "
    "WHERE v.batch_id=? ORDER BY v.record_id"
)


def vector_owner_rows(db: sqlite3.Connection, batch_id: str) -> tuple[OwnerRow, ...]:
    """Return (record, revision, extraction, unit, asset) per record, by record ID.

    The unit is set for text records and the asset for image records.
    """
    return tuple(OwnerRow(*row) for row in db.execute(_OWNER_ROWS, (batch_id,)))
