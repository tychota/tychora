"""Snapshots, the active pointer and content-addressed membership pages."""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.publication import PublicationPlan, SnapshotMember, SnapshotView
from tychora.contracts.snapshots import (
    MembershipPage,
    PageBatch,
    PageMember,
    SlotFamily,
    membership_sha,
)
from tychora.ports.repositories import (
    BatchRow,
    Cursor,
    GcReport,
    PublicationFootprint,
)

from .artifacts import SqliteArtifactRepository, artifact_ref
from .batches import BATCH_SELECT, batch_from_row
from .families import FAMILY_COLUMNS, family_from_row
from .workspace import require_write

_SNAPSHOT_ROW = (
    "SELECT workspace_id,parent_snapshot_id,membership_sha,config_sha,member_count "
    "FROM snapshots WHERE snapshot_id=?"
)
# A full member read walks the snapshot's pages in bucket order and reads each
# page through the page_members primary key, so it is linear in the members.
PAGE_IDS = "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? ORDER BY bucket"
PAGE_MEMBERS = (
    "SELECT source_id,revision_id,extraction_id,is_default FROM page_members "
    "WHERE page_id=? ORDER BY revision_id"
)
_FAMILY_COLUMNS = ",".join(f"f.{name}" for name in FAMILY_COLUMNS.split(","))
# Most recently published snapshots kept besides the active one, its parent and
# every snapshot a query run, an evaluation, a job or a lease still needs.
RETAINED_SNAPSHOTS = 16
_IN_IDS = "IN (SELECT value FROM json_each(?))"
_RETAINED = (
    "SELECT active_snapshot_id FROM workspaces WHERE active_snapshot_id IS NOT NULL "
    "UNION SELECT s.parent_snapshot_id FROM snapshots s JOIN workspaces w "
    "ON s.snapshot_id=w.active_snapshot_id WHERE s.parent_snapshot_id IS NOT NULL "
    "UNION SELECT snapshot_id FROM (SELECT snapshot_id FROM snapshots "
    "ORDER BY published_at DESC,snapshot_id DESC LIMIT ?) "
    "UNION SELECT snapshot_id FROM query_runs WHERE expires_at>? "
    "UNION SELECT snapshot_id FROM evaluation_runs "
    "UNION SELECT base_snapshot_id FROM jobs WHERE base_snapshot_id IS NOT NULL "
    "AND state IN ('queued','running','cancelling')"
)
_UNBOUND_BATCH = (
    "NOT EXISTS (SELECT 1 FROM page_batches p WHERE p.batch_id=b.batch_id) "
    "AND NOT EXISTS (SELECT 1 FROM snapshot_root_batches r WHERE r.batch_id=b.batch_id)"
)


def _ambiguous_workspace() -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "Workspace identity is missing or ambiguous.",
        reason="canonical.row_invalid",
    )


