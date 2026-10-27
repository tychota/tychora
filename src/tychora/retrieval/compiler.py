"""Compile one pinned snapshot into a revision-level eligibility view."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from tychora.configuration.collection_policy import collection_is_active
from tychora.configuration.global_facets import SOURCE_REF as GLOBAL_FACET_SOURCE_REF
from tychora.configuration.global_facets import (
    binding_bytes as global_facet_binding_bytes,
)
from tychora.configuration.graph_policy import resolve_graph_traversal_policy
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry, ScopedFilter, compile_filter
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView, GraphBinding, RevisionGrant
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt, LegacyEligibilityView
from tychora.contracts.identity import JsonValue, digest
from tychora.contracts.publication import SnapshotMember, SnapshotView
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
from tychora.ports.artifacts import ArtifactReader
from tychora.ports.repositories import FacetValue, MemberRow, ReadTransaction, ReviewRow
from tychora.retrieval.selection import RevisionFact, select_temporal_revisions

if TYPE_CHECKING:
    from tychora.retrieval.cache import EligibilityCache

REVIEW_STATES = ("unreviewed", "needs_review", "accepted", "rejected")
_GLOBAL_BINDING_LIMIT = 65536


class EligibilityKey(NamedTuple):
    """What a compiled view depends on; equal keys share one view."""

    snapshot_id: str
    policy_epoch: int
    scope_digest: str
    policy_digest: str
    request_digest: str


def eligible_cache_key(view: EligibilityView | LegacyEligibilityView) -> EligibilityKey:
    """The key a compiled view answers; a legacy view carries the same five fields."""
    return EligibilityKey(
        view.snapshot_id,
        view.policy_epoch,
        view.scope_digest,
        view.eligibility_policy_digest,
        view.eligibility_request_digest,
    )


@dataclass(frozen=True)
class CompiledEligibility:
    """A view and what compiling it decided about editions and historical kinds."""

    view: EligibilityView
    warnings: tuple[str, ...] = ()
    historical_kind_roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class ModePolicy:
    """A retrieval mode bound to the caller's uses."""

    name: str
    digest: str
    allowed_lanes: frozenset[str]
    allowed_uses: frozenset[str]
    original_reviews: frozenset[str]
    proxy_reviews: frozenset[str]
    proxy_disabled: bool
    # Exact retained inspection alone reads archived collections; never cached.
    include_archived: bool = False


@dataclass(frozen=True)
class _Job:
    """One request bound to its key, mode and compiled filter."""

    key: EligibilityKey
    snapshot: SnapshotView
    scope: RequestScope
    request: QueryRequest
    mode: ModePolicy
    selected: ScopedFilter | None


@dataclass(frozen=True)
class _Kind:
    definition: JsonValue
    envelope: FrozenJsonMap
    allowed_uses: frozenset[str]
    role: str | None


def _invalid(message: str, reason: str) -> TychoraError:
    return TychoraError("INVALID_REQUEST", message, reason=reason)


