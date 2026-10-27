"""Eligibility compilation and request-local policy boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from tychora.configuration.graph_policy import resolve_graph_traversal_policy
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.registry import Registry, compile_filter
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, LegacyEligibilityView
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.publication import SnapshotMember, SnapshotView
from tychora.contracts.requests import (
    QueryRequest,
    RequestScope,
    canonical_scope_projection,
)
from tychora.retrieval.compiler import filter_matches
from tychora.retrieval.selection import RevisionFact, select_temporal_revisions

if TYPE_CHECKING:
    from tychora.configuration.models import ResolvedConfig


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
    batches: tuple[tuple[str, str, str | None], ...] = ()
    lexical_chunks: tuple[tuple[str, str], ...] = ()
    graph_receipt: GraphBatchReceipt | None = None


class EligibilityReader(Protocol):
    registry: Registry

    def read_eligibility_facts(
        self,
        snapshot: SnapshotView,
        members: tuple[SnapshotMember, ...] | None = None,
    ) -> EligibilityFacts: ...

    def retained_config(self, snapshot: SnapshotView) -> ResolvedConfig: ...

    def policy_epoch(self, workspace_id: str) -> int: ...


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


class LegacyEligibilityCompiler:
    """Compile one pinned snapshot against detached current canonical facts."""

    def __init__(self, reader: EligibilityReader):
        self.reader = reader
        self._workspace_ids: dict[str, str] = {}

    def compile_attempt(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        *,
        members: tuple[SnapshotMember, ...] | None = None,
        _retained_inspection: bool = False,
    ) -> LegacyAttempt:
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
        return LegacyAttempt(
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
    ) -> tuple[LegacyEligibilityView, tuple[str, ...]]:
        self._workspace_ids[snapshot.snapshot_id] = snapshot.workspace_id
        registry = self.reader.registry
        limits = config.values.limits
        if (
            request.limit > limits.limit
            or request.budgets.max_tokens > limits.max_tokens
            or request.budgets.max_response_bytes > limits.max_response_bytes
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Request exceeds resolved profile limits",
                reason="request.limit_exceeded",
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
                "INVALID_REQUEST",
                "Snapshot and resolved profile differ",
                reason="profile.snapshot_mismatch",
            )
        if snapshot.config_sha != retained_config_sha:
            read_retained = getattr(self.reader, "retained_config", None)
            if not callable(read_retained):
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Snapshot and resolved profile differ",
                    reason="profile.snapshot_mismatch",
                )
            retained_config = read_retained(snapshot)
            if not pinned_runtime_compatible(retained_config, config):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE",
                    "Pinned snapshot requires publication under current settings.",
                    reason="index.rebuild_required",
                )
        mode_name = request.mode or config.values.mode or config.values.default_mode
        if mode_name is None or mode_name not in registry.modes:
            raise TychoraError(
                "INVALID_REQUEST",
                "Retrieval mode is unavailable",
                reason="mode.unavailable",
            )
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
                or filter_matches(
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
                or filter_matches(
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
            chunk
            for chunk, extraction in facts.lexical_chunks
            if extraction in extraction_ids
        }
        selected_vectors = {item.record_id for item in facts.vectors}
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
        graph_binding = facts.graph_receipt
        batch_ids = [
            batch
            for slot, batch, extraction in facts.batches
            if slot != "graph" and extraction in extraction_ids
        ]
        if graph_binding is not None:
            batch_ids.append(graph_binding.generation_id)
        # Every assertion decided after the generation's watermark is blocked;
        # an assertion outside the generation is never reached by traversal.
        blocked = frozenset(
            assertion
            for assertion, decision in facts.assertion_decisions
            if graph_binding is not None and decision > graph_binding.decision_watermark
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
        view = LegacyEligibilityView(
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
        self, view: LegacyEligibilityView, stage: Literal["provider", "delivery"]
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


def _dependency_lookup(
    view: LegacyEligibilityView, facts: EligibilityFacts
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
class LegacyAttempt:
    """One unit-level compiled attempt and the dependency sets of what it admits."""

    view: LegacyEligibilityView
    compiler: LegacyEligibilityCompiler
    dependencies: Mapping[str, frozenset[str]]
    warnings: tuple[str, ...]
    historical_kind_roles: Mapping[str, str]
    historical_kind_envelopes: Mapping[str, FrozenJsonMap]

    def dependencies_for(self, representation_id: str) -> frozenset[str]:
        """The dependency set of an eligible representation."""
        try:
            return self.dependencies[representation_id]
        except KeyError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Representation is not eligible"
            ) from None
