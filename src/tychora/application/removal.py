"""Owner-thread source withdrawal and retained-original binding release."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from typing import TYPE_CHECKING, Literal, cast

from tychora.adapters.files.references import (
    MAX_MANIFEST_BYTES,
    TYPED_MANIFEST_ROOTS,
    ClosureWalk,
    workspace_closure_budget,
)
from tychora.adapters.sqlite.bindings import BoundSet, live_by_rows
from tychora.contracts.artifacts import ArtifactRef, ManifestRef, registered_manifest
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    JobHandle,
    MaintenancePlan,
    MaintenanceResult,
    OriginalBindingCut,
    OriginalReferenceCut,
    PurgeOriginalsMaintenance,
    RemoveMaintenance,
)
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.ports.jobs import JobRepository


def _now() -> str:
    return datetime.now(UTC).isoformat()


MAX_REMOVAL_PLAN_ITEMS = 10_000


class RemovalService:
    """Run on the canonical owner thread; filesystem deletion follows tombstone commit."""

    def __init__(self, owner: WorkspaceOwner, jobs: JobRepository) -> None:
        self.owner = owner
        self.jobs = jobs

    def _authorized_source(
        self, source_id: str, scope: RequestScope, *, withdrawn: bool = False
    ) -> str:
        grant = self.owner.scope.narrow(scope)
        row = self.owner.db.execute(
            "SELECT s.collection_id,s.withdrawn_at,c.access_floor,s.access_floor FROM sources s "
            "JOIN collections c USING(collection_id) "
            "WHERE s.source_id=? AND c.workspace_id=?",
            (source_id, self.owner.workspace_id),
        ).fetchone()
        if (
            row is None
            or row[0] not in grant.collection_ids
            or "manage" not in grant.capabilities
            or max(row[2:4]) > grant.access_ceiling
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Source is outside the management grant."
            )
        if withdrawn and row[1] is None:
            raise TychoraError(
                "INVALID_REQUEST", "Withdraw the source before removing originals."
            )
        return row[0]

    def _job(
        self, operation: str, scope: RequestScope, collection_id: str, intent: dict
    ) -> JobHandle:
        effective = self.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "registry",
                        "schema_version": "1",
                        "data": self.owner.config.model_dump(mode="json"),
                    }
                )
            ),
            "application/json",
        )
        grant = self.owner.scope.narrow(scope)
        scoped = grant.model_copy(update={"collection_ids": frozenset({collection_id})})
        handle = self.jobs.accept(operation, scoped, None, intent, effective)
        self.jobs.start(handle.id)
        return handle

    def remove_source(self, source_id: str, scope: RequestScope) -> JobHandle:
        collection_id = self._authorized_source(source_id, scope)
        handle = self._job("remove", scope, collection_id, {"source_id": source_id})
        try:
            self.owner.store.withdraw_source(source_id)
            self.jobs.complete(handle.id)
        except BaseException:
            self.jobs.fail(
                handle.id,
                TychoraError("JOB_INTERRUPTED", "Source withdrawal did not complete."),
            )
            raise
        return JobHandle(id=handle.id, operation="remove", state="succeeded")

    def _bindings(
        self, source_id: str, revision_ids: tuple[str, ...], scope: RequestScope
    ) -> dict[str, set[str]]:
        if not revision_ids or len(set(revision_ids)) != len(revision_ids):
            raise TychoraError("INVALID_REQUEST", "Select distinct revisions.")
        found: dict[str, set[str]] = {}
        for revision_id in revision_ids:
            row = self.owner.db.execute(
                "SELECT original_sha,access_rank FROM revisions WHERE revision_id=? AND source_id=?",
                (revision_id, source_id),
            ).fetchone()
            if row is None or row[1] > self.owner.scope.narrow(scope).access_ceiling:
                raise TychoraError(
                    "FORBIDDEN_SCOPE", "Revision is outside the selected source."
                )
            shas = {row[0]}
            shas.update(
                sha
                for (sha,) in self.owner.db.execute(
                    "SELECT artifact_sha FROM revision_artifacts WHERE revision_id=? "
                    "AND role IN ('original','source_file')",
                    (revision_id,),
                )
            )
            if not shas:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Revision has no original binding."
                )
            found[revision_id] = shas
        return found

    def _plan(
        self, revision_ids: tuple[str, ...], bindings: dict[str, set[str]]
    ) -> MaintenancePlan:
        db = self.owner.db
        binding_rows: list[OriginalBindingCut] = []
        proposed = frozenset(
            (revision_id, sha) for revision_id, shas in bindings.items() for sha in shas
        )
        if len(proposed) > MAX_REMOVAL_PLAN_ITEMS:
            raise TychoraError("INVALID_REQUEST", "Original binding plan is too large.")
        derived: set[str] = set()
        for revision_id in revision_ids:
            original = db.execute(
                "SELECT original_sha FROM revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()[0]
            roles: dict[str, set[str]] = {original: {"revision_original"}}
            for role, sha in db.execute(
                "SELECT role,artifact_sha FROM revision_artifacts WHERE revision_id=?",
                (revision_id,),
            ):
                if role in {"original", "source_file"}:
                    roles.setdefault(sha, set()).add(role)
                else:
                    derived.add(sha)
            for row in db.execute(
                "SELECT document_artifact_sha,assertions_artifact_sha,diagnostics_artifact_sha "
                "FROM extractions WHERE revision_id=?",
                (revision_id,),
            ):
                derived.update(sha for sha in row if sha is not None)
            for (sha,) in db.execute(
                "SELECT a.artifact_sha FROM assets a JOIN extractions e USING(extraction_id) "
                "WHERE e.revision_id=? UNION SELECT p.derivation_sha FROM asset_proxies p "
                "JOIN assets a USING(asset_id) JOIN extractions e USING(extraction_id) "
                "WHERE e.revision_id=?",
                (revision_id, revision_id),
            ):
                derived.add(sha)
            for (sha,) in db.execute(
                "SELECT DISTINCT b.vectors_sha FROM index_batches b "
                "JOIN extractions e USING(extraction_id) "
                "WHERE b.vectors_sha IS NOT NULL AND e.revision_id=?",
                (revision_id,),
            ):
                derived.add(sha)
            for sha, role_set in sorted(roles.items()):
                binding_rows.append(
                    OriginalBindingCut(
                        revision_id=revision_id,
                        artifact_sha=sha,
                        roles=cast(
                            tuple[
                                Literal["revision_original", "original", "source_file"],
                                ...,
                            ],
                            tuple(sorted(role_set)),
                        ),
                        already_removed=db.execute(
                            "SELECT 1 FROM original_payload_tombstones "
                            "WHERE revision_id=? AND artifact_sha=?",
                            (revision_id, sha),
                        ).fetchone()
                        is not None,
                    )
                )
        selected = sorted({sha for _, sha in proposed})
        if len(derived) + len(binding_rows) + len(selected) > MAX_REMOVAL_PLAN_ITEMS:
            raise TychoraError("INVALID_REQUEST", "Removal plan is too large.")
        cut: list[OriginalReferenceCut] = []
        for sha in selected:
            row = db.execute(
                "SELECT size_bytes,payload_state FROM artifacts WHERE artifact_sha=?",
                (sha,),
            ).fetchone()
            if row is None:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Original registration is missing."
                )
            disposition = (
                "shared"
                if self._live_root(sha, proposed)
                else "already_deleted"
                if row[1] == "deleted"
                else "pending_lease"
                if self.owner.artifacts.payload_is_pinned(sha)
                or self.owner.leases.any_pinned()
                else "delete"
            )
            cut.append(
                OriginalReferenceCut(
                    artifact_sha=sha,
                    size_bytes=row[0],
                    disposition=disposition,
                )
            )
        return MaintenancePlan(
            operation="purge-originals",
            target_ids=revision_ids,
            actions=(
                "remove selected revision original bindings",
                "release unreferenced payloads after lease checks",
            ),
            estimated_bytes=sum(item.size_bytes for item in cut),
            original_bindings=tuple(binding_rows),
            reference_cut=tuple(cut),
            retained_derived_artifact_shas=tuple(sorted(derived)),
        )

    def purge_originals(
        self,
        source_id: str,
        revision_ids: tuple[str, ...],
        *,
        confirm_original_removal: bool = False,
        scope: RequestScope,
        admitted: bool = False,
    ) -> MaintenanceResult:
        if confirm_original_removal and not admitted:
            with self.owner.comparison_admission.mutation_nowait():
                return self.purge_originals(
                    source_id,
                    revision_ids,
                    confirm_original_removal=True,
                    scope=scope,
                    admitted=True,
                )
        collection_id = self._authorized_source(source_id, scope, withdrawn=True)
        bindings = self._bindings(source_id, revision_ids, scope)
        selected = frozenset(sha for shas in bindings.values() for sha in shas)
        plan = self._plan(revision_ids, bindings)
        if not confirm_original_removal:
            return MaintenanceResult(plan=plan)
        for sha in selected:
            row = self.owner.db.execute(
                "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                (sha,),
            ).fetchone()
            if row is None:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Original artifact registration is missing."
                )
            if (
                row[3] == "present"
                and not self.owner.artifacts.stat(
                    ArtifactRef.from_row((sha, *row[:3]))
                ).available
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Present original payload is missing."
                )
        handle = self._job(
            "purge-originals",
            scope,
            collection_id,
            {
                "source_id": source_id,
                "revision_ids": list(revision_ids),
                "confirm_original_removal": True,
            },
        )
        try:
            with self.owner.store.immediate_transaction():
                self._authorized_source(source_id, scope, withdrawn=True)
                if self._bindings(source_id, revision_ids, scope) != bindings:
                    raise TychoraError(
                        "WORKSPACE_BUSY",
                        "Original bindings changed; preview again.",
                        retryable=True,
                    )
                plan = self._plan(revision_ids, bindings)
                for revision_id, shas in bindings.items():
                    for sha in shas:
                        if not self.owner.db.execute(
                            "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
                            (revision_id, sha),
                        ).fetchone():
                            self.owner.db.execute(
                                "INSERT INTO original_payload_tombstones(revision_id,artifact_sha,removal_job_id,removed_at) VALUES (?,?,?,?)",
                                (revision_id, sha, handle.id, _now()),
                            )
                self.owner.db.execute(
                    "UPDATE workspaces SET policy_epoch=policy_epoch+1 WHERE workspace_id=?",
                    (self.owner.workspace_id,),
                )
                for sha in selected:
                    if not self._live_root(sha):
                        self.owner.db.execute(
                            "UPDATE artifacts SET payload_state='delete_pending',payload_deleted_at=NULL "
                            "WHERE artifact_sha=? AND payload_state='present'",
                            (sha,),
                        )
            counts = self.reconcile_pending_deletions(selected)
            self.jobs.complete(handle.id)
        except BaseException:
            self.jobs.fail(
                handle.id,
                TychoraError("JOB_INTERRUPTED", "Original removal did not complete."),
            )
            raise
        return MaintenanceResult(
            plan=plan,
            job=JobHandle(id=handle.id, operation="purge-originals", state="succeeded"),
            deleted_payloads=counts["deleted_payloads"],
            shared_payloads=counts["shared_payloads"],
            pending_payloads=counts["pending_payloads"],
        )

    def _manifest_ref(self, sha: str, kind: str) -> ManifestRef:
        row = self.owner.db.execute(
            "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
            (sha,),
        ).fetchone()
        if row is None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Required manifest descriptor is missing."
            )
        return registered_manifest(ArtifactRef.from_row((sha, *row[:3])), kind)

    def _load_manifest(self, link: ManifestRef):
        """Read and decode one verified manifest for the closure walk."""

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise TychoraError("INTEGRITY_ERROR", "Duplicate manifest field.")
                value[key] = item
            return value

        with self.owner.artifacts.open_verified(link.artifact) as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise TychoraError(
                "INTEGRITY_ERROR", "Required manifest exceeds byte limit."
            )
        try:
            if link.type == "assertion":
                return [
                    json.loads(line, object_pairs_hook=unique)
                    for line in raw.splitlines()
                ]
            return json.loads(raw, object_pairs_hook=unique)
        except (UnicodeError, ValueError):
            raise TychoraError(
                "INTEGRITY_ERROR", "Required manifest is malformed."
            ) from None

    def _rooted(self, *, walk: bool = True) -> frozenset[str]:
        """Return every artifact a typed root manifest or a job capture reaches.

        One walk with one visited set covers every root, so each manifest is
        read at most once per call however many roots share it (every retained
        snapshot reaches the same pages and index inputs). The workspace's own
        artifact rows bound the walk. Published captures that no unpublished
        root reached are walked last, with every capture they reach treated as
        published. Without ``walk`` nothing is read and the set is empty.
        """
        if not walk:
            return frozenset()
        db = self.owner.db
        roots = {
            (sha, kind)
            for query, kind in TYPED_MANIFEST_ROOTS
            for (sha,) in db.execute(query).fetchall()
        }
        captures = db.execute(
            "SELECT captured_inputs_sha,state='complete' AND result_snapshot_id IS NOT NULL "
            "FROM jobs WHERE captured_inputs_sha IS NOT NULL"
        ).fetchall()
        roots |= {(sha, "job_capture") for sha, published in captures if not published}
        closure = ClosureWalk(
            load=self._load_manifest, budget=workspace_closure_budget(db)
        )
        closure.walk(
            tuple(self._manifest_ref(sha, kind) for sha, kind in sorted(roots))
        )
        published = sorted(
            {sha for sha, published in captures if published} - closure.loaded
        )
        if published:
            closure.walk(
                tuple(self._manifest_ref(sha, "job_capture") for sha in published),
                all_captures_published=True,
            )
        return closure.artifacts

    def _live_root(
        self,
        sha: str,
        proposed: frozenset[tuple[str, str]] = frozenset(),
        rooted: frozenset[str] | None = None,
    ) -> bool:
        """Return whether a row or a root manifest still binds ``sha``.

        ``rooted`` is a result of ``_rooted`` taken since the last change to the
        root rows; without it the roots are walked now.
        """
        if live_by_rows(self.owner.db, sha, proposed):
            return True
        return sha in (self._rooted() if rooted is None else rooted)

    def reconcile_pending_deletions(
        self, candidates: frozenset[str] | None = None
    ) -> dict[str, int]:
        """Resume pending payload deletions on the owner before any copy of the database is taken."""
        if self.owner.restore_deletion_suspended:
            return {"deleted_payloads": 0, "shared_payloads": 0, "pending_payloads": 0}
        selected = candidates or frozenset(
            sha
            for (sha,) in self.owner.db.execute(
                "SELECT artifact_sha FROM artifacts WHERE payload_state='delete_pending'"
            )
        )
        counts = {"deleted_payloads": 0, "shared_payloads": 0, "pending_payloads": 0}
        # Invariant: the whole pass is one owner-thread call on the owner's only
        # connection, and between reading the bindings and the last unlink it
        # writes nothing but artifacts.payload_state. No binding or root row can
        # change during the pass, so one read of the bindings and one walk of
        # the roots decide every candidate, and no recheck after an unlink is
        # needed (test_a_reconcile_pass_writes_only_payload_states pins this).
        rooted = self._rooted(walk=bool(selected))
        bound = BoundSet.load(self.owner.db, selected, rooted=rooted)
        for sha in sorted(selected):
            with self.owner.artifacts.payload_guard(sha):
                row = self.owner.db.execute(
                    "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                    (sha,),
                ).fetchone()
                if row is None:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Original artifact registration is missing."
                    )
                if bound.binds(sha):
                    if row[3] == "delete_pending":
                        ref = ArtifactRef.from_row((sha, *row[:3]))
                        if not self.owner.artifacts.stat(ref).available:
                            raise TychoraError(
                                "INTEGRITY_ERROR", "Live original payload is missing."
                            )
                        self.owner.db.execute(
                            "UPDATE artifacts SET payload_state='present' WHERE artifact_sha=?",
                            (sha,),
                        )
                    counts["shared_payloads"] += 1
                    continue
                if row[3] == "deleted":
                    counts["deleted_payloads"] += 1
                    continue
                if (
                    row[3] != "delete_pending"
                    or self.owner.artifacts.payload_is_pinned(sha)
                    or self.owner.leases.any_pinned()
                ):
                    counts["pending_payloads"] += 1
                    continue
                ref = ArtifactRef.from_row((sha, *row[:3]))
                self.owner.artifacts.unlink_registered(ref)
                with self.owner.store.immediate_transaction():
                    self.owner.db.execute(
                        "UPDATE artifacts SET payload_state='deleted',payload_deleted_at=? "
                        "WHERE artifact_sha=? AND payload_state='delete_pending'",
                        (_now(), sha),
                    )
                counts["deleted_payloads"] += 1
        return counts


async def run_public_removal(
    owner: WorkspaceOwner,
    repository: JobRepository,
    scope: RequestScope,
    request: RemoveMaintenance | PurgeOriginalsMaintenance,
) -> MaintenanceResult:
    def execute() -> MaintenanceResult:
        service = RemovalService(owner, repository)
        if isinstance(request, RemoveMaintenance):
            if len(request.source_ids) != 1:
                raise TychoraError("INVALID_REQUEST", "Remove one source per job.")
            handle = service.remove_source(request.source_ids[0], scope)
            return MaintenanceResult(
                plan=MaintenancePlan(
                    operation="remove",
                    target_ids=request.source_ids,
                    actions=("withdraw sources and advance policy epoch",),
                ),
                job=handle,
            )
        return service.purge_originals(
            request.source_id,
            request.revision_ids,
            confirm_original_removal=request.confirm_original_removal,
            scope=scope,
            admitted=True,
        )

    ctx = ExecutionContext()
    if isinstance(request, PurgeOriginalsMaintenance):
        async with owner.comparison_admission.mutation(ctx):
            return await owner.run_blocking(execute, ctx)
    return await owner.run_blocking(execute, ctx)
