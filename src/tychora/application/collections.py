"""Owner-lane collection mutations and bounded, conservative source inventory."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from tychora.contracts.collections import (
    CollectionConsentView,
    CollectionCreate,
    CollectionEdit,
    CollectionPage,
    CollectionPolicyV2,
    CollectionSummary,
    DocumentPage,
    DocumentSummary,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.operations import SourceChannelCoverage
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

from .collection_policy import parse_collection_policy
from .consent import enabled_upload_providers
from .discovery import _scoped_after, _scoped_cursor
from .workspace_authority import refresh_managed_owner


class CollectionService:
    def __init__(self, owner):
        self.owner = owner

    def _epoch(self):
        return self.owner.db.execute(
            "SELECT policy_epoch FROM workspaces WHERE workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchone()[0]

    def _grant(self, scope, capability="read"):
        grant = self.owner.scope.narrow(scope)
        if capability not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Collection capability is unavailable."
            )
        return grant

    def _row(self, cid, grant):
        row = self.owner.db.execute(
            "SELECT collection_id,workspace_id,slug,title,access_floor,policy_json FROM collections WHERE collection_id=? AND workspace_id=?",
            (cid, self.owner.workspace_id),
        ).fetchone()
        if (
            row is None
            or cid not in grant.collection_ids
            or row[4] > grant.access_ceiling
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Collection is outside the grant.")
        return row

    def _kind(self, kind_id):
        if kind_id is None:
            return
        registry = self.owner.store.registry
        kind = registry.kinds.get(kind_id)
        if kind is None or kind["normalizer"] not in registry.normalizers:
            raise TychoraError(
                "INVALID_REQUEST", "Collection default kind is unavailable."
            )

    def _summary(self, row, epoch=None):
        policy = parse_collection_policy(
            row[5], allow_injected_provider_ids=not self.owner.managed_default_owner
        )
        inherited = (
            "document@1" if "document@1" in self.owner.store.registry.kinds else None
        )
        return CollectionSummary(
            collection_id=row[0],
            workspace_id=row[1],
            slug=row[2],
            title=row[3],
            access_floor=row[4],
            archived_at=policy.archived_at,
            default_kind_id=policy.default_kind_id,
            effective_default_kind_id=policy.default_kind_id or inherited,
            policy_epoch=self._epoch() if epoch is None else epoch,
        )

    async def list(
        self,
        scope: RequestScope,
        ctx: ExecutionContext,
        *,
        cursor=None,
        limit=50,
        include_archived=False,
    ) -> CollectionPage:
        def read():
            grant = self._grant(scope)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise TychoraError(
                    "INVALID_REQUEST", "Collection page limit is invalid."
                )
            epoch = self._epoch()
            subject = f"collections:{epoch}:{include_archived}"
            after = _scoped_after(self.owner, cursor, subject, grant, limit) or ""
            rows = self.owner.db.execute(
                "SELECT collection_id,workspace_id,slug,title,access_floor,policy_json FROM collections "
                "WHERE workspace_id=? AND collection_id>? AND access_floor<=? "
                "AND collection_id IN (SELECT value FROM json_each(?)) "
                "AND (? OR json_extract(policy_json,'$.archived_at') IS NULL) ORDER BY collection_id LIMIT ?",
                (
                    self.owner.workspace_id,
                    after,
                    grant.access_ceiling,
                    json.dumps(sorted(grant.collection_ids)),
                    include_archived,
                    limit + 1,
                ),
            ).fetchall()
            return CollectionPage(
                collections=tuple(self._summary(row, epoch) for row in rows[:limit]),
                next_cursor=_scoped_cursor(
                    self.owner, subject, rows[limit - 1][0], grant, limit
                )
                if len(rows) > limit
                else None,
                policy_epoch=epoch,
            )

        return await self.owner.run_blocking(read, ctx)

    async def get(self, collection_id, scope, ctx):
        return await self.owner.run_blocking(
            lambda: self._summary(self._row(collection_id, self._grant(scope))), ctx
        )

    def _mutate(self, scope, operation):
        grant = self._grant(scope, "manage")
        self.owner.db.execute("BEGIN IMMEDIATE")
        try:
            cid, changed = operation(grant)
            if changed:
                self.owner.db.execute(
                    "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id=?",
                    (self.owner.workspace_id,),
                )
            self.owner.db.commit()
        except sqlite3.IntegrityError:
            self.owner.db.rollback()
            raise TychoraError(
                "IDEMPOTENCY_CONFLICT", "Collection slug is already in use."
            ) from None
        except BaseException:
            self.owner.db.rollback()
            raise
        refresh_managed_owner(self.owner)
        # Creation widens only the managed future-call grant, never this request object.
        return self._summary(self._row(cid, self.owner.scope))

    async def create(
        self,
        request: CollectionCreate,
        scope: RequestScope,
        ctx: ExecutionContext,
        *,
        managed_owner_authorized: bool,
    ):
        def create(grant):
            if (
                not managed_owner_authorized
                or not self.owner.managed_default_owner
                or grant != self.owner.scope
            ):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Creation requires the full managed workspace owner.",
                )
            if request.slug == "default":
                raise TychoraError(
                    "INVALID_REQUEST", "The default collection slug is reserved."
                )
            self._kind(request.default_kind_id)
            cid = str(uuid4())
            policy = CollectionPolicyV2(default_kind_id=request.default_kind_id)
            self.owner.db.execute(
                "INSERT INTO collections(collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,?)",
                (
                    cid,
                    self.owner.workspace_id,
                    request.slug,
                    request.title,
                    policy.model_dump_json(),
                ),
            )
            return cid, True

        return await self.owner.run_blocking(lambda: self._mutate(scope, create), ctx)

    async def edit(self, collection_id: str, request: CollectionEdit, scope, ctx):
        def edit(grant):
            row = self._row(collection_id, grant)
            if (row[2] == "default" or request.slug == "default") and row[
                2
            ] != request.slug:
                raise TychoraError(
                    "INVALID_REQUEST", "The default collection slug is reserved."
                )
            policy = parse_collection_policy(
                row[5],
                allow_injected_provider_ids=not self.owner.managed_default_owner,
            )
            original_policy = policy
            if "default_kind_id" in request.model_fields_set:
                self._kind(request.default_kind_id)
                policy = policy.model_copy(
                    update={"default_kind_id": request.default_kind_id}
                )
            changed = (
                row[2:4]
                != (
                    request.slug,
                    request.title,
                )
                or policy != original_policy
            )
            if changed:
                self.owner.db.execute(
                    "UPDATE collections SET slug=?,title=?,policy_json=? WHERE collection_id=?",
                    (
                        request.slug,
                        request.title,
                        policy.model_dump_json(),
                        collection_id,
                    ),
                )
            return collection_id, changed

        return await self.owner.run_blocking(lambda: self._mutate(scope, edit), ctx)

    async def set_archived(self, collection_id, archived, scope, ctx):
        def archive(grant):
            row = self._row(collection_id, grant)
            policy = parse_collection_policy(
                row[5],
                allow_injected_provider_ids=not self.owner.managed_default_owner,
            )
            changed = (policy.archived_at is not None) != archived
            if changed:
                policy = policy.model_copy(
                    update={"archived_at": datetime.now(UTC) if archived else None}
                )
                self.owner.db.execute(
                    "UPDATE collections SET policy_json=? WHERE collection_id=?",
                    (policy.model_dump_json(), collection_id),
                )
            return collection_id, changed

        return await self.owner.run_blocking(lambda: self._mutate(scope, archive), ctx)

    async def consent(self, collection_id, scope, ctx):
        def read():
            grant = self._grant(scope)
            policy = parse_collection_policy(
                self._row(collection_id, grant)[5],
                allow_injected_provider_ids=not self.owner.managed_default_owner,
            )
            return CollectionConsentView(
                collection_id=collection_id,
                collection_upload_providers=policy.upload_providers,
                owner_upload_providers=policy.owner_upload_providers,
                caller_upload_providers=tuple(sorted(grant.upload_providers)),
                enabled_upload_providers=tuple(
                    sorted(enabled_upload_providers(self.owner.config))
                ),
                policy_epoch=self._epoch(),
                managed_grant_refresh_available=bool(
                    self.owner.managed_default_owner
                    and self.owner._managed_scope_listeners
                ),
            )

        return await self.owner.run_blocking(read, ctx)

    async def documents(self, scope, ctx, *, cursor=None, limit=50):
        def read():
            grant = self._grant(scope)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise TychoraError("INVALID_REQUEST", "Document page limit is invalid.")
            epoch = self._epoch()
            subject = f"documents:{epoch}"
            after = _scoped_after(self.owner, cursor, subject, grant, limit) or ""
            # Filter source authorization in SQL before taking one page. Never load
            # private revision metadata merely to discard it in Python later.
            rows = self.owner.db.execute(
                "SELECT s.source_id,s.source_key,s.source_type,s.collection_id,c.title,s.created_at,c.policy_json "
                "FROM sources s JOIN collections c USING(collection_id) WHERE c.workspace_id=? "
                "AND s.source_id>? AND s.withdrawn_at IS NULL AND s.access_floor<=? AND c.access_floor<=? "
                "AND s.collection_id IN (SELECT value FROM json_each(?)) "
                "AND json_extract(c.policy_json,'$.archived_at') IS NULL ORDER BY s.source_id LIMIT ?",
                (
                    self.owner.workspace_id,
                    after,
                    grant.access_ceiling,
                    grant.access_ceiling,
                    json.dumps(sorted(grant.collection_ids)),
                    limit + 1,
                ),
            ).fetchall()
            page = rows[:limit]
            for raw in {row[6] for row in page}:
                parse_collection_policy(
                    raw,
                    allow_injected_provider_ids=not self.owner.managed_default_owner,
                )
            # Only the selected published revision can describe published content.
            # At most one revision per source and no decoded corpus artifacts.
            selected = self.owner.db.execute(
                "SELECT s.source_id,r.revision_id,m.extraction_id,r.title,r.captured_at,a.media_type,a.size_bytes,k.definition_json,k.definition_hash "
                "FROM snapshot_members m JOIN workspaces w ON w.active_snapshot_id=m.snapshot_id "
                "JOIN sources s USING(source_id) JOIN revisions r ON r.revision_id=m.revision_id "
                "JOIN kinds k USING(kind_id) LEFT JOIN artifacts a ON a.artifact_sha=r.original_sha "
                "WHERE w.workspace_id=? AND m.is_default=1 AND r.access_rank<=? "
                "AND s.source_id IN (SELECT value FROM json_each(?)) "
                "AND EXISTS(SELECT 1 FROM json_each(r.allowed_uses_json) ru "
                "JOIN json_each(k.definition_json,'$.kind.allowed_uses') ku ON ku.value=ru.value "
                "JOIN json_each(?) su ON su.value=ru.value) ORDER BY s.source_id LIMIT 100",
                (
                    self.owner.workspace_id,
                    grant.access_ceiling,
                    json.dumps([row[0] for row in page]),
                    json.dumps(sorted(grant.uses)),
                ),
            ).fetchall()
            visible = {}
            for row in selected:
                envelope = json.loads(row[7])
                recorded = envelope.pop("envelope_digest", None)
                if (
                    recorded != row[8]
                    or digest("historical-kind-v1", envelope) != recorded
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained kind definition is invalid."
                    )
                visible[row[0]] = row
            channel_rows = self.owner.db.execute(
                "SELECT b.extraction_id,sb.slot,b.state,b.family_id FROM index_batches b "
                "JOIN snapshot_batches sb USING(batch_id) JOIN workspaces w ON w.active_snapshot_id=sb.snapshot_id "
                "WHERE w.workspace_id=? AND b.extraction_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY b.extraction_id,sb.slot LIMIT 401",
                (self.owner.workspace_id, json.dumps([row[2] for row in selected])),
            ).fetchall()
            channels = {}
            if len(channel_rows) <= 400:
                for extraction, channel, state, family in channel_rows:
                    channels.setdefault(extraction, []).append(
                        SourceChannelCoverage(
                            family_id=family, channel=channel, state=state
                        )
                    )
            documents = []
            for row in page:
                revision = visible.get(row[0])
                documents.append(
                    DocumentSummary(
                        source_id=row[0],
                        source_key=row[1],
                        source_type=row[2],
                        collection_id=row[3],
                        collection_title=row[4],
                        created_at=datetime.fromisoformat(row[5]),
                        title=revision[3] if revision and revision[3] else row[1],
                        revision_id=revision[1] if revision else None,
                        extraction_id=revision[2] if revision else None,
                        captured_at=datetime.fromisoformat(revision[4])
                        if revision
                        else None,
                        original_media_type=revision[5] if revision else None,
                        original_size_bytes=revision[6] if revision else None,
                        published_content_state="published"
                        if revision
                        else "not_observed",
                        channel_observations=tuple(channels.get(revision[2], ()))
                        if revision
                        else (),
                        channel_readiness=tuple(
                            item.channel
                            for item in channels.get(revision[2], ())
                            if item.state == "ready"
                        )
                        if revision
                        else (),
                        channel_observations_complete=bool(
                            revision and len(channel_rows) <= 400
                        ),
                    )
                )
            return DocumentPage(
                documents=tuple(documents),
                policy_epoch=epoch,
                next_cursor=_scoped_cursor(
                    self.owner, subject, page[-1][0], grant, limit
                )
                if len(rows) > limit
                else None,
            )

        return await self.owner.run_blocking(read, ctx)
