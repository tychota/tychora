"""The unit-level eligibility compiler v1 used, kept to check the revision-level one."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic import model_validator

from tychora.adapters.sqlite.repositories import (
    SqliteBatchRepository,
    SqliteRepresentationRepository,
    SqliteWorkspaceRepository,
    artifact_ref,
)
from tychora.adapters.sqlite.repositories.errors import facts_missing
from tychora.configuration.collection_policy import collection_is_active
from tychora.configuration.global_facets import (
    ROLE as GLOBAL_FACET_ROLE,
)
from tychora.configuration.global_facets import (
    SOURCE_REF as GLOBAL_FACET_SOURCE_REF,
)
from tychora.configuration.global_facets import (
    binding_bytes as global_facet_binding_bytes,
)
from tychora.configuration.graph_policy import resolve_graph_traversal_policy
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.registry import compile_filter
from tychora.contracts.base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonNegativeInt,
    Sha256,
)
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.publication import SnapshotMember, SnapshotView
from tychora.contracts.requests import (
    QueryRequest,
    RequestScope,
    canonical_scope_projection,
)
from tychora.retrieval.compiler import FacetSubject, filter_matches
from tychora.retrieval.selection import (
    RevisionFact,
    TemporalSelector,
    select_temporal_revisions,
)

if TYPE_CHECKING:
    from tychora.adapters.sqlite.corpus import CorpusStore
    from tychora.configuration.models import ResolvedConfig


class OracleView(Contract):
    """Pinned request eligibility; graph bindings are absent together when disabled."""

    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    scope_digest: Sha256
    eligibility_policy_digest: Sha256
    eligibility_request_digest: Sha256
    selected_batch_ids: tuple[Identifier, ...]
    revision_ids: frozenset[Identifier]
    source_unit_ids: frozenset[Identifier]
    context_unit_ids: frozenset[Identifier]
    structural_unit_ids: frozenset[Identifier]
    proxy_unit_ids: frozenset[Identifier]
    chunk_ids: frozenset[Identifier]
    asset_ids: frozenset[Identifier]
    vector_record_ids: frozenset[Identifier]
    blocked_assertion_ids: frozenset[Sha256]
    graph_generation_id: Identifier | None = None
    graph_family_id: Sha256 | None = None
    graph_config_sha: Sha256 | None = None
    graph_decision_watermark: NonNegativeInt | None = None
    graph_manifest_digest: Sha256 | None = None

    @model_validator(mode="after")
    def graph_binding(self) -> Self:
        fields = (
            self.graph_generation_id,
            self.graph_family_id,
            self.graph_config_sha,
            self.graph_decision_watermark,
            self.graph_manifest_digest,
        )
        if any(value is None for value in fields) and any(
            value is not None for value in fields
        ):
            raise ValueError("Graph ready bindings must be present or absent together")
        if not self.source_unit_ids <= self.context_unit_ids:
            raise ValueError("Candidate sources must be context eligible")
        if self.context_unit_ids & self.structural_unit_ids:
            raise ValueError("Structural and source context must be separate")
        if (
            self.graph_generation_id is not None
            and self.graph_generation_id not in self.selected_batch_ids
        ):
            raise ValueError("Selected graph generation is absent from ready batches")
        return self

    @property
    def graph_eligible_unit_ids(self) -> frozenset[str]:
        return self.context_unit_ids | self.structural_unit_ids


@dataclass(frozen=True)
class OracleRevisionFact(RevisionFact):
    """A revision fact with the fields only v1 carried on it."""

    global_facets: frozenset[str] = frozenset()
    collection_archived: bool = False


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
    revisions: tuple[OracleRevisionFact, ...]
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


class OracleCompiler:
    """Compile one pinned snapshot against detached current canonical facts."""

    def __init__(self, store: CorpusStore):
        self.store = store

    def compile_eligibility(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
    ) -> OracleView:
        facts = read_eligibility_facts(self.store, snapshot)
        return self._compile(snapshot, scope, request, config, facts)[0]

    def _compile(
        self,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        facts: EligibilityFacts,
    ) -> tuple[OracleView, tuple[str, ...]]:
        registry = self.store.registry
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
            read_retained = getattr(self.store, "retained_config", None)
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
            revisions, TemporalSelector.of(request)
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
                    FacetSubject(
                        revision_facets.get(item.revision_id, {}),
                        dict(item.facets),
                        request.include_global,
                        revisions_by_id[item.revision_id].global_facets,
                    ),
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
                    FacetSubject(
                        revision_facets.get(item.revision_id, {}),
                        dict(item.facets),
                        request.include_global,
                        revisions_by_id[item.revision_id].global_facets,
                    ),
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
        view = OracleView(
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


def overridden_uses(uses: frozenset[str], stored: str | None) -> frozenset[str]:
    """A revision's uses narrowed by its source's stored override, if any.

    ``stored`` is the JSON list ``set_allowed_uses_override`` writes, or
    ``None`` when the source has no override; an override only narrows.
    """
    if stored is None:
        return uses
    return uses & frozenset(json.loads(stored))


def _integrity(
    message: str, reason: str = "canonical.binding_mismatch"
) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason=reason)


def _persisted(db, owner_ids: tuple[str, ...]) -> dict[str, frozenset[str]]:
    """The stored dependency set of every owner; any absent set is refused."""
    found = SqliteRepresentationRepository(db).dependencies(owner_ids)
    if len(found) != len(set(owner_ids)) or not all(found.values()):
        raise facts_missing()
    return dict(found)


def _unit_facets(db, unit_id: str) -> tuple[tuple[str, object], ...]:
    """A unit's facet values, each as its one typed value."""
    return tuple(
        (key, text if text is not None else boolean if boolean is not None else number)
        for key, text, number, boolean in db.execute(
            "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values "
            "WHERE unit_id=?",
            (unit_id,),
        )
    )


