"""The rows a verified restore reads from, and the state it sets on, an imported database."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope

RestoreDatabase = sqlite3.Connection


def authorize_restore(db: RestoreDatabase, scope: RequestScope) -> None:
    """Refuse a restore whose scope does not cover the whole imported workspace."""
    if "manage" not in scope.capabilities:
        raise TychoraError("FORBIDDEN_SCOPE", "Restore requires management grant")
    collections = db.execute(
        "SELECT collection_id,access_floor FROM collections"
    ).fetchall()
    if any(
        collection_id not in scope.collection_ids or floor > scope.access_ceiling
        for collection_id, floor in collections
    ):
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Restore requires whole-workspace authority"
        )
    for query in (
        "SELECT MAX(access_floor) FROM sources",
        "SELECT MAX(access_rank) FROM revisions",
    ):
        maximum = db.execute(query).fetchone()[0]
        if maximum is not None and maximum > scope.access_ceiling:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Restore requires whole-workspace authority"
            )


def bootstrap_scope(db: RestoreDatabase) -> RequestScope:
    """Grant a local owner only this already-verified archive's collections."""
    collection_ids = frozenset(
        row[0] for row in db.execute("SELECT collection_id FROM collections")
    )
    ceilings = [
        db.execute("SELECT COALESCE(MAX(access_floor),0) FROM collections").fetchone()[
            0
        ],
        db.execute("SELECT COALESCE(MAX(access_floor),0) FROM sources").fetchone()[0],
        db.execute("SELECT COALESCE(MAX(access_rank),0) FROM revisions").fetchone()[0],
    ]
    return RequestScope(
        principal_id="local-owner",
        collection_ids=collection_ids,
        access_ceiling=max(ceilings),
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage", "vision"}),
    )


def active_snapshot_config_ref(db: RestoreDatabase) -> ArtifactRef | None:
    """The registered artifact of the active snapshot's configuration, or ``None``."""
    row = db.execute(
        "SELECT s.config_sha FROM workspaces w LEFT JOIN snapshots s "
        "ON s.snapshot_id=w.active_snapshot_id"
    ).fetchone()
    if row is None or row[0] is None:
        return None
    ref_row = db.execute(
        "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
        (row[0],),
    ).fetchone()
    if ref_row is None:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Active snapshot configuration is missing",
            reason="canonical.row_invalid",
        )
    return ArtifactRef.from_row((row[0], *ref_row[:3]))


def artifact_registration(db: RestoreDatabase, sha: str) -> ArtifactRef | None:
    """The registered artifact ``sha``, or ``None``."""
    row = db.execute(
        "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
        (sha,),
    ).fetchone()
    return None if row is None else ArtifactRef.from_row((sha, *row[:3]))


def retained_config_shas(db: RestoreDatabase) -> frozenset[str]:
    """The configuration artifacts jobs and snapshots retain."""
    return frozenset(
        row[0]
        for row in db.execute(
            "SELECT effective_config_sha FROM jobs UNION SELECT config_sha FROM snapshots"
        )
        if row[0] is not None
    )


def retained_extractions(db: RestoreDatabase) -> tuple[tuple[Any, ...], ...]:
    """``(extraction, revision, manifest SHA, diagnostics SHA, registration JSON)`` rows."""
    return tuple(
        db.execute(
            "SELECT e.extraction_id,e.revision_id,e.document_artifact_sha,"
            "e.diagnostics_artifact_sha,s.registration_json FROM extractions e "
            "JOIN revisions r ON r.revision_id=e.revision_id "
            "JOIN sources s ON s.source_id=r.source_id"
        ).fetchall()
    )


def has_document_locator(db: RestoreDatabase, extraction_id: str) -> bool:
    """Whether any unit of the extraction has a document locator."""
    return (
        db.execute(
            "SELECT 1 FROM locators l JOIN evidence_units u USING(unit_id) "
            "WHERE u.extraction_id=? AND l.locator_type='document' LIMIT 1",
            (extraction_id,),
        ).fetchone()
        is not None
    )


def source_unit_attributes(db: RestoreDatabase, extraction_id: str) -> tuple[str, ...]:
    """The attributes JSON of each source-origin unit of the extraction."""
    return tuple(
        attributes_json
        for (attributes_json,) in db.execute(
            "SELECT attributes_json FROM evidence_units "
            "WHERE extraction_id=? AND text_origin='source'",
            (extraction_id,),
        )
    )


def unit_values(db: RestoreDatabase, extraction_id: str) -> tuple[dict[str, Any], ...]:
    """Each unit row of the extraction, by column name, in ordinal order."""
    cursor = db.execute(
        "SELECT * FROM evidence_units WHERE extraction_id=? ORDER BY ordinal",
        (extraction_id,),
    )
    fields = [column[0] for column in cursor.description]
    return tuple(dict(zip(fields, row)) for row in cursor)


def unit_locator_details(db: RestoreDatabase, unit_id: str) -> tuple[str, ...]:
    """The locator detail JSON of the unit, in fragment order."""
    return tuple(
        item[0]
        for item in db.execute(
            "SELECT details_json FROM locators WHERE unit_id=? ORDER BY fragment_ordinal",
            (unit_id,),
        )
    )


def restored_workspace_ids(db: RestoreDatabase) -> tuple[str, ...]:
    """The workspace IDs the restored database holds."""
    return tuple(row[0] for row in db.execute("SELECT workspace_id FROM workspaces"))


def set_operational_state(path: Path) -> None:
    """Reset index batches and unfinished jobs of the restored database, atomically."""
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE index_batches SET state='pending' WHERE family_id IN "
            "(SELECT family_id FROM index_families WHERE channel IN ('text','image','graph'))"
        )
        now = datetime.now(UTC).isoformat()
        db.execute(
            "UPDATE jobs SET state='complete',updated_at=? WHERE state IN "
            "('queued','running','cancelling') AND result_snapshot_id IS NOT NULL",
            (now,),
        )
        db.execute(
            "UPDATE jobs SET state='failed',error_code='JOB_INTERRUPTED',updated_at=? "
            "WHERE state IN ('queued','running','cancelling')",
            (now,),
        )
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Restore state broke foreign keys",
                reason="invariant.unexpected",
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
