"""Pinned canonical migration; upgrades require the maintenance owner."""

import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files

SCHEMA_VERSION = 1
SCHEMA_CHECKSUM = "b98d888098fa0ea790484f37ab8cf746afc2299576411767012c9e3a3c5642a9"


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
