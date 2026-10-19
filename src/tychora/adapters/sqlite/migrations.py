"""Pinned canonical migration; upgrades require the maintenance owner."""

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from tychora.contracts.errors import TychoraError

SCHEMA_VERSION = 2
SCHEMA_CHECKSUM = "793f03cc454083123ce003d41294a389723fc82ba9e0deab0638cd04e8cce404"
# Receipts of earlier format-2 schemas: a rebuild, not a foreign database.
EARLIER_FORMAT_TWO_CHECKSUMS = (
    "5899bf5b00ec52d686602369801491bdc6e50d6e9dd803e790bb1cd32d15cabe",
)
FORMAT_ONE_CHECKSUM = "b98d888098fa0ea790484f37ab8cf746afc2299576411767012c9e3a3c5642a9"


def format_old_error() -> TychoraError:
    """The refusal for a workspace or archive written by an older format."""
    return TychoraError(
        "PROFILE_INCOMPATIBLE",
        "This workspace uses an older storage format. Recreate it with "
        "`tychora --workspace NEW manage rebuild workspace --from OLD`.",
        reason="workspace.format_old",
    )


def receipt_is_older(rows: list[tuple[int, str]]) -> bool:
    """True when a migration receipt names a format older than the packaged one.

    An earlier format-2 schema counts as older: its receipt carries the current
    version with a checksum this package replaced.
    """
    if len(rows) != 1:
        return False
    version, checksum = rows[0]
    return version < SCHEMA_VERSION or (
        version == SCHEMA_VERSION and checksum in EARLIER_FORMAT_TWO_CHECKSUMS
    )


def _disable_extension_loading(db: sqlite3.Connection) -> None:
    # Python omits this method when SQLite extension support is unavailable.
    if disable := getattr(db, "enable_load_extension", None):
        disable(False)


def supported_migration_target(target_version: int) -> int:
    """Format 2 is the sole packaged version; never infer an upgrade from archive SQL."""
    if type(target_version) is not int or target_version != SCHEMA_VERSION:
        raise TychoraError(
            "PROFILE_INCOMPATIBLE",
            "Unsupported migration target",
            reason="workspace.schema_mismatch",
        )
    return target_version


def _imported_receipt_is_older(db: sqlite3.Connection) -> bool:
    """Read only the migration receipt of an untrusted, read-only database."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return False
    try:
        rows = db.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    except sqlite3.DatabaseError:
        return False
    return receipt_is_older(rows)


def open_verified_import(path: Path) -> sqlite3.Connection:
    """Establish the packaged-schema trust boundary before reading imported rows."""
    if path.is_symlink() or not path.is_file():
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Unsafe imported database path",
            reason="workspace.database_invalid",
        )
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
                if _imported_receipt_is_older(db):
                    raise format_old_error()
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Imported schema differs from packaged schema",
                    reason="workspace.schema_mismatch",
                )
        finally:
            expected.close()
        # The receipt is the only user table read before integrity_check; the exact
        # object comparison above has already vouched for the schema around it.
        rows = db.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if receipt_is_older(rows):
            raise format_old_error()
        if rows != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Imported migration receipt differs",
                reason="workspace.schema_mismatch",
            )
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Imported database failed integrity check",
                reason="workspace.database_invalid",
            )
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Imported database has broken foreign keys",
                reason="workspace.database_invalid",
            )
        return db
    except sqlite3.DatabaseError:
        db.close()
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Imported database cannot be inspected",
            reason="workspace.database_invalid",
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
