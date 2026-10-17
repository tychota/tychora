"""Read-only inventory of an older workspace, and collection seeding in a new one.

Only tables whose columns are unchanged since storage format 1 are read, so the
rebuild can enumerate any older workspace's retained originals without a
format-specific reader. The database is read through a copy when a -wal exists,
so the old workspace is never written.
"""

import fcntl
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from tychora.contracts.errors import TychoraError

from .receipt import open_untouched


class InventoryCollection(Contract):
    """A collection of the older workspace, enough to recreate it."""

    collection_id: Identifier
    slug: NonEmptyStr
    title: NonEmptyStr
    access_floor: NonNegativeInt
    policy_json: NonEmptyStr


class InventoryRevision(Contract):
    """A revision with its retained original, or None when the payload is gone."""

    revision_id: Identifier
    kind_id: Identifier
    metadata_json: NonEmptyStr
    captured_at: NonEmptyStr
    access_rank: Annotated[int, Field(ge=0, le=3)]
    declared_review_state: NonEmptyStr
    allowed_uses_json: NonEmptyStr
    original: ArtifactRef | None


class InventorySource(Contract):
    """A source with its revisions in capture order."""

    source_id: Identifier
    collection_id: Identifier
    source_key: NonEmptyStr
    source_type: Literal["document", "repository"]
    registration_json: NonEmptyStr
    access_floor: Annotated[int, Field(ge=0, le=3)]
    withdrawn: bool
    revisions: tuple[InventoryRevision, ...]


class InventoryReview(Contract):
    """The latest review state of one revision."""

    revision_id: Identifier
    state: NonEmptyStr
    actor_id: NonEmptyStr


class WorkspaceInventory(Contract):
    """Everything the rebuild needs to know about an older workspace."""

    workspace_id: Identifier
    collections: tuple[InventoryCollection, ...]
    sources: tuple[InventorySource, ...]
    reviews: tuple[InventoryReview, ...]
    finer_reviews: NonNegativeInt


_REVISIONS = (
    "SELECT r.source_id,r.revision_id,r.kind_id,r.metadata_json,r.captured_at,"
    "r.access_rank,r.declared_review_state,r.allowed_uses_json,"
    "a.artifact_sha,a.size_bytes,a.media_type,a.storage_key,a.payload_state,"
    "EXISTS(SELECT 1 FROM original_payload_tombstones t "
    "WHERE t.revision_id=r.revision_id) "
    "FROM revisions r JOIN artifacts a ON a.artifact_sha=r.original_sha "
    "ORDER BY r.captured_at,r.revision_id"
)
_LATEST_REVIEWS = (
    "SELECT e.revision_id,e.state,e.actor_id FROM review_events e JOIN "
    "(SELECT revision_id,MAX(review_event_id) AS latest FROM review_events "
    "WHERE revision_id IS NOT NULL GROUP BY revision_id) l "
    "ON l.latest=e.review_event_id ORDER BY e.review_event_id"
)


def _require_no_live_owner(root: Path) -> None:
    lock_path = root / ".runtime" / "owner.lock"
    if not lock_path.exists():
        return
    fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TychoraError(
                "WORKSPACE_BUSY",
                "The old workspace is open in another process; close it first.",
                reason="owner.live",
            ) from None
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _revisions(db: sqlite3.Connection) -> dict[str, list[InventoryRevision]]:
    grouped: dict[str, list[InventoryRevision]] = {}
    for row in db.execute(_REVISIONS):
        retained = row[12] == "present" and not row[13]
        grouped.setdefault(row[0], []).append(
            InventoryRevision(
                revision_id=row[1],
                kind_id=row[2],
                metadata_json=row[3],
                captured_at=row[4],
                access_rank=row[5],
                declared_review_state=row[6],
                allowed_uses_json=row[7],
                original=ArtifactRef.from_row(row[8:12]) if retained else None,
            )
        )
    return grouped


def _sources(db: sqlite3.Connection) -> tuple[InventorySource, ...]:
    revisions = _revisions(db)
    return tuple(
        InventorySource(
            source_id=row[0],
            collection_id=row[1],
            source_key=row[2],
            source_type=row[3],
            registration_json=row[4],
            access_floor=row[5],
            withdrawn=row[6] is not None,
            revisions=tuple(revisions.get(row[0], ())),
        )
        for row in db.execute(
            "SELECT source_id,collection_id,source_key,source_type,registration_json,"
            "access_floor,withdrawn_at FROM sources ORDER BY created_at,source_id"
        )
    )


