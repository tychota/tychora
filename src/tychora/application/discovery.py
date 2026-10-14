"""Detached vocabulary discovery and separately authorized corpus counts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from importlib import metadata
from typing import TYPE_CHECKING, cast

from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.composition import VisionCapabilityDiagnostics
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.operations import (
    CapabilityEntry,
    DefinitionEntry,
    DiscoverRequest,
    DiscoveryResult,
    KindMetadataField,
    KindMetadataForm,
    NamedCount,
    SourceChannelCoverage,
    SourceChannelPage,
    SourceExtractionCoverage,
    SourceExtractionPage,
    SourceRevisionCoverage,
    SourceRevisionPage,
    SourceSelectionCandidate,
    SourceSelectionPage,
)
from tychora.contracts.requests import (
    RequestScope,
    canonical_scope_projection,
    query_run_scope_digests,
)
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.models import ResolvedConfig
    from tychora.contracts.code import Resolution

SUBJECTS = (
    "kinds",
    "facets",
    "relations",
    "profiles",
    "languages",
    "sources",
    "settings",
    "runs",
    "models",
)


def _permitted_resolutions(
    records: tuple[Resolution, ...],
    eligible_units: frozenset[str],
    eligible_revisions: frozenset[str],
    source_id: str | None = None,
) -> tuple[tuple[Resolution, ...], bool]:
    """Admit only complete resolution provenance inside the current view."""
    permitted = []
    limited = False
    for item in records:
        if (
            source_id is not None
            and item.source_id != source_id
            or item.revision_id not in eligible_revisions
            or item.source_unit_id not in eligible_units
            or item.truncated
            or not set(item.candidates) <= eligible_units
            or item.selected_target is not None
            and item.selected_target not in eligible_units
        ):
            limited = True
            continue
        permitted.append(item)
    return tuple(permitted), limited


def _scoped_resolution_rates(
    records: tuple[Resolution, ...],
    eligible_units: frozenset[str],
    eligible_revisions: frozenset[str],
    source_id: str | None = None,
) -> FrozenJsonMap:
    """Use only references whose origin and every disclosed target remain readable."""
    from tychora.adapters.code.resolve import resolution_rates

    permitted, limited = _permitted_resolutions(
        records, eligible_units, eligible_revisions, source_id
    )
    rates = resolution_rates(permitted)
    return FrozenJsonMap(
        {
            "rates": [item.model_dump(mode="json") for item in rates.values()],
            "unsupported_references": sum(not item.supported for item in permitted),
            "scope_limited": limited,
        }
    )


def _capabilities(config: ResolvedConfig) -> tuple[CapabilityEntry, ...]:
    values = config.values
    requested = {
        "sqlite": True,
        "markdown-it-py": values.extraction.parser == "markdown",
        "docling": values.extraction.parser == "docling",
        "tree-sitter": values.extraction.parser == "code",
        "chromadb": values.index.text.backend == "chroma"
        or values.index.image.backend == "chroma",
        "ladybug": values.index.graph == "ladybug",
        "voyageai": any(
            provider.provider == "voyage"
            for provider in (
                values.providers.text,
                values.providers.image,
                values.providers.rerank,
            )
        ),
    }
    entries = []
    for name, required in sorted(requested.items()):
        if name == "sqlite":
            version = "stdlib"
        else:
            try:
                version = metadata.version(name)
            except metadata.PackageNotFoundError:
                version = None
        entries.append(
            CapabilityEntry(
                name=name,
                installed=version is not None,
                required=required,
                version=version,
            )
        )
    return tuple(entries)


def _cursor(subject: str, key: str) -> str:
    value = json.dumps([subject, key], separators=(",", ":")).encode()
    return urlsafe_b64encode(value).decode().rstrip("=")


def _after(cursor: str | None, subject: str) -> str | None:
    if cursor is None:
        return None
    if len(cursor) > 4096:
        raise TychoraError("INVALID_REQUEST", "Invalid discovery cursor.")
    try:
        raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or value[0] != subject
            or not isinstance(value[1], str)
        ):
            raise ValueError
        return value[1]
    except (ValueError, UnicodeError):
        raise TychoraError("INVALID_REQUEST", "Invalid discovery cursor.") from None


def _scoped_cursor(
    owner, subject: str, key: str, scope: RequestScope, limit: int
) -> str:
    binding = [
        subject,
        key,
        digest("source-cursor-scope-v2", canonical_scope_projection(scope)),
        limit,
    ]
    mac = hmac.new(
        owner._runtime.record["token"].encode(),
        canonical_bytes(binding),
        hashlib.sha256,
    ).hexdigest()
    return urlsafe_b64encode(canonical_bytes([*binding, mac])).decode().rstrip("=")


def _scoped_after(
    owner, cursor: str | None, subject: str, scope: RequestScope, limit: int
) -> str | None:
    if cursor is None:
        return None
    if len(cursor) > 4096:
        raise TychoraError("INVALID_REQUEST", "Invalid discovery cursor.")
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(raw)
        if (
            not isinstance(value, list)
            or len(value) != 5
            or value[0] != subject
            or not isinstance(value[1], str)
            or value[2]
            != digest("source-cursor-scope-v2", canonical_scope_projection(scope))
            or value[3] != limit
            or not isinstance(value[4], str)
        ):
            raise ValueError
        expected = hmac.new(
            owner._runtime.record["token"].encode(),
            canonical_bytes(value[:4]),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, value[4]):
            raise ValueError
        return value[1]
    except (ValueError, UnicodeError, TypeError):
        raise TychoraError("INVALID_REQUEST", "Invalid discovery cursor.") from None


class DiscoveryService:
    def __init__(
        self,
        config: ResolvedConfig,
        registry: Registry,
        owner: WorkspaceOwner | None = None,
    ) -> None:
        if registry.registry_digest != config.registry_digest:
            raise TychoraError("INVALID_REQUEST", "Registry and configuration differ.")
        self.config = config
        self.registry = registry
        self.owner = owner

    def _definitions(self, subject: str) -> dict[str, FrozenJsonMap]:
        registry = self.registry
        match subject:
            case "kinds":
                return cast(dict[str, FrozenJsonMap], dict(registry.kinds))
            case "facets":
                return cast(dict[str, FrozenJsonMap], dict(registry.facets))
            case "relations":
                return cast(dict[str, FrozenJsonMap], dict(registry.relations))
            case "profiles":
                return cast(dict[str, FrozenJsonMap], dict(registry.modes))
            case "languages":
                return {
                    "python": FrozenJsonMap(
                        {
                            "label": "Python",
                            "dialects": ["python3"],
                            "reference_forms": ["import", "call", "inheritance"],
                        }
                    ),
                    "typescript": FrozenJsonMap(
                        {
                            "label": "TypeScript and JavaScript",
                            "dialects": ["typescript", "tsx", "javascript", "jsx"],
                            "reference_forms": [
                                "import",
                                "export",
                                "call",
                                "inheritance",
                                "jsx",
                            ],
                        }
                    ),
                }
            case "settings":
                safe = self.config.inspect(owner_authorized=False)
                values = cast(FrozenJsonMap, safe["values"])
                return {
                    "resolved": FrozenJsonMap(
                        {
                            "profile": self.config.profile,
                            "origins": safe["origins"],
                            "retrieval": values["retrieval"],
                            "acquisition": values["acquisition"],
                            "limits": values["limits"],
                            "maintenance": {
                                "health_scope": "whole_workspace",
                                "health_capability": "read",
                                "resume_deletions_capability": "manage",
                            },
                            "parser_limits": {
                                "native_source_bytes": min(
                                    4 * 1024 * 1024,
                                    self.config.values.acquisition.limits.max_file_bytes,
                                ),
                                "native_markdown_block_tokens": 100000,
                                "native_json_records": 4096,
                                "native_extraction_root_bytes": 16 * 1024 * 1024,
                                "docling_conversion_bytes": 256 * 1024 * 1024,
                                "docling_extraction_root_bytes": 16 * 1024 * 1024,
                                "docling_image_pixels": 40_000_000,
                                "docling_native_grid_items": 100000,
                                "docling_hierarchy_depth": 128,
                                "docling_staged_descriptors": 4096,
                                "document_semantic_units_parts_separators": 100000,
                                "document_semantic_depth": 128,
                                "document_semantic_serialized_bytes": 256 * 1024 * 1024,
                                "document_required_input_bytes": 256 * 1024 * 1024,
                                "document_required_plus_replay_bytes": 512
                                * 1024
                                * 1024,
                            },
                        }
                    )
                }
            case "models":
                safe = self.config.inspect(owner_authorized=False)
                providers = cast(
                    FrozenJsonMap, cast(FrozenJsonMap, safe["values"])["providers"]
                )
                return {
                    name: cast(FrozenJsonMap, value)
                    for name, value in providers.items()
                }
            case "sources" | "runs":
                return {}
        raise TychoraError(
            "INVALID_REQUEST",
            "Unknown discovery subject. Available: " + ", ".join(SUBJECTS),
        )

    def _counts(self, subject: str, scope: RequestScope) -> dict[str, int]:
        if self.owner is None or subject != "kinds":
            return {}
        owner = self.owner
        granted = owner.scope.narrow(scope)
        if "read" not in granted.capabilities:
            return {}

        def read() -> dict[str, int]:
            rows = owner.db.execute(
                "SELECT DISTINCT r.revision_id,r.kind_id,s.source_id,c.collection_id,"
                "r.access_rank,s.access_floor,c.access_floor,s.withdrawn_at,"
                "r.allowed_uses_json,k.definition_json,k.definition_hash "
                "FROM revisions r JOIN sources s USING(source_id) "
                "JOIN collections c USING(collection_id) "
                "JOIN kinds k USING(kind_id) "
                "WHERE c.workspace_id=? AND json_extract(c.policy_json,'$.archived_at') IS NULL ORDER BY r.revision_id",
                (owner.workspace_id,),
            )
            sources: dict[str, set[str]] = {}
            for (
                _revision,
                kind,
                source,
                collection,
                access,
                source_floor,
                collection_floor,
                withdrawn,
                uses_raw,
                definition_raw,
                definition_hash,
            ) in rows:
                if (
                    collection not in granted.collection_ids
                    or max(access, source_floor, collection_floor)
                    > granted.access_ceiling
                    or withdrawn is not None
                ):
                    continue
                try:
                    envelope = json.loads(definition_raw)
                    recorded = envelope.pop("envelope_digest")
                    if (
                        recorded != definition_hash
                        or digest("historical-kind-v1", envelope) != recorded
                    ):
                        raise ValueError
                    kind_uses = set(envelope["kind"]["allowed_uses"])
                    revision_uses = set(json.loads(uses_raw))
                except (ValueError, KeyError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained kind definition is invalid."
                    ) from None
                if not (kind_uses & revision_uses & granted.uses):
                    continue
                sources.setdefault(kind, set()).add(source)
            return {kind: len(ids) for kind, ids in sources.items()}

        return owner.run_blocking_sync(read, ExecutionContext())

    def _source_rows(
        self,
        scope: RequestScope,
        *,
        after: str = "",
        limit: int = 100,
        source_id: str | None = None,
    ) -> tuple[tuple, ...]:
        if self.owner is None:
            return ()
        owner = self.owner
        granted = owner.scope.narrow(scope)
        if "read" not in granted.capabilities or not granted.collection_ids:
            return ()
        if not 1 <= limit <= 100:
            raise TychoraError("INVALID_REQUEST", "Source page limit is invalid.")

        def read() -> tuple[tuple, ...]:
            selector = (
                "s.source_id=?"
                if source_id is not None
                else "s.source_id>? AND json_extract(c.policy_json,'$.archived_at') IS NULL"
            )
            return tuple(
                owner.db.execute(
                    "SELECT s.source_id,s.source_key,s.source_type,s.collection_id "
                    "FROM sources s JOIN collections c USING(collection_id) "
                    "WHERE c.workspace_id=? AND s.withdrawn_at IS NULL "
                    "AND s.access_floor<=? AND c.access_floor<=? "
                    "AND s.collection_id IN (SELECT value FROM json_each(?)) "
                    f"AND {selector} ORDER BY s.source_id LIMIT ?",
                    (
                        owner.workspace_id,
                        granted.access_ceiling,
                        granted.access_ceiling,
                        json.dumps(sorted(granted.collection_ids)),
                        source_id if source_id is not None else after,
                        1 if source_id is not None else limit + 1,
                    ),
                ).fetchall()
            )

        return owner.run_blocking_sync(read, ExecutionContext())

    def _revision_page(
        self,
        source_id: str,
        scope: RequestScope,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceRevisionPage:
        assert self.owner is not None
        if not 1 <= limit <= 100:
            raise TychoraError("INVALID_REQUEST", "Revision page limit is invalid.")
        owner = self.owner
        granted = owner.scope.narrow(scope)
        subject = f"source-revisions:{source_id}"
        after = _scoped_after(owner, cursor, subject, granted, limit) or ""

        def read() -> SourceRevisionPage:
            rows = owner.db.execute(
                "SELECT r.revision_id,r.kind_id,r.access_rank,r.allowed_uses_json,"
                "k.definition_json,k.definition_hash FROM revisions r "
                "JOIN kinds k USING(kind_id) WHERE r.source_id=? "
                "AND r.revision_id>? AND r.access_rank<=? "
                "AND EXISTS (SELECT 1 FROM json_each(r.allowed_uses_json) ru "
                "JOIN json_each(k.definition_json,'$.kind.allowed_uses') ku "
                "ON ku.value=ru.value JOIN json_each(?) su ON su.value=ru.value) "
                "ORDER BY r.revision_id LIMIT ?",
                (
                    source_id,
                    after,
                    granted.access_ceiling,
                    json.dumps(sorted(granted.uses)),
                    limit + 1,
                ),
            ).fetchall()
            page_rows = rows[:limit]
            revisions: list[SourceRevisionCoverage] = []
            limited = granted != owner.scope
            for (
                revision,
                kind,
                access,
                uses_raw,
                definition_raw,
                definition_hash,
            ) in page_rows:
                if access > granted.access_ceiling:
                    limited = True
                    continue
                try:
                    envelope = json.loads(definition_raw)
                    recorded = envelope.pop("envelope_digest")
                    if (
                        recorded != definition_hash
                        or digest("historical-kind-v1", envelope) != recorded
                    ):
                        raise ValueError
                    uses = (
                        set(envelope["kind"]["allowed_uses"])
                        & set(json.loads(uses_raw))
                        & granted.uses
                    )
                except (ValueError, KeyError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained kind definition is invalid."
                    ) from None
                if not uses:
                    limited = True
                    continue
                extraction_rows = owner.db.execute(
                    "SELECT extraction_id FROM extractions WHERE revision_id=? "
                    "ORDER BY extraction_id LIMIT 9",
                    (revision,),
                ).fetchall()
                extractions = [
                    self._extraction_coverage(extraction, kind, granted)
                    for (extraction,) in extraction_rows[:8]
                ]
                revisions.append(
                    SourceRevisionCoverage(
                        revision_id=revision,
                        kind_id=kind,
                        stages=FrozenJsonMap({"capture": "retained"}),
                        extractions=tuple(extractions),
                        extractions_truncated=len(extraction_rows) > 8,
                    )
                )
            return SourceRevisionPage(
                source_id=source_id,
                revisions=tuple(revisions),
                next_cursor=_scoped_cursor(
                    owner, subject, page_rows[-1][0], granted, limit
                )
                if len(rows) > limit and page_rows
                else None,
                scope_limited=limited,
            )

        return owner.run_blocking_sync(read, ExecutionContext())

    def _extraction_coverage(
        self, extraction: str, kind: str, scope: RequestScope
    ) -> SourceExtractionCoverage:
        assert self.owner is not None
        vision_status = {}
        if kind != "code_file@1":
            row = self.owner.db.execute(
                "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM extractions e JOIN artifacts a "
                "ON a.artifact_sha=e.diagnostics_artifact_sha "
                "WHERE e.extraction_id=?",
                (extraction,),
            ).fetchone()
            if row is not None:
                ref = ArtifactRef(
                    sha256=row[0],
                    size_bytes=row[1],
                    media_type=row[2],
                    storage_key=row[3],
                )
                if ref.size_bytes > 65536 or ref.media_type != "application/json":
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Vision capability diagnostic differs."
                    )
                with self.owner.artifacts.open_verified(ref) as stream:
                    payload = stream.read(ref.size_bytes + 1)
                try:
                    diagnostic = VisionCapabilityDiagnostics.model_validate_json(
                        payload
                    )
                    if canonical_bytes(diagnostic.model_dump(mode="json")) != payload:
                        raise ValueError
                except (ValueError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Vision capability diagnostic differs."
                    ) from None
                if diagnostic.gaps:
                    vision_status["vision"] = "incomplete"
                if diagnostic.pdf_coverage is not None:
                    coverage = diagnostic.pdf_coverage
                    vision_status["pdf_processing"] = (
                        "indexed_with_gaps"
                        if coverage.no_text_detected_pages
                        else "processed"
                    )
                    if coverage.ocr_pages:
                        vision_status["ocr"] = "needs_review"
                    vision_status["pdf_coverage"] = coverage.model_dump(mode="json")
        channel_rows = self.owner.db.execute(
            "SELECT f.channel,b.state FROM index_batches b "
            "JOIN index_families f USING(family_id) "
            "WHERE b.extraction_id=? ORDER BY f.channel,f.family_id LIMIT 17",
            (extraction,),
        ).fetchall()
        return SourceExtractionCoverage(
            extraction_id=extraction,
            stages=FrozenJsonMap(
                {"extraction": "retained", **dict(channel_rows[:16]), **vision_status}
            ),
            code_resolution=self._code_resolution(extraction, scope)
            if kind == "code_file@1"
            else None,
            channels_truncated=len(channel_rows) > 16,
        )

    def _visible_revision(
        self, source_id: str, revision_id: str, scope: RequestScope
    ) -> str:
        assert self.owner is not None
        if not self._source_rows(scope, source_id=source_id, limit=1):
            raise TychoraError("NOT_FOUND", "Source is unavailable.")
        owner = self.owner
        granted = owner.scope.narrow(scope)

        def read() -> str:
            row = owner.db.execute(
                "SELECT r.kind_id,k.definition_json,k.definition_hash "
                "FROM revisions r JOIN kinds k USING(kind_id) "
                "WHERE r.source_id=? AND r.revision_id=? AND r.access_rank<=? "
                "AND EXISTS (SELECT 1 FROM json_each(r.allowed_uses_json) ru "
                "JOIN json_each(k.definition_json,'$.kind.allowed_uses') ku "
                "ON ku.value=ru.value JOIN json_each(?) su ON su.value=ru.value) "
                "LIMIT 1",
                (
                    source_id,
                    revision_id,
                    granted.access_ceiling,
                    json.dumps(sorted(granted.uses)),
                ),
            ).fetchone()
            if row is None:
                raise TychoraError("NOT_FOUND", "Revision is unavailable.")
            try:
                envelope = json.loads(row[1])
                recorded = envelope.pop("envelope_digest")
                if (
                    recorded != row[2]
                    or digest("historical-kind-v1", envelope) != recorded
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained kind definition is invalid."
                ) from None
            return row[0]

        return owner.run_blocking_sync(read, ExecutionContext())

    def source_extractions(
        self,
        source_id: str,
        revision_id: str,
        scope: RequestScope,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceExtractionPage:
        if not 1 <= limit <= 20:
            raise TychoraError("INVALID_REQUEST", "Extraction page limit is invalid.")
        kind = self._visible_revision(source_id, revision_id, scope)
        assert self.owner is not None
        owner = self.owner
        granted = owner.scope.narrow(scope)
        subject = f"source-extractions:{source_id}:{revision_id}"
        after = _scoped_after(owner, cursor, subject, granted, limit) or ""

        def read() -> SourceExtractionPage:
            rows = owner.db.execute(
                "SELECT extraction_id FROM extractions WHERE revision_id=? "
                "AND extraction_id>? ORDER BY extraction_id LIMIT ?",
                (revision_id, after, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return SourceExtractionPage(
                source_id=source_id,
                revision_id=revision_id,
                extractions=tuple(
                    self._extraction_coverage(extraction, kind, granted)
                    for (extraction,) in page
                ),
                next_cursor=_scoped_cursor(owner, subject, page[-1][0], granted, limit)
                if len(rows) > limit and page
                else None,
            )

        return owner.run_blocking_sync(read, ExecutionContext())

    def source_channels(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        scope: RequestScope,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> SourceChannelPage:
        if not 1 <= limit <= 100:
            raise TychoraError("INVALID_REQUEST", "Channel page limit is invalid.")
        self._visible_revision(source_id, revision_id, scope)
        assert self.owner is not None
        owner = self.owner
        granted = owner.scope.narrow(scope)
        subject = f"source-channels:{source_id}:{revision_id}:{extraction_id}"
        after = _scoped_after(owner, cursor, subject, granted, limit) or ""

        def read() -> SourceChannelPage:
            if (
                owner.db.execute(
                    "SELECT 1 FROM extractions WHERE revision_id=? AND extraction_id=? LIMIT 1",
                    (revision_id, extraction_id),
                ).fetchone()
                is None
            ):
                raise TychoraError("NOT_FOUND", "Extraction is unavailable.")
            rows = owner.db.execute(
                "SELECT f.family_id,f.channel,b.state FROM index_batches b "
                "JOIN index_families f USING(family_id) WHERE b.extraction_id=? "
                "AND f.family_id>? ORDER BY f.family_id LIMIT ?",
                (extraction_id, after, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return SourceChannelPage(
                source_id=source_id,
                revision_id=revision_id,
                extraction_id=extraction_id,
                channels=tuple(
                    SourceChannelCoverage(
                        family_id=family, channel=channel, state=state
                    )
                    for family, channel, state in page
                ),
                next_cursor=_scoped_cursor(owner, subject, page[-1][0], granted, limit)
                if len(rows) > limit and page
                else None,
            )

        return owner.run_blocking_sync(read, ExecutionContext())

    def source_selections(
        self,
        source_id: str,
        revision_id: str,
        extraction_id: str,
        scope: RequestScope,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceSelectionPage:
        """Page bounded source refs and text fields from a verified raw parser root."""
        from tychora.contracts.documents import DocumentUnit
        from tychora.ingestion.documents import replay_document

        if not 1 <= limit <= 50:
            raise TychoraError("INVALID_REQUEST", "Selection page limit is invalid.")
        self._visible_revision(source_id, revision_id, scope)
        assert self.owner is not None
        owner = self.owner
        granted = owner.scope.narrow(scope)
        subject = f"source-selections:{source_id}:{revision_id}:{extraction_id}"
        after = _scoped_after(owner, cursor, subject, granted, limit) or ""

        def read() -> SourceSelectionPage:
            row = owner.db.execute(
                "SELECT a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM extractions e JOIN artifacts a ON a.artifact_sha=e.document_artifact_sha "
                "WHERE e.extraction_id=? AND e.revision_id=?",
                (extraction_id, revision_id),
            ).fetchone()
            original = owner.db.execute(
                "SELECT a.payload_state FROM revision_artifacts ra "
                "JOIN artifacts a ON a.artifact_sha=ra.artifact_sha "
                "WHERE ra.revision_id=? AND ra.role='original' LIMIT 1",
                (revision_id,),
            ).fetchone()
            if row is None or original is None or original[0] != "present":
                raise TychoraError("NOT_FOUND", "Selectable extraction is unavailable.")

            def read_ref(ref: ArtifactRef) -> bytes:
                if ref.size_bytes > 16 * 1024 * 1024:
                    raise TychoraError(
                        "INVALID_REQUEST", "Selection inspection exceeds its limit."
                    )
                with owner.artifacts.open_verified(ref) as stream:
                    payload = stream.read(ref.size_bytes + 1)
                if len(payload) != ref.size_bytes:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Retained selection artifact differs."
                    )
                return payload

            root_ref = ArtifactRef(
                sha256=row[0], size_bytes=row[1], media_type=row[2], storage_key=row[3]
            )
            root = json.loads(read_ref(root_ref))
            if root.get("data", {}).get("representation") != "document-composition@1":
                raise TychoraError(
                    "NOT_FOUND", "Selectable document extraction is unavailable."
                )
            raw_links = [
                link
                for link in root.get("required_manifests", [])
                if link.get("type") == "extraction"
            ]
            if len(raw_links) != 1:
                raise TychoraError("INTEGRITY_ERROR", "Retained parser root differs.")
            raw_ref = ArtifactRef.model_validate(raw_links[0]["artifact"])
            raw = read_ref(raw_ref)
            raw_data = json.loads(raw)["data"]
            doc_ref = ArtifactRef.model_validate(raw_data["docling_json_ref"])
            parsed = replay_document(raw, read_ref(doc_ref), raw_ref)
            if (
                parsed.registration.source.source_id != source_id
                or parsed.registration.revision.revision_id != revision_id
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained selector lineage differs."
                )
            choices: list[SourceSelectionCandidate] = []
            if parsed.original_image is not None:
                image = parsed.original_image
                choices.append(
                    SourceSelectionCandidate(
                        kind="image",
                        source_native_ref=image.native_ref,
                        width=image.width,
                        height=image.height,
                    )
                )
            else:
                choices.extend(
                    SourceSelectionCandidate(
                        kind="image",
                        source_native_ref=asset.native_ref,
                        width=asset.width,
                        height=asset.height,
                    )
                    for asset in parsed.assets
                    if asset.asset_type in ("page", "picture")
                    and asset.artifact_ref.media_type in ("image/png", "image/jpeg")
                )
            for unit in parsed.units:
                if not isinstance(unit, DocumentUnit):
                    continue
                for field in ("text", "marker"):
                    value = unit.raw_item.get(field)
                    if isinstance(value, str) and value:
                        choices.append(
                            SourceSelectionCandidate(
                                kind="context",
                                source_native_ref=unit.native_ref,
                                field=field,
                                source_text_sha256=hashlib.sha256(
                                    value.encode()
                                ).hexdigest(),
                                source_length=len(value),
                                preview=value[:256],
                            )
                        )
                data = unit.raw_item.get("data")
                cells = data.get("table_cells") if isinstance(data, Mapping) else None
                if isinstance(cells, tuple):
                    for index, cell in enumerate(cells):
                        value = cell.get("text") if isinstance(cell, Mapping) else None
                        if isinstance(value, str) and value:
                            choices.append(
                                SourceSelectionCandidate(
                                    kind="context",
                                    source_native_ref=unit.native_ref,
                                    field="table_cell",
                                    cell_index=index,
                                    source_text_sha256=hashlib.sha256(
                                        value.encode()
                                    ).hexdigest(),
                                    source_length=len(value),
                                    preview=value[:256],
                                )
                            )
                if len(choices) > 100_000:
                    raise TychoraError(
                        "INVALID_REQUEST", "Selection inspection exceeds its limit."
                    )

            def key(item: SourceSelectionCandidate) -> str:
                return f"{item.kind}:{item.source_native_ref}:{item.field or ''}:{item.cell_index if item.cell_index is not None else ''}"

            ordered = sorted(choices, key=key)
            page = [item for item in ordered if key(item) > after][: limit + 1]
            selected = page[:limit]
            return SourceSelectionPage(
                source_id=source_id,
                revision_id=revision_id,
                extraction_id=extraction_id,
                captured_original_sha256=parsed.original_ref.sha256,
                retained_extraction_sha256=parsed.raw_document_ref.sha256,
                candidates=tuple(selected),
                next_cursor=_scoped_cursor(
                    owner, subject, key(selected[-1]), granted, limit
                )
                if len(page) > limit and selected
                else None,
            )

        return owner.run_blocking_sync(read, ExecutionContext())

    def source_revisions(
        self,
        source_id: str,
        scope: RequestScope,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SourceRevisionPage:
        if not self._source_rows(scope, source_id=source_id, limit=1):
            raise TychoraError("NOT_FOUND", "Source is unavailable.")
        return self._revision_page(source_id, scope, cursor=cursor, limit=limit)

    def _source_definitions(
        self,
        scope: RequestScope,
        *,
        after: str = "",
        limit: int = 100,
        source_id: str | None = None,
    ) -> tuple[dict[str, FrozenJsonMap], str | None]:
        assert self.owner is not None
        rows = self._source_rows(scope, after=after, limit=limit, source_id=source_id)
        page_rows = rows if source_id is not None else rows[:limit]
        definitions = {}
        for source, source_key, source_type, collection in page_rows:
            preview = self._revision_page(source, scope, limit=1)
            definitions[source] = FrozenJsonMap(
                {
                    "label": source_key,
                    "collection_id": collection,
                    "source_type": source_type,
                    "stages": {"registration": "registered"},
                    "revisions": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in preview.revisions
                    ],
                    "next_revision_cursor": preview.next_cursor,
                    "scope_limited": preview.scope_limited,
                }
            )
        next_cursor = (
            _scoped_cursor(
                self.owner,
                "sources",
                page_rows[-1][0],
                self.owner.scope.narrow(scope),
                limit,
            )
            if source_id is None and len(rows) > limit and page_rows
            else None
        )
        return definitions, next_cursor

    def _run_definitions(
        self,
        scope: RequestScope,
        *,
        after: str = "",
        limit: int = 100,
        run_id: str | None = None,
    ) -> tuple[dict[str, FrozenJsonMap], str | None]:
        if self.owner is None or self.owner.scope.narrow(scope) != scope:
            return {}, None
        owner = self.owner
        v2_digest, exact_v1_digest = query_run_scope_digests(scope)

        def read():
            selection = "run_id=?" if run_id is not None else "run_id>?"
            rows = owner.db.execute(
                "SELECT run_id,state,created_at,snapshot_id,retained_payload_sha "
                "FROM query_runs WHERE principal_id=? AND scope_digest IN (?,?) AND "
                + selection
                + " ORDER BY run_id LIMIT ?",
                (
                    scope.principal_id,
                    v2_digest,
                    exact_v1_digest,
                    run_id if run_id is not None else after,
                    1 if run_id is not None else limit + 1,
                ),
            ).fetchall()
            page = rows if run_id is not None else rows[:limit]
            definitions = {
                run_id: FrozenJsonMap(
                    {
                        "state": state,
                        "created_at": created_at,
                        "snapshot_id": snapshot_id,
                        "scope_limited": True,
                        "request_retained": retained_sha is not None,
                    }
                )
                for run_id, state, created_at, snapshot_id, retained_sha in page
            }
            next_cursor = (
                _cursor("runs", page[-1][0])
                if run_id is None and len(rows) > limit and page
                else None
            )
            return definitions, next_cursor

        return owner.run_blocking_sync(read, ExecutionContext())

    def _code_resolution(self, extraction: str, scope: RequestScope) -> FrozenJsonMap:
        """Read typed retained rates only inside the current pinned eligibility view."""
        from tychora.contracts.code import Resolution
        from tychora.contracts.requests import QueryRequest
        from tychora.retrieval.eligibility import EligibilityCompiler

        assert self.owner is not None
        owner = self.owner
        with owner.store.pin_snapshot(None) as snapshot:
            if extraction not in {item.extraction_id for item in snapshot.members}:
                return FrozenJsonMap(
                    {"rates": [], "unsupported_references": 0, "scope_limited": True}
                )
            limits = owner.config.values.limits
            preferred = owner.config.values.mode or owner.config.values.default_mode
            readable_modes = [
                name
                for name, definition in owner.store.registry.modes.items()
                if set(
                    cast(
                        tuple[str, ...], cast(FrozenJsonMap, definition)["allowed_uses"]
                    )
                )
                & scope.uses
            ]
            mode = (
                preferred
                if preferred in readable_modes
                else next(iter(sorted(readable_modes)), None)
            )
            if mode is None:
                return FrozenJsonMap(
                    {"rates": [], "unsupported_references": 0, "scope_limited": True}
                )
            request = QueryRequest.from_mapping(
                {
                    "question": "Code resolution discovery",
                    "revision_selector": "all",
                    "mode": mode,
                    "exact_only": True,
                    "limit": min(8, limits.limit),
                    "budgets": {
                        "max_tokens": min(6000, limits.max_tokens),
                        "max_response_bytes": min(1048576, limits.max_response_bytes),
                    },
                }
            )
            compiler = EligibilityCompiler(owner.store)
            view = compiler.compile_eligibility(snapshot, scope, request, owner.config)
            row = owner.db.execute(
                "SELECT e.revision_id,e.diagnostics_artifact_sha,r.source_id "
                "FROM extractions e JOIN revisions r USING(revision_id) "
                "WHERE e.extraction_id=?",
                (extraction,),
            ).fetchone()
            if row is None or row[0] not in view.revision_ids or row[1] is None:
                return FrozenJsonMap(
                    {"rates": [], "unsupported_references": 0, "scope_limited": True}
                )
            ref_row = owner.db.execute(
                "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
                (row[1],),
            ).fetchone()
            if (
                ref_row is None
                or ref_row[2] != row[1]
                or ref_row[3] != "present"
                or ref_row[0] > 16 * 1024 * 1024
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Code resolution artifact is unavailable."
                )
            ref = ArtifactRef(
                sha256=row[1],
                size_bytes=ref_row[0],
                media_type=ref_row[1],
                storage_key=ref_row[2],
            )
            with owner.artifacts.open_verified(ref) as stream:
                try:
                    payload = json.load(stream)
                    raw = payload["data"]["resolutions"]
                    if not isinstance(raw, list) or len(raw) > 100000:
                        raise ValueError
                    records = tuple(Resolution.from_mapping(item) for item in raw)
                except (ValueError, KeyError, TypeError):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Code resolution artifact is invalid."
                    ) from None
            summary = _scoped_resolution_rates(
                records, view.graph_eligible_unit_ids, view.revision_ids, row[2]
            )
            compiler.revalidate_epoch(view, "delivery")
            return summary

    async def kind_metadata_schema(
        self, kind_id: str, scope: RequestScope, ctx: ExecutionContext
    ) -> KindMetadataForm:
        """Expose only a current authorized kind's bounded, retained schema closure."""
        if self.owner is None:
            raise TychoraError("NOT_FOUND", "Kind metadata is unavailable.")
        owner = self.owner

        def read():
            ctx.checkpoint()
            granted = owner.scope.narrow(scope)
            kind = self.registry.kinds.get(kind_id)
            if (
                "read" not in granted.capabilities
                or not granted.collection_ids
                or not isinstance(kind, Mapping)
                or not set(kind.get("allowed_uses", granted.uses)) & granted.uses
                or owner.db.execute(
                    "SELECT 1 FROM collections WHERE workspace_id=? "
                    "AND access_floor<=? AND collection_id IN "
                    "(SELECT value FROM json_each(?)) LIMIT 1",
                    (
                        owner.workspace_id,
                        granted.access_ceiling,
                        json.dumps(sorted(granted.collection_ids)),
                    ),
                ).fetchone()
                is None
            ):
                raise TychoraError("NOT_FOUND", "Kind metadata is unavailable.")
            envelope = cast(dict, self.registry.historical_definitions()[kind_id])
            schema_id = envelope["schema_bindings"]["metadata_schema"]
            schemas = envelope["schemas"]
            nodes = 0

            def count_nodes(node):
                nonlocal nodes
                nodes += 1
                if nodes > 64:
                    raise TychoraError(
                        "BUDGET_TOO_SMALL", "Metadata schema node budget exceeded."
                    )
                if isinstance(node, Mapping):
                    for group in ("properties", "$defs"):
                        for child in node.get(group, {}).values():
                            count_nodes(child)
                    for key in ("items", "additionalProperties"):
                        if key in node:
                            count_nodes(node[key])

            for schema in schemas.values():
                count_nodes(schema)
            root = schemas[schema_id]
            fields = []
            for name, definition in root.get("properties", {}).items():
                types = (
                    definition.get("type", ())
                    if isinstance(definition, Mapping)
                    else ()
                )
                types = (types,) if isinstance(types, str) else tuple(types)
                advanced = (
                    not types
                    or bool(
                        set(types) - {"string", "integer", "number", "boolean", "null"}
                    )
                    or isinstance(definition, Mapping)
                    and "$ref" in definition
                )
                fields.append(
                    KindMetadataField(
                        name=name,
                        required=name in root.get("required", ()),
                        value_types=types,
                        description=definition.get("description", "")
                        if isinstance(definition, Mapping)
                        else "",
                        constraints=FrozenJsonMap(definition)
                        if isinstance(definition, Mapping)
                        else definition,
                        advanced=advanced,
                    )
                )
            form = KindMetadataForm(
                kind_id=kind_id,
                schema_id=schema_id,
                schema_digests=envelope["schema_digests"],
                fields=tuple(fields),
                schemas=schemas,
                schema_reference_bindings=envelope["schema_reference_bindings"],
                # Simple fields describe a closed object only. Preserve every
                # other root constraint for the schema-aware advanced editor.
                advanced=bool(
                    set(root)
                    - {
                        "$id",
                        "$schema",
                        "$defs",
                        "title",
                        "description",
                        "type",
                        "properties",
                        "required",
                        "additionalProperties",
                    }
                )
                or root.get("type") != "object"
                or bool(
                    set(root.get("required", ())) - root.get("properties", {}).keys()
                )
                or root.get("additionalProperties", True) is not False
                or any(field.advanced for field in fields),
            )
            if len(form.model_dump_json().encode("utf-8")) > 256 * 1024:
                raise TychoraError(
                    "BUDGET_TOO_SMALL", "Metadata schema response budget exceeded."
                )
            return form

        return await owner.run_blocking(read, ctx)

    def definition(
        self, subject: str, item_id: str, scope: RequestScope
    ) -> DefinitionEntry:
        """Return one scoped definition with the same schema identity as pages."""
        if subject == "sources":
            definitions, _ = self._source_definitions(scope, source_id=item_id, limit=1)
        elif subject == "runs":
            definitions, _ = self._run_definitions(scope, run_id=item_id, limit=1)
        else:
            definitions = self._definitions(subject)
        value = definitions.get(item_id)
        if value is None:
            raise TychoraError("NOT_FOUND", "Definition is unavailable.")
        return DefinitionEntry(
            id=item_id,
            version=str(value.get("schema_version", 1)),
            label=str(value.get("label", item_id)),
            definition=value,
            schema_digest=digest("discovery-definition-v1", subject, item_id, value),
        )

    def discover(
        self, request: DiscoverRequest | str, scope: RequestScope
    ) -> DiscoveryResult:
        if isinstance(request, str):
            if request not in SUBJECTS:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Unknown discovery subject. Available: " + ", ".join(SUBJECTS),
                )
            request = DiscoverRequest(subject=request)
        subject = request.subject
        if subject == "sources":
            if self.owner is None:
                return DiscoveryResult(subject="sources")
            definitions, next_cursor = self._source_definitions(
                scope,
                after=_scoped_after(
                    self.owner,
                    request.cursor,
                    "sources",
                    self.owner.scope.narrow(scope),
                    request.limit,
                )
                or "",
                limit=request.limit,
            )
            return DiscoveryResult(
                subject="sources",
                definitions=tuple(
                    DefinitionEntry(
                        id=key,
                        version="1",
                        label=str(value.get("label", key)),
                        definition=value,
                        schema_digest=digest(
                            "discovery-definition-v1", "sources", key, value
                        ),
                    )
                    for key, value in definitions.items()
                ),
                next_cursor=next_cursor,
            )
        if subject == "runs":
            definitions, next_cursor = self._run_definitions(
                scope,
                after=_after(request.cursor, "runs") or "",
                limit=request.limit,
            )
            return DiscoveryResult(
                subject="runs",
                definitions=tuple(
                    DefinitionEntry(
                        id=key,
                        version=str(value.get("schema_version", 1)),
                        label=str(value.get("label", key)),
                        definition=value,
                        schema_digest=digest(
                            "discovery-definition-v1", "runs", key, value
                        ),
                    )
                    for key, value in definitions.items()
                ),
                next_cursor=next_cursor,
            )
        definitions = self._definitions(subject)
        counts = self._counts(subject, scope)
        if subject == "kinds":
            definitions = {
                key: FrozenJsonMap(
                    {
                        **dict(value),
                        "schema_ids": cast(
                            dict, self.registry.artifact["definition_schema_bindings"]
                        )["kinds"][key],
                        "normalizer_capabilities": self.registry.normalizers[
                            cast(str, value["normalizer"])
                        ],
                    }
                )
                for key, value in definitions.items()
            }
        entries = sorted(definitions)
        after = _after(request.cursor, subject)
        if after is not None:
            entries = [key for key in entries if key > after]
        page = entries[: request.limit]
        result = tuple(
            DefinitionEntry(
                id=key,
                version=str(definitions[key].get("schema_version", 1)),
                label=str(definitions[key].get("label", key)),
                definition=definitions[key],
                schema_digest=digest(
                    "discovery-definition-v1", subject, key, definitions[key]
                ),
            )
            for key in page
        )
        return DiscoveryResult(
            subject=subject,
            definitions=result,
            counts=tuple(NamedCount(name=key, count=counts.get(key, 0)) for key in page)
            if self.owner is not None and subject == "kinds"
            else (),
            next_cursor=_cursor(subject, page[-1])
            if len(entries) > len(page)
            else None,
            capability_status=_capabilities(self.config)
            if subject in {"settings", "models"}
            else (),
            capabilities=frozenset(
                item.name for item in _capabilities(self.config) if item.installed
            )
            if subject in {"settings", "models"}
            else frozenset(),
        )
