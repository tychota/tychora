"""Pinned canonical migration; upgrades require the maintenance owner."""

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from tychora.contracts.errors import TychoraError

SCHEMA_VERSION = 1
SCHEMA_CHECKSUM = "b98d888098fa0ea790484f37ab8cf746afc2299576411767012c9e3a3c5642a9"


def _disable_extension_loading(db: sqlite3.Connection) -> None:
    # Python omits this method when SQLite extension support is unavailable.
    if disable := getattr(db, "enable_load_extension", None):
        disable(False)


def supported_migration_target(target_version: int) -> int:
    """V1 is the sole packaged version; never infer an upgrade from archive SQL."""
    if type(target_version) is not int or target_version != SCHEMA_VERSION:
        raise TychoraError("PROFILE_INCOMPATIBLE", "Unsupported migration target")
    return target_version


def open_verified_import(path: Path) -> sqlite3.Connection:
    """Establish the packaged-schema trust boundary before reading imported rows."""
    if path.is_symlink() or not path.is_file():
        raise TychoraError("INTEGRITY_ERROR", "Unsafe imported database path")
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        _disable_extension_loading(db)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        expected = sqlite3.connect(":memory:")
        try:
            _disable_extension_loading(expected)
            expected.execute("PRAGMA trusted_schema=OFF")
            expected.executescript(migration_sql())
            schema_query = (
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
            if (
                db.execute(schema_query).fetchall()
                != expected.execute(schema_query).fetchall()
            ):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Imported schema differs from packaged schema",
                )
        finally:
            expected.close()
        # The only read from an imported user table occurs after exact object comparison.
        if db.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE", "Imported migration receipt differs"
            )
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise TychoraError(
                "INTEGRITY_ERROR", "Imported database failed integrity check"
            )
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Imported database has broken foreign keys"
            )
        return db
    except sqlite3.DatabaseError:
        db.close()
        raise TychoraError(
            "INTEGRITY_ERROR", "Imported database cannot be inspected"
        ) from None
    except BaseException:
        db.close()
        raise


def migration_sql() -> str:
    raw = files("tychora").joinpath("resources/migrations/0001.sql").read_bytes()
    if sha256(raw).hexdigest() != SCHEMA_CHECKSUM:
        raise ValueError("Packaged migration checksum mismatch")
    return raw.decode("utf-8")


def initialize_schema(db: sqlite3.Connection) -> None:
    # executescript owns this explicit transaction, including the migration receipt.
    try:
        db.executescript("BEGIN IMMEDIATE;\n" + migration_sql())
        db.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (SCHEMA_VERSION, SCHEMA_CHECKSUM, datetime.now(UTC).isoformat()),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