def _integrity(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


def absent_snapshot() -> TychoraError:
    """The refusal for a snapshot that was collected or never published."""
    return TychoraError(
        "NOT_FOUND", "Snapshot is unavailable", reason="snapshot.absent"
    )


def _member(row: tuple) -> SnapshotMember:
    return SnapshotMember(
        source_id=row[0],
        revision_id=row[1],
        extraction_id=row[2],
        is_default=bool(row[3]),
    )


class SqliteSnapshotRepository:
    """Snapshot rows, the workspace's active snapshot and page rows."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = True) -> None:
        self._db = db
        self._writable = writable

    def active_id(self) -> str | None:
        """Return the active snapshot ID, or ``None`` before the first publication."""
        rows = self._db.execute("SELECT active_snapshot_id FROM workspaces").fetchall()
        if len(rows) != 1:
            raise _ambiguous_workspace()
        return rows[0][0]

    def set_active(self, snapshot_id: str) -> None:
        """Point the workspace at a published snapshot."""
        require_write(self._db, self._writable)
        updated = self._db.execute(
            "UPDATE workspaces SET active_snapshot_id=?", (snapshot_id,)
        ).rowcount
        if updated != 1:
            raise _ambiguous_workspace()

    def view(self, snapshot_id: str) -> SnapshotView:
        """Read a published snapshot from its rows; no artifact is opened."""
        row = self._db.execute(_SNAPSHOT_ROW, (snapshot_id,)).fetchone()
        if row is None:
            raise absent_snapshot()
        page_ids = tuple(
            r[0]
            for r in self._db.execute(
                "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? ORDER BY bucket",
                (snapshot_id,),
            )
        )
        (count,) = self._db.execute(
            "SELECT COUNT(*) FROM snapshot_members WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if membership_sha(page_ids) != row[2] or count != row[4]:
            raise _integrity("Snapshot rows differ from their membership digest")
        return SnapshotView(
            snapshot_id=snapshot_id,
            workspace_id=row[0],
            parent_snapshot_id=row[1],
            config_sha=row[3],
            membership_sha=row[2],
            member_count=row[4],
            page_ids=page_ids,
            families=self._families(snapshot_id),
        )

    def member(self, snapshot_id: str, extraction_id: str) -> SnapshotMember | None:
        """Return the member that selects this extraction, or ``None``."""
        row = self._db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members "
            "WHERE snapshot_id=? AND extraction_id=?",
            (snapshot_id, extraction_id),
        ).fetchone()
        if row is not None:
            return _member(row)
        if self._db.execute(_SNAPSHOT_ROW, (snapshot_id,)).fetchone() is None:
            raise absent_snapshot()
        return None

    def all_members(self, snapshot_id: str) -> Iterator[SnapshotMember]:
        """Yield every member, one membership page at a time in bucket order.

        Members come in bucket order, not source order. A snapshot whose pages
        hold a different number of members than its row records is refused.
        """
        row = self._db.execute(_SNAPSHOT_ROW, (snapshot_id,)).fetchone()
        if row is None:
            raise absent_snapshot()
        page_ids = [r[0] for r in self._db.execute(PAGE_IDS, (snapshot_id,))]
        count = 0
        for page_id in page_ids:
            rows = self._db.execute(PAGE_MEMBERS, (page_id,)).fetchall()
            count += len(rows)
            yield from map(_member, rows)
        if count != row[4]:
            raise _integrity("Snapshot members differ from their recorded count")

    def members(
        self, snapshot_id: str, *, after: Cursor | None = None, limit: int = 1000
    ) -> tuple[tuple[SnapshotMember, ...], Cursor | None]:
        """Return one page of members in source, revision and extraction order."""
        if not 1 <= limit <= 10_000:
            raise TychoraError(
                "INVALID_REQUEST",
                "Member page size must be 1..10000",
                reason="cursor.invalid",
            )
        start = (
            ("", "", "")
            if after is None
            else (*map(str, after.sort_key), after.last_id)
        )
        if len(start) != 3:
            raise TychoraError(
                "INVALID_REQUEST", "Cursor is invalid.", reason="cursor.invalid"
            )
        rows = self._db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members "
            "WHERE snapshot_id=? AND (source_id,revision_id,extraction_id)>(?,?,?) "
            "ORDER BY source_id,revision_id,extraction_id LIMIT ?",
            (snapshot_id, *start, limit + 1),
        ).fetchall()
        page = tuple(_member(row) for row in rows[:limit])
        if len(rows) <= limit:
            return page, None
        last = page[-1]
        return page, Cursor(
            sort_key=(last.source_id, last.revision_id), last_id=last.extraction_id
        )

    def _families(self, snapshot_id: str) -> tuple[SlotFamily, ...]:
        rows = self._db.execute(
            f"SELECT si.slot,si.required,{_FAMILY_COLUMNS} FROM snapshot_indexes si "
            "JOIN index_families f USING(family_id) WHERE si.snapshot_id=? "
            "ORDER BY si.slot",
            (snapshot_id,),
        ).fetchall()
        try:
            return tuple(
                SlotFamily(
                    slot=r[0], required=bool(r[1]), family=family_from_row(r[2:])
                )
                for r in rows
            )
        except ValueError:
            raise _integrity("Snapshot slot differs from its family") from None

    def insert_snapshot(self, plan: PublicationPlan) -> None:
        """Store a snapshot over pages already stored, with its slots and root batches."""
        require_write(self._db, self._writable)
        buckets = dict(
            self._db.execute(
                "SELECT page_id,bucket FROM membership_pages "
                "WHERE page_id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(plan.page_ids)),),
            ).fetchall()
        )
        if set(buckets) != set(plan.page_ids) or len(set(buckets.values())) != len(
            buckets
        ):
            raise _integrity("Snapshot pages are missing or share a bucket")
        self._db.execute(
            "INSERT INTO snapshots (snapshot_id,workspace_id,parent_snapshot_id,"
            "membership_sha,config_sha,manifest_sha,published_at,job_id,member_count) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                plan.snapshot_id,
                plan.workspace_id,
                plan.base_snapshot_id,
                membership_sha(plan.page_ids),
                plan.config_sha,
                plan.manifest.sha256,
                datetime.now(UTC).isoformat(),
                plan.job_id,
                plan.member_count,
            ),
        )
        self._db.executemany(
            "INSERT INTO snapshot_indexes (snapshot_id,slot,family_id,required) "
            "VALUES (?,?,?,?)",
            [
                (plan.snapshot_id, f.slot, f.family.family_id, int(f.required))
                for f in plan.families
            ],
        )
        self._db.executemany(
            "INSERT INTO snapshot_pages (snapshot_id,bucket,page_id) VALUES (?,?,?)",
            [
                (plan.snapshot_id, buckets[page_id], page_id)
                for page_id in plan.page_ids
            ],
        )
        self._db.executemany(
            "INSERT INTO snapshot_root_batches (snapshot_id,slot,batch_id) VALUES (?,?,?)",
            [(plan.snapshot_id, b.slot, b.batch_id) for b in plan.root_batches],
        )

    def insert_page(self, page: MembershipPage, manifest: ArtifactRef) -> None:
        """Store a page and its rows once; an identical repeat is a no-op."""
        require_write(self._db, self._writable)
        expected = (page.bucket, manifest.sha256, len(page.members))
        self._db.execute(
            "INSERT INTO membership_pages (page_id,bucket,manifest_sha,member_count) "
            "VALUES (?,?,?,?) ON CONFLICT(page_id) DO NOTHING",
            (page.page_id, *expected),
        )
        stored = self._db.execute(
            "SELECT bucket,manifest_sha,member_count FROM membership_pages WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
        if stored != expected:
            raise _integrity("Membership page differs from its immutable row")
        self._db.executemany(
            "INSERT INTO page_members (page_id,source_id,revision_id,extraction_id,"
            "is_default) VALUES (?,?,?,?,?) ON CONFLICT(page_id,revision_id) DO NOTHING",
            [
                (
                    page.page_id,
                    m.source_id,
                    m.revision_id,
                    m.extraction_id,
                    int(m.is_default),
                )
                for m in page.members
            ],
        )
        self._db.executemany(
            "INSERT INTO page_batches (page_id,slot,batch_id) VALUES (?,?,?) "
            "ON CONFLICT(page_id,slot,batch_id) DO NOTHING",
            [
                (page.page_id, b.slot, b.batch_id)
                for m in page.members
                for b in m.batches
            ],
        )

    def page_manifests(self, page_ids: tuple[str, ...]) -> Mapping[str, ArtifactRef]:
        """Return the manifest artifact of each page; an unknown page is refused."""
        rows = self._db.execute(
            "SELECT p.page_id,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
            "FROM membership_pages p JOIN artifacts a ON a.artifact_sha=p.manifest_sha "
            "WHERE p.page_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(set(page_ids))),),
        ).fetchall()
        found = {row[0]: artifact_ref(row[1:5]) for row in rows}
        if set(found) != set(page_ids):
            raise _integrity("Membership page manifest is not registered")
        return found

    def page(self, snapshot_id: str, bucket: int) -> MembershipPage | None:
        """Return a snapshot's page for one bucket, or ``None`` when it has none."""
        row = self._db.execute(
            "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? AND bucket=?",
            (snapshot_id, bucket),
        ).fetchone()
        return None if row is None else self._load_page(row[0], bucket)

    def _page_batches(self, page_id: str) -> dict[str, list[PageBatch]]:
        batches: dict[str, list[PageBatch]] = {}
        rows = self._db.execute(
            BATCH_SELECT + "JOIN page_batches p ON p.batch_id=b.batch_id "
            "WHERE p.page_id=? ORDER BY p.slot,b.batch_id",
            (page_id,),
        )
        for row in rows:
            batch = batch_from_row(row)
            batches.setdefault(batch.extraction_id or "", []).append(batch.page_batch())
        return batches

    def _load_page(self, page_id: str, bucket: int) -> MembershipPage:
        members = self._db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM page_members "
            "WHERE page_id=? ORDER BY source_id,revision_id",
            (page_id,),
        ).fetchall()
        batches = self._page_batches(page_id)
        try:
            return MembershipPage(
                bucket=bucket,
                page_id=page_id,
                members=tuple(
                    PageMember(
                        source_id=m[0],
                        revision_id=m[1],
                        extraction_id=m[2],
                        is_default=bool(m[3]),
                        batches=tuple(batches.get(m[2], ())),
                    )
                    for m in members
                ),
            )
        except ValueError:
            raise _integrity("Membership page rows differ from its identity") from None

    def retained_ids(self, keep_last: int = RETAINED_SNAPSHOTS) -> frozenset[str]:
        """Return the snapshots the retention rule keeps.

        The rule keeps the active snapshot and its parent, the last
        ``keep_last`` published, those of unexpired query runs and of
        evaluation runs, and the recorded base of every unfinished job.
        """
        rows = self._db.execute(
            _RETAINED, (keep_last, datetime.now(UTC).isoformat())
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def footprint(self, snapshot_id: str) -> PublicationFootprint:
        """Return the root bytes and the bytes of pages new relative to the parent."""
        root = self._db.execute(
            "SELECT a.size_bytes,s.parent_snapshot_id FROM snapshots s "
            "JOIN artifacts a ON a.artifact_sha=s.manifest_sha WHERE s.snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if root is None:
            raise absent_snapshot()
        pages = self._db.execute(
            "SELECT COUNT(*),COALESCE(SUM(a.size_bytes),0),COALESCE(MAX(a.size_bytes),0) "
            "FROM snapshot_pages sp JOIN membership_pages p ON p.page_id=sp.page_id "
            "JOIN artifacts a ON a.artifact_sha=p.manifest_sha WHERE sp.snapshot_id=? "
            "AND NOT EXISTS (SELECT 1 FROM snapshot_pages b "
            "WHERE b.snapshot_id IS ? AND b.page_id=sp.page_id)",
            (snapshot_id, root[1]),
        ).fetchone()
        return PublicationFootprint(
            snapshot_id=snapshot_id,
            root_bytes=root[0],
            new_page_bytes=pages[1],
            new_pages=pages[0],
            largest_artifact_bytes=max(root[0], pages[2]),
        )

    def collect(
        self,
        limit: int = 64,
        *,
        leased: frozenset[str] = frozenset(),
        include_unpublished: bool = False,
    ) -> GcReport:
        """Delete unretained snapshots and orphan pages; mark orphan batches removed.

        At most ``limit`` snapshots go, oldest first. ``leased`` names snapshots
        and batches in use outside SQL; neither is collected. Batches the
        deleted snapshots and pages bound become ``removed`` once nothing binds
        them. ``include_unpublished`` also marks every other unbound batch,
        unless a job is unfinished, because its batches are not yet bound.
        Released snapshot and page manifests become ``delete_pending``.
        """
        require_write(self._db, self._writable)
        keep = self.retained_ids(RETAINED_SNAPSHOTS) | leased
        doomed = [
            row[0]
            for row in self._db.execute(
                f"SELECT snapshot_id FROM snapshots WHERE snapshot_id NOT {_IN_IDS} "
                "ORDER BY published_at,snapshot_id LIMIT ?",
                (json.dumps(sorted(keep)), limit),
            )
        ]
        manifests, pages, roots = self._delete_snapshots(doomed)
        page_ids, page_manifests, page_batches = self._delete_orphan_pages(
            pages, everywhere=include_unpublished
        )
        skipped = include_unpublished and self._jobs_unfinished()
        orphans = self._orphan_batches(
            roots | page_batches, leased, everywhere=include_unpublished and not skipped
        )
        # The report carries each batch's state before it became removed.
        orphan_rows = self._batch_rows(orphans)
        self._db.execute(
            f"UPDATE index_batches SET state='removed' WHERE batch_id {_IN_IDS}",
            (json.dumps(orphans),),
        )
        return GcReport(
            snapshots=tuple(doomed),
            pages=page_ids,
            orphan_batches=orphan_rows,
            released_artifacts=self._release(manifests | page_manifests),
            unpublished_skipped=skipped,
        )

    def _delete_snapshots(
        self, doomed: list[str]
    ) -> tuple[set[str], set[str], set[str]]:
        """Delete snapshot rows; return their manifests, pages and root batches."""
        ids = json.dumps(doomed)

        def column(sql: str) -> set[str]:
            return {row[0] for row in self._db.execute(sql, (ids,))}

        manifests = column(
            f"SELECT manifest_sha FROM snapshots WHERE snapshot_id {_IN_IDS}"
        )
        pages = column(
            f"SELECT page_id FROM snapshot_pages WHERE snapshot_id {_IN_IDS}"
        )
        roots = column(
            f"SELECT batch_id FROM snapshot_root_batches WHERE snapshot_id {_IN_IDS}"
        )
        # Table names come only from this literal tuple, children first.
        for table in (
            "snapshot_pages",
            "snapshot_indexes",
            "snapshot_root_batches",
            "snapshots",
        ):
            self._db.execute(f"DELETE FROM {table} WHERE snapshot_id {_IN_IDS}", (ids,))
        return manifests, pages, roots

    def _delete_orphan_pages(
        self, candidates: set[str], *, everywhere: bool
    ) -> tuple[tuple[str, ...], set[str], set[str]]:
        """Delete pages no snapshot binds; return them, their manifests and batches."""
        rows = self._db.execute(
            "SELECT page_id,manifest_sha FROM membership_pages p WHERE NOT EXISTS "
            "(SELECT 1 FROM snapshot_pages s WHERE s.page_id=p.page_id) "
            f"AND (? OR p.page_id {_IN_IDS}) ORDER BY page_id",
            (int(everywhere), json.dumps(sorted(candidates))),
        ).fetchall()
        ids = json.dumps([row[0] for row in rows])
        batches = {
            row[0]
            for row in self._db.execute(
                f"SELECT batch_id FROM page_batches WHERE page_id {_IN_IDS}", (ids,)
            )
        }
        # Table names come only from this literal tuple, children first.
        for table in ("page_batches", "page_members", "membership_pages"):
            self._db.execute(f"DELETE FROM {table} WHERE page_id {_IN_IDS}", (ids,))
        return tuple(row[0] for row in rows), {row[1] for row in rows}, batches

    def _jobs_unfinished(self) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running','cancelling') "
                "LIMIT 1"
            ).fetchone()
            is not None
        )

    def _orphan_batches(
        self, candidates: set[str], leased: frozenset[str], *, everywhere: bool
    ) -> list[str]:
        """Return unbound, unleased batches: published candidates, or every one.

        A ``removed`` batch is always a candidate: it was deferred because its
        native store was closed, and every later pass retries it.
        """
        return [
            row[0]
            for row in self._db.execute(
                f"SELECT batch_id FROM index_batches b WHERE {_UNBOUND_BATCH} "
                f"AND batch_id NOT {_IN_IDS} AND (? OR state='removed' OR (state IN ('ready','pending') "
                f"AND batch_id {_IN_IDS})) ORDER BY batch_id",
                (
                    json.dumps(sorted(leased)),
                    int(everywhere),
                    json.dumps(sorted(candidates)),
                ),
            )
        ]

    def _batch_rows(self, batch_ids: list[str]) -> tuple[BatchRow, ...]:
        rows = self._db.execute(
            BATCH_SELECT + f"WHERE b.batch_id {_IN_IDS} ORDER BY b.batch_id",
            (json.dumps(batch_ids),),
        ).fetchall()
        return tuple(batch_from_row(row) for row in rows)

    def _release(self, shas: set[str]) -> tuple[str, ...]:
        """Mark manifests no snapshot or page row binds as ``delete_pending``."""
        artifacts = SqliteArtifactRepository(self._db, writable=self._writable)
        released = []
        for sha in sorted(shas):
            if self._db.execute(
                "SELECT 1 FROM snapshots WHERE manifest_sha=? UNION ALL "
                "SELECT 1 FROM membership_pages WHERE manifest_sha=? LIMIT 1",
                (sha, sha),
            ).fetchone():
                continue
            if artifacts.release(sha):
                released.append(sha)
        return tuple(released)
