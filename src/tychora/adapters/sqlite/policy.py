"""Append-only policy observations with transactional workspace epochs."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from tychora.adapters.files.assertions import iter_verified_assertions
from tychora.adapters.sqlite.repositories import (
    SqliteWorkspaceRepository,
    artifact_ref,
)
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import AllowedUsesOverride, ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.artifacts import ArtifactReader

from .validation import fail

if TYPE_CHECKING:
    from tychora.configuration.registry import Registry

_TARGET_KINDS = {"revision_id": "revision", "unit_id": "unit", "asset_id": "asset"}
_GENERATION_USE = "compose"
_REFERENCE_USE = "lookup"


def now() -> str:
    return datetime.now(UTC).isoformat()


class PolicyRepository:
    db: sqlite3.Connection
    artifacts: ArtifactReader
    registry: "Registry"

    @contextmanager
    def _write(self):
        if self.db.in_transaction:
            fail("Corpus writes require their own transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _subject(
        self, target: ReviewTarget
    ) -> tuple[str, str, tuple[str, str, str, str]]:
        subject = target.model_dump(exclude_none=True)
        if len(subject) != 1:
            fail("Review target must identify exactly one subject")
        column, identifier = next(iter(subject.items()))
        tables = {
            "revision_id": ("revisions t", "t.revision_id"),
            "unit_id": (
                "evidence_units t JOIN extractions e USING(extraction_id)",
                "e.revision_id",
            ),
            "asset_id": (
                "assets t JOIN extractions e USING(extraction_id)",
                "e.revision_id",
            ),
        }
        table, revision = tables[column]
        row = self.db.execute(
            f"SELECT r.revision_id,s.source_id,c.workspace_id,t.declared_review_state "
            f"FROM {table} JOIN revisions r ON r.revision_id={revision} "
            f"JOIN sources s ON s.source_id=r.source_id JOIN collections c USING(collection_id) "
            f"WHERE t.{column}=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Policy subject is unavailable")
        return column, identifier, row

    def _bump(self, workspace_id: str) -> None:
        workspace = SqliteWorkspaceRepository(self.db, writable=True)
        if workspace.workspace_id() != workspace_id:
            fail("Policy subject belongs to another workspace")
        workspace.bump_policy_epoch()

    def review_state(self, target: ReviewTarget) -> str:
        column, identifier, (rid, _, _, declared) = self._subject(target)
        if (
            column != "revision_id"
            and self.review_state(ReviewTarget(revision_id=rid)) == "rejected"
        ):
            return "rejected"
        current = self.db.execute(
            "SELECT state FROM review_current WHERE target_kind=? AND target_id=?",
            (_TARGET_KINDS[column], identifier),
        ).fetchone()
        return current[0] if current else declared

    def record_review(
        self,
        target: ReviewTarget,
        state: str,
        actor: str,
        *,
        decided_at: datetime | None = None,
    ) -> int:
        if decided_at is not None and decided_at.tzinfo is None:
            raise TychoraError(
                "INVALID_REQUEST",
                "A decision time must be timezone-aware",
                reason="review.time_naive",
            )
        if (
            state not in ("unreviewed", "needs_review", "accepted", "rejected")
            or not actor.strip()
        ):
            raise TychoraError("INVALID_REQUEST", "Invalid review state or actor")
        with self._write():
            column, identifier, (_, _, workspace, _) = self._subject(target)
            cursor = self.db.execute(
                f"INSERT INTO review_events ({column},state,actor_id,created_at) VALUES (?,?,?,?)",
                (
                    identifier,
                    state,
                    actor,
                    now()
                    if decided_at is None
                    else decided_at.astimezone(UTC).isoformat(),
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                fail("SQLite did not return the inserted policy event identity")
            self.db.execute(
                "INSERT INTO review_current(target_kind,target_id,state,review_event_id) "
                "VALUES (?,?,?,?) ON CONFLICT(target_kind,target_id) DO UPDATE SET "
                "state=excluded.state,review_event_id=excluded.review_event_id",
                (_TARGET_KINDS[column], identifier, state, event_id),
            )
            self._bump(workspace)
            return event_id

    def raise_access_floor(self, target: ReviewTarget, rank: int) -> None:
        if type(rank) is not int or not 0 <= rank <= 3:
            raise TychoraError("INVALID_REQUEST", "Invalid access rank")
        with self._write():
            _, _, (_, source, workspace, _) = self._subject(target)
            previous = self.db.execute(
                "SELECT access_floor FROM sources WHERE source_id=?", (source,)
            ).fetchone()[0]
            if rank < previous:
                fail("Access floor cannot silently decrease")
            if rank > previous:
                self.db.execute(
                    "UPDATE sources SET access_floor=? WHERE source_id=?",
                    (rank, source),
                )
                self._bump(workspace)

    def withdraw_source(self, source_id: str) -> None:
        with self._write():
            row = self.db.execute(
                "SELECT workspace_id,withdrawn_at FROM sources JOIN collections USING(collection_id) WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise TychoraError("NOT_FOUND", "Source is unavailable")
            if row[1] is None:
                self.db.execute(
                    "UPDATE sources SET withdrawn_at=? WHERE source_id=?",
                    (now(), source_id),
                )
                self._bump(row[0])

    def set_allowed_uses_override(
        self,
        source_id: str,
        override: AllowedUsesOverride,
        *,
        collection_ids: frozenset[str] | None = None,
    ) -> None:
        """Narrow the uses a source's revisions grant; ``None`` removes the override.

        ``never_generate`` drops the generation use, ``reference_only`` keeps
        lookup only, and a tuple keeps those uses. The stored value is the
        narrowed list; eligibility intersects it with each revision's uses. A
        source outside ``collection_ids``, when given, reads as absent.
        """
        with self._write():
            row = self.db.execute(
                "SELECT c.workspace_id,s.collection_id FROM sources s "
                "JOIN collections c USING(collection_id) WHERE s.source_id=?",
                (source_id,),
            ).fetchone()
            if row is None or (
                collection_ids is not None and row[1] not in collection_ids
            ):
                raise TychoraError(
                    "NOT_FOUND", "Source is unavailable", reason="source.absent"
                )
            stored = self._narrowed_uses(override)
            self.db.execute(
                "UPDATE sources SET allowed_uses_override=? WHERE source_id=?",
                (
                    None
                    if stored is None
                    else json.dumps(stored, separators=(",", ":")),
                    source_id,
                ),
            )
            self._bump(row[0])

    def _narrowed_uses(self, override: AllowedUsesOverride) -> list[str] | None:
        """The sorted uses an override keeps, or ``None`` for no override."""
        known = self._known_uses()
        if override is None:
            return None
        if override == "never_generate":
            return sorted(known - {_GENERATION_USE})
        if override == "reference_only":
            return sorted(known & {_REFERENCE_USE})
        if isinstance(override, tuple) and set(override) <= known:
            return sorted(set(override))
        raise TychoraError(
            "INVALID_REQUEST",
            "Allowed-use override names an unknown use",
            reason="policy.use_unknown",
        )

    def _known_uses(self) -> frozenset[str]:
        """Every use a mode of the active registry defines."""
        return frozenset(
            use
            for mode in cast(dict[str, FrozenJsonMap], self.registry.modes).values()
            for use in cast(tuple[str, ...], mode["allowed_uses"])
        )

    def _assertion_owners(self, artifact_sha: str) -> frozenset[str]:
        rows = self.db.execute(
            "SELECT DISTINCT c.workspace_id FROM extractions e "
            "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) JOIN artifacts a ON a.artifact_sha=e.assertions_artifact_sha "
            "WHERE e.assertions_artifact_sha=? AND a.payload_state='present'",
            (artifact_sha,),
        ).fetchall()
        if not rows:
            fail("Assertion artifact is not registered to an extraction")
        return frozenset(row[0] for row in rows)

    def _verify_assertion(self, artifact_sha: str, record_id: str, assertion_id: str):
        self._assertion_owners(artifact_sha)
        row = self.db.execute(
            "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
            (artifact_sha,),
        ).fetchone()
        ref = artifact_ref((artifact_sha, *row[:3]))
        matched = sum(
            item.assertion_id == assertion_id and item.record_id == record_id
            for item in iter_verified_assertions(ref, self.artifacts)
        )
        if matched != 1:
            fail("Assertion record binding or artifact digest differs")

    def record_assertion_decision(
        self,
        assertion_id: str,
        artifact_sha: str,
        record_id: str,
        state: str,
        actor: str,
    ) -> int:
        if (
            state not in ("recorded", "proposed", "confirmed", "rejected")
            or not actor.strip()
        ):
            raise TychoraError("INVALID_REQUEST", "Invalid assertion state or actor")
        if self.db.in_transaction:
            fail("Corpus writes require their own transaction")
        self._verify_assertion(artifact_sha, record_id, assertion_id)
        with self._write():
            owners = self._assertion_owners(artifact_sha)
            bindings = self.db.execute(
                "SELECT DISTINCT provenance_artifact_sha,provenance_record_id FROM assertion_decisions WHERE assertion_id=?",
                (assertion_id,),
            ).fetchall()
            if bindings and bindings != [(artifact_sha, record_id)]:
                fail("Assertion identity already has another immutable record binding")
            cursor = self.db.execute(
                "INSERT INTO assertion_decisions (assertion_id,provenance_artifact_sha,provenance_record_id,state,actor_id,created_at) VALUES (?,?,?,?,?,?)",
                (assertion_id, artifact_sha, record_id, state, actor, now()),
            )
            for workspace in owners:
                self._bump(workspace)
            event_id = cursor.lastrowid
            if event_id is None:
                fail("SQLite did not return the inserted policy event identity")
            return event_id
