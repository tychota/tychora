"""Owner-aware evidence query composition over reviewed retrieval producers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import struct
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from tychora.adapters.sqlite.history import HistoryRepository, RunMetrics
from tychora.adapters.sqlite.lexical import LexicalIndex
from tychora.adapters.voyage.text import QueryOperationBinding
from tychora.application.reranking import prepare_rerank_facts
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import (
    CandidateKey,
    ChannelCandidate,
)
from tychora.contracts.corpus import AssetRow
from tychora.contracts.embeddings import (
    RegisteredImageQuery,
    TextQuery,
    VisualInput,
    VisualResizeDerivation,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    Coverage,
    DecisionRequirement,
    RelationChecks,
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.graph import GraphCandidates
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.requests import (
    ImageInput,
    PassageInput,
    QueryRequest,
    QuestionInput,
    RequestScope,
)
from tychora.contracts.requirements import ResponseRequirement, active_requirements
from tychora.contracts.vectors import WorkLimits
from tychora.formatting.render import OutputProfile
from tychora.ports.execution import ExecutionContext
from tychora.ports.query import TextQueryProvider, VisualQueryProvider
from tychora.ports.tokens import TokenCounter
from tychora.ports.vectors import VectorIndex
from tychora.retrieval.admission import LanePolicy, admit
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.eligibility import (
    EligibilityAttempt,
    EligibilityCompiler,
    run_pinned_eligibility,
)
from tychora.retrieval.fusion import FusedCandidate, fuse
from tychora.retrieval.graph import augment
from tychora.retrieval.lexical_query import retrieve_exact_lexical
from tychora.retrieval.query import result_cache_key, validate_query_text
from tychora.retrieval.rerank import (
    RankedEvidence,
    RankedEvidenceItem,
    RerankContext,
    rerank_candidates,
)
from tychora.retrieval.response import (
    DeliveredResult,
    ResponseAssembler,
    ResponseContext,
    inspect_relations,
    prepare_records,
)
from tychora.retrieval.segments import Selection, assemble_segments
from tychora.retrieval.visuals import prepare_visual_candidates, select_visuals

if TYPE_CHECKING:
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.application.owner import WorkspaceOwner
    from tychora.ports.graph import GraphStore


@dataclass(frozen=True)
class _PreparedResult:
    selection: Selection
    records: tuple[StructuredRecord, ...]
    checks: RelationChecks
    visuals: tuple[VisualEvidence, ...]
    coverage: Coverage
    warnings: tuple[str, ...]
    decisions: tuple[DecisionRequirement, ...]
    candidate_count: int


def query_binding_from_owner(
    owner: WorkspaceOwner, scope: RequestScope, policy_epoch: int
) -> QueryOperationBinding:
    """Derive upload identity from the owner's narrowed current grants."""
    granted = owner.scope.narrow(scope)
    return QueryOperationBinding(
        principal_upload_scope_sha=digest(
            "query-upload-scope-v1",
            owner.workspace_id,
            granted.principal_id,
            sorted(granted.collection_ids),
            granted.access_ceiling,
            sorted(granted.uses),
            sorted(granted.capabilities),
            sorted(granted.upload_providers),
            policy_epoch,
        ),
        collection_ids=granted.collection_ids,
        dependency_ids=frozenset(),
        policy_epoch=policy_epoch,
    )


def linked_visual_context(
    db: sqlite3.Connection,
    asset_candidates: tuple[ChannelCandidate, ...],
    eligible_unit_ids: frozenset[str],
) -> tuple[ChannelCandidate, ...]:
    """Add only real source links of eligible matched original assets."""
    linked: list[str] = []
    for asset in asset_candidates:
        if asset.key[0] != "asset":
            continue
        rows = db.execute(
            "SELECT unit_id FROM unit_assets WHERE asset_id=? AND role='source' ORDER BY unit_id LIMIT 2049",
            (asset.key[1],),
        ).fetchall()
        if len(rows) > 2048:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Visual source links exceed the bound."
            )
        for (unit_id,) in rows:
            if unit_id in eligible_unit_ids and unit_id not in linked:
                linked.append(unit_id)
    return tuple(
        ChannelCandidate(key=("unit", unit_id), rank=rank)
        for rank, unit_id in enumerate(linked, 1)
    )


