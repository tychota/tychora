"""Detached vocabulary discovery and separately authorized corpus counts."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from importlib import metadata
from typing import TYPE_CHECKING, cast

from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.operations import (
    CapabilityEntry,
    DefinitionEntry,
    DiscoverRequest,
    DiscoveryResult,
    NamedCount,
    SourceExtractionCoverage,
    SourceRevisionCoverage,
)
from tychora.contracts.requests import RequestScope
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
                "WHERE c.workspace_id=? ORDER BY r.revision_id",
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

    def _source_definitions(self, scope: RequestScope) -> dict[str, FrozenJsonMap]:
        if self.owner is None:
            return {}
        owner = self.owner
        granted = owner.scope.narrow(scope)
        if "read" not in granted.capabilities:
            return {}

        def read() -> dict[str, FrozenJsonMap]:
            definitions = {}
            sources = owner.db.execute(
                "SELECT s.source_id,s.source_key,s.source_type,s.collection_id,"
                "s.access_floor,s.withdrawn_at,c.access_floor FROM sources s "
                "JOIN collections c USING(collection_id) WHERE c.workspace_id=? "
                "ORDER BY s.source_id",
                (owner.workspace_id,),
            ).fetchall()
            for (
                source,
                source_key,
                source_type,
                collection,
                source_floor,
                withdrawn,
                collection_floor,
            ) in sources:
                if (
                    collection not in granted.collection_ids
                    or max(source_floor, collection_floor) > granted.access_ceiling
                    or withdrawn is not None
                ):
                    continue
                revisions = []
                limited = False
                rows = owner.db.execute(
                    "SELECT r.revision_id,r.kind_id,r.access_rank,r.allowed_uses_json,"
                    "k.definition_json,k.definition_hash FROM revisions r "
                    "JOIN kinds k USING(kind_id) WHERE r.source_id=? ORDER BY r.revision_id",
                    (source,),
                )
                for (
                    revision,
                    kind,
                    access,
                    uses_raw,
                    definition_raw,
                    definition_hash,
                ) in rows:
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
                    extractions: list[SourceExtractionCoverage] = []
                    for (extraction,) in owner.db.execute(
                        "SELECT extraction_id FROM extractions WHERE revision_id=? "
                        "ORDER BY extraction_id",
                        (revision,),
                    ):
                        channels = {
                            channel: state
                            for channel, state in owner.db.execute(
                                "SELECT f.channel,b.state FROM index_batches b "
                                "JOIN index_families f USING(family_id) "
                                "WHERE b.extraction_id=? ORDER BY f.channel",
                                (extraction,),
                            )
                        }
                        extractions.append(
                            SourceExtractionCoverage(
                                extraction_id=extraction,
                                stages=FrozenJsonMap(
                                    {"extraction": "retained", **channels}
                                ),
                                code_resolution=self._code_resolution(
                                    extraction, granted
                                )
                                if kind == "code_file@1"
                                else None,
                            )
                        )
                    revisions.append(
                        SourceRevisionCoverage(
                            revision_id=revision,
                            kind_id=kind,
                            stages=FrozenJsonMap({"capture": "retained"}),
                            extractions=tuple(extractions),
                        ).model_dump(mode="json", exclude_none=True)
                    )
                definitions[source] = FrozenJsonMap(
                    {
                        "label": source_key,
                        "collection_id": collection,
                        "source_type": source_type,
                        "stages": {"registration": "registered"},
                        "revisions": revisions,
                        "scope_limited": limited,
                    }
                )
            return definitions

        return owner.run_blocking_sync(read, ExecutionContext())

    def _run_definitions(self, scope: RequestScope) -> dict[str, FrozenJsonMap]:
        if self.owner is None or self.owner.scope.narrow(scope) != scope:
            return {}
        owner = self.owner

        def read() -> dict[str, FrozenJsonMap]:
            rows = owner.db.execute(
                "SELECT run_id,state,created_at,snapshot_id,retained_payload_sha "
                "FROM query_runs WHERE principal_id=? AND scope_digest=? ORDER BY run_id",
                (
                    scope.principal_id,
                    digest("query-run-scope-v1", scope.model_dump(mode="json")),
                ),
            )
            return {
                run_id: FrozenJsonMap(
                    {
                        "state": state,
                        "created_at": created_at,
                        "snapshot_id": snapshot_id,
                        "scope_limited": True,
                        "request_retained": retained_sha is not None,
                    }
                )
                for run_id, state, created_at, snapshot_id, retained_sha in rows
            }

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
        definitions = self._definitions(subject)
        if subject == "sources":
            definitions = self._source_definitions(scope)
        elif subject == "runs":
            definitions = self._run_definitions(scope)
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
