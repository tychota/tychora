"""The rows source withdrawal and original removal read and write."""

import sqlite3
from datetime import datetime

from .workspace import require_write

# Typed manifest roots whose closures keep artifacts live, as (query, kind).
TYPED_MANIFEST_ROOTS = (
    ("SELECT manifest_sha FROM snapshots", "snapshot"),
    ("SELECT manifest_sha FROM membership_pages", "membership_page"),
    ("SELECT input_manifest_sha FROM index_batches", "index_input"),
    ("SELECT effective_config_sha FROM jobs", "registry"),
    ("SELECT manifest_sha FROM evaluation_datasets", "evaluation"),
    (
        "SELECT retained_payload_sha FROM query_runs WHERE retained_payload_sha IS NOT NULL",
        "retained_run",
    ),
)


def revision_tombstoned(db: sqlite3.Connection, revision_id: str, sha: str) -> bool:
    """Whether the revision's payload ``sha`` was removed (one statement, shared)."""
    return (
        db.execute(
            "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
            (revision_id, sha),
        ).fetchone()
        is not None
    )


class SqliteRemovalRepository:
    """One statement per method behind source withdrawal and original removal."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db
        self._writable = writable

    def source_grant(self, source_id: str, workspace_id: str) -> tuple | None:
        """``(collection, withdrawn_at, collection floor, source floor)`` or ``None``."""
        return self._db.execute(
            "SELECT s.collection_id,s.withdrawn_at,c.access_floor,s.access_floor FROM sources s "
            "JOIN collections c USING(collection_id) "
            "WHERE s.source_id=? AND c.workspace_id=?",
            (source_id, workspace_id),
        ).fetchone()

    def revision_original(
        self, revision_id: str, source_id: str
    ) -> tuple[str, int] | None:
        """``(original SHA, access rank)`` of the source's revision, or ``None``."""
        return self._db.execute(
            "SELECT original_sha,access_rank FROM revisions WHERE revision_id=? AND source_id=?",
            (revision_id, source_id),
        ).fetchone()

    def original_artifacts(self, revision_id: str) -> tuple[str, ...]:
        """The revision's bound original and source-file payload SHAs."""
        return tuple(
            sha
            for (sha,) in self._db.execute(
                "SELECT artifact_sha FROM revision_artifacts WHERE revision_id=? "
                "AND role IN ('original','source_file')",
                (revision_id,),
            )
        )

    def revision_payloads(self, revision_id: str) -> dict[str, tuple[str, ...]]:
        """The revision's payload SHAs by role.

        ``revision_original`` is the revision row's original; ``original`` and
        ``source_file`` are its bindings of those roles; ``derived`` holds
        every other binding, the extraction artifacts, the asset and proxy
        artifacts and the vector batch artifacts.
        """
        db = self._db
        original = db.execute(
            "SELECT original_sha FROM revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()[0]
        found: dict[str, list[str]] = {
            "revision_original": [original],
            "original": [],
            "source_file": [],
            "derived": [],
        }
        for role, sha in db.execute(
            "SELECT role,artifact_sha FROM revision_artifacts WHERE revision_id=?",
            (revision_id,),
        ):
            found[role if role in {"original", "source_file"} else "derived"].append(
                sha
            )
        for row in db.execute(
            "SELECT document_artifact_sha,assertions_artifact_sha,diagnostics_artifact_sha "
            "FROM extractions WHERE revision_id=?",
            (revision_id,),
        ):
            found["derived"].extend(sha for sha in row if sha is not None)
        for (sha,) in db.execute(
            "SELECT a.artifact_sha FROM assets a JOIN extractions e USING(extraction_id) "
            "WHERE e.revision_id=? UNION SELECT p.derivation_sha FROM asset_proxies p "
            "JOIN assets a USING(asset_id) JOIN extractions e USING(extraction_id) "
            "WHERE e.revision_id=?",
            (revision_id, revision_id),
        ):
            found["derived"].append(sha)
        for (sha,) in db.execute(
            "SELECT DISTINCT b.vectors_sha FROM index_batches b "
            "JOIN extractions e USING(extraction_id) "
            "WHERE b.vectors_sha IS NOT NULL AND e.revision_id=?",
            (revision_id,),
        ):
            found["derived"].append(sha)
        return {role: tuple(shas) for role, shas in found.items()}

    def tombstoned(self, revision_id: str, sha: str) -> bool:
        """Whether the revision's payload was removed."""
        return revision_tombstoned(self._db, revision_id, sha)

    def insert_tombstone(
        self, revision_id: str, sha: str, job_id: str, removed_at: datetime
    ) -> None:
        """Record that the revision's payload was removed by the job."""
        require_write(self._db, self._writable)
        self._db.execute(
            "INSERT INTO original_payload_tombstones(revision_id,artifact_sha,removal_job_id,removed_at) VALUES (?,?,?,?)",
            (revision_id, sha, job_id, removed_at.isoformat()),
        )

    def manifest_roots(self) -> frozenset[tuple[str, str]]:
        """``(SHA, kind)`` of every typed manifest root."""
        return frozenset(
            (sha, kind)
            for query, kind in TYPED_MANIFEST_ROOTS
            for (sha,) in self._db.execute(query).fetchall()
        )

    def capture_roots(self) -> tuple[tuple[str, bool], ...]:
        """``(captured inputs SHA, published)`` of every job capture."""
        return tuple(
            (sha, bool(published))
            for sha, published in self._db.execute(
                "SELECT captured_inputs_sha,state='complete' AND result_snapshot_id IS NOT NULL "
                "FROM jobs WHERE captured_inputs_sha IS NOT NULL"
            ).fetchall()
        )