def _collections(db: sqlite3.Connection) -> tuple[InventoryCollection, ...]:
    return tuple(
        InventoryCollection(
            collection_id=row[0],
            slug=row[1],
            title=row[2],
            access_floor=row[3],
            policy_json=row[4],
        )
        for row in db.execute(
            "SELECT collection_id,slug,title,access_floor,policy_json "
            "FROM collections ORDER BY slug"
        )
    )


def _inventory(db: sqlite3.Connection) -> WorkspaceInventory:
    identities = db.execute("SELECT workspace_id FROM workspaces").fetchall()
    if len(identities) != 1:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "The old workspace identity is missing or ambiguous.",
            reason="canonical.row_invalid",
        )
    finer = db.execute(
        "SELECT COUNT(*) FROM review_events WHERE revision_id IS NULL"
    ).fetchone()[0]
    return WorkspaceInventory(
        workspace_id=identities[0][0],
        collections=_collections(db),
        sources=_sources(db),
        reviews=tuple(
            InventoryReview(revision_id=row[0], state=row[1], actor_id=row[2])
            for row in db.execute(_LATEST_REVIEWS)
        ),
        finer_reviews=finer,
    )


def read_inventory(root: Path) -> WorkspaceInventory:
    """Enumerate an older workspace's collections, sources, originals and reviews."""
    _require_no_live_owner(root)
    database = root / "corpus.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise TychoraError(
            "NOT_FOUND",
            "The old workspace has no corpus database.",
            reason="workspace.absent",
        )
    with open_untouched(database) as db:
        try:
            return _inventory(db)
        except ValidationError:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "The old workspace holds a row this version cannot read.",
                reason="canonical.row_invalid",
            ) from None


def read_original(
    root: Path, ref: ArtifactRef, max_bytes: int = 1024**3
) -> bytes | None:
    """Return a retained original's verified bytes, or None when the file is gone.

    A missing artifacts directory counts as every file being gone; nothing is
    ever created inside the older workspace.
    """
    if ref.size_bytes > max_bytes:
        raise TychoraError(
            "LIMIT_EXCEEDED",
            "A retained original exceeds the rebuild limit.",
            reason="rebuild.original_too_large",
        )
    directory = root / "artifacts"
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        with LocalArtifacts(directory).open_verified(ref) as stream:
            return stream.read()
    except TychoraError as error:
        if error.reason == "payload.missing":
            return None
        raise


def _seed_one(
    db: sqlite3.Connection, workspace_id: str, item: InventoryCollection
) -> tuple[bool, bool]:
    """Insert or refresh one collection; return (inserted, changed)."""
    values = (item.title, item.access_floor, item.policy_json)
    if db.execute(
        "UPDATE collections SET title=?,access_floor=?,policy_json=? "
        "WHERE collection_id=? AND slug=? "
        "AND (title<>? OR access_floor<>? OR policy_json<>?)",
        (*values, item.collection_id, item.slug, *values),
    ).rowcount:
        return False, True
    if db.execute(
        "SELECT 1 FROM collections WHERE collection_id=? AND slug=?",
        (item.collection_id, item.slug),
    ).fetchone():
        return False, False
    db.execute(
        "INSERT INTO collections(collection_id,workspace_id,slug,title,"
        "access_floor,policy_json) VALUES (?,?,?,?,?,?)",
        (item.collection_id, workspace_id, item.slug, *values),
    )
    return True, True


def seed_collections(
    db: sqlite3.Connection,
    workspace_id: str,
    collections: tuple[InventoryCollection, ...],
) -> int:
    """Insert missing collections and refresh changed ones; return inserted count.

    The policy epoch moves only when a row was inserted or changed. A workspace
    identity that is not the destination's is refused.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        if not db.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id=?", (workspace_id,)
        ).fetchone():
            raise TychoraError(
                "INTEGRITY_ERROR",
                "The destination workspace identity does not match.",
                reason="canonical.row_invalid",
            )
        results = [_seed_one(db, workspace_id, item) for item in collections]
        if any(changed for _, changed in results):
            db.execute(
                "UPDATE workspaces SET policy_epoch=policy_epoch+1 "
                "WHERE workspace_id=?",
                (workspace_id,),
            )
        db.commit()
        return sum(inserted for inserted, _ in results)
    except BaseException:
        db.rollback()
        raise
