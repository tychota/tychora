"""The rows upload authorization and captured-source registration read and write."""

import sqlite3

from .workspace import require_write


class SqliteUploadPolicyRepository:
    """One statement per method behind the canonical upload policy."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db
        self._writable = writable

    def collection_policy(self, collection_id: str) -> tuple[str, int, str] | None:
        """``(workspace ID, access floor, policy JSON)`` of the collection, or ``None``."""
        return self._db.execute(
            "SELECT workspace_id,access_floor,policy_json FROM collections WHERE collection_id=?",
            (collection_id,),
        ).fetchone()

    def kind_row(self, kind_id: str) -> tuple | None:
        """The recorded kind row, or ``None``."""
        return self._db.execute(
            "SELECT kind_id,definition_hash,label,description,role,definition_json "
            "FROM kinds WHERE kind_id=?",
            (kind_id,),
        ).fetchone()

    def insert_kind(self, row: tuple) -> None:
        """Record a kind row."""
        require_write(self._db, self._writable)
        self._db.execute(
            "INSERT INTO kinds "
            "(kind_id,definition_hash,label,description,role,definition_json) "
            "VALUES (?,?,?,?,?,?)",
            row,
        )

    def source_rows(
        self, source_id: str, collection_id: str, source_key: str
    ) -> tuple[tuple, ...]:
        """The source rows that share the ID or the collection's source key."""
        return tuple(
            self._db.execute(
                "SELECT source_id,collection_id,source_key,source_type,registration_json,"
                "access_floor,withdrawn_at,created_at FROM sources "
                "WHERE source_id=? OR (collection_id=? AND source_key=?)",
                (source_id, collection_id, source_key),
            ).fetchall()
        )

    def insert_source(self, row: tuple) -> None:
        """Record a source row."""
        require_write(self._db, self._writable)
        self._db.execute(
            "INSERT INTO sources "
            "(source_id,collection_id,source_key,source_type,registration_json,"
            "access_floor,withdrawn_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
            row,
        )

    def captured_binding(self, kind_id: str, source_id: str) -> tuple | None:
        """The source, collection and kind rows a captured revision binds, or ``None``."""
        return self._db.execute(
            "SELECT c.workspace_id,c.collection_id,c.access_floor,s.source_id,s.source_key,"
            "s.source_type,s.registration_json,s.access_floor,s.withdrawn_at,c.policy_json,"
            "k.definition_json,k.definition_hash FROM sources s "
            "JOIN collections c USING(collection_id) JOIN kinds k ON k.kind_id=? "
            "WHERE s.source_id=?",
            (kind_id, source_id),
        ).fetchone()

    def revision_grant(self, revision_id: str) -> tuple | None:
        """The policy rows that grant a revision, or ``None``."""
        return self._db.execute(
            "SELECT c.workspace_id,c.collection_id,c.access_floor,s.access_floor,r.access_rank,s.withdrawn_at,c.policy_json,r.allowed_uses_json,k.definition_json,k.definition_hash "
            "FROM revisions r JOIN sources s USING(source_id) JOIN collections c USING(collection_id) JOIN kinds k USING(kind_id) WHERE r.revision_id=?",
            (revision_id,),
        ).fetchone()

    def identity_owner(self, identity: str) -> tuple[tuple[str, str], ...]:
        """``(revision ID, revision|unit|asset)`` for each owner of the identity."""
        return tuple(
            self._db.execute(
                "SELECT revision_id,'revision' FROM revisions WHERE revision_id=? "
                "UNION ALL SELECT e.revision_id,'unit' FROM evidence_units u JOIN extractions e USING(extraction_id) WHERE u.unit_id=? "
                "UNION ALL SELECT e.revision_id,'asset' FROM assets a JOIN extractions e USING(extraction_id) WHERE a.asset_id=?",
                (identity, identity, identity),
            ).fetchall()
        )

    def proxy_requirements(
        self, unit_id: str, limit: int
    ) -> tuple[str, tuple[str, ...]] | None:
        """``(asset ID, up to limit required unit IDs)`` of a proxy unit, or ``None``."""
        proxy = self._db.execute(
            "SELECT asset_id FROM asset_proxies WHERE proxy_unit_id=?", (unit_id,)
        ).fetchone()
        if proxy is None:
            return None
        rows = self._db.execute(
            "SELECT required_unit_id FROM asset_proxy_dependencies WHERE proxy_unit_id=? LIMIT ?",
            (unit_id, limit),
        ).fetchall()
        return proxy[0], tuple(row[0] for row in rows)
