"""Explicit local SQLite initialization and compatibility-checked opening."""

import sqlite3
from pathlib import Path

from tychora.contracts.errors import TychoraError

from .migrations import (
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    format_old_error,
    initialize_schema,
    migration_sql,
    receipt_is_older,
)


def _refuse_older_format(path: Path) -> None:
    """Refuse an older-format database without writing a byte of it.

    The check opens the file read-only, so closing it neither checkpoints nor
    removes a -wal left by a crashed owner. Without such sidecar files the open
    is also immutable, which stops SQLite from creating a -shm.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return
    sidecars = any(
        path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm")
    )
    uri = path.as_uri() + ("?mode=ro" if sidecars else "?mode=ro&immutable=1")
    try:
        db = sqlite3.connect(uri, uri=True)
        try:
            rows = db.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.DatabaseError:
        return
    if receipt_is_older(rows):
        raise format_old_error()


def open_corpus(path: Path, *, initialize: bool = False) -> sqlite3.Connection:
    if not initialize and not path.is_file():
        raise TychoraError(
            "NOT_FOUND",
            "Corpus database does not exist",
            reason="workspace.absent",
        )
    _refuse_older_format(path)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if sqlite3.sqlite_version_info < (3, 38, 0):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "SQLite 3.38 or newer is required",
                reason="sqlite.version_unsupported",
            )
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=2000")
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if not tables and initialize:
            initialize_schema(db)
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone():
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Corpus schema is unmanaged",
                reason="workspace.schema_mismatch",
            )
        rows = db.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if receipt_is_older(rows):
            raise format_old_error()
        if rows != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Corpus schema is incompatible",
                reason="workspace.schema_mismatch",
            )
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(migration_sql())
            query = (
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
            if db.execute(query).fetchall() != expected.execute(query).fetchall():
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Corpus schema definition differs",
                    reason="workspace.schema_mismatch",
                )
        finally:
            expected.close()
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db
    except BaseException:
        db.close()
        raise
