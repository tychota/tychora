"""Read a database's migration receipt without writing any file next to it."""

import shutil
import sqlite3
import tempfile
from pathlib import Path

from tychora.contracts.errors import TychoraError

_RECEIPT = "SELECT version, checksum FROM schema_migrations ORDER BY version"
_HAS_RECEIPT = (
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
)


def _query(uri: str) -> list[tuple[int, str]]:
    db = sqlite3.connect(uri, uri=True)
    try:
        if not db.execute(_HAS_RECEIPT).fetchone():
            return []
        return db.execute(_RECEIPT).fetchall()
    finally:
        db.close()


def read_receipt_untouched(path: Path) -> list[tuple[int, str]]:
    """Return the migration receipt rows of an existing database, leaving it as is.

    A database with no -wal or -shm is opened read-only and immutable. When
    either exists, a crashed owner may have left committed pages in the -wal and
    even a read-only open would rewrite the -shm or create the missing sidecar,
    so the database and its -wal are copied into a private directory and the
    copy is read. Any failure refuses with a typed error; the caller never
    falls back to a read-write open.
    """
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    try:
        if not (wal.exists() or shm.exists()):
            return _query(path.as_uri() + "?mode=ro&immutable=1")
        with tempfile.TemporaryDirectory(prefix="tychora-receipt-") as scratch:
            copy = Path(scratch) / path.name
            shutil.copyfile(path, copy)
            if wal.exists():
                shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
            return _query(copy.as_uri() + "?mode=ro")
    except (OSError, sqlite3.DatabaseError):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "The workspace database cannot be inspected.",
            reason="workspace.database_invalid",
        ) from None
