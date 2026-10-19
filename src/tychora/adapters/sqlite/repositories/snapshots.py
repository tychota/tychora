"""Snapshots, the active pointer and content-addressed membership pages."""

import json
import sqlite3
from collections.abc import Mapping
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

from .artifacts import artifact_ref
from .batches import BATCH_SELECT, batch_from_row
from .families import FAMILY_COLUMNS, family_from_row
from .workspace import SqliteWorkspaceRepository, require_write

_SNAPSHOT_ROW = (
    "SELECT workspace_id,parent_snapshot_id,membership_sha,config_sha,member_count "
    "FROM snapshots WHERE snapshot_id=?"
)
_FAMILY_COLUMNS = ",".join(f"f.{name}" for name in FAMILY_COLUMNS.split(","))


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
        members = self._members(snapshot_id)
        if membership_sha(page_ids) != row[2] or len(members) != row[4]:
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
            members=members,
            lexical_statistics_epoch=SqliteWorkspaceRepository(
                self._db, writable=False
            ).lexical_epoch(),
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

    def _members(self, snapshot_id: str) -> tuple[SnapshotMember, ...]:
        return tuple(
            SnapshotMember(
                source_id=r[0],
                revision_id=r[1],
                extraction_id=r[2],
                is_default=bool(r[3]),
            )
            for r in self._db.execute(
                "SELECT source_id,revision_id,extraction_id,is_default "
                "FROM snapshot_members WHERE snapshot_id=? "
                "ORDER BY source_id,revision_id,extraction_id",
                (snapshot_id,),
            )
        )

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
