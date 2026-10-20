"""Bytes one publication writes: its root document and its new pages."""

import sqlite3

from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.ports.repositories import PublicationFootprint


def publication_footprint(
    db: sqlite3.Connection, snapshot_id: str
) -> PublicationFootprint:
    """Read one snapshot's publication footprint; call on the connection's owner thread."""
    with SqliteUnitOfWork(db).read() as tx:
        return tx.snapshots.footprint(snapshot_id)
