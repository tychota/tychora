"""Pinned exact evidence, registered asset and bounded relation inspection."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from tychora.adapters.ladybug.traversal import resolve_graph_traversal_policy
from tychora.adapters.sqlite.eligibility import retained_rows
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application.discovery import (
    _permitted_resolutions,
    _scoped_resolution_rates,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import CodepointRange, FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Excerpt, RelationPath
from tychora.contracts.graph import GraphBudget
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.operations import (
    ExtractionContentRequest,
    ExtractionUnitsPage,
    ExtractionWindowsPage,
    InspectionItem,
    InspectionResult,
    InspectRequest,
    PartialEvidenceMetadata,
    RelationRequest,
    RelationResult,
)
from tychora.contracts.publication import SnapshotMember
from tychora.contracts.requests import (
    QueryRequest,
    RequestScope,
    query_run_scope_matches,
)
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.eligibility import EligibilityCompiler

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.ports.graph import GraphStore

MAX_INSPECTION_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 1024 * 1024


def _deny() -> None:
    raise TychoraError("FORBIDDEN_SCOPE", "Item is outside the readable scope.")


def _ranges(request: InspectRequest, length: int) -> tuple[CodepointRange, ...]:
    if request.range is not None and request.ranges:
        raise TychoraError("INVALID_REQUEST", "Supply range or ranges, not both.")
    selected = request.ranges or ((request.range,) if request.range is not None else ())
    previous = -1
    for interval in selected:
        if not (0 <= interval.char_start < interval.char_end <= length):
            raise TychoraError("INVALID_REQUEST", "Range lies outside the source unit.")
        if interval.char_start < previous:
            raise TychoraError(
                "INVALID_REQUEST", "Ranges must be ordered and disjoint."
            )
        previous = interval.char_end
    return selected


class InspectionService:
    def __init__(self, owner: WorkspaceOwner, graph: GraphStore | None = None) -> None:
        self.owner = owner
        self.graph = graph
        self.compiler = EligibilityCompiler(owner.store)
        # Opaque continuations never disclose excluded row IDs or positions.
        self._content_cursors: dict[str, tuple[str, int]] = {}

    def _attempt(self, snapshot, scope: RequestScope):
        preferred = (
            self.owner.config.values.mode or self.owner.config.values.default_mode
        )
        readable_modes = [
            name
            for name, definition in self.owner.store.registry.modes.items()
            if set(
                cast(tuple[str, ...], cast(FrozenJsonMap, definition)["allowed_uses"])
            )
            & scope.uses
        ]
        mode = (
            preferred
            if preferred in readable_modes
            else next(iter(sorted(readable_modes)), None)
        )
        if mode is None:
            _deny()
        limits = self.owner.config.values.limits
        query = QueryRequest.from_mapping(
            {
                "question": "Exact inspection",
                "revision_selector": "all",
                "exact_only": True,
                "mode": mode,
                "limit": min(8, limits.limit),
                "budgets": {
                    "max_tokens": min(6000, limits.max_tokens),
                    "max_response_bytes": min(1048576, limits.max_response_bytes),
                },
            }
        )
        return self.compiler.compile_attempt(
            snapshot, scope, query, self.owner.config, _retained_inspection=True
        )

    def _artifact(
        self,
        sha: str,
        *,
        original: bool = False,
        max_bytes: int = MAX_INSPECTION_PAYLOAD_BYTES,
    ) -> tuple[bytes, str]:
        row = self.owner.db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (sha,),
        ).fetchone()
        if row is None or row[2] != sha or row[3] != "present":
            raise TychoraError(
                "ORIGINAL_REMOVED" if original else "NOT_FOUND",
                "Original payload was removed."
                if original
                else "Registered asset is unavailable.",
            )
        if row[0] > max_bytes:
            raise TychoraError(
                "INVALID_REQUEST", "Inspection payload exceeds its limit."
            )
        ref = ArtifactRef.from_row((sha, *row[:3]))
        with self.owner.artifacts.open_verified(ref) as stream:
            return stream.read(), row[1]

    def _inspect_job(
        self, request: InspectRequest, scope: RequestScope
    ) -> InspectionResult:
        repository = JobRepository(
            self.owner.db,
            artifacts=self.owner.artifacts,
            workspace_id=self.owner.workspace_id,
        )
        repository.authorize(request.id, scope, write=False)
        row = self.owner.db.execute(
            "SELECT operation,state,created_at,updated_at FROM jobs WHERE job_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Job is unavailable.")
        return InspectionResult(
            subject="jobs",
            id=request.id,
            records=(
                InspectionItem(
                    id=request.id,
                    record_type="job_summary",
                    schema_id="job-summary@1",
                    value=FrozenJsonMap(
                        dict(
                            zip(("operation", "state", "created_at", "updated_at"), row)
                        )
                    ),
                ),
            ),
        )

    def _inspect_run(
        self, request: InspectRequest, scope: RequestScope
    ) -> InspectionResult:
        row = self.owner.db.execute(
            "SELECT principal_id,scope_digest,state,created_at,snapshot_id,retained_payload_sha "
            "FROM query_runs WHERE run_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Run is unavailable.")
        if (
            self.owner.scope.narrow(scope) != scope
            or row[0] != scope.principal_id
            or not query_run_scope_matches(row[1], scope)
        ):
            _deny()
        return InspectionResult(
            subject="runs",
            id=request.id,
            records=(
                InspectionItem(
                    id=request.id,
                    record_type="run_summary",
                    schema_id="run-summary@1",
                    value=FrozenJsonMap(
                        {
                            "state": row[2],
                            "created_at": row[3],
                            "snapshot_id": row[4],
                            "scope_limited": True,
                            "request_retained": row[5] is not None,
                        }
                    ),
                ),
            ),
        )

    def _inspect_diagnostics(self, request, snapshot, attempt):
        member = next(
            (item for item in snapshot.members if item.extraction_id == request.id),
            None,
        )
        if member is None:
            raise TychoraError("NOT_FOUND", "Diagnostic extraction is unavailable.")
        if member.revision_id not in attempt.view.revision_ids:
            _deny()
        row = self.owner.db.execute(
            "SELECT e.diagnostics_artifact_sha,r.source_id FROM extractions e "
            "JOIN revisions r USING(revision_id) WHERE e.extraction_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Diagnostic extraction is unavailable.")
        retained = row[0] is not None
        detail: list[InspectionItem] = []
        summary: dict = {"retained": retained, "scope_limited": True}
        if retained:
            payload, _ = self._artifact(row[0], max_bytes=MAX_DIAGNOSTIC_BYTES)
            try:
                parsed = json.loads(payload)
            except (ValueError, UnicodeError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained diagnostics are invalid."
                ) from None
            if not isinstance(parsed, (dict, list)):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained diagnostics are invalid."
                )
            data = parsed.get("data") if isinstance(parsed, dict) else None
            if isinstance(data, dict) and "resolutions" in data:
                from tychora.contracts.code import Resolution
                from tychora.ports.language import SyntaxDiagnostic

                try:
                    raw = data["resolutions"]
                    if not isinstance(raw, list) or len(raw) > 100000:
                        raise ValueError
                    records = tuple(Resolution.from_mapping(item) for item in raw)
                    syntax = data.get("syntax", [])
                    if not isinstance(syntax, list) or len(syntax) > 100000:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained diagnostics are invalid."
                    ) from None
                eligible = attempt.view.graph_eligible_unit_ids
                permitted, limited = _permitted_resolutions(
                    records, eligible, attempt.view.revision_ids, row[1]
                )
                summary = dict(
                    _scoped_resolution_rates(
                        records, eligible, attempt.view.revision_ids, row[1]
                    )
                )
                summary["retained"] = True
                for item in permitted:
                    detail.append(
                        InspectionItem(
                            id=item.reference_id,
                            record_type="resolution",
                            schema_id="code-resolution@1",
                            value=FrozenJsonMap(item.model_dump(mode="json")),
                        )
                    )
                for entry in syntax:
                    try:
                        path = entry["relative_path"]
                        file_units = {
                            unit_id
                            for (unit_id,) in self.owner.db.execute(
                                "SELECT DISTINCT l.unit_id FROM locators l "
                                "JOIN evidence_units u USING(unit_id) "
                                "WHERE u.extraction_id=? AND l.locator_type='code' "
                                "AND l.source_ref=?",
                                (request.id, path),
                            )
                        }
                        diagnostics = tuple(
                            SyntaxDiagnostic.from_mapping(value)
                            for value in entry["diagnostics"]
                        )
                        coverage = entry["coverage"]
                        if not isinstance(path, str) or not isinstance(coverage, str):
                            raise TypeError
                    except (ValueError, KeyError, TypeError):
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Retained diagnostics are invalid."
                        ) from None
                    if not file_units or not file_units <= eligible:
                        limited = True
                        continue
                    detail.append(
                        InspectionItem(
                            id=digest("diagnostic-file-v1", request.id, path),
                            record_type="syntax_diagnostics",
                            schema_id="code-syntax-diagnostics@1",
                            value=FrozenJsonMap(
                                {
                                    "relative_path": path,
                                    "coverage": coverage,
                                    "diagnostics": [
                                        item.model_dump(mode="json")
                                        for item in diagnostics
                                    ],
                                }
                            ),
                        )
                    )
                summary["scope_limited"] = limited
            elif (
                isinstance(parsed, dict)
                and parsed.get("type") == "document_vision_capability"
            ):
                from tychora.contracts.composition import VisionCapabilityDiagnostics

                try:
                    diagnostic = VisionCapabilityDiagnostics.model_validate_json(
                        payload
                    )
                    from tychora.contracts.identity import canonical_bytes

                    if canonical_bytes(diagnostic.model_dump(mode="json")) != payload:
                        raise ValueError
                except (ValueError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained PDF diagnostics are invalid."
                    ) from None
                if diagnostic.pdf_coverage is not None:
                    summary["pdf_coverage"] = diagnostic.pdf_coverage.model_dump(
                        mode="json"
                    )
                    if diagnostic.pdf_coverage.ocr_pages:
                        summary["ocr_review_state"] = "needs_review"
                    summary["detection_note"] = (
                        "No-text-detected is detector-relative, not a blank-page assertion."
                    )
                    summary["scope_limited"] = False
            else:
                summary["unavailable_fields"] = ["resolution_records", "rates"]
        return InspectionResult(
            subject="diagnostics",
            id=request.id,
            records=(
                InspectionItem(
                    id=request.id,
                    record_type="diagnostic_summary",
                    schema_id="diagnostic-summary@1",
                    value=FrozenJsonMap(summary),
                ),
                *detail,
            ),
        )

    def _inspect_evidence(self, request, snapshot, attempt, *, prepared=None):
        owner = self.owner
        row = owner.db.execute(
            "SELECT e.extraction_id,x.revision_id FROM evidence_units e "
            "JOIN extractions x USING(extraction_id) WHERE e.unit_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Evidence unit is unavailable.")
        extraction, revision = row
        member = next(
            (
                item
                for item in snapshot.members
                if item.extraction_id == extraction and item.revision_id == revision
            ),
            None,
        )
        if member is None or revision not in attempt.view.revision_ids:
            _deny()
        unit = owner.store._hydrate((request.id,))[0]
        selected = _ranges(request, len(unit.text))
        eligible = (
            attempt.view.revision_ids
            | attempt.view.context_unit_ids
            | attempt.view.structural_unit_ids
            | attempt.view.asset_ids
        )
        full_unit_readable = request.id in (
            attempt.view.context_unit_ids | attempt.view.structural_unit_ids
        )
        if not selected:
            if not full_unit_readable:
                _deny()
            evidence = unit
            partial_evidence = None
            excerpts = ()
        else:
            # The retained representation validator reconstructs exact per-range
            # ownership from canonical locators/source parts, never from originals.
            if prepared is None:
                prepared = prepare_extraction(retained_rows(owner.store, member))
            representation = next(
                (item for item in prepared.units if item.unit.unit_id == request.id),
                None,
            )
            if representation is None:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained source range is unavailable."
                )
            if unit.unit_type == "asset_proxy":
                # The compiled retained attempt already applies the selected
                # (including caller-use fallback) mode's derived review policy
                # and the complete source/asset dependency closure. Proxies are
                # atomic; a ranged read cannot bypass their admission.
                if request.id not in attempt.view.proxy_unit_ids:
                    _deny()
            else:
                mode_name = owner.config.values.mode or owner.config.values.default_mode
                if mode_name is None:
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE", "Inspection mode is unavailable."
                    )
                mode = cast(FrozenJsonMap, owner.store.registry.modes[mode_name])
                allowed_reviews = set(
                    cast(tuple[str, ...], mode["original_review_states"])
                )
                if unit.review_state not in allowed_reviews:
                    _deny()
            for interval in selected:
                dependencies = frozenset(
                    dependency
                    for owned in representation.ownership
                    if owned.char_start < interval.char_end
                    and interval.char_start < owned.char_end
                    for dependency in owned.dependency_ids
                )
                if not dependencies <= eligible | {request.id}:
                    _deny()
            excerpts = tuple(
                Excerpt(
                    unit_id=request.id,
                    char_start=interval.char_start,
                    char_end=interval.char_end,
                    text=unit.text[interval.char_start : interval.char_end],
                )
                for interval in selected
            )
            evidence = None
            partial_evidence = PartialEvidenceMetadata(
                unit_id=unit.unit_id,
                extraction_id=unit.extraction_id,
                unit_type=unit.unit_type,
                canonical_text_sha=unit.text_sha if full_unit_readable else None,
                canonical_text_length=len(unit.text) if full_unit_readable else None,
            )
        records = ()
        if not selected:
            rows = owner.db.execute(
                "SELECT record_type,schema_hash,data_json FROM structured_records WHERE unit_id=?",
                (request.id,),
            ).fetchall()
            records = tuple(
                InspectionItem(
                    id=request.id,
                    record_type=kind,
                    schema_id=schema,
                    value=FrozenJsonMap(json.loads(value)),
                )
                for kind, schema, value in rows
            )
            if unit.unit_type == "asset_proxy" and unit.attributes.get(
                "proxy_kind"
            ) in {"ocr", "description"}:
                proxy_row = owner.db.execute(
                    "SELECT derivation_sha FROM asset_proxies WHERE proxy_unit_id=?",
                    (request.id,),
                ).fetchone()
                if proxy_row is None:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Derived proxy binding is unavailable."
                    )
                payload, media_type = self._artifact(
                    proxy_row[0], max_bytes=1024 * 1024
                )
                try:
                    root = json.loads(payload)
                    data = root["data"]
                    result = data["result"]
                    if (
                        media_type != "application/json"
                        or canonical_bytes(root) != payload
                        or root.get("type") != "vision_derivation"
                        or root.get("schema_version") != "1"
                        or not isinstance(data, dict)
                        or not isinstance(result, dict)
                    ):
                        raise ValueError
                    details = {
                        "task": data["task"],
                        "model": data["model"],
                        "prompt_identity": data["prompt_identity"],
                        "image_sha256": data["image_sha256"],
                        "source_context_sha256": data["source_context_sha256"],
                        "dependency_ids": data["dependency_ids"],
                        "target_id": result.get("target_id"),
                        "literal_transcription": result.get("literal_transcription"),
                        "interpretation": result.get("interpretation"),
                        "uncertainties": result.get("uncertainties", []),
                    }
                except (ValueError, TypeError, KeyError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Derived proxy receipt differs."
                    ) from None
                records = (
                    *records,
                    InspectionItem(
                        id=request.id,
                        record_type="vision_derivation",
                        schema_id="vision-derivation@1",
                        value=FrozenJsonMap(details),
                    ),
                )
        return InspectionResult(
            subject="evidence",
            id=request.id,
            evidence=evidence,
            partial_evidence=partial_evidence,
            excerpts=excerpts,
            records=records,
        )

    def _inspect_asset(self, request, attempt):
        row = self.owner.db.execute(
            "SELECT a.artifact_sha,a.locator_json,x.revision_id "
            "FROM assets a JOIN extractions x USING(extraction_id) WHERE a.asset_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Registered asset is unavailable.")
        sha, locator, revision = row
        if (
            request.id not in attempt.view.asset_ids
            or revision not in attempt.view.revision_ids
        ):
            _deny()
        if self.owner.db.execute(
            "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
            (revision, sha),
        ).fetchone():
            raise TychoraError("ORIGINAL_REMOVED", "Original payload was removed.")
        payload, media_type = self._artifact(sha, original=True)
        return InspectionResult(
            subject="assets",
            id=request.id,
            original_ref=sha,
            payload=payload,
            media_type=media_type,
            records=(
                InspectionItem(
                    id=request.id,
                    record_type="asset_locator",
                    schema_id="asset-locator@1",
                    value=FrozenJsonMap(json.loads(locator)),
                ),
            ),
        )

    def _inspect_revision(self, request, snapshot, attempt):
        if request.id not in attempt.view.revision_ids:
            _deny()
        row = self.owner.db.execute(
            "SELECT r.original_sha,r.title FROM revisions r WHERE r.revision_id=?",
            (request.id,),
        ).fetchone()
        if row is None:
            raise TychoraError("NOT_FOUND", "Revision is unavailable.")
        sha, title = row
        if request.original or request.view == "original":
            selected_extractions = tuple(
                item.extraction_id
                for item in snapshot.members
                if item.revision_id == request.id
            )
            readable = attempt.view.context_unit_ids | attempt.view.structural_unit_ids
            for extraction in selected_extractions:
                for (unit_id,) in self.owner.db.execute(
                    "SELECT u.unit_id FROM evidence_units u "
                    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                    "WHERE u.extraction_id=? AND p.proxy_unit_id IS NULL",
                    (extraction,),
                ):
                    if unit_id not in readable:
                        _deny()
            binding = self.owner.db.execute(
                "SELECT 1 FROM revision_artifacts WHERE revision_id=? AND artifact_sha=? AND role='original'",
                (request.id, sha),
            ).fetchone()
            removed = self.owner.db.execute(
                "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
                (request.id, sha),
            ).fetchone()
            if binding is None or removed:
                raise TychoraError("ORIGINAL_REMOVED", "Original payload was removed.")
            payload, media = self._artifact(sha, original=True)
            return InspectionResult(
                subject="revisions",
                id=request.id,
                original_ref=sha,
                payload=payload,
                media_type=media,
            )
        return InspectionResult(
            subject="revisions",
            id=request.id,
            records=(
                InspectionItem(
                    id=request.id,
                    record_type="revision_summary",
                    schema_id="revision-summary@1",
                    value=FrozenJsonMap({"title": title}),
                ),
            ),
        )

    @contextmanager
    def _pin_retained(self, request: InspectRequest):
        """Resolve explicit canonical identity without borrowing old permissions.

        This inspection-only view has no native projections. Its members come
        from retained canonical extractions, independently of snapshot retention.
        Current policy and retained range ownership are compiled in the usual
        same-epoch read. Owner serialization and leases hold these identities and
        their payloads through delivery.
        """
        selectors = {
            "evidence": "e.extraction_id=(SELECT extraction_id FROM evidence_units WHERE unit_id=?)",
            "assets": "e.extraction_id=(SELECT extraction_id FROM assets WHERE asset_id=?)",
            "diagnostics": "e.extraction_id=?",
            "revisions": "e.revision_id=?",
        }
        rows = self.owner.db.execute(
            "SELECT s.source_id,e.revision_id,e.extraction_id FROM extractions e "
            "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
            "JOIN collections c USING(collection_id) WHERE c.workspace_id=? AND "
            + selectors[request.subject]
            + " ORDER BY e.extraction_id",
            (self.owner.workspace_id, request.id),
        ).fetchall()
        if not rows:
            if request.subject == "revisions":
                _deny()
            raise TychoraError("NOT_FOUND", "Retained identity is unavailable.")
        members = tuple(
            SnapshotMember(source_id=s, revision_id=r, extraction_id=e)
            for s, r, e in rows
        )
        keys = frozenset(value for row in rows for value in row)
        payloads = set()
        for member in members:
            payloads.update(
                row[0]
                for row in self.owner.db.execute(
                    "SELECT artifact_sha FROM revision_artifacts WHERE revision_id=? "
                    "UNION SELECT document_artifact_sha FROM extractions WHERE extraction_id=? "
                    "UNION SELECT assertions_artifact_sha FROM extractions WHERE extraction_id=? "
                    "UNION SELECT diagnostics_artifact_sha FROM extractions WHERE extraction_id=? "
                    "UNION SELECT artifact_sha FROM assets WHERE extraction_id=?",
                    (member.revision_id, *(member.extraction_id,) * 4),
                )
                if row[0] is not None
            )
        with (
            self.owner.store.leases.pin(keys),
            self.owner.artifacts.pin_payloads(frozenset(payloads)),
            self.owner.store.pin_snapshot(None) as active,
        ):
            membership = digest(
                "retained-inspection-v1", [m.model_dump(mode="json") for m in members]
            )
            yield active.model_copy(
                update={
                    "snapshot_id": membership,
                    "membership_sha": membership,
                    "members": members,
                    "slots": (),
                }
            )

    def _inspect_sync(
        self, request: InspectRequest, scope: RequestScope, ctx: ExecutionContext
    ):
        granted = self.owner.scope.narrow(scope)
        if "read" not in granted.capabilities:
            _deny()
        if request.subject != "evidence" and (
            request.range is not None or request.ranges
        ):
            raise TychoraError("INVALID_REQUEST", "Ranges require an evidence unit.")
        if request.subject == "jobs":
            return self._inspect_job(request, granted)
        if request.subject == "runs":
            return self._inspect_run(request, scope)
        if (
            request.subject == "revisions"
            and (request.original or request.view == "original")
            and "manage" in granted.capabilities
        ):
            # A collection manager may distinguish a removed original binding
            # after withdrawal. Ordinary readers still pass through eligibility
            # below, so the tombstone is never disclosed to a hidden reader.
            row = self.owner.db.execute(
                "SELECT c.collection_id,c.access_floor,s.access_floor,r.access_rank,"
                "r.original_sha,s.withdrawn_at FROM revisions r "
                "JOIN sources s USING(source_id) JOIN collections c USING(collection_id) "
                "WHERE r.revision_id=? AND c.workspace_id=?",
                (request.id, self.owner.workspace_id),
            ).fetchone()
            if (
                row is not None
                and row[0] in granted.collection_ids
                and max(row[1:4]) <= granted.access_ceiling
                and row[5] is not None
                and self.owner.db.execute(
                    "SELECT 1 FROM original_payload_tombstones "
                    "WHERE revision_id=? AND artifact_sha=?",
                    (request.id, row[4]),
                ).fetchone()
            ):
                raise TychoraError("ORIGINAL_REMOVED", "Original payload was removed.")
        with self._pin_retained(request) as snapshot:
            ctx.checkpoint()
            attempt = self._attempt(snapshot, granted)
            if request.subject == "evidence":
                result = self._inspect_evidence(request, snapshot, attempt)
            elif request.subject == "assets":
                result = self._inspect_asset(request, attempt)
            elif request.subject == "revisions":
                result = self._inspect_revision(request, snapshot, attempt)
            elif request.subject == "diagnostics":
                result = self._inspect_diagnostics(request, snapshot, attempt)
            else:
                raise TychoraError(
                    "INVALID_REQUEST", "Inspection subject is unavailable."
                )
            self.compiler.revalidate_epoch(attempt.view, "delivery")
            ctx.checkpoint()
            return result

    async def inspect(
        self, request: InspectRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> InspectionResult:
        return await self.owner.run_blocking(
            lambda: self._inspect_sync(request, scope, ctx), ctx
        )

    async def extraction_units(
        self,
        request: ExtractionContentRequest,
        scope: RequestScope,
        ctx: ExecutionContext,
    ) -> ExtractionUnitsPage:
        from tychora.application.retained_content import content_page

        return await self.owner.run_blocking(
            lambda: content_page(self, request, scope, ctx, windows=False), ctx
        )

    async def extraction_windows(
        self,
        request: ExtractionContentRequest,
        scope: RequestScope,
        ctx: ExecutionContext,
    ) -> ExtractionWindowsPage:
        from tychora.application.retained_content import content_page

        return await self.owner.run_blocking(
            lambda: content_page(self, request, scope, ctx, windows=True), ctx
        )

    async def relations(
        self, request: RelationRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> RelationResult:
        return await self.owner.run_blocking(
            lambda: self._relations_sync(request, scope, ctx), ctx
        )

    def _relations_sync(
        self, request: RelationRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> RelationResult:
        granted = self.owner.scope.narrow(scope)
        if "read" not in granted.capabilities:
            _deny()
        with self.owner.store.pin_snapshot(None) as snapshot:
            ctx.checkpoint()
            attempt = self._attempt(snapshot, granted)
            view = attempt.view
            eligible = view.graph_eligible_unit_ids
            if any(seed not in eligible for seed in request.unit_ids):
                _deny()
            if self.graph is None or view.graph_generation_id is None:
                raise TychoraError(
                    "INDEX_INCOMPLETE",
                    "Graph inspection requires a rebuilt ready generation.",
                )
            policy = resolve_graph_traversal_policy(
                self.owner.config, self.owner.store.registry
            )
            allowed = {
                item.predicate
                for item in policy.inspection_predicates
                if item.view == request.view
            }
            if request.predicates - allowed:
                raise TychoraError(
                    "INVALID_REQUEST", "Relation predicates exceed the pinned policy."
                )
            predicates = tuple(sorted(request.predicates or allowed))
            query_digest = digest(
                "relation-inspection-query-v1",
                {
                    "unit_ids": sorted(set(request.unit_ids)),
                    "predicates": predicates,
                    "direction": request.direction,
                    "view": request.view,
                    "max_hops": request.max_hops,
                },
            )
            cursor_identity = [
                view.snapshot_id,
                view.policy_epoch,
                view.scope_digest,
                view.graph_generation_id,
                view.graph_manifest_digest,
                query_digest,
            ]
            last = ""
            if request.cursor is not None:
                try:
                    raw = urlsafe_b64decode(
                        request.cursor + "=" * (-len(request.cursor) % 4)
                    )
                    cursor = json.loads(raw)
                    if (
                        not isinstance(cursor, list)
                        or len(cursor) != 7
                        or cursor[:6] != cursor_identity
                        or not isinstance(cursor[6], str)
                    ):
                        raise ValueError
                    last = cursor[6]
                except (ValueError, UnicodeError, TypeError):
                    raise TychoraError(
                        "INVALID_REQUEST", "Invalid relation cursor."
                    ) from None
            if not predicates or request.max_hops == 0:
                return RelationResult()
            limits = self.owner.config.values.limits
            budget = GraphBudget(
                max_rows=min(limits.graph_work_limit, 2000),
                max_inspection_edges=100,
                max_inspection_hops=min(request.max_hops, limits.inspection_hops, 2),
            )
            page = self.graph.get_assertions(
                view,
                request.unit_ids,
                predicates,
                budget,
                ctx,
                direction=request.direction,
                relation_view=request.view,
            )
            paths = []
            incomplete = page.truncation is not None or not page.exhausted
            for path in page.assertions:
                if (
                    path.source_unit_id not in eligible
                    or path.target_unit_id not in eligible
                    or not path.required_support_ids
                    or not path.required_support_ids <= eligible
                    or set(path.assertion_ids) & view.blocked_assertion_ids
                    or path.predicate not in predicates
                ):
                    incomplete = True
                    continue
                paths.append(
                    RelationPath(
                        unit_ids=(path.source_unit_id, path.target_unit_id),
                        assertion_ids=path.assertion_ids,
                        predicates=(path.predicate,),
                        support_unit_ids=path.required_support_ids,
                        provenance=FrozenJsonMap({"status": path.status}),
                    )
                )
            keyed = sorted(
                (
                    (
                        digest(
                            "relation-inspection-path-v1", item.model_dump(mode="json")
                        ),
                        item,
                    )
                    for item in paths
                ),
                key=lambda value: value[0],
            )
            remaining = [(key, item) for key, item in keyed if key > last]
            selected = remaining[: request.max_edges]
            more = len(remaining) > len(selected)
            next_cursor = None
            if more and selected:
                next_cursor = (
                    urlsafe_b64encode(
                        json.dumps(
                            [*cursor_identity, selected[-1][0]],
                            separators=(",", ":"),
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            self.compiler.revalidate_epoch(view, "delivery")
            ctx.checkpoint()
            return RelationResult(
                paths=tuple(item for _, item in selected),
                truncated=incomplete or more,
                next_cursor=next_cursor,
            )
