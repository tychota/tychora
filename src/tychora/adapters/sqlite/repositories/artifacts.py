"""Registered artifacts as ``ArtifactRef`` values."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError

from .workspace import require_write


def artifact_ref(row: Sequence[object]) -> ArtifactRef:
    """Build a reference from a (sha256, size_bytes, media_type, storage_key) row."""
    return ArtifactRef.from_row(row)


class SqliteArtifactRepository:
    """Lookups and idempotent registration in ``artifacts``."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def get(self, sha256: str) -> ArtifactRef | None:
        """Return the present artifact, or ``None`` when absent."""
        row = self._db.execute(
            "SELECT artifact_sha,size_bytes,media_type,storage_key FROM artifacts "
            "WHERE artifact_sha=? AND payload_state='present'",
            (sha256,),
        ).fetchone()
        return None if row is None else artifact_ref(row)

    def register(self, ref: ArtifactRef) -> bool:
        """Register an artifact; return whether it was newly added."""
        require_write(self._db, self._writable)
        row = self._db.execute(
            "SELECT size_bytes,storage_key,payload_state FROM artifacts "
            "WHERE artifact_sha=?",
            (ref.sha256,),
        ).fetchone()
        if row is not None:
            if tuple(row) != (ref.size_bytes, ref.storage_key, "present"):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "A registered artifact differs from its reference.",
                    reason="canonical.binding_mismatch",
                )
            return False
        self._db.execute(
            "INSERT INTO artifacts "
            "(artifact_sha,size_bytes,media_type,storage_key,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
                ref.storage_key,
                datetime.now(UTC).isoformat(),
            ),
        )
        return True
