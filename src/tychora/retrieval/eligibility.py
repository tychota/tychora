"""Eligibility compilation and request-local policy boundaries."""

from __future__ import annotations

from tychora.contracts.graph import EligibilityView


def eligible_cache_key(view: EligibilityView) -> tuple[str, int, str, str, str]:
    """Separate eligibility by snapshot, current policy, caller and mode/request."""
    return (
        view.snapshot_id,
        view.policy_epoch,
        view.scope_digest,
        view.eligibility_policy_digest,
        view.eligibility_request_digest,
    )


from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from datetime import date
from types import MappingProxyType
from typing import Literal


@dataclass(frozen=True)
class RevisionFact:
    """Detached selected edition and current canonical authorization facts."""

    source_id: str
    revision_id: str
    extraction_id: str
    collection_id: str
    kind_id: str
    access_rank: int
    source_floor: int
    collection_floor: int
    allowed_uses: frozenset[str]
    review_state: str
    withdrawn: bool
    published_on: str | None
    effective_from: str | None
    effective_until: str | None
    is_default: bool
    kind_allowed_uses: frozenset[str] | None = None
    global_facets: frozenset[str] = frozenset()
    collection_archived: bool = False


def _parsed_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def select_temporal_revisions(
    revisions: tuple[RevisionFact, ...],
    *,
    explicit_ids: tuple[str, ...] = (),
    selector: Literal["current", "all"] = "current",
    as_of: date | None = None,
    cutoff: date | None = None,
    unknown_dates: Literal["strict", "permissive"] = "strict",
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Select retained editions after independently applying both date constraints."""
    selected: set[str] = set()
    warnings: list[str] = []
    grouped: dict[str, list[RevisionFact]] = {}
    explicit = set(explicit_ids)
    for revision in revisions:
        if explicit and revision.revision_id not in explicit:
            continue
        grouped.setdefault(revision.source_id, []).append(revision)
    for source, editions in grouped.items():
        known: list[tuple[date, RevisionFact]] = []
        unknown: list[RevisionFact] = []
        for edition in editions:
            start = _parsed_date(edition.effective_from)
            end = _parsed_date(edition.effective_until)
            published = _parsed_date(edition.published_on)
            if as_of is not None and end is not None and as_of >= end:
                continue
            if cutoff is not None and published is not None and published > cutoff:
                continue
            missing = (
                as_of is not None
                and start is None
                or cutoff is not None
                and published is None
            )
            if missing:
                if unknown_dates == "permissive":
                    unknown.append(edition)
                continue
            if as_of is not None and start is not None and as_of < start:
                continue
            known.append((start or published or date.min, edition))
        if explicit or selector == "all":
            selected.update(item.revision_id for _, item in known)
            selected.update(item.revision_id for item in unknown)
        elif as_of is not None or cutoff is not None:
            if known:
                latest = max(key for key, _ in known)
                winners = [item for key, item in known if key == latest]
                selected.update(item.revision_id for item in winners)
                if len(known) > 1:
                    warnings.append(
                        f"Overlapping retained editions for source {source}"
                    )
            selected.update(item.revision_id for item in unknown)
        else:
            selected.update(item.revision_id for _, item in known if item.is_default)
    return frozenset(selected), tuple(warnings)


from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, cast

from tychora.adapters.ladybug.traversal import resolve_graph_traversal_policy
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.registry import Registry, compile_filter
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, graph_manifest_digest
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.publication import RetainedSlot, SnapshotMember, SnapshotView
from tychora.contracts.requests import (
    AndFilter,
    EqFilter,
    FilterExpr,
    InFilter,
    OrFilter,
    QueryRequest,
    RangeFilter,
    RequestScope,
    canonical_scope_projection,
)
from tychora.ports.execution import ExecutionContext

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.models import ResolvedConfig
    from tychora.ports.repositories import ReadTransaction


@dataclass(frozen=True)
class UnitFact:
    unit_id: str
    revision_id: str
    extraction_id: str
    unit_type: str
    review_state: str
    citable: bool
    dependency_ids: frozenset[str]
    proxy_asset_id: str | None = None
    facets: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ChunkFact:
    chunk_id: str
    unit_id: str
    dependency_ids: frozenset[str]
    context_group_key: str = ""


@dataclass(frozen=True)
class AssetFact:
    asset_id: str
    revision_id: str
    review_state: str
    dependency_ids: frozenset[str]
    source_owner_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VectorFact:
    record_id: str
    chunk_id: str | None = None
    asset_id: str | None = None
    dependency_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EligibilityFacts:
    policy_epoch: int
    revisions: tuple[RevisionFact, ...]
    units: tuple[UnitFact, ...]
    chunks: tuple[ChunkFact, ...]
    assets: tuple[AssetFact, ...]
    vectors: tuple[VectorFact, ...] = ()
    assertion_decisions: tuple[tuple[str, int], ...] = ()
    revision_facets: tuple[tuple[str, str, object], ...] = ()
    historical_definitions: tuple[tuple[str, object], ...] = ()
    slots: tuple[RetainedSlot, ...] = ()


class EligibilityReader(Protocol):
    registry: Registry

    def read_eligibility_facts(
        self,
        snapshot: SnapshotView,
        members: tuple[SnapshotMember, ...] | None = None,
    ) -> EligibilityFacts: ...

    def retained_config(self, snapshot: SnapshotView) -> ResolvedConfig: ...

    def policy_epoch(self, workspace_id: str) -> int: ...

    def pin_snapshot(
        self, selector: str | None
    ) -> AbstractContextManager[SnapshotView]: ...


def _filter_matches(
    expr: FilterExpr,
    registry: Registry,
    source_values: dict[str, object],
    child_values: dict[str, object],
    *,
    include_global: bool,
    global_facets: frozenset[str],
) -> bool:
    if isinstance(expr, AndFilter):
        return all(
            _filter_matches(
                child,
                registry,
                source_values,
                child_values,
                include_global=include_global,
                global_facets=global_facets,
            )
            for child in expr.children
        )
    if isinstance(expr, OrFilter):
        return any(
            _filter_matches(
                child,
                registry,
                source_values,
                child_values,
                include_global=include_global,
                global_facets=global_facets,
            )
            for child in expr.children
        )
    facet = registry.facet(expr.field)
    facet_key = facet["key"]
    assert isinstance(facet_key, str)
    explicit_global = facet_key in global_facets
    source = cast(JsonValue, source_values.get(expr.field))
    child = cast(JsonValue, child_values.get(expr.field))
    if (
        source is None
        and child is not None
        and not str(facet["source_binding"]).startswith("metadata.")
    ):
        source, child = child, None
    if isinstance(expr, EqFilter):
        return registry.facet_matches(
            expr.field,
            expr.value,
            source,
            explicit_global=explicit_global,
            include_global=include_global,
            child_value=child,
        )
    if isinstance(expr, InFilter):
        return any(
            registry.facet_matches(
                expr.field,
                value,
                source,
                explicit_global=explicit_global,
                include_global=include_global,
                child_value=child,
            )
            for value in expr.values
        )
    if isinstance(expr, RangeFilter):
        if source is None:
            return (
                explicit_global
                and include_global
                and registry.facet(expr.field).get("allow_global") is True
            )

        def within(raw: object) -> bool:
            value = cast(Any, raw)
            if expr.lower is not None and (
                value < expr.lower if expr.include_lower else value <= expr.lower
            ):
                return False
            return expr.upper is None or not (
                value > expr.upper if expr.include_upper else value >= expr.upper
            )

        return within(source) and (child is None or within(child))
    raise TychoraError("INVALID_REQUEST", "Unsupported scoped filter")


def _allowed_dependencies(
    dependencies: frozenset[str],
    revisions: AbstractSet[str],
    units: AbstractSet[str],
    assets: AbstractSet[str],
) -> bool:
    return all(
        item in revisions or item in units or item in assets for item in dependencies
    )


def _dependents_index(
    revision_ids: frozenset[str],
    tables: tuple[tuple[bool, Mapping[str, frozenset[str]]], ...],
) -> dict[str, list[tuple[bool, str]]]:
    dependents: dict[str, list[tuple[bool, str]]] = {}
    for is_unit, table in tables:
        for key, dependencies in table.items():
            for item in dependencies - revision_ids:
                dependents.setdefault(item, []).append((is_unit, key))
    return dependents


def _dependency_fixpoint(
    revision_ids: frozenset[str],
    unit_dependencies: Mapping[str, frozenset[str]],
    asset_dependencies: Mapping[str, frozenset[str]],
) -> tuple[frozenset[str], frozenset[str]]:
    """Greatest set of units and assets whose dependencies all stay eligible.

    Removal is monotone, so a worklist reaches the same fixpoint as repeated full
    passes in time linear in the items plus their dependency edges.
    """
    units = set(unit_dependencies)
    assets = set(asset_dependencies)
    tables = ((True, unit_dependencies), (False, asset_dependencies))
    dependents = _dependents_index(revision_ids, tables)
    pending = [
        (is_unit, key)
        for is_unit, table in tables
        for key, dependencies in table.items()
        if not _allowed_dependencies(dependencies, revision_ids, units, assets)
    ]
    while pending:
        is_unit, key = pending.pop()
        members = units if is_unit else assets
        if key in members:
            members.remove(key)
            if key not in units and key not in assets:
                pending.extend(dependents.get(key, ()))
    return frozenset(units), frozenset(assets)


class EligibilityCompiler:
    """Compile one pinned snapshot against detached current canonical facts."""

    def __init__(self, reader: EligibilityReader):
        self.reader = reader
        self._workspace_ids: dict[str, str] = {}

    def compile_eligibility(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        *,
        members: tuple[SnapshotMember, ...] | None = None,
    ) -> EligibilityView:
        facts = self.reader.read_eligibility_facts(snapshot, members)
        return self._compile(snapshot, scope, request, config, facts)[0]

    def compile_attempt(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        *,
        members: tuple[SnapshotMember, ...] | None = None,
        _retained_inspection: bool = False,
    ) -> EligibilityAttempt:
        facts = self.reader.read_eligibility_facts(snapshot, members)
        if _retained_inspection:
            # Exact retained inspection alone: these detached facts are never cached
            # or reused by query/graph/vector admission.
            facts = replace(
                facts,
                revisions=tuple(
                    replace(item, collection_archived=False) for item in facts.revisions
                ),
            )
        view, warnings = self._compile(snapshot, scope, request, config, facts)
        definitions = dict(facts.historical_definitions)
        selected_all_kinds = {
            item.kind_id
            for item in facts.revisions
            if item.revision_id in view.revision_ids
        }
        selected_kinds = selected_all_kinds - set(self.reader.registry.kinds)
        stored_envelopes = {
            kind: FrozenJsonMap(envelope)
            for kind in selected_all_kinds
            if isinstance((envelope := definitions.get(kind)), dict)
        }
        historical_roles: dict[str, str] = {}
        for kind in selected_kinds:
            envelope = definitions.get(kind)
            if not isinstance(envelope, dict):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Selected historical kind definition is unavailable",
                )
            definition = envelope.get("kind")
            role = definition.get("role") if isinstance(definition, dict) else None
            if not isinstance(role, str) or not role:
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Selected historical kind role is unavailable",
                )
            historical_roles[kind] = role
        return EligibilityAttempt(
            view=view,
            compiler=self,
            dependencies=MappingProxyType(_dependency_lookup(view, facts)),
            historical_kind_roles=MappingProxyType(historical_roles),
            historical_kind_envelopes=MappingProxyType(stored_envelopes),
            warnings=warnings,
        )

    def _compile(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        facts: EligibilityFacts,
    ) -> tuple[EligibilityView, tuple[str, ...]]:
        self._workspace_ids[snapshot.snapshot_id] = snapshot.workspace_id
        registry = self.reader.registry
        limits = config.values.limits
        if (
            request.limit > limits.limit
            or request.budgets.max_tokens > limits.max_tokens
            or request.budgets.max_response_bytes > limits.max_response_bytes
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Request exceeds resolved profile limits"
            )
        retained_config_sha = sha256(
            canonical_bytes(
                {
                    "type": "registry",
                    "schema_version": "1",
                    "data": config.model_dump(mode="json"),
                }
            )
        ).hexdigest()
        if registry.registry_digest != config.registry_digest:
            raise TychoraError(
                "INVALID_REQUEST", "Snapshot and resolved profile differ"
            )
        if snapshot.config_sha != retained_config_sha:
            read_retained = getattr(self.reader, "retained_config", None)
            if not callable(read_retained):
                raise TychoraError(
                    "INVALID_REQUEST", "Snapshot and resolved profile differ"
                )
            retained_config = read_retained(snapshot)
            if not pinned_runtime_compatible(retained_config, config):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Pinned snapshot requires publication under current settings.",
                )
        mode_name = request.mode or config.values.mode or config.values.default_mode
        if mode_name is None or mode_name not in registry.modes:
            raise TychoraError("INVALID_REQUEST", "Retrieval mode is unavailable")
        mode = cast(dict[str, Any], registry.modes[mode_name])
        allowed_lanes = frozenset(mode["allowed_lanes"])
        allowed_uses = frozenset(mode["allowed_uses"]) & scope.uses
        original_reviews = frozenset(mode["original_review_states"])
        proxy_policy = mode["derived_proxy_policy"]
        proxy_reviews = frozenset(mode.get("derived_proxy_review_states", ()))
        if proxy_policy == "accepted_only":
            proxy_reviews &= {"accepted"}
        if not mode.get("allow_derived_proxies", True):
            proxy_policy = "disabled"
        selected_filter = (
            compile_filter(request.filters, registry) if request.filters else None
        )
        policy = resolve_graph_traversal_policy(config, registry)
        revisions = tuple(
            item
            for item in facts.revisions
            if item.collection_id in scope.collection_ids
            and not item.withdrawn
            and not item.collection_archived
            and max(item.access_rank, item.source_floor, item.collection_floor)
            <= scope.access_ceiling
            and item.review_state in original_reviews
            and bool(item.allowed_uses & allowed_uses)
            and (
                item.kind_allowed_uses is None
                or bool(item.kind_allowed_uses & allowed_uses)
            )
        )
        if facts.historical_definitions:
            definitions = dict(facts.historical_definitions)
            readable_kinds = frozenset(item.kind_id for item in revisions)
            registry.validate_activation(
                readable_kinds,
                cast(
                    dict[str, JsonValue],
                    {key: definitions[key] for key in readable_kinds},
                ),
            )
        revisions = tuple(
            item
            for item in revisions
            if registry.lane_bindings.get(item.kind_id) in allowed_lanes
        )
        temporal, warnings = select_temporal_revisions(
            revisions,
            explicit_ids=request.revision_ids,
            selector=request.revision_selector,
            as_of=request.as_of,
            cutoff=request.cutoff,
            unknown_dates=request.unknown_dates,
        )
        revision_ids = temporal
        revisions_by_id = {item.revision_id: item for item in facts.revisions}
        units = {
            item.unit_id: item
            for item in facts.units
            if item.revision_id in revision_ids
        }
        assets = {
            item.asset_id: item
            for item in facts.assets
            if item.revision_id in revision_ids
        }
        revision_facets: dict[str, dict[str, object]] = {}
        for revision_id, key, value in facts.revision_facets:
            revision_facets.setdefault(revision_id, {})[key] = value
        permitted_units = frozenset(
            uid
            for uid, item in units.items()
            if (
                item.review_state in proxy_reviews
                if item.proxy_asset_id is not None
                else item.review_state in original_reviews
            )
            and (item.proxy_asset_id is None or proxy_policy != "disabled")
            and (
                selected_filter is None
                or selected_filter.all_units is None
                or _filter_matches(
                    selected_filter.all_units,
                    registry,
                    revision_facets.get(item.revision_id, {}),
                    dict(item.facets),
                    include_global=request.include_global,
                    global_facets=revisions_by_id[item.revision_id].global_facets,
                )
            )
        )
        permitted_assets = frozenset(
            aid for aid, item in assets.items() if item.review_state in original_reviews
        )
        # A dependency is a real canonical leaf, never a replacement revision claim.
        eligible_units, eligible_assets = _dependency_fixpoint(
            revision_ids,
            {uid: units[uid].dependency_ids for uid in permitted_units},
            {aid: assets[aid].dependency_ids for aid in permitted_assets},
        )
        context_source_units = frozenset(
            uid
            for uid in eligible_units
            if units[uid].proxy_asset_id is None and units[uid].citable
        )

        def candidate_matches_unit(item: UnitFact) -> bool:
            return (
                selected_filter is None
                or selected_filter.matched_candidates is None
                or _filter_matches(
                    selected_filter.matched_candidates,
                    registry,
                    revision_facets.get(item.revision_id, {}),
                    dict(item.facets),
                    include_global=request.include_global,
                    global_facets=revisions_by_id[item.revision_id].global_facets,
                )
            )

        source_units = frozenset(
            uid
            for uid in context_source_units
            if units[uid].proxy_asset_id is None
            and units[uid].citable
            and candidate_matches_unit(units[uid])
        )
        structural_units = frozenset(
            uid
            for uid in eligible_units
            if units[uid].proxy_asset_id is None and not units[uid].citable
        )
        proxy_units = frozenset(
            uid for uid in eligible_units if units[uid].proxy_asset_id is not None
        )
        candidate_proxy_units = frozenset(
            uid for uid in proxy_units if candidate_matches_unit(units[uid])
        )
        candidate_assets = frozenset(
            aid
            for aid in eligible_assets
            if selected_filter is None
            or selected_filter.matched_candidates is None
            or any(
                owner in eligible_units
                and owner in assets[aid].dependency_ids
                and units[owner].proxy_asset_id is None
                and candidate_matches_unit(units[owner])
                for owner in assets[aid].source_owner_ids
            )
        )
        context_units = context_source_units | proxy_units
        extraction_ids = {
            item.extraction_id
            for item in facts.revisions
            if item.revision_id in revision_ids
        }
        selected_chunks = {
            row.chunk_id
            for slot in facts.slots
            if slot.slot == "lexical"
            for batch in slot.batches
            if batch.extraction_id in extraction_ids
            for row in batch.lexical_records
        }
        selected_vectors = {
            row.record_id
            for slot in facts.slots
            if slot.slot in {"text", "image"}
            for batch in slot.batches
            if batch.extraction_id in extraction_ids
            for row in batch.vector_records
        }
        chunk_owner_units = source_units | candidate_proxy_units
        eligible_chunks = frozenset(
            item.chunk_id
            for item in facts.chunks
            if item.chunk_id in selected_chunks
            if item.unit_id in chunk_owner_units
            and _allowed_dependencies(
                item.dependency_ids, revision_ids, eligible_units, eligible_assets
            )
        )
        eligible_vectors = frozenset(
            item.record_id
            for item in facts.vectors
            if item.record_id in selected_vectors
            and (item.chunk_id in eligible_chunks or item.asset_id in candidate_assets)
            and item.dependency_ids
            and _allowed_dependencies(
                item.dependency_ids, revision_ids, eligible_units, eligible_assets
            )
        )
        batch_ids: list[str] = []
        graph_binding = None
        graph_assertion_ids: frozenset[str] = frozenset()
        for slot in facts.slots:
            for batch in slot.batches:
                if slot.slot == "graph":
                    if not isinstance(batch.receipt, GraphBatchReceipt):
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Graph batch receipt is unavailable"
                        )
                    if (
                        batch.graph_manifest is None
                        or batch.receipt.manifest_digest
                        != graph_manifest_digest(batch.graph_manifest)
                    ):
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Graph manifest binding differs"
                        )
                    graph_binding = batch.receipt
                    graph_assertion_ids = frozenset(batch.graph_manifest.assertion_ids)
                    batch_ids.append(batch.receipt.generation_id)
                elif batch.extraction_id in extraction_ids:
                    batch_ids.append(batch.batch_id)
        blocked = frozenset(
            assertion
            for assertion, decision in facts.assertion_decisions
            if graph_binding is not None
            and assertion in graph_assertion_ids
            and decision > graph_binding.decision_watermark
        )
        graph_kwargs = (
            {
                "graph_generation_id": graph_binding.generation_id,
                "graph_family_id": graph_binding.family_id,
                "graph_config_sha": graph_binding.config_sha,
                "graph_decision_watermark": graph_binding.decision_watermark,
                "graph_manifest_digest": graph_binding.manifest_digest,
            }
            if graph_binding is not None
            else {}
        )
        view = EligibilityView(
            snapshot_id=snapshot.snapshot_id,
            policy_epoch=facts.policy_epoch,
            scope_digest=digest(
                "eligibility-scope-v2", canonical_scope_projection(scope)
            ),
            eligibility_policy_digest=digest(
                "eligibility-policy-v1",
                config.eligibility_fingerprint,
                mode_name,
                mode,
                policy.model_dump(mode="json"),
            ),
            eligibility_request_digest=digest(
                "eligibility-request-v1",
                request.model_dump(mode="json"),
                selected_filter.model_dump(mode="json") if selected_filter else None,
            ),
            selected_batch_ids=tuple(batch_ids),
            revision_ids=revision_ids,
            source_unit_ids=source_units,
            context_unit_ids=context_units,
            structural_unit_ids=structural_units,
            proxy_unit_ids=proxy_units,
            chunk_ids=eligible_chunks,
            asset_ids=eligible_assets,
            vector_record_ids=eligible_vectors,
            blocked_assertion_ids=blocked,
            **graph_kwargs,
        )
        return view, warnings

    def revalidate_epoch(
        self, view: EligibilityView, stage: Literal["provider", "delivery"]
    ) -> None:
        if stage not in ("provider", "delivery"):
            raise ValueError("Unknown eligibility boundary")
        if (
            self.reader.policy_epoch(self._workspace_ids[view.snapshot_id])
            != view.policy_epoch
        ):
            raise TychoraError(
                "SCOPE_CHANGED",
                "Workspace policy changed during retrieval",
                retryable=True,
            )


from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager


def _dependency_lookup(
    view: EligibilityView, facts: EligibilityFacts
) -> dict[str, frozenset[str]]:
    lookup = {
        item.unit_id: item.dependency_ids
        for item in facts.units
        if item.unit_id in view.context_unit_ids
        or item.unit_id in view.structural_unit_ids
    }
    lookup.update(
        {
            item.chunk_id: item.dependency_ids
            for item in facts.chunks
            if item.chunk_id in view.chunk_ids
        }
    )
    lookup.update(
        {
            item.asset_id: item.dependency_ids
            for item in facts.assets
            if item.asset_id in view.asset_ids
        }
    )
    for item in facts.vectors:
        if item.record_id in view.vector_record_ids:
            owner = item.chunk_id or item.asset_id
            if owner is None or owner not in lookup:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Vector has no eligible owner dependency"
                )
            lookup[item.record_id] = item.dependency_ids
    return lookup


@dataclass(frozen=True)
class EligibilityAttempt:
    """One compiled attempt whose external calls require a fresh policy epoch."""

    view: EligibilityView
    compiler: EligibilityCompiler
    dependencies: Mapping[str, frozenset[str]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    owner: WorkspaceOwner | None = None
    ctx: ExecutionContext | None = None
    snapshot: SnapshotView | None = None
    historical_kind_roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def dependencies_for(self, representation_id: str) -> frozenset[str]:
        try:
            return self.dependencies[representation_id]
        except KeyError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Representation is not eligible"
            ) from None

    async def owner_provider[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Recheck before an existing owner-authorized provider operation."""
        if self.owner is None:
            self.compiler.revalidate_epoch(self.view, "provider")
        else:
            assert self.ctx is not None
            await self.owner.run_write(
                lambda: self.compiler.revalidate_epoch(self.view, "provider"),
                self.ctx,
                "interactive",
            )
        return await call()


async def run_pinned_eligibility[T](
    compiler: EligibilityCompiler,
    selector: str | None,
    scope: RequestScope,
    request: QueryRequest,
    config: ResolvedConfig,
    operation: Callable[[EligibilityAttempt], Awaitable[T]],
    *,
    owner: WorkspaceOwner | None = None,
    ctx: ExecutionContext | None = None,
    on_invalidated: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Hold one pin through both attempts, provider operations and delivery."""
    if owner is not None and (ctx is None or owner.store is not compiler.reader):
        raise ValueError(
            "Owner-backed eligibility requires its bound store and context"
        )
    if owner is not None:
        scope = owner.scope.narrow(scope)
    if owner is not None and ctx is not None:
        catalog = owner.catalog

        def resolve(tx: ReadTransaction) -> SnapshotView:
            snapshot_id = selector or tx.snapshots.active_id()
            if snapshot_id is None:
                raise TychoraError(
                    "NOT_FOUND",
                    "No published snapshot is available",
                    reason="snapshot.absent",
                )
            return catalog.get(tx, snapshot_id)

        resolved: SnapshotView | None = await owner.run_read(resolve, ctx)
        pin = owner.store.leases.pin(frozenset({resolved.snapshot_id}))
    else:
        pin = compiler.reader.pin_snapshot(selector)
        resolved = None
    with pin as pinned:
        snapshot = resolved or pinned
        assert snapshot is not None
        for attempt_number in range(2):
            compiled = (
                await owner.run_write(
                    lambda: compiler.compile_attempt(snapshot, scope, request, config),
                    ctx,
                    "interactive",
                )
                if owner is not None and ctx is not None
                else compiler.compile_attempt(snapshot, scope, request, config)
            )
            attempt = replace(compiled, owner=owner, ctx=ctx, snapshot=snapshot)
            completed_operation = False
            try:
                result = await operation(attempt)
                completed_operation = True
                if owner is not None and ctx is not None:
                    await owner.run_write(
                        lambda view=attempt.view: compiler.revalidate_epoch(
                            view, "delivery"
                        ),
                        ctx,
                        "interactive",
                    )
                else:
                    compiler.revalidate_epoch(attempt.view, "delivery")
            except TychoraError as error:
                if (
                    error.code == "SCOPE_CHANGED"
                    and completed_operation
                    and on_invalidated is not None
                ):
                    await on_invalidated(result)
                if error.code != "SCOPE_CHANGED" or attempt_number:
                    raise
                continue
            return result
        raise AssertionError("Unreachable eligibility retry state")