def _differs(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


def check_request_limits(request: QueryRequest, config: ResolvedConfig) -> None:
    """Refuse a request beyond the profile's limits."""
    limits = config.values.limits
    if (
        request.limit > limits.limit
        or request.budgets.max_tokens > limits.max_tokens
        or request.budgets.max_response_bytes > limits.max_response_bytes
    ):
        raise _invalid(
            "Request exceeds resolved profile limits", "request.limit_exceeded"
        )


def request_digest(request: QueryRequest, selected: ScopedFilter | None) -> str:
    """Digest of the request fields that change what a query may read."""
    return digest(
        "eligibility-request-v2",
        selected.model_dump(mode="json") if selected is not None else None,
        list(request.revision_ids),
        request.revision_selector,
        request.as_of.isoformat() if request.as_of else None,
        request.cutoff.isoformat() if request.cutoff else None,
        request.unknown_dates,
        request.include_global,
    )


def filter_matches(
    expr: FilterExpr,
    registry: Registry,
    source_values: dict[str, object],
    child_values: dict[str, object],
    *,
    include_global: bool,
    global_facets: frozenset[str],
) -> bool:
    """Whether a revision's and a unit's facet values satisfy a scoped filter."""
    if isinstance(expr, AndFilter):
        return all(
            filter_matches(
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
            filter_matches(
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


def _filter_keys(expr: FilterExpr) -> set[str]:
    if isinstance(expr, (AndFilter, OrFilter)):
        return set().union(*(_filter_keys(child) for child in expr.children))
    return {expr.field}


def _grouped(rows: Iterable[FacetValue]) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = defaultdict(dict)
    for row in rows:
        found[row.owner_id][row.facet_key] = row.value
    return found


def _member_rows(
    tx: ReadTransaction,
    snapshot: SnapshotView,
    members: tuple[SnapshotMember, ...] | None,
) -> tuple[MemberRow, ...]:
    """One row per member, with its revision, source, collection and kind facts."""
    if members is not None:
        if not members:
            return ()
        defaults = {m.extraction_id: m.is_default for m in members}
        rows = tuple(
            row._replace(is_default=defaults[row.extraction_id])
            for row in tx.eligibility.members(snapshot.snapshot_id, tuple(defaults))
        )
    else:
        rows = tx.eligibility.members(snapshot.snapshot_id)
        if len(rows) != snapshot.member_count:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Snapshot members lack canonical rows",
                reason="canonical.row_invalid",
            )
    if any(row.workspace_id != snapshot.workspace_id for row in rows):
        raise _differs("Selected extraction belongs to another workspace")
    return rows


def _granted_uses(row: MemberRow) -> frozenset[str]:
    """The revision's uses narrowed by its source's override; an override only narrows."""
    uses = frozenset(json.loads(row.allowed_uses_json))
    if row.allowed_uses_override is not None:
        uses &= frozenset(json.loads(row.allowed_uses_override))
    return uses


def _readable(
    row: MemberRow, kind: _Kind, scope: RequestScope, mode: ModePolicy, active: bool
) -> bool:
    """Revision-level policy: scope, floors, withdrawal, archive, review and uses."""
    uses = _granted_uses(row)
    return (
        row.collection_id in scope.collection_ids
        and not row.withdrawn
        and active
        and max(row.access_rank, row.source_floor, row.collection_floor)
        <= scope.access_ceiling
        and row.review_state in mode.original_reviews
        and bool(uses & mode.allowed_uses)
        and bool(kind.allowed_uses & mode.allowed_uses)
    )


def _collection_admitted(row: MemberRow, mode: ModePolicy) -> bool:
    """An active collection, or any collection for exact retained inspection."""
    return mode.include_archived or collection_is_active(row.collection_policy_json)


def _admitted_members(
    rows: tuple[MemberRow, ...],
    kinds: Mapping[str, _Kind],
    scope: RequestScope,
    mode: ModePolicy,
    registry: Registry,
) -> list[MemberRow]:
    """Members whose revision passes policy and whose kind's lane the mode allows."""
    active: dict[str, bool] = {}
    readable = []
    for row in rows:
        if row.collection_id not in active:
            active[row.collection_id] = _collection_admitted(row, mode)
        if _readable(row, kinds[row.kind_id], scope, mode, active[row.collection_id]):
            readable.append(row)
    readable_kinds = frozenset(row.kind_id for row in readable)
    registry.validate_activation(
        readable_kinds, {kind: kinds[kind].definition for kind in readable_kinds}
    )
    return [
        row
        for row in readable
        if registry.lane_bindings.get(row.kind_id) in mode.allowed_lanes
    ]


def _revision_fact(row: MemberRow, kind: _Kind) -> RevisionFact:
    """Edition-selection facts; selection reads no global facets."""
    return RevisionFact(
        source_id=row.source_id,
        revision_id=row.revision_id,
        extraction_id=row.extraction_id,
        collection_id=row.collection_id,
        kind_id=row.kind_id,
        access_rank=row.access_rank,
        source_floor=row.source_floor,
        collection_floor=row.collection_floor,
        allowed_uses=_granted_uses(row),
        review_state=row.review_state,
        withdrawn=row.withdrawn,
        published_on=row.published_on,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        is_default=row.is_default,
        kind_allowed_uses=kind.allowed_uses,
    )


def _excluded(row: ReviewRow, mode: ModePolicy) -> bool:
    if row.kind == "asset":
        return row.state not in mode.original_reviews
    if row.proxy:
        return mode.proxy_disabled or row.state not in mode.proxy_reviews
    return row.state not in mode.original_reviews


def _exclusions(
    tx: ReadTransaction,
    chosen: list[MemberRow],
    mode: ModePolicy,
    failing: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Units and assets the mode excludes, closed under dependency."""
    extractions = tuple(sorted(row.extraction_id for row in chosen))
    excluded_states = tuple(s for s in REVIEW_STATES if s not in mode.original_reviews)
    reviews = {
        row.target_id: row
        for row in tx.eligibility.review_rows(extractions, excluded_states)
    }
    rejected = tuple(
        sorted(row.extraction_id for row in chosen if row.review_state == "rejected")
    )
    if rejected:
        # A rejected revision's units and assets are rejected whatever their own reviews.
        for row in tx.eligibility.review_rows(rejected, REVIEW_STATES):
            reviews[row.target_id] = row._replace(state="rejected")
    seeds = set(failing) | {
        target for target, row in reviews.items() if _excluded(row, mode)
    }
    closure = tx.eligibility.dependents(tuple(sorted(seeds)))
    return (
        frozenset(i for i, kind in closure.items() if kind == "unit"),
        frozenset(i for i, kind in closure.items() if kind == "asset"),
    )


def _selected_batches(
    rows: Iterable[tuple[str, str, str | None]], extraction_ids: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = defaultdict(list)
    for slot, batch, extraction in rows:
        if slot == "graph" or extraction in extraction_ids:
            found[slot].append(batch)
    return {slot: tuple(batches) for slot, batches in found.items()}


def _graph_binding(
    tx: ReadTransaction, batches: Mapping[str, tuple[str, ...]], all_eligible: bool
) -> GraphBinding | None:
    graph = batches.get("graph", ())
    if not graph:
        return None
    if len(graph) != 1:
        raise _differs("Snapshot binds more than one graph generation")
    receipt = tx.batches.receipt(graph[0])
    if not isinstance(receipt, GraphBatchReceipt):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Graph batch receipt is unavailable",
            reason="canonical.row_invalid",
        )
    return GraphBinding(
        family_id=receipt.family_id, all_eligible=all_eligible, receipt=receipt
    )


def _historical(
    registry: Registry, kinds: Mapping[str, _Kind], chosen: list[MemberRow]
) -> tuple[Mapping[str, str], Mapping[str, FrozenJsonMap]]:
    """Roles of selected kinds the current registry lacks, and every selected envelope."""
    selected = {row.kind_id for row in chosen}
    roles = {}
    for kind_id in sorted(selected - set(registry.kinds)):
        role = kinds[kind_id].role
        if not role:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Selected historical kind role is unavailable",
                reason="kind.historical_unavailable",
            )
        roles[kind_id] = role
    return (
        MappingProxyType(roles),
        MappingProxyType({kind: kinds[kind].envelope for kind in selected}),
    )


class EligibilityCompiler:
    """Compiles revision-level views; kind envelopes and mode policies are cached."""

    def __init__(self, registry: Registry, artifacts: ArtifactReader) -> None:
        self.registry = registry
        self.artifacts = artifacts
        self._kinds: dict[tuple[str, str], _Kind] = {}
        self._modes: dict[tuple[str, str, frozenset[str]], ModePolicy] = {}
        self._compatible: set[tuple[str, str]] = set()
        self._lock = Lock()

    def mode(
        self, config: ResolvedConfig, request: QueryRequest, scope: RequestScope
    ) -> ModePolicy:
        """The request's mode bound to the scope's uses, with its policy digest."""
        name = request.mode or config.values.mode or config.values.default_mode
        if name is None or name not in self.registry.modes:
            raise _invalid("Retrieval mode is unavailable", "mode.unavailable")
        key = (config.config_sha, name, scope.uses)
        with self._lock:
            cached = self._modes.get(key)
        if cached is not None:
            return cached
        definition = cast(Mapping[str, Any], self.registry.modes[name])
        policy = resolve_graph_traversal_policy(config, self.registry)
        proxy_reviews = frozenset(definition.get("derived_proxy_review_states", ()))
        if definition["derived_proxy_policy"] == "accepted_only":
            proxy_reviews &= {"accepted"}
        mode = ModePolicy(
            name=name,
            digest=digest(
                "eligibility-policy-v1",
                config.eligibility_fingerprint,
                name,
                definition,
                policy.model_dump(mode="json"),
            ),
            allowed_lanes=frozenset(definition["allowed_lanes"]),
            allowed_uses=frozenset(definition["allowed_uses"]) & scope.uses,
            original_reviews=frozenset(definition["original_review_states"]),
            proxy_reviews=proxy_reviews,
            proxy_disabled=definition["derived_proxy_policy"] == "disabled"
            or not definition.get("allow_derived_proxies", True),
        )
        with self._lock:
            self._modes[key] = mode
        return mode

    def knows_compatible(self, snapshot: SnapshotView, config: ResolvedConfig) -> bool:
        """Whether the snapshot's retained configuration is known to serve ``config``."""
        with self._lock:
            return (
                snapshot.config_sha == config.retained_sha
                or (
                    snapshot.config_sha,
                    config.config_sha,
                )
                in self._compatible
            )

    def mark_compatible(
        self, snapshot: SnapshotView, config: ResolvedConfig, retained: ResolvedConfig
    ) -> None:
        """Record that the retained configuration serves ``config``, or refuse it."""
        if not pinned_runtime_compatible(retained, config):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Pinned snapshot requires publication under current settings.",
                reason="index.rebuild_required",
            )
        with self._lock:
            self._compatible.add((snapshot.config_sha, config.config_sha))

    def ensure_compatible(
        self,
        snapshot: SnapshotView,
        config: ResolvedConfig,
        read_retained: Callable[[], ResolvedConfig],
    ) -> None:
        """Check the snapshot's retained configuration once per configuration pair."""
        if not self.knows_compatible(snapshot, config):
            self.mark_compatible(snapshot, config, read_retained())

    def key(
        self,
        tx: ReadTransaction,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
    ) -> tuple[EligibilityKey, ModePolicy, ScopedFilter | None]:
        """The cache key of this request, with its mode and compiled filter."""
        check_request_limits(request, config)
        if self.registry.registry_digest != config.registry_digest:
            raise _invalid(
                "Snapshot and resolved profile differ", "profile.snapshot_mismatch"
            )
        if not self.knows_compatible(snapshot, config):
            raise _invalid(
                "Snapshot configuration was not checked against the profile",
                "profile.snapshot_mismatch",
            )
        mode = self.mode(config, request, scope)
        selected = (
            compile_filter(request.filters, self.registry) if request.filters else None
        )
        return (
            EligibilityKey(
                snapshot.snapshot_id,
                tx.workspace.policy_epoch(),
                digest("eligibility-scope-v2", canonical_scope_projection(scope)),
                mode.digest,
                request_digest(request, selected),
            ),
            mode,
            selected,
        )

    def compile(
        self,
        tx: ReadTransaction,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        *,
        members: tuple[SnapshotMember, ...] | None = None,
        include_archived: bool = False,
    ) -> CompiledEligibility:
        """Compile without the cache (explicit retained members are never cached).

        ``include_archived`` admits archived collections, for exact retained
        inspection only; its policy digest differs from the mode's own.
        """
        key, mode, selected = self.key(tx, snapshot, scope, request, config)
        if include_archived:
            mode = replace(
                mode,
                digest=digest("eligibility-policy-archived-v1", mode.digest),
                include_archived=True,
            )
            key = key._replace(policy_digest=mode.digest)
        job = _Job(key, snapshot, scope, request, mode, selected)
        return self._compile(tx, job, members)

    def compile_cached(
        self,
        tx: ReadTransaction,
        cache: EligibilityCache,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
    ) -> CompiledEligibility:
        """Return the cached view for this key, compiling it once on a miss."""
        key, mode, selected = self.key(tx, snapshot, scope, request, config)
        job = _Job(key, snapshot, scope, request, mode, selected)
        return cache.get_or_compile(key, lambda: self._compile(tx, job, None))

    def _compile(
        self,
        tx: ReadTransaction,
        job: _Job,
        members: tuple[SnapshotMember, ...] | None,
    ) -> CompiledEligibility:
        key, snapshot, request = job.key, job.snapshot, job.request
        rows = _member_rows(tx, snapshot, members)
        kinds = self._kind_envelopes(tx, rows)
        admitted = _admitted_members(rows, kinds, job.scope, job.mode, self.registry)
        selected_ids, warnings = select_temporal_revisions(
            tuple(_revision_fact(r, kinds[r.kind_id]) for r in admitted),
            explicit_ids=request.revision_ids,
            selector=request.revision_selector,
            as_of=request.as_of,
            cutoff=request.cutoff,
            unknown_dates=request.unknown_dates,
        )
        chosen = [row for row in admitted if row.revision_id in selected_ids]
        # Global facets matter only to filters, and only for the chosen editions.
        facets = self._global_facets(tx, chosen) if job.selected is not None else {}
        grants = {
            row.revision_id: RevisionGrant(
                extraction_id=row.extraction_id,
                source_id=row.source_id,
                kind_id=row.kind_id,
                lane=cast(str, self.registry.lane_bindings[row.kind_id]),
                collection_id=row.collection_id,
            )
            for row in chosen
        }
        failing, unit_filter = self._filter_sets(
            tx, job.selected, job.request, chosen, facets
        )
        units, assets = _exclusions(tx, chosen, job.mode, failing)
        batches = _selected_batches(
            tx.eligibility.batches(snapshot.snapshot_id),
            frozenset(g.extraction_id for g in grants.values()),
        )
        complete = (
            len(grants) == snapshot.member_count
            and not units
            and not assets
            and unit_filter is None
        )
        view = EligibilityView(
            snapshot_id=key.snapshot_id,
            policy_epoch=key.policy_epoch,
            scope_digest=key.scope_digest,
            eligibility_policy_digest=key.policy_digest,
            eligibility_request_digest=key.request_digest,
            revisions=grants,
            excluded_unit_ids=units,
            excluded_asset_ids=assets,
            unit_filter=unit_filter,
            selected_batches=batches,
            graph=_graph_binding(tx, batches, complete),
            all_members_eligible=len(grants) == snapshot.member_count,
        )
        roles, envelopes = _historical(self.registry, kinds, chosen)
        return CompiledEligibility(view, warnings, roles, envelopes)

    def _filter_sets(
        self,
        tx: ReadTransaction,
        selected: ScopedFilter | None,
        request: QueryRequest,
        chosen: list[MemberRow],
        global_facets: Mapping[str, frozenset[str]],
    ) -> tuple[frozenset[str], frozenset[str] | None]:
        """Units failing the all-units filter, and units passing the candidate filter."""
        if selected is None or (
            selected.all_units is None and selected.matched_candidates is None
        ):
            return frozenset(), None
        revision_of = {row.extraction_id: row.revision_id for row in chosen}
        keys: set[str] = set()
        for expr in (selected.all_units, selected.matched_candidates):
            if expr is not None:
                keys |= _filter_keys(expr)
        facet_keys = tuple(sorted(keys))
        extractions = tuple(sorted(revision_of))
        units = tx.eligibility.unit_flags(extractions)
        by_unit = _grouped(tx.eligibility.unit_facets(extractions, facet_keys))
        by_revision = _grouped(
            tx.eligibility.revision_facets(
                tuple(sorted(revision_of.values())), facet_keys
            )
        )

        def matches(expr: FilterExpr, unit_id: str, extraction_id: str) -> bool:
            revision = revision_of[extraction_id]
            return filter_matches(
                expr,
                self.registry,
                by_revision.get(revision, {}),
                by_unit.get(unit_id, {}),
                include_global=request.include_global,
                global_facets=global_facets.get(revision, frozenset()),
            )

        failing = frozenset(
            u.unit_id
            for u in units
            if selected.all_units is not None
            and not matches(selected.all_units, u.unit_id, u.extraction_id)
        )
        if selected.matched_candidates is None:
            return failing, None
        return failing, frozenset(
            u.unit_id
            for u in units
            if matches(selected.matched_candidates, u.unit_id, u.extraction_id)
        )

    def _kind_envelopes(
        self, tx: ReadTransaction, rows: Iterable[MemberRow]
    ) -> dict[str, _Kind]:
        """Each member kind's verified envelope, cached by kind and definition hash."""
        needed = {(row.kind_id, row.definition_hash) for row in rows}
        with self._lock:
            missing = sorted(
                {kind for kind, h in needed if (kind, h) not in self._kinds}
            )
        for kind_id, definition_hash, raw in tx.eligibility.kind_definitions(
            tuple(missing)
        ):
            envelope = json.loads(raw)
            recorded = envelope.pop("envelope_digest", None)
            if (
                recorded != definition_hash
                or digest("historical-kind-v1", envelope) != recorded
            ):
                raise _differs("Original kind envelope differs from canonical binding")
            definition = {**envelope, "envelope_digest": recorded}
            body = envelope.get("kind") or {}
            with self._lock:
                self._kinds[(kind_id, definition_hash)] = _Kind(
                    definition=cast(JsonValue, definition),
                    envelope=FrozenJsonMap(definition),
                    allowed_uses=frozenset(body.get("allowed_uses", ())),
                    role=body.get("role"),
                )
        with self._lock:
            return {kind: self._kinds[(kind, h)] for kind, h in needed}

    def _global_facets(
        self, tx: ReadTransaction, chosen: list[MemberRow]
    ) -> dict[str, frozenset[str]]:
        """Verified global facets of the chosen revisions; read only for filters."""
        revisions = tuple(sorted(row.revision_id for row in chosen))
        markers: dict[str, list] = defaultdict(list)
        for revision, source_ref, ref in tx.eligibility.global_facet_markers(revisions):
            markers[revision].append((source_ref, ref))
        found: dict[str, frozenset[str]] = {}
        for revision, raw, metadata_sha in tx.eligibility.revision_metadata(revisions):
            metadata = FrozenJsonMap(json.loads(raw))
            if digest("metadata-v1", metadata) != metadata_sha:
                raise _differs("Selected metadata differs from immutable digest")
            facets, binding = global_facet_binding_bytes(
                metadata, revision, metadata_sha, self.registry
            )
            self._verify_binding(binding, markers.get(revision, []))
            found[revision] = facets
        return found

    def _verify_binding(self, binding: bytes | None, rows: list) -> None:
        unverified = TychoraError(
            "PROFILE_INCOMPATIBLE",
            "Retained global facet lacks a verified binding; publish a new edition",
            reason="facet.binding_unverified",
        )
        if binding is None:
            if rows:
                raise _differs("Undeclared global facet binding exists")
            return
        if (
            len(rows) != 1
            or rows[0][0] != GLOBAL_FACET_SOURCE_REF
            or rows[0][1] is None
            or rows[0][1].size_bytes > _GLOBAL_BINDING_LIMIT
        ):
            raise unverified
        try:
            with self.artifacts.open_verified(rows[0][1]) as stream:
                if stream.read(_GLOBAL_BINDING_LIMIT + 1) != binding:
                    raise unverified
        except OSError as error:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Global facet binding payload is unavailable",
                reason="payload.missing",
            ) from error