class QueryService:
    """Async query service; public synchronous bridges belong to transport."""

    def __init__(
        self,
        owner: WorkspaceOwner,
        *,
        output_counter: TokenCounter,
        lexical: LexicalIndex | None = None,
        graph: GraphStore | None = None,
        text_provider: TextQueryProvider | None = None,
        text_index: VectorIndex | None = None,
        visual_provider: VisualQueryProvider | None = None,
        visual_index: VectorIndex | None = None,
        reranker: VoyageReranker | None = None,
        rerank_counter: TokenCounter | None = None,
        rerank_unavailable: TychoraError | None = None,
        caches: QueryCaches | None = None,
        history: HistoryRepository | None = None,
        profiles: tuple[OutputProfile, ...] | None = None,
    ) -> None:
        self.owner = owner
        self.registry = owner.store.registry
        self.compiler = EligibilityCompiler(owner.store)
        self.lexical = lexical or LexicalIndex(owner.db)
        self.graph = graph
        self.text_provider = text_provider
        self.text_index = text_index
        self.visual_provider = visual_provider
        self.visual_index = visual_index
        self.reranker = reranker
        self.rerank_counter = rerank_counter
        self.rerank_unavailable = rerank_unavailable
        self.counter = output_counter
        self.caches = caches or QueryCaches()
        self.history = history or HistoryRepository(owner.db, artifacts=owner.artifacts)
        self.profiles = profiles or (_default_profile(owner.config, self.registry),)
        self.assembler = ResponseAssembler()
        self.delivery_serializer = None
        self.delivery_serialization = None

    def _projection_fingerprint(self) -> str:
        parts = (
            self.owner.config.output_fingerprint,
            tuple(profile.model_dump(mode="json") for profile in self.profiles),
        )
        return digest(
            "query-projection-v1",
            *(
                parts
                if self.delivery_serialization is None
                else (*parts, self.delivery_serialization)
            ),
        )

    def _provider_fingerprint(self) -> str:
        def factory_identity(provider: TextQueryProvider | VisualQueryProvider | None):
            if provider is None:
                return None
            return {
                "capabilities": provider.capabilities.model_dump(mode="json"),
                "credential_binding_id": provider.credential_binding_id,
                "remote": provider.remote,
                "available": provider.available,
            }

        return digest(
            "query-provider-v1",
            self.owner.config.query_fingerprint,
            self.owner.config.values.providers.model_dump(mode="json"),
            self.owner.config.values.index.model_dump(mode="json"),
            factory_identity(self.text_provider),
            factory_identity(self.visual_provider),
            self.text_index is not None,
            self.visual_index is not None,
            self.reranker.credential_binding_id if self.reranker else None,
        )

    def _validate_provider_families(self, snapshot_id: str) -> None:
        """Check actual factory identity against configured and pinned family before cache reuse."""
        from collections.abc import Mapping

        snapshot = self.owner.store.resolve_snapshot(snapshot_id)
        retained_configs: dict[str, ResolvedConfig] = {}
        current_artifact_sha = sha256(
            canonical_bytes(
                {
                    "type": "registry",
                    "schema_version": "1",
                    "data": self.owner.config.model_dump(mode="json"),
                }
            )
        ).hexdigest()
        for channel, provider, index, settings, modality in (
            (
                "text",
                self.text_provider,
                self.text_index,
                self.owner.config.values.providers.text,
                "text",
            ),
            (
                "image",
                self.visual_provider,
                self.visual_index,
                self.owner.config.values.providers.image,
                "visual",
            ),
        ):
            if provider is None:
                continue
            caps = provider.capabilities
            if (
                provider.caches is not self.caches
                or caps.provider != settings.provider
                or caps.model != settings.model
                or caps.dimensions != settings.dimensions
                or caps.modality != modality
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query provider binding differs from configuration.",
                )
            slot = next((slot for slot in snapshot.slots if slot.slot == channel), None)
            if slot is None:
                continue
            if index is None:
                raise TychoraError(
                    "CAPABILITY_MISSING", "Pinned vector index is unavailable."
                )
            raw = slot.family.config_json.get("spec")
            if not isinstance(raw, Mapping):
                raise TychoraError(
                    "INDEX_INCOMPLETE", "Pinned vector family is unavailable."
                )
            try:
                spec = FamilySpec.model_validate(dict(raw))
            except ValueError:
                raise TychoraError(
                    "INDEX_INCOMPLETE", "Pinned vector family is invalid."
                ) from None
            origin_sha = slot.family.config_json.get("effective_config_sha")
            if not isinstance(origin_sha, str) or len(origin_sha) != 64:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Pinned vector family origin is invalid."
                )
            if origin_sha not in retained_configs:
                retained_configs[origin_sha] = (
                    self.owner.config
                    if origin_sha == current_artifact_sha
                    else self.owner.store.retained_config_sha(origin_sha)
                )
            retained_config = retained_configs[origin_sha]
            if not pinned_runtime_compatible(retained_config, self.owner.config):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query representation differs from pinned configuration.",
                )
            retained_settings = getattr(retained_config.values.providers, channel)
            current_identity = settings.model_dump(mode="json")
            retained_identity = retained_settings.model_dump(mode="json")
            current_identity.pop("credential_ref", None)
            retained_identity.pop("credential_ref", None)
            if current_identity != retained_identity:
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query representation differs from pinned configuration.",
                )
            expected = digest(
                f"{channel}-family-config-v1",
                origin_sha,
                retained_settings.model_dump(mode="json"),
                caps.model_dump(mode="json"),
            )
            if (
                spec.config_sha != expected
                or spec.family_id != slot.family.family_id
                or spec.modality != modality
                or spec.provider != caps.provider
                or spec.model != caps.model
                or spec.dimensions != caps.dimensions
                or spec.representation_fingerprint != caps.representation_fingerprint
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query provider differs from pinned vector family.",
                )

    def _replay_ready(self, request: QueryRequest) -> bool:
        """Conservatively establish current local/remote capability before channels."""
        if request.exact_only:
            return True
        offline = self.owner.config.values.offline
        if (
            _needs_text(request)
            and self.owner.config.values.index.text.backend != "disabled"
        ):
            provider = self.text_provider
            if (
                provider is None
                or self.text_index is None
                or provider.caches is not self.caches
                or not provider.available
                or (offline and provider.remote)
            ):
                return False
        if (
            _needs_visual(request)
            and self.owner.config.values.index.image.backend != "disabled"
        ):
            visual = self.visual_provider
            if (
                visual is None
                or self.visual_index is None
                or visual.caches is not self.caches
                or not visual.available
                or (offline and visual.remote)
            ):
                return False
        return not (
            _needs_text(request)
            and self.owner.config.values.providers.rerank.provider != "disabled"
            and not self.owner.config.values.retrieval.allow_degraded
            and (
                self.reranker is None
                or self.rerank_counter is None
                or self.rerank_unavailable is not None
                or not self.reranker.available
                or offline
            )
        )

    async def replay(
        self, run_id: str, scope: RequestScope, ctx: ExecutionContext
    ) -> DeliveredResult:
        """Reauthorize an explicitly retained request before channel execution."""
        ctx.checkpoint()
        granted = self.owner.scope.narrow(scope)
        request = await self.owner.run_blocking(
            lambda: self.history.replay_request(
                run_id,
                scope=granted,
                snapshot_id=self.owner.store.resolve_snapshot(None).snapshot_id,
                config_sha=self.owner.config.config_sha,
                projection_fingerprint=self._projection_fingerprint(),
                provider_fingerprint=self._provider_fingerprint(),
                providers_available=self._replay_ready,
            ),
            ctx,
        )
        return await self.search(request, granted, ctx)

    async def search(
        self, request: QueryRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> DeliveredResult:
        started = time.monotonic()
        ctx.checkpoint()
        if request.profile is not None and request.profile != self.owner.config.profile:
            raise TychoraError(
                "INVALID_REQUEST", "Request profile differs from workspace."
            )
        validate_query_text(
            request,
            self.text_provider.counter
            if self.text_provider is not None
            else self.visual_provider.counter
            if self.visual_provider is not None
            else self.rerank_counter or self.counter,
        )
        granted = self.owner.scope.narrow(scope)
        mode = _mode(self.registry, self.owner.config, request)
        requirements = tuple(
            ResponseRequirement.model_validate(value)
            for value in cast(tuple[object, ...], mode.get("requirements", ()))
        )
        active_requirements(requirements, request)

        async def operation(
            attempt: EligibilityAttempt,
        ) -> DeliveredResult:
            await self.owner.run_blocking(
                lambda: self._validate_provider_families(attempt.view.snapshot_id), ctx
            )
            lexical_epoch = await self.owner.run_blocking(self._lexical_epoch, ctx)
            key = result_cache_key(
                request,
                attempt.view.snapshot_id,
                attempt.view.policy_epoch,
                lexical_epoch,
                attempt.view.scope_digest,
                self.owner.config.config_sha,
                self._provider_fingerprint(),
                self._projection_fingerprint(),
            )
            cached = self.caches.get_result(key)
            if isinstance(cached, _PreparedResult):
                prepared = cached
                cache_hit = True
            else:
                prepared, actual_epoch = await self._prepare(
                    request, granted, attempt, mode, ctx
                )
                key = result_cache_key(
                    request,
                    attempt.view.snapshot_id,
                    attempt.view.policy_epoch,
                    actual_epoch,
                    attempt.view.scope_digest,
                    self.owner.config.config_sha,
                    self._provider_fingerprint(),
                    self._projection_fingerprint(),
                )
                self.caches.put_result(key, prepared)
                lexical_epoch = actual_epoch
                cache_hit = False
            delivered = self._deliver(
                request, granted, attempt, requirements, mode, prepared
            )
            metrics = RunMetrics(
                policy_epoch=attempt.view.policy_epoch,
                lexical_statistics_epoch=lexical_epoch,
                config_sha=self.owner.config.config_sha,
                request_fingerprint=digest(
                    "query-request-v1", request.model_dump(mode="json")
                ),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                candidate_count=prepared.candidate_count,
                cache_hit=cache_hit,
            )

            def record() -> None:
                retained = (
                    self.history.retain_request(
                        delivered.result.run_id,
                        request,
                        scope=granted,
                        snapshot_id=delivered.result.snapshot_id,
                        config_sha=self.owner.config.config_sha,
                        projection_fingerprint=self._projection_fingerprint(),
                        provider_fingerprint=self._provider_fingerprint(),
                    )
                    if self.history.retains_requests
                    else None
                )
                self.history.record(delivered, metrics, retained)

            await self.owner.run_blocking(record, ctx)
            return delivered

        async def supersede(delivered: DeliveredResult) -> None:
            await self.owner.run_blocking(
                lambda: self.history.supersede(delivered.result.run_id),
                ExecutionContext(),
            )

        return await run_pinned_eligibility(
            self.compiler,
            None,
            granted,
            request,
            self.owner.config,
            operation,
            owner=self.owner,
            ctx=ctx,
            on_invalidated=supersede,
        )

    def _lexical_epoch(self) -> int:
        row = self.owner.db.execute(
            "SELECT lexical_statistics_epoch FROM workspaces WHERE workspace_id=?",
            (self.owner.workspace_id,),
        ).fetchone()
        if row is None:
            raise TychoraError("INTEGRITY_ERROR", "Workspace statistics are absent.")
        return int(row[0])

    async def _prepare(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        mode: FrozenJsonMap,
        ctx: ExecutionContext,
    ) -> tuple[_PreparedResult, int]:
        view = attempt.view
        channels = await self.owner.run_blocking(
            lambda: retrieve_exact_lexical(
                self.owner.store,
                self.lexical,
                view,
                request,
                self.registry,
                80,
                ctx,
            ),
            ctx,
        )
        lexical_epoch = next(
            (
                channel.lexical_statistics_epoch
                for channel in channels
                if channel.lexical_statistics_epoch is not None
            ),
            None,
        )
        if lexical_epoch is None:
            lexical_epoch = await self.owner.run_blocking(self._lexical_epoch, ctx)
        assert lexical_epoch is not None
        incomplete: list[str] = [
            channel.channel for channel in channels if not channel.complete
        ]
        completed: list[str] = [
            channel.channel for channel in channels if channel.complete
        ]
        if incomplete and not self.owner.config.values.retrieval.allow_degraded:
            raise TychoraError("INDEX_INCOMPLETE", "A retrieval channel is incomplete.")
        votes: dict[str, tuple[ChannelCandidate, ...]] = {
            channel.channel: (*channel.source_candidates, *channel.asset_candidates)
            for channel in channels
        }
        if (
            _needs_text(request)
            and self.owner.config.values.index.text.backend != "disabled"
        ):
            try:
                votes["text"] = await self._text_candidates(
                    request, scope, attempt, ctx
                )
                completed.append("text")
            except TychoraError as error:
                if (
                    not self.owner.config.values.retrieval.allow_degraded
                    or error.code
                    not in {
                        "CAPABILITY_MISSING",
                        "INDEX_INCOMPLETE",
                        "PROVIDER_FAILURE",
                        "FORBIDDEN_SCOPE",
                    }
                ):
                    raise
                incomplete.append("text")
        if (
            _needs_visual(request)
            and self.owner.config.values.index.image.backend != "disabled"
        ):
            try:
                visual_votes = await self._visual_candidates(
                    request, scope, attempt, ctx
                )
                linked = await self.owner.run_blocking(
                    lambda: linked_visual_context(
                        self.owner.db, visual_votes, view.source_unit_ids
                    ),
                    ctx,
                )
                votes["visual"] = (*visual_votes, *linked)
                completed.append("visual")
            except TychoraError as error:
                if (
                    not self.owner.config.values.retrieval.allow_degraded
                    or error.code
                    not in {
                        "CAPABILITY_MISSING",
                        "INDEX_INCOMPLETE",
                        "PROVIDER_FAILURE",
                        "FORBIDDEN_SCOPE",
                    }
                ):
                    raise
                incomplete.append("visual")
        fused = fuse(votes)
        graph_candidates = GraphCandidates()
        if not request.exact_only and self.graph is not None:
            graph_candidates = await asyncio.to_thread(
                augment, view, fused, self.owner.config, self.graph, ctx
            )
            graph_votes = tuple(
                ChannelCandidate(key=("unit", item.unit_id), rank=rank)
                for rank, item in enumerate(graph_candidates.candidates, 1)
                if item.unit_id in view.source_unit_ids
            )
            if graph_votes:
                votes["graph"] = graph_votes
                fused = fuse(votes)
        lanes = await self.owner.run_blocking(
            lambda: self._candidate_lanes(fused, view), ctx
        )
        required = cast(tuple[str, ...], mode.get("required_lanes", ()))
        active = active_requirements(
            tuple(
                ResponseRequirement.model_validate(v)
                for v in cast(tuple[object, ...], mode.get("requirements", ()))
            ),
            request,
        )
        required = tuple(
            dict.fromkeys((*required, *(r.value for r in active if r.target == "lane")))
        )
        lane_order = self.owner.config.values.output.lane_order or tuple(
            self.registry.lanes
        )
        admission = admit(
            fused,
            tuple(item.key for item in channels[0].source_candidates),
            {
                strategy: tuple(
                    ("unit", item.unit_id)
                    for item in graph_candidates.candidates
                    if item.strategy == strategy
                )
                for strategy in ("structural", "semantic")
            },
            LanePolicy(
                mode=self.owner.config.values.selection.lane_reservation,
                order=lane_order,
                required=required,
                known=tuple(self.registry.lanes),
            ),
            lanes=lanes,
        )
        facts = await prepare_rerank_facts(self.owner, attempt, admission, votes, ctx)
        if request.exact_only:
            ranked = RankedEvidence(
                tuple(
                    RankedEvidenceItem(
                        candidate,
                        None,
                        facts.get(candidate.key),
                        "disabled" if candidate.key[0] == "unit" else "independent",
                    )
                    for candidate in admission.selected
                )
            )
        else:
            ranked = await rerank_candidates(
                request,
                admission,
                RerankContext(
                    owner=self.owner,
                    attempt=attempt,
                    reranker=self.reranker,
                    counter=self.rerank_counter,
                    facts=cast(Any, facts),
                    execution=ctx,
                    allow_degraded=self.owner.config.values.retrieval.allow_degraded,
                    unavailable=self.rerank_unavailable,
                ),
            )
        paths = tuple(
            path
            for item in (*graph_candidates.candidates, *graph_candidates.enrichments)
            for path in item.paths
        )
        selection = assemble_segments(
            ranked,
            view,
            self.owner.config,
            self.counter,
            continuation_paths=paths,
            historical_kind_roles=attempt.historical_kind_roles,
            total_tokens=request.budgets.max_tokens,
            result_limit=request.limit,
        )
        visual_candidates = await prepare_visual_candidates(
            self.owner, attempt, ranked, ctx
        )
        visuals = select_visuals(visual_candidates, selection.segments)
        records = prepare_records(selection, ranked, attempt)
        checks = RelationChecks(status="not_evaluated")
        check_settings = self.owner.config.values.checks.relations
        if not request.exact_only and check_settings.enabled and self.graph is not None:
            checks = await asyncio.to_thread(
                inspect_relations,
                self.graph,
                view,
                tuple(
                    dict.fromkeys(
                        uid
                        for segment in selection.segments
                        for uid in segment.matched_unit_ids
                    )
                ),
                self.owner.config.values.retrieval.graph.predicates,
                ctx,
                expansion_inspected_rows=graph_candidates.inspected_rows,
                registry=self.registry,
                max_rows=self.owner.config.values.limits.graph_work_limit,
                max_assertions=check_settings.max_assertions,
            )
        unavailable = tuple(incomplete)
        if ranked.failure is not None:
            unavailable += ("rerank",)
        coverage = Coverage(
            status="partial"
            if unavailable or not graph_candidates.exhausted
            else "complete",
            completed_channels=tuple(completed),
            unavailable_channels=unavailable,
        )
        decisions = tuple(
            DecisionRequirement(
                requirement_id=digest("applicable-version-decision-v1", warning),
                reason=warning,
            )
            for warning in attempt.warnings
            if warning.startswith("Overlapping retained editions")
        )
        return (
            _PreparedResult(
                selection,
                records,
                checks,
                visuals,
                coverage,
                attempt.warnings,
                decisions,
                len(fused),
            ),
            lexical_epoch,
        )

    def _candidate_lanes(
        self, fused: tuple[FusedCandidate, ...], view: object
    ) -> dict[CandidateKey, str]:
        lanes: dict[CandidateKey, str] = {}
        for candidate in fused:
            kind, identifier = candidate.key
            if kind == "unit":
                row = self.owner.db.execute(
                    "SELECT r.kind_id FROM evidence_units u "
                    "JOIN extractions e USING(extraction_id) "
                    "JOIN revisions r USING(revision_id) WHERE u.unit_id=?",
                    (identifier,),
                ).fetchone()
            else:
                row = self.owner.db.execute(
                    "SELECT r.kind_id FROM assets a "
                    "JOIN extractions e USING(extraction_id) "
                    "JOIN revisions r USING(revision_id) WHERE a.asset_id=?",
                    (identifier,),
                ).fetchone()
            lane = self.registry.lane_bindings.get(row[0]) if row else None
            if not isinstance(lane, str):
                raise TychoraError(
                    "PROFILE_INCOMPATIBLE", "Candidate lane is unavailable."
                )
            lanes[candidate.key] = lane
        return lanes

    async def _text_candidates(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        ctx: ExecutionContext,
    ) -> tuple[ChannelCandidate, ...]:
        from tychora.contracts.requests import PassageInput, QuestionInput

        if not isinstance(request.input, (QuestionInput, PassageInput)):
            return ()
        query_text = request.input.text
        base = self.text_provider
        index = self.text_index
        if base is None or index is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured text retrieval is unavailable."
            )
        if base.caches is not self.caches:
            raise TychoraError(
                "INTEGRITY_ERROR", "Query embedding cache ownership differs."
            )
        embedder = base.bind(self.owner, scope, attempt.view.policy_epoch)
        if embedder.capabilities != base.capabilities:
            raise TychoraError(
                "INTEGRITY_ERROR", "Text query capability binding differs."
            )
        (query_vector,) = await attempt.owner_provider(
            lambda: self.owner.embed_queries(
                embedder,
                (query_text,),
                ctx,
                scope=scope,
            )
        )
        mapping = await self.owner.run_blocking(
            lambda: self._vector_owners(attempt, "unit"), ctx
        )
        payload = struct.pack(
            "<" + "f" * len(query_vector.values), *query_vector.values
        )
        page = await asyncio.to_thread(
            index.search,
            payload,
            mapping,
            80,
            WorkLimits(deadline=ctx.deadline),
            ctx,
        )
        if page.missing_record_ids or not page.coverage_complete or page.truncated:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Text vector coverage is incomplete."
            )
        allowed = set(mapping.values())
        if any(item.key not in allowed for item in page.candidates):
            raise TychoraError(
                "INTEGRITY_ERROR", "Vector candidate left the pinned scope."
            )
        return tuple(
            ChannelCandidate(key=item.key, rank=rank, score=-item.distance)
            for rank, item in enumerate(page.candidates, 1)
        )

    def _vector_owners(
        self, attempt: EligibilityAttempt, kind: str
    ) -> dict[str, CandidateKey]:
        view = attempt.view
        output: dict[str, CandidateKey] = {}
        for record_id in view.vector_record_ids:
            row = self.owner.db.execute(
                "SELECT c.unit_id,v.asset_id,p.asset_id FROM vector_records v "
                "LEFT JOIN chunks c ON c.chunk_id=v.chunk_id "
                "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
                "WHERE v.record_id=?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise TychoraError(
                    "INDEX_INCOMPLETE", "Selected vector record is absent."
                )
            unit_id, asset_id, proxy_asset_id = row
            if kind == "unit" and unit_id in view.source_unit_ids:
                output[record_id] = ("unit", unit_id)
            elif (
                kind == "unit"
                and unit_id in view.proxy_unit_ids
                and proxy_asset_id in view.asset_ids
            ):
                output[record_id] = ("asset", proxy_asset_id)
            elif kind == "asset" and asset_id in view.asset_ids:
                output[record_id] = ("asset", asset_id)
        return output

    async def _visual_candidates(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        ctx: ExecutionContext,
    ) -> tuple[ChannelCandidate, ...]:
        base = self.visual_provider
        index = self.visual_index
        if base is None or index is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured visual retrieval is unavailable."
            )
        if base.caches is not self.caches:
            raise TychoraError(
                "INTEGRITY_ERROR", "Visual query cache ownership differs."
            )
        current = await base.bind(self.owner, scope, ctx)
        if current.capabilities != base.capabilities:
            raise TychoraError(
                "INTEGRITY_ERROR", "Visual query capability binding differs."
            )
        query: TextQuery | RegisteredImageQuery
        if isinstance(request.input, ImageInput):
            asset_ref = request.input.asset_ref
            query = await self.owner.run_blocking(
                lambda: self._registered_image_query(asset_ref, attempt),
                ctx,
            )
        elif isinstance(request.input, (QuestionInput, PassageInput)):
            query = TextQuery(text=request.input.text)
        else:
            raise TychoraError("INVALID_REQUEST", "Visual query input is invalid.")
        vector = await attempt.owner_provider(
            lambda: self.owner.embed_visual_query(current, query, ctx, scope=scope)
        )
        mapping = await self.owner.run_blocking(
            lambda: self._vector_owners(attempt, "asset"), ctx
        )
        payload = struct.pack("<" + "f" * len(vector.values), *vector.values)
        page = await asyncio.to_thread(
            index.search,
            payload,
            mapping,
            80,
            WorkLimits(deadline=ctx.deadline),
            ctx,
        )
        if page.missing_record_ids or not page.coverage_complete or page.truncated:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Visual vector coverage is incomplete."
            )
        allowed = set(mapping.values())
        if any(item.key not in allowed for item in page.candidates):
            raise TychoraError(
                "INTEGRITY_ERROR", "Visual candidate left the pinned scope."
            )
        return tuple(
            ChannelCandidate(key=item.key, rank=rank, score=-item.distance)
            for rank, item in enumerate(page.candidates, 1)
        )

    def _registered_image_query(
        self, asset_id: str, attempt: EligibilityAttempt
    ) -> RegisteredImageQuery:
        view = attempt.view
        if asset_id not in view.asset_ids:
            raise TychoraError("FORBIDDEN_SCOPE", "Image query asset is not eligible.")
        row = self.owner.db.execute(
            "SELECT a.asset_id,a.extraction_id,a.artifact_sha,a.asset_type,a.native_ref,"
            "a.locator_json,a.caption,a.declared_review_state,e.revision_id,"
            "p.size_bytes,p.media_type,p.storage_key,p.payload_state "
            "FROM assets a JOIN extractions e USING(extraction_id) "
            "JOIN artifacts p ON p.artifact_sha=a.artifact_sha WHERE a.asset_id=?",
            (asset_id,),
        ).fetchone()
        if row is None or row[12] != "present" or row[11] != row[2]:
            raise TychoraError(
                "ORIGINAL_REMOVED", "Image query original is unavailable."
            )
        asset = AssetRow.model_validate(
            {
                "asset_id": row[0],
                "extraction_id": row[1],
                "artifact_sha": row[2],
                "asset_type": row[3],
                "native_ref": row[4],
                "locator": json.loads(row[5]),
                "caption": row[6],
                "declared_review_state": row[7],
            }
        )
        if row[8] not in view.revision_ids:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Image query revision is not selected."
            )
        source_rows = self.owner.db.execute(
            "SELECT u.unit_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
            "WHERE ua.asset_id=? AND ua.role='source' ORDER BY u.unit_id LIMIT 2049",
            (asset_id,),
        ).fetchall()
        if not source_rows or len(source_rows) > 2048:
            raise TychoraError(
                "INTEGRITY_ERROR", "Image query source links are invalid."
            )
        dependencies = frozenset({asset_id, *(item[0] for item in source_rows)})
        if not dependencies <= (view.graph_eligible_unit_ids | view.asset_ids):
            raise TychoraError("FORBIDDEN_SCOPE", "Image query source is not eligible.")
        if not attempt.dependencies_for(asset_id) <= (
            view.revision_ids | view.graph_eligible_unit_ids | view.asset_ids
        ):
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Image query ownership is not eligible."
            )
        ref = ArtifactRef(
            sha256=row[2], size_bytes=row[9], media_type=row[10], storage_key=row[11]
        )
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise TychoraError(
                "CAPABILITY_MISSING", "Image operations require the documents extra."
            ) from None
        with (
            self.owner.artifacts.open_verified(ref) as stream,
            Image.open(stream) as encoded,
        ):
            if encoded.width * encoded.height > 40_000_000:
                raise TychoraError(
                    "INVALID_REQUEST", "Image query exceeds pixel ceiling."
                )
            pixels = ImageOps.exif_transpose(encoded).convert("RGB")
            pixels.load()
        width, height = pixels.size
        descriptor = VisualInput.from_registered(
            asset=asset,
            artifact_ref=ref,
            revision_id=row[8],
            context="",
            dependency_ids=dependencies,
            pixel_sha256=sha256(pixels.tobytes()).hexdigest(),
            width=width,
            height=height,
            resize=VisualResizeDerivation(
                source_width=width,
                source_height=height,
                width=width,
                height=height,
                mode="none",
            ),
        )
        return RegisteredImageQuery(image=descriptor)

    def _deliver(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        requirements: tuple[ResponseRequirement, ...],
        mode: FrozenJsonMap,
        prepared: _PreparedResult,
    ) -> DeliveredResult:
        return self.assembler.assemble(
            prepared.selection,
            prepared.records,
            prepared.checks,
            request,
            ResponseContext(
                run_id=str(uuid4()),
                snapshot_id=attempt.view.snapshot_id,
                scope=scope,
                counter=self.counter,
                profiles=self.profiles,
                eligible_unit_ids=attempt.view.graph_eligible_unit_ids,
                visuals=prepared.visuals,
                coverage=prepared.coverage,
                warnings=prepared.warnings,
                decision_requirements=prepared.decisions,
                required_lanes=cast(tuple[str, ...], mode.get("required_lanes", ())),
                requirements=requirements,
                checks_required=self.owner.config.values.checks.relations.required,
                max_tokens=self.owner.config.values.limits.max_tokens,
                max_response_bytes=self.owner.config.values.limits.max_response_bytes,
                delivery_serializer=self.delivery_serializer,
                delivery_serialization=self.delivery_serialization,
            ),
        )