_SNAPSHOT_BATCHES = (
    "SELECT sb.slot,sb.batch_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
    "ORDER BY sb.slot,sb.batch_id"
)
_LEXICAL_CHUNKS = (
    "SELECT r.chunk_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN lexical_records r ON r.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot='lexical'"
)
_VECTOR_ROWS = (
    "SELECT v.record_id,v.chunk_id,v.asset_id,v.group_id,b.extraction_id "
    "FROM snapshot_batches sb JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN vector_records v ON v.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot IN ('text','image') "
    "AND b.extraction_id IN (SELECT value FROM json_each(?))"
)


def _vector_facts(
    db, snapshot_id: str, selected: set[str], assets: Mapping[str, AssetFact]
) -> tuple[VectorFact, ...]:
    """Each selected vector record with its context group's or asset's set."""
    rows = db.execute(
        _VECTOR_ROWS, (snapshot_id, json.dumps(sorted(selected)))
    ).fetchall()
    groups = _persisted(db, tuple({row[3] for row in rows if row[3] is not None}))
    facts = []
    for record_id, chunk_id, asset_id, group_id, _ in rows:
        if group_id is not None:
            dependencies = groups[group_id]
        elif asset_id in assets:
            dependencies = assets[asset_id].dependency_ids
        else:
            raise _integrity("Image vector has no canonical asset")
        facts.append(
            VectorFact(
                record_id,
                chunk_id=chunk_id,
                asset_id=asset_id,
                dependency_ids=dependencies,
            )
        )
    return tuple(facts)


def _graph_receipt(db, batches) -> GraphBatchReceipt | None:
    """The receipt of the snapshot's graph root batch, if it binds one."""
    graph = [batch for slot, batch, _ in batches if slot == "graph"]
    if not graph:
        return None
    if len(graph) != 1:
        raise _integrity("Snapshot binds more than one graph generation")
    receipt = SqliteBatchRepository(db, writable=False).receipt(graph[0])
    if not isinstance(receipt, GraphBatchReceipt):
        raise _integrity("Graph batch receipt is unavailable", "canonical.row_invalid")
    return receipt


