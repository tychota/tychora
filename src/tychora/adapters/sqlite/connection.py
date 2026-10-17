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
from .receipt import read_receipt_untouched, require_no_live_owner


def refuse_older_format(path: Path) -> None:
    """Refuse an older-format database before anything is written next to it.

    The receipt is read through `read_receipt_untouched`, so the old file, its
    -wal and its -shm keep their exact bytes.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return
    if receipt_is_older(read_receipt_untouched(path)):
        raise format_old_error()


_OWNED = "Workspace already has an owner; retry when it exits"


def refuse_owned_or_older(root: Path) -> None:
    """Refuse a workspace held by a live owner, then one in an older format.

    A live owner's database always has a -wal and a -shm, so reading its receipt
    untouched would copy the whole database and could see it change mid-copy.
    The owner lock is therefore probed first, without creating anything, and
    only a workspace with no live owner has its receipt read. An owner that
    starts during that read turns the resulting refusal into WORKSPACE_BUSY.
    """
    require_no_live_owner(root, _OWNED)
    try:
        refuse_older_format(root / "corpus.sqlite3")
    except TychoraError as error:
        if error.reason == "workspace.database_invalid":
            require_no_live_owner(root, _OWNED)
        raise


def open_corpus(
    path: Path, *, initialize: bool = False, format_checked: bool = False
) -> sqlite3.Connection:
    """Open the corpus database after verifying its schema.

    `format_checked` tells that the caller already refused an older format
    through `refuse_owned_or_older`, so the receipt is not read untouched again.
    """
    if not initialize and not path.is_file():
        raise TychoraError(
            "NOT_FOUND",
            "Corpus database does not exist",
            reason="workspace.absent",
        )
    if not format_checked:
        refuse_older_format(path)
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if sqlite3.sqlite_version_info < (3, 38, 0):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "SQLite 3.38 or newer is required",
                reason="storage.version_unsupported",
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
