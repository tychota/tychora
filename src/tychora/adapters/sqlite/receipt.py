"""Read an existing database without writing any file next to it."""

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tychora.contracts.errors import TychoraError

_RECEIPT = "SELECT version, checksum FROM schema_migrations ORDER BY version"
_HAS_RECEIPT = (
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
)


def _stamp(path: Path) -> tuple[int, int] | None:
    """Return the size and modification time of a file, or None when it is absent."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_size, info.st_mtime_ns


def _copy_to(scratch: str, path: Path, wal: Path) -> Path:
    """Copy the database and its -wal into a directory and return the copy."""
    copy = Path(scratch) / path.name
    shutil.copyfile(path, copy)
    if wal.exists():
        shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
    return copy


@contextmanager
def open_untouched(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection that sees every commit and writes nothing.

    A database with no -wal or -shm is opened read-only and immutable. When
    either exists, a crashed owner may have left committed pages in the -wal and
    even a read-only open would rewrite the -shm or create the missing sidecar,
    so the database and its -wal are copied into a private directory and the
    copy is read. No concurrent writer is assumed: the files are compared before
    and after the copy and the read is refused when either changed. Any failure
    refuses with a typed error; the caller never falls back to a read-write open.
    """
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    try:
        if not (wal.exists() or shm.exists()):
            db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                yield db
            finally:
                db.close()
            return
        with tempfile.TemporaryDirectory(prefix="tychora-untouched-") as scratch:
            before = (_stamp(path), _stamp(wal))
            copy = _copy_to(scratch, path, wal)
            if (_stamp(path), _stamp(wal)) != before:
                raise OSError("The database changed while it was copied")
            db = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
            try:
                yield db
            finally:
                db.close()
    except (OSError, sqlite3.DatabaseError):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "The workspace database cannot be inspected.",
            reason="workspace.database_invalid",
        ) from None


def read_receipt_untouched(path: Path) -> list[tuple[int, str]]:
    """Return the migration receipt rows of an existing database, leaving it as is."""
    with open_untouched(path) as db:
        if not db.execute(_HAS_RECEIPT).fetchone():
            return []
        return db.execute(_RECEIPT).fetchall()