def _all_members(store: CorpusStore, snapshot_id: str) -> Iterator[SnapshotMember]:
    """Every member of the snapshot, page by page, inside the open read."""
    with store.uow.read() as tx:
        yield from tx.snapshots.all_members(snapshot_id)


def read_eligibility_facts(
    store: CorpusStore,
    snapshot: SnapshotView,
    members: Iterable[SnapshotMember] | None = None,
) -> EligibilityFacts:
    """Detach one same-epoch policy observation; release the read before dispatch."""
    if members is None:
        members = _all_members(store, snapshot.snapshot_id)
    db = store.db
    if db.in_transaction:
        raise _integrity(
            "Eligibility requires its own short read", "invariant.unexpected"
        )
    db.execute("BEGIN")
    try:
        policy_epoch_value = SqliteWorkspaceRepository(db).policy_epoch()
        revision_facts: list[OracleRevisionFact] = []
        unit_facts: list[UnitFact] = []
        chunk_facts: list[ChunkFact] = []
        asset_facts: list[AssetFact] = []
        revision_facets: list[tuple[str, str, object]] = []
        historical: dict[str, object] = {}
        selected_extractions: set[str] = set()
        for member in members:
            row = db.execute(
                "SELECT r.kind_id,r.access_rank,r.allowed_uses_json,r.published_on,"
                "r.effective_from,r.effective_until,c.collection_id,c.access_floor,"
                "s.access_floor,s.withdrawn_at,c.workspace_id,k.definition_json,k.definition_hash,r.metadata_json,r.metadata_sha,c.policy_json,"
                "s.allowed_uses_override "
                "FROM revisions r JOIN sources s USING(source_id) "
                "JOIN collections c USING(collection_id) JOIN kinds k USING(kind_id) "
                "JOIN extractions e USING(revision_id) "
                "WHERE r.revision_id=? AND s.source_id=? AND e.extraction_id=?",
                (member.revision_id, member.source_id, member.extraction_id),
            ).fetchone()
            if row is None or row[10] != snapshot.workspace_id:
                raise _integrity("Selected extraction no longer has canonical facts")
            selected_extractions.add(member.extraction_id)
            envelope = json.loads(row[11])
            recorded = envelope.pop("envelope_digest", None)
            if (
                recorded != row[12]
                or digest("historical-kind-v1", envelope) != recorded
            ):
                raise _integrity(
                    "Original kind envelope differs from canonical binding"
                )
            historical[row[0]] = {**envelope, "envelope_digest": recorded}
            kind_uses = frozenset(envelope["kind"].get("allowed_uses", ()))
            metadata = FrozenJsonMap(json.loads(row[13]))
            if digest("metadata-v1", metadata) != row[14]:
                raise _integrity("Selected metadata differs from immutable digest")
            global_facets, global_binding = global_facet_binding_bytes(
                metadata,
                member.revision_id,
                row[14],
                store.registry,
            )
            marker_rows = db.execute(
                "SELECT ra.source_ref,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM revision_artifacts ra LEFT JOIN artifacts a USING(artifact_sha) "
                "WHERE ra.revision_id=? AND ra.role=?",
                (member.revision_id, GLOBAL_FACET_ROLE),
            ).fetchall()
            if global_binding is None:
                if marker_rows:
                    raise _integrity("Undeclared global facet binding exists")
            else:
                if (
                    len(marker_rows) != 1
                    or marker_rows[0][0] != GLOBAL_FACET_SOURCE_REF
                    or marker_rows[0][1] is None
                    or marker_rows[0][2] > 65536
                ):
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Retained global facet lacks a verified binding; publish a new edition",
                    )
                marker = marker_rows[0]
                try:
                    with store.artifacts.open_verified(
                        artifact_ref(marker[1:5])
                    ) as stream:
                        if stream.read(65537) != global_binding:
                            raise TychoraError(
                                "PROFILE_INCOMPATIBLE", "Global facet semantics differ"
                            )
                except OSError as error:
                    raise _integrity(
                        "Global facet binding payload is unavailable"
                    ) from error
            revision_facts.append(
                OracleRevisionFact(
                    source_id=member.source_id,
                    revision_id=member.revision_id,
                    extraction_id=member.extraction_id,
                    collection_id=row[6],
                    collection_archived=not collection_is_active(row[15]),
                    kind_id=row[0],
                    access_rank=row[1],
                    source_floor=row[8],
                    collection_floor=row[7],
                    allowed_uses=overridden_uses(
                        frozenset(json.loads(row[2])), row[16]
                    ),
                    review_state=store.review_state(
                        ReviewTarget(revision_id=member.revision_id)
                    ),
                    withdrawn=row[9] is not None,
                    published_on=row[3],
                    effective_from=row[4],
                    effective_until=row[5],
                    is_default=member.is_default,
                    kind_allowed_uses=kind_uses,
                    global_facets=global_facets,
                )
            )
            for key, text, number, boolean in db.execute(
                "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values WHERE revision_id=?",
                (member.revision_id,),
            ):
                value = (
                    text
                    if text is not None
                    else boolean
                    if boolean is not None
                    else number
                )
                revision_facets.append((member.revision_id, key, value))
            unit_rows = db.execute(
                "SELECT u.unit_id,u.unit_type,u.citable,p.asset_id FROM evidence_units u "
                "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                "WHERE u.extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT chunk_id,unit_id,context_group_key FROM chunks WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            asset_ids = tuple(
                row[0]
                for row in db.execute(
                    "SELECT asset_id FROM assets WHERE extraction_id=?",
                    (member.extraction_id,),
                )
            )
            dependencies = _persisted(
                db,
                (
                    *(row[0] for row in unit_rows),
                    *(row[0] for row in chunk_rows),
                    *asset_ids,
                ),
            )
            for uid, unit_type, citable, proxy_asset in unit_rows:
                unit_facts.append(
                    UnitFact(
                        uid,
                        member.revision_id,
                        member.extraction_id,
                        unit_type,
                        store.review_state(ReviewTarget(unit_id=uid)),
                        bool(citable),
                        dependencies[uid],
                        proxy_asset_id=proxy_asset,
                        facets=_unit_facets(db, uid),
                    )
                )
            source_asset_owners: dict[str, set[str]] = defaultdict(set)
            for uid, aid in db.execute(
                "SELECT ua.unit_id,ua.asset_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=? AND ua.role='source'",
                (member.extraction_id,),
            ):
                source_asset_owners[aid].add(uid)
            for aid in asset_ids:
                asset_facts.append(
                    AssetFact(
                        aid,
                        member.revision_id,
                        store.review_state(ReviewTarget(asset_id=aid)),
                        dependencies[aid],
                        source_owner_ids=frozenset(source_asset_owners.get(aid, ())),
                    )
                )
            chunk_facts.extend(
                ChunkFact(chunk_id, uid, dependencies[chunk_id], group)
                for chunk_id, uid, group in chunk_rows
            )
        assets_by_id = {item.asset_id: item for item in asset_facts}
        batches = tuple(db.execute(_SNAPSHOT_BATCHES, (snapshot.snapshot_id,)))
        result = EligibilityFacts(
            policy_epoch=policy_epoch_value,
            revisions=tuple(revision_facts),
            units=tuple(unit_facts),
            chunks=tuple(chunk_facts),
            assets=tuple(asset_facts),
            vectors=_vector_facts(
                db, snapshot.snapshot_id, selected_extractions, assets_by_id
            ),
            assertion_decisions=tuple(
                db.execute(
                    "SELECT assertion_id,MAX(decision_id) FROM assertion_decisions "
                    "GROUP BY assertion_id"
                )
            ),
            revision_facets=tuple(revision_facets),
            historical_definitions=tuple(sorted(historical.items())),
            batches=batches,
            lexical_chunks=tuple(db.execute(_LEXICAL_CHUNKS, (snapshot.snapshot_id,))),
            graph_receipt=_graph_receipt(db, batches),
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
