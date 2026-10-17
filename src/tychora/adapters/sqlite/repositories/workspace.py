"""The single workspace row and the assertion decision watermark."""

import sqlite3

from tychora.contracts.errors import TychoraError

_ROW = "SELECT workspace_id,policy_epoch,lexical_statistics_epoch,schema_version FROM workspaces"


def require_write(db: sqlite3.Connection, writable: bool) -> None:
    """Refuse a mutation in a read transaction or outside any transaction."""
    if not writable or not db.in_transaction:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "A mutation requires a write transaction.",
            reason="invariant.unexpected",
        )


class SqliteWorkspaceRepository:
    """Reads the one row of ``workspaces`` and the decision watermark."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def _row(self) -> tuple[str, int, int, int]:
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

    def format_version(self) -> int:
        """Return the storage format recorded for the workspace."""
        return self._row()[3]

    def lexical_epoch(self) -> int:
        """Return the current lexical statistics epoch."""
        return self._row()[2]

    def decision_watermark(self) -> int:
        """Return the highest recorded decision sequence number."""
        return self._db.execute(
            "SELECT COALESCE(MAX(decision_id),0) FROM assertion_decisions"
        ).fetchone()[0]
