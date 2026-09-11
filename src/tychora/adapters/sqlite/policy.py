"""Append-only policy observations with transactional workspace epochs."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.artifacts import ArtifactReader

from .validation import fail


def now() -> str:
    return datetime.now(UTC).isoformat()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("Duplicate assertion JSON field")
        result[key] = value
    return result


class PolicyRepository:
    db: sqlite3.Connection
    artifacts: ArtifactReader

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
        self.db.execute(
            "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id=?",
            (workspace_id,),
        )

    def review_state(self, target: ReviewTarget) -> str:
        column, identifier, (rid, _, _, declared) = self._subject(target)
        if (
            column != "revision_id"
            and self.review_state(ReviewTarget(revision_id=rid)) == "rejected"
        ):
            return "rejected"
        event = self.db.execute(
            f"SELECT state FROM review_events WHERE {column}=? ORDER BY review_event_id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        return event[0] if event else declared

    def record_review(self, target: ReviewTarget, state: str, actor: str) -> int:
        if (
            state not in ("unreviewed", "needs_review", "accepted", "rejected")
            or not actor.strip()
        ):
            raise TychoraError("INVALID_REQUEST", "Invalid review state or actor")
        with self._write():
            column, identifier, (_, _, workspace, _) = self._subject(target)
            cursor = self.db.execute(
                f"INSERT INTO review_events ({column},state,actor_id,created_at) VALUES (?,?,?,?)",
                (identifier, state, actor, now()),
            )
            self._bump(workspace)
            return cursor.lastrowid

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
        ref = ArtifactRef(
            sha256=artifact_sha,
            size_bytes=row[0],
            media_type=row[1],
            storage_key=row[2],
        )
        if ref.size_bytes > 16 * 1024 * 1024:
            fail("Assertion manifest exceeds verification bound")
        hasher, size, record_ids, assertion_ids = sha256(), 0, set(), set()
        matched = False
        try:
            with self.artifacts.open_verified(ref) as stream:
                while line := stream.readline(1024 * 1024 + 1):
                    if len(line) > 1024 * 1024:
                        fail("Assertion JSONL record exceeds verification bound")
                    hasher.update(line)
                    size += len(line)
                    if size > 16 * 1024 * 1024:
                        fail("Assertion manifest exceeds verification bound")
                    data = json.loads(line.decode("utf-8"), object_pairs_hook=_object)
                    if (
                        not isinstance(data, dict)
                        or data.get("type") != "assertion"
                        or data.get("schema_version") != "1"
                    ):
                        fail("Unsupported assertion record framing")
                    aid, rid = data.get("assertion_id"), data.get("record_id")
                    if (
                        not isinstance(aid, str)
                        or not aid
                        or not isinstance(rid, str)
                        or not rid
                        or aid in assertion_ids
                        or rid in record_ids
                    ):
                        fail("Assertion record identities are missing or ambiguous")
                    assertion_ids.add(aid)
                    record_ids.add(rid)
                    matched |= aid == assertion_id and rid == record_id
        except (OSError, UnicodeDecodeError, ValueError):
            fail("Assertion artifact cannot be verified")
        if not matched or size != ref.size_bytes or hasher.hexdigest() != ref.sha256:
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
            return cursor.lastrowid
