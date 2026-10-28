"""The rows exact inspection reads, one statement per method."""

import sqlite3

from tychora.contracts.publication import SnapshotMember
from tychora.ports.repositories import RetainedSubject

_MEMBERS = (
    "SELECT s.source_id,e.revision_id,e.extraction_id FROM extractions e "
    "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
    "JOIN collections c USING(collection_id) WHERE c.workspace_id=? AND "
)


def _member_selector(subject: RetainedSubject) -> str:
    """The extraction condition that selects a retained subject's members."""
    match subject:
        case "evidence":
            return "e.extraction_id=(SELECT extraction_id FROM evidence_units WHERE unit_id=?)"
        case "assets":
            return "e.extraction_id=(SELECT extraction_id FROM assets WHERE asset_id=?)"
        case "diagnostics":
            return "e.extraction_id=?"
        case "revisions":
            return "e.revision_id=?"


class SqliteInspectionRepository:
    """Single-row and small-set reads behind exact inspection."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def job_summary(self, job_id: str) -> tuple[str, str, str, str] | None:
        """``(operation, state, created_at, updated_at)`` of the job, or ``None``."""
        return self._db.execute(
            "SELECT operation,state,created_at,updated_at FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()

    def run_summary(
        self, run_id: str
    ) -> tuple[str, str, str, str, str, str | None] | None:
        """``(principal, scope digest, state, created_at, snapshot, retained payload)``."""
        return self._db.execute(
            "SELECT principal_id,scope_digest,state,created_at,snapshot_id,retained_payload_sha "
            "FROM query_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()

    def extraction_diagnostics(
        self, extraction_id: str
    ) -> tuple[str | None, str] | None:
        """``(diagnostics artifact SHA, source ID)`` of the extraction, or ``None``."""
        return self._db.execute(
            "SELECT e.diagnostics_artifact_sha,r.source_id FROM extractions e "
            "JOIN revisions r USING(revision_id) WHERE e.extraction_id=?",
            (extraction_id,),
        ).fetchone()

    def code_file_units(self, extraction_id: str, relative_path: str) -> frozenset[str]:
        """The units of the extraction with a code locator into the file."""
        return frozenset(
            unit_id
            for (unit_id,) in self._db.execute(
                "SELECT DISTINCT l.unit_id FROM locators l "
                "JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=? AND l.locator_type='code' "
                "AND l.source_ref=?",
                (extraction_id, relative_path),
            )
        )

    def unit_owner(self, unit_id: str) -> tuple[str, str] | None:
        """``(extraction_id, revision_id)`` of the unit, or ``None``."""
        return self._db.execute(
            "SELECT e.extraction_id,x.revision_id FROM evidence_units e "
            "JOIN extractions x USING(extraction_id) WHERE e.unit_id=?",
            (unit_id,),
        ).fetchone()

    def unit_records(self, unit_id: str) -> tuple[tuple[str, str, str], ...]:
        """``(record_type, schema_hash, data_json)`` of each structured record."""
        return tuple(
            self._db.execute(
                "SELECT record_type,schema_hash,data_json FROM structured_records WHERE unit_id=?",
                (unit_id,),
            ).fetchall()
        )

    def proxy_derivation(self, unit_id: str) -> str | None:
        """The derivation artifact SHA behind a derived proxy unit, or ``None``."""
        row = self._db.execute(
            "SELECT derivation_sha FROM asset_proxies WHERE proxy_unit_id=?",
            (unit_id,),
        ).fetchone()
        return None if row is None else row[0]

    def asset_summary(self, asset_id: str) -> tuple[str, str, str] | None:
        """``(artifact_sha, locator_json, revision_id)`` of the asset, or ``None``."""
        return self._db.execute(
            "SELECT a.artifact_sha,a.locator_json,x.revision_id "
            "FROM assets a JOIN extractions x USING(extraction_id) WHERE a.asset_id=?",
            (asset_id,),
        ).fetchone()

    def tombstoned(self, revision_id: str, artifact_sha: str) -> bool:
        """Whether the revision's original payload was removed."""
        return (
            self._db.execute(
                "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
                (revision_id, artifact_sha),
            ).fetchone()
            is not None
        )

    def revision_summary(self, revision_id: str) -> tuple[str, str] | None:
        """``(original_sha, title)`` of the revision, or ``None``."""
        return self._db.execute(
            "SELECT r.original_sha,r.title FROM revisions r WHERE r.revision_id=?",
            (revision_id,),
        ).fetchone()

    def original_bound(self, revision_id: str, artifact_sha: str) -> bool:
        """Whether the revision binds the artifact as its original."""
        return (
            self._db.execute(
                "SELECT 1 FROM revision_artifacts WHERE revision_id=? AND artifact_sha=? AND role='original'",
                (revision_id, artifact_sha),
            ).fetchone()
            is not None
        )

    def retained_members(
        self, subject: RetainedSubject, item_id: str, workspace_id: str
    ) -> tuple[SnapshotMember, ...]:
        """The retained extractions that hold the subject, by extraction ID."""
        rows = self._db.execute(
            _MEMBERS + _member_selector(subject) + " ORDER BY e.extraction_id",
            (workspace_id, item_id),
        ).fetchall()
        return tuple(
            SnapshotMember(source_id=s, revision_id=r, extraction_id=e)
            for s, r, e in rows
        )

    def member_payloads(self, member: SnapshotMember) -> frozenset[str]:
        """Every payload SHA the member's revision and extraction bind."""
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT artifact_sha FROM revision_artifacts WHERE revision_id=? "
                "UNION SELECT document_artifact_sha FROM extractions WHERE extraction_id=? "
                "UNION SELECT assertions_artifact_sha FROM extractions WHERE extraction_id=? "
                "UNION SELECT diagnostics_artifact_sha FROM extractions WHERE extraction_id=? "
                "UNION SELECT artifact_sha FROM assets WHERE extraction_id=?",
                (member.revision_id, *(member.extraction_id,) * 4),
            )
            if row[0] is not None
        )

    def revision_policy(
        self, revision_id: str, workspace_id: str
    ) -> tuple[str, int, int, int, str, str | None] | None:
        """``(collection, collection floor, source floor, access rank, original, withdrawn)``."""
        return self._db.execute(
            "SELECT c.collection_id,c.access_floor,s.access_floor,r.access_rank,"
            "r.original_sha,s.withdrawn_at FROM revisions r "
            "JOIN sources s USING(source_id) JOIN collections c USING(collection_id) "
            "WHERE r.revision_id=? AND c.workspace_id=?",
            (revision_id, workspace_id),
        ).fetchone()
