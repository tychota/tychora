"""Read-only inventory of an older workspace, and collection seeding in a new one.

Only tables whose columns are unchanged since storage format 1 are read, so the
rebuild can enumerate any older workspace's retained originals without a
format-specific reader. The database is read through a copy when a -wal exists,
so the old workspace is never written.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, ValidationError

from tychora.adapters.files.cas import LocalArtifacts
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import Contract, Identifier, NonEmptyStr, NonNegativeInt
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes

from .receipt import open_untouched, require_no_live_owner


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
    """The latest review state of one revision, unit or asset."""

    target_kind: Literal["revision", "unit", "asset"]
    target_id: NonEmptyStr
    state: NonEmptyStr
    actor_id: NonEmptyStr
    decided_at: AwareDatetime


class WorkspaceInventory(Contract):
    """Everything the rebuild needs to know about an older workspace."""

    workspace_id: Identifier
    collections: tuple[InventoryCollection, ...]
    sources: tuple[InventorySource, ...]
    reviews: tuple[InventoryReview, ...]


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
    " UNION ALL ".join(
        f"SELECT '{kind}',e.{kind}_id,e.state,e.actor_id,e.created_at,e.review_event_id "
        f"FROM review_events e JOIN (SELECT {kind}_id AS target,"
        f"MAX(review_event_id) AS latest FROM review_events "
        f"WHERE {kind}_id IS NOT NULL GROUP BY {kind}_id) l "
        f"ON l.latest=e.review_event_id"
        for kind in ("revision", "unit", "asset")
    )
    + " ORDER BY 6"
)


def _aware(value: str) -> datetime:
    """Parse a stored timestamp; a naive one is read as UTC, the zone all rows use."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    return WorkspaceInventory(
        workspace_id=identities[0][0],
        collections=_collections(db),
        sources=_sources(db),
        reviews=tuple(
            InventoryReview(
                target_kind=row[0],
                target_id=row[1],
                state=row[2],
                actor_id=row[3],
                decided_at=_aware(row[4]),
            )
            for row in db.execute(_LATEST_REVIEWS)
        ),
    )


def read_inventory(root: Path) -> WorkspaceInventory:
    """Enumerate an older workspace's collections, sources, originals and reviews."""
    require_no_live_owner(
        root, "The old workspace is open in another process; close it first."
    )
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
        "UPDATE collections SET title=?,access_floor=max(access_floor,?),policy_json=? "
        "WHERE collection_id=? AND slug=? "
        "AND (title<>? OR access_floor<? OR policy_json<>?)",
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

    A refreshed collection keeps the stricter of its current and the supplied
    access floor, so seeding never lowers one. The policy epoch moves only when a
    row was inserted or changed. A workspace identity that is not the
    destination's is refused.
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


def _restore_source(db: sqlite3.Connection, source: InventorySource) -> bool:
    return bool(
        db.execute(
            "UPDATE sources SET access_floor=? WHERE source_id=? AND access_floor<?",
            (source.access_floor, source.source_id, source.access_floor),
        ).rowcount
    )


def _restore_revision(
    db: sqlite3.Connection, source_id: str, revision: InventoryRevision
) -> bool:
    current = db.execute(
        "SELECT access_rank,allowed_uses_json,declared_review_state FROM revisions "
        "WHERE revision_id=? AND source_id=?",
        (revision.revision_id, source_id),
    ).fetchone()
    if current is None:
        return False
    uses = canonical_bytes(
        sorted(
            set(json.loads(current[1])) & set(json.loads(revision.allowed_uses_json))
        )
    ).decode()
    wanted = (
        max(current[0], revision.access_rank),
        uses,
        revision.declared_review_state,
    )
    if tuple(current) == wanted:
        return False
    db.execute(
        "UPDATE revisions SET access_rank=?,allowed_uses_json=?,declared_review_state=? "
        "WHERE revision_id=? AND source_id=?",
        (*wanted, revision.revision_id, source_id),
    )
    return True


def restore_policy(
    db: sqlite3.Connection,
    workspace_id: str,
    sources: tuple[InventorySource, ...],
) -> int:
    """Carry an older workspace's access and review policy onto rebuilt sources.

    A source access floor and a revision access rank only ever rise, allowed uses
    only ever narrow to what both workspaces permit, and a revision takes the old
    declared review state, because a re-add can only produce the default one and
    the old state is the reviewed one. A rebuild never lowers confidentiality.
    Rows the new workspace does not hold are skipped, so any subset of sources
    may have been re-added. The policy epoch moves once when anything changed.
    Returns the number of rows changed.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        changed = 0
        for source in sources:
            changed += _restore_source(db, source)
            changed += sum(
                _restore_revision(db, source.source_id, revision)
                for revision in source.revisions
            )
        if changed:
            db.execute(
                "UPDATE workspaces SET policy_epoch=policy_epoch+1 "
                "WHERE workspace_id=?",
                (workspace_id,),
            )
        db.commit()
        return changed
    except BaseException:
        db.rollback()
        raise
