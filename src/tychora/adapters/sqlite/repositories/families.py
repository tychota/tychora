"""Index family rows and their persisted creation nonces."""

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.snapshots import IndexFamily

from .workspace import require_write

_COLUMNS = (
    "family_id,channel,backend,provider,model,dimension,config_sha,config_json,"
    "backend_namespace,created_at"
)


def family_from_row(row: Sequence[Any]) -> IndexFamily:
    """Build a family from a row selected in ``_COLUMNS`` order."""
    try:
        return IndexFamily(
            family_id=row[0],
            channel=row[1],
            backend=row[2],
            provider=row[3],
            model=row[4],
            dimension=row[5],
            config_sha=row[6],
            config_json=FrozenJsonMap(json.loads(row[7])),
            backend_namespace=row[8],
            created_at=row[9],
        )
    except (TypeError, ValueError):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Index family row is invalid.",
            reason="canonical.row_invalid",
        ) from None


def _family_values(family: IndexFamily) -> tuple[object, ...]:
    return (
        family.family_id,
        family.channel,
        family.backend,
        family.provider,
        family.model,
        family.dimension,
        family.config_sha,
        canonical_bytes(family.config_json).decode(),
        family.backend_namespace,
        family.created_at,
    )


class SqliteFamilyRepository:
    """Lookups and idempotent creation in ``index_families``."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def get(self, family_id: str) -> IndexFamily | None:
        """Return the stored family, or ``None`` when absent."""
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM index_families WHERE family_id=?", (family_id,)
        ).fetchone()
        return None if row is None else family_from_row(row)

    def nonce(self, channel: str, identity_sha: str) -> str | None:
        """Return the nonce of the earliest family created for this identity."""
        row = self._db.execute(
            "SELECT creation_nonce FROM index_families WHERE channel=? AND "
            "identity_sha=? ORDER BY created_at,family_id LIMIT 1",
            (channel, identity_sha),
        ).fetchone()
        return None if row is None else row[0]

    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None:
        """Store a family once; an identical repeat is a no-op, a different row fails."""
        require_write(self._db, self._writable)
        values = (*_family_values(family), identity_sha, nonce)
        self._db.execute(
            f"INSERT INTO index_families ({_COLUMNS},identity_sha,creation_nonce) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(family_id) DO NOTHING",
            values,
        )
        stored = self._db.execute(
            f"SELECT {_COLUMNS},identity_sha,creation_nonce FROM index_families "
            "WHERE family_id=?",
            (family.family_id,),
        ).fetchone()
        if stored != values:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Index family differs from its immutable row.",
                reason="canonical.binding_mismatch",
            )