def _needs_text(request: QueryRequest) -> bool:
    return (
        not request.exact_only
        and "text" in request.modalities
        and isinstance(request.input, (QuestionInput, PassageInput))
    )


def _needs_visual(request: QueryRequest) -> bool:
    return not request.exact_only and (
        "image" in request.modalities or isinstance(request.input, ImageInput)
    )


def _mode(registry: Registry, config: object, request: QueryRequest) -> FrozenJsonMap:
    from tychora.configuration.models import ResolvedConfig

    resolved = cast(ResolvedConfig, config)
    name = request.mode or resolved.values.mode or resolved.values.default_mode
    value = registry.modes.get(name) if name is not None else None
    if not isinstance(value, FrozenJsonMap):
        raise TychoraError("INVALID_REQUEST", "Retrieval mode is unavailable.")
    return value


def _default_profile(config: object, registry: Registry) -> OutputProfile:
    from tychora.configuration.models import ResolvedConfig

    resolved = cast(ResolvedConfig, config)
    settings = resolved.values.output
    return OutputProfile(
        renderer=cast(Any, settings.renderer),
        lane_order=settings.lane_order,
        lane_labels=FrozenJsonMap(
            {
                name: lane["label"]
                for name, lane in registry.lanes.items()
                if isinstance(lane, FrozenJsonMap)
            }
        ),
        include_empty_lanes=settings.include_empty_lanes,
        title=settings.title,
        envelope_tag=settings.envelope_tag,
    )
