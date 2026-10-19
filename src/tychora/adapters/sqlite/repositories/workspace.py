"""The single workspace row and the assertion decision watermark."""

import sqlite3

from tychora.contracts.errors import TychoraError

_ROW = "SELECT workspace_id,policy_epoch,lexical_statistics_epoch FROM workspaces"


def require_write(db: sqlite3.Connection, writable: bool) -> None:
    """Refuse a mutation in a read transaction or outside any transaction."""
    if not writable or not db.in_transaction:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "A mutation requires a write transaction.",
            reason="invariant.unexpected",
        )


class SqliteWorkspaceRepository:
    """The one row of ``workspaces``, its epochs and the decision watermark."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def _row(self) -> tuple[str, int, int]:
        rows = self._db.execute(_ROW).fetchall()
        if len(rows) != 1:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Workspace identity is missing or ambiguous.",
                reason="canonical.row_invalid",
            )
        return rows[0]

    def workspace_id(self) -> str:
        """Return the workspace identifier."""
        return self._row()[0]

    def policy_epoch(self) -> int:
        """Return the current policy epoch."""
        return self._row()[1]

    def lexical_epoch(self) -> int:
        """Return the current lexical statistics epoch."""
        return self._row()[2]

    def decision_watermark(self) -> int:
        """Return the highest recorded decision sequence number."""
        return self._db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]

    def bump_lexical_epoch(self) -> int:
        """Advance the lexical statistics epoch once and return the new value."""
        require_write(self._db, self._writable)
        self._row()
        return self._db.execute(
            "UPDATE workspaces SET lexical_statistics_epoch=lexical_statistics_epoch+1 "
            "RETURNING lexical_statistics_epoch"
        ).fetchone()[0]

    def collection_ids(self) -> frozenset[str]:
        """Return every collection of the workspace."""
        return frozenset(
            row[0] for row in self._db.execute("SELECT collection_id FROM collections")
        )
