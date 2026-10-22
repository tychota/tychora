"""Canonical rows that keep an artifact payload live.

A payload stays live while a revision binds it as an original that is neither
tombstoned nor proposed for removal, while a revision binds it in any other
role, while a direct column names it, or while a locator of a revision that
does not bind it as an original cites it. Typed manifest closures are decided
separately.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

# Columns whose row binds an artifact whatever the revision state.
DIRECT_BINDINGS = (
    ("extractions", "document_artifact_sha"),
    ("extractions", "assertions_artifact_sha"),
    ("extractions", "diagnostics_artifact_sha"),
    ("assets", "artifact_sha"),
    ("asset_proxies", "derivation_sha"),
    ("index_batches", "vectors_sha"),
    ("snapshots", "manifest_sha"),
    ("membership_pages", "manifest_sha"),
    ("index_batches", "input_manifest_sha"),
    ("jobs", "effective_config_sha"),
    ("jobs", "request_artifact_sha"),
    ("query_runs", "retained_payload_sha"),
    ("evaluation_datasets", "manifest_sha"),
    ("evaluation_cases", "payload_sha"),
    ("evaluation_results", "result_artifact_sha"),
    ("job_steps", "output_artifact_sha"),
    ("review_events", "reason_artifact_sha"),
    ("assertion_decisions", "provenance_artifact_sha"),
    ("assertion_decisions", "reason_artifact_sha"),
)
_ORIGINAL_ROLES = frozenset({"original", "source_file"})
_DIRECT_QUERY = (
    " UNION ALL ".join(
        f"SELECT 1 FROM {table} WHERE {column}=?" for table, column in DIRECT_BINDINGS
    )
    + " LIMIT 1"
)
_TOMBSTONED = (
    "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?"
)
_LOCATOR_REVISIONS = (
    "SELECT e.revision_id,l.artifact_sha FROM locators l "
    "JOIN evidence_units u USING(unit_id) JOIN extractions e USING(extraction_id)"
)

_DIRECT_COLUMNS = tuple(
    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
    for table, column in DIRECT_BINDINGS
)
_DERIVED_ROLES = (
    "SELECT artifact_sha FROM revision_artifacts "
    "WHERE role NOT IN ('original','source_file')"
)
_ORIGINAL_BINDINGS = (
    "SELECT revision_id,original_sha FROM revisions UNION ALL "
    "SELECT revision_id,artifact_sha FROM revision_artifacts "
    "WHERE role IN ('original','source_file')"
)

Proposed = frozenset[tuple[str, str]]


def _grouped(rows, candidates: frozenset[str]) -> dict[str, frozenset[str]]:
    """Group ``(revision_id, sha)`` rows by candidate SHA."""
    grouped: defaultdict[str, set[str]] = defaultdict(set)
    for revision_id, sha in rows:
        if sha in candidates:
            grouped[sha].add(revision_id)
    return {sha: frozenset(ids) for sha, ids in grouped.items()}


def live_by_rows(db: sqlite3.Connection, sha: str, proposed: Proposed = frozenset()):
    """Decide one payload with per-payload lookups.

    Fits a few payloads; a pass over many uses ``BoundSet``.
    """

    def kept(revision_id: str) -> bool:
        return (revision_id, sha) not in proposed and not db.execute(
            _TOMBSTONED, (revision_id, sha)
        ).fetchone()

    for (revision_id,) in db.execute(
        "SELECT revision_id FROM revisions WHERE original_sha=?", (sha,)
    ):
        if kept(revision_id):
            return True
    for revision_id, role in db.execute(
        "SELECT revision_id,role FROM revision_artifacts WHERE artifact_sha=?", (sha,)
    ):
        if role not in _ORIGINAL_ROLES or kept(revision_id):
            return True
    if db.execute(_DIRECT_QUERY, (sha,) * len(DIRECT_BINDINGS)).fetchone():
        return True
    for (revision_id,) in db.execute(
        "SELECT e.revision_id FROM locators l JOIN evidence_units u USING(unit_id) "
        "JOIN extractions e USING(extraction_id) WHERE l.artifact_sha=?",
        (sha,),
    ):
        original_binding = db.execute(
            "SELECT 1 FROM revisions WHERE revision_id=? AND original_sha=? "
            "UNION ALL SELECT 1 FROM revision_artifacts WHERE revision_id=? "
            "AND artifact_sha=? AND role IN ('original','source_file') LIMIT 1",
            (revision_id, sha, revision_id, sha),
        ).fetchone()
        if original_binding is None or kept(revision_id):
            return True
    return False


@dataclass(frozen=True)
class BoundSet:
    """The row bindings of a set of candidate payloads, read once.

    Reads each binding table once, whatever the number of candidates, and
    keeps only rows that name a candidate. Valid while the rows it read do not
    change, such as within one owner-thread pass.
    """

    direct: frozenset[str]
    originals: dict[str, frozenset[str]]
    tombstones: frozenset[tuple[str, str]]
    cited: dict[str, frozenset[str]]
    rooted: frozenset[str] = frozenset()

    @classmethod
    def load(
        cls,
        db: sqlite3.Connection,
        candidates: frozenset[str],
        *,
        rooted: frozenset[str] = frozenset(),
    ) -> "BoundSet":
        """Read the bindings of ``candidates`` with one query per binding column.

        ``rooted`` holds what typed manifest closures reach in the same pass.
        Without candidates nothing is read.
        """
        if not candidates:
            return cls(frozenset(), {}, frozenset(), {}, rooted)
        direct = {
            sha
            for query in (*_DIRECT_COLUMNS, _DERIVED_ROLES)
            for (sha,) in db.execute(query)
            if sha in candidates
        }
        return cls(
            direct=frozenset(direct),
            originals=_grouped(db.execute(_ORIGINAL_BINDINGS), candidates),
            tombstones=frozenset(
                (revision_id, sha)
                for revision_id, sha in db.execute(
                    "SELECT revision_id,artifact_sha FROM original_payload_tombstones"
                )
                if sha in candidates
            ),
            cited=_grouped(db.execute(_LOCATOR_REVISIONS), candidates),
            rooted=rooted,
        )

    def binds(self, sha: str, proposed: Proposed = frozenset()) -> bool:
        """Return whether a row or a root keeps ``sha`` live.

        Rows decide as ``live_by_rows`` does.
        """
        if sha in self.direct or sha in self.rooted:
            return True
        originals = self.originals.get(sha, frozenset())
        if any(
            (revision_id, sha) not in proposed
            and (revision_id, sha) not in self.tombstones
            for revision_id in originals
        ):
            return True
        return any(
            revision_id not in originals for revision_id in self.cited.get(sha, ())
        )
