"""Snapshots, the active pointer and content-addressed membership pages."""

import json
import sqlite3
from collections.abc import Mapping

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.snapshots import MembershipPage, PageBatch, PageMember

from .artifacts import artifact_ref
from .batches import BATCH_SELECT, batch_from_row
from .workspace import require_write


def _ambiguous_workspace() -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "Workspace identity is missing or ambiguous.",
        reason="canonical.row_invalid",
    )


def _integrity(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


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
