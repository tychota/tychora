"""Workspace identity bootstrap and the kinds a workspace retains."""

import json
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue


def _identity_error(message: str) -> TychoraError:
    """A workspace identity the corpus cannot vouch for."""
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


def bootstrap_workspace(db: sqlite3.Connection) -> str:
    """Return the workspace ID; mint it for an empty schema, refuse any ambiguity."""
    rows = db.execute("SELECT workspace_id FROM workspaces").fetchall()
    if not rows:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for (table,) in tables:
            if table == "schema_migrations" or table.startswith(
                ("sqlite_", "lexical_fts")
            ):
                continue
            # Names come from the already-verified installed schema.
            if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise _identity_error("Existing corpus has no workspace identity")
        workspace_id = str(uuid4())
        db.execute(
            "INSERT INTO workspaces (workspace_id,schema_version,created_at) VALUES (?,2,?)",
            (workspace_id, datetime.now(UTC).isoformat()),
        )
        db.commit()
        return workspace_id
    if len(rows) != 1:
        raise _identity_error("Workspace identity is ambiguous")
    workspace_id = rows[0][0]
    try:
        if str(UUID(workspace_id)) != workspace_id:
            raise ValueError
    except ValueError:
        raise _identity_error("Invalid workspace identity") from None
    return workspace_id


def retained_kinds(
    db: sqlite3.Connection, workspace_id: str, collection_ids: frozenset[str]
) -> dict[str, JsonValue]:
    """The definitions of the kinds revisions in ``collection_ids`` retain."""
    return {
        kind: json.loads(definition)
        for kind, definition, collection in db.execute(
            "SELECT DISTINCT k.kind_id,k.definition_json,c.collection_id FROM kinds k "
            "JOIN revisions r USING(kind_id) JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) WHERE c.workspace_id=?",
            (workspace_id,),
        )
        if collection in collection_ids
    }
