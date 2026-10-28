"""Owner-aware evidence query composition over reviewed retrieval producers."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from tychora.adapters.sqlite.history import HistoryRepository, RunMetrics
from tychora.adapters.voyage.text import QueryOperationBinding
from tychora.application.query_plan import (
    QueryBindings,
    QueryPlan,
    needs_text,
    needs_visual,
    plan_for,
)
from tychora.application.reranking import prepare_rerank_facts
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.configuration.stopwords import load_stopwords
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.corpus import AssetRow
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    RegisteredImageQuery,
    TextQuery,
    VisualInput,
    VisualResizeDerivation,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    DecisionRequirement,
    RelationChecks,
    RelationPath,
)
from tychora.contracts.graph import GraphCandidates, GraphPath, GraphScope
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.publication import SnapshotView
from tychora.contracts.requests import (
    ImageInput,
    PassageInput,
    QueryRequest,
    QuestionInput,
    RequestScope,
)
from tychora.contracts.requirements import ResponseRequirement, active_requirements
from tychora.formatting.render import OutputProfile
from tychora.ports.execution import ExecutionContext
from tychora.ports.query import TextQueryProvider, VisualQueryProvider
from tychora.ports.repositories import ReadTransaction
from tychora.ports.tokens import TokenCounter
from tychora.ports.vectors import VectorIndex
from tychora.retrieval.admission import LanePolicy, admit
from tychora.retrieval.attempt import (
    AttemptHost,
    EligibilityAttempt,
    is_eligible_asset,
    is_eligible_unit,
    run_pinned_eligibility,
)
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.cached_selection import (
    CACHE_OVERHEAD_BYTES,
    CachedSelection,
    PreparedEvidence,
)
from tychora.retrieval.compiler import (
    EligibilityCompiler,
    EligibilityInputs,
    eligible_cache_key,
)
from tychora.retrieval.coverage import build_coverage, missing_batch_slots
from tychora.retrieval.fusion import FusedCandidate, fuse
from tychora.retrieval.graph import augment
from tychora.retrieval.graph_scope import graph_scope
from tychora.retrieval.lexical_query import retrieve_exact_lexical, runs_lexical
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
from tychora.retrieval.segments import assemble_segments
from tychora.retrieval.selection import EditionDecision
from tychora.retrieval.survivors import load_survivors
from tychora.retrieval.vectors import (
    CHANNEL_LIMIT,
    vector_candidates,
    visual_candidates,
)
from tychora.retrieval.visuals import prepare_visual_candidates, select_visuals

if TYPE_CHECKING:
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.application.owner import WorkspaceOwner
    from tychora.ports.graph import GraphStore


_LOG = logging.getLogger(__name__)
# The visual channel reads the publication slot named ``image``.
_SLOT_CHANNELS = {"image": "visual"}
_INCOMPLETE_MESSAGES = {
    "channel.unavailable": "A retrieval channel is incomplete.",
    "batch.missing": "A batch of an eligible revision is missing.",
}

type _VectorChannel = tuple[tuple[ChannelCandidate, ...], bool]


@dataclass(frozen=True)
class _QueryJob:
    """One authorized request with its mode and plan, as preparation reads it."""

    request: QueryRequest
    scope: RequestScope
    mode: FrozenJsonMap
    plan: QueryPlan


async def _run_independent_channels[T](
    calls: tuple[Callable[[], Awaitable[T]], ...], ctx: ExecutionContext
) -> tuple[T, ...]:
    """Overlap at most two independent waits and drain them on failure/cancel."""
    if len(calls) > 2:
        raise ValueError("At most two independent query channels may run")
    ctx.checkpoint()

    async def checked(call: Callable[[], Awaitable[T]]) -> T:
        ctx.checkpoint()
        result = await call()
        ctx.checkpoint()
        return result

    tasks = tuple(asyncio.create_task(checked(call)) for call in calls)
    try:
        async with asyncio.timeout_at(ctx.deadline):
            return tuple(await asyncio.gather(*tasks))
    except BaseException as error:
        ctx.cancelled.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(error, TimeoutError):
            raise TychoraError(
                "DEADLINE_EXCEEDED", "Operation deadline exceeded"
            ) from error
        raise


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


class QueryService:
    """Async query service; public synchronous bridges belong to transport."""

    def __init__(
        self,
        owner: WorkspaceOwner,
        *,
        output_counter: TokenCounter,
        bindings: QueryBindings | None = None,
        auto_rerank: bool = False,
        caches: QueryCaches | None = None,
        history: HistoryRepository | None = None,
        profiles: tuple[OutputProfile, ...] | None = None,
    ) -> None:
        bindings = bindings or QueryBindings()
        self.bindings = bindings
        self.owner = owner
        self.registry = owner.store.registry
        lexical = owner.config.values.retrieval.lexical
        self.stopwords = load_stopwords(lexical.languages, lexical.stopwords).words
        self.compiler = EligibilityCompiler(self.registry, owner.artifacts)
        self.graph: GraphStore | None = bindings.graph
        self.text_provider: TextQueryProvider | None = bindings.text_provider
        self.text_index: VectorIndex | None = bindings.text_index
        self.visual_provider: VisualQueryProvider | None = bindings.visual_provider
        self.visual_index: VectorIndex | None = bindings.visual_index
        self.reranker: VoyageReranker | None = bindings.reranker
        self.rerank_counter: TokenCounter | None = bindings.rerank_counter
        self.rerank_unavailable = bindings.rerank_unavailable
        self.auto_rerank = auto_rerank
        self.counter = output_counter
        self.caches = caches or QueryCaches()
        bindings.validate(owner.config, self.caches)
        self.history = history or HistoryRepository(owner.db, artifacts=owner.artifacts)
        self.profiles = profiles or (_default_profile(owner.config, self.registry),)
        self.assembler = ResponseAssembler()
        self.delivery_serializer = None
        self.delivery_serialization = None

    def _projection_fingerprint(self, config: ResolvedConfig | None = None) -> str:
        config = config or self.owner.config
        parts = (
            config.output_fingerprint,
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

    def _provider_fingerprint(self, config: ResolvedConfig | None = None) -> str:
        config = config or self.owner.config

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
            config.query_fingerprint,
            config.values.providers.model_dump(mode="json"),
            config.values.index.model_dump(mode="json"),
            factory_identity(self.text_provider),
            factory_identity(self.visual_provider),
            self.text_index is not None,
            self.visual_index is not None,
            self.reranker.credential_binding_id if self.reranker else None,
        )

    def _validate_provider_families(
        self, snapshot: SnapshotView, plan: QueryPlan
    ) -> None:
        """Check each planned channel's provider against its pinned family before cache reuse.

        The provider's own identity was checked once, by ``QueryBindings.validate``.
        """
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
            if provider is None or not getattr(plan, channel):
                continue
            caps = provider.capabilities
            slot = snapshot.family(channel)
            if slot is None:
                # Only a channel the request selected itself is refused.
                if channel in plan.explicit:
                    raise TychoraError(
                        "CAPABILITY_MISSING", "Pinned vector family is unavailable."
                    )
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
            raw_capabilities = slot.family.config_json.get("capabilities")
            if raw_capabilities is None:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Retained vector capability proof is absent; publish a new family and snapshot.",
                )
            try:
                retained_capabilities = EmbeddingCapabilities.model_validate_json(
                    canonical_bytes(raw_capabilities)
                )
            except (TypeError, ValueError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained vector capability proof is invalid."
                ) from None
            if retained_capabilities != caps:
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query provider differs from pinned vector family.",
                )
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
                raw_capabilities,
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
            needs_text(request)
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
            needs_visual(request)
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
            needs_text(request)
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
        if "read" not in granted.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Search requires read capability.")

        def retained_request():
            from tychora.configuration.additive_compatibility import (
                qualified_additive_registry_extension,
            )

            snapshot = self.owner.store.resolve_snapshot(None)

            def read(config):
                return self.history.replay_request(
                    run_id,
                    scope=granted,
                    snapshot_id=snapshot.snapshot_id,
                    config_sha=config.config_sha,
                    projection_fingerprint=self._projection_fingerprint(config),
                    provider_fingerprint=self._provider_fingerprint(config),
                    providers_available=self._replay_ready,
                )

            try:
                return read(self.owner.config)
            except TychoraError as error:
                if error.code != "REPLAY_UNAVAILABLE":
                    raise
                retained = self.owner.store.retained_config(snapshot)
                if not qualified_additive_registry_extension(
                    retained, self.owner.config
                ):
                    raise
                # Recheck every recorded identity using the exact snapshot config;
                # provider instance, credentials, scope and snapshot checks stay strict.
                return read(retained)

        request = await self.owner.run_write(retained_request, ctx, "interactive")
        return await self.search(request, granted, ctx)

    async def search(
        self, request: QueryRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> DeliveredResult:
        granted = self.owner.scope.narrow(scope)
        if "read" not in granted.capabilities:
            raise TychoraError("FORBIDDEN_SCOPE", "Search requires read capability.")
        started = time.monotonic()
        ctx.checkpoint()
        preflight = getattr(self.counter, "ensure_available", None)
        if preflight is not None:
            await self.owner.run_cpu(preflight, ctx)
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
        mode = _mode(self.registry, self.owner.config, request)
        requirements = tuple(
            ResponseRequirement.model_validate(value)
            for value in cast(tuple[object, ...], mode.get("requirements", ()))
        )
        active_requirements(requirements, request)
        job = _QueryJob(
            request, granted, mode, plan_for(request, self.owner.config, self.bindings)
        )

        async def operation(
            attempt: EligibilityAttempt,
        ) -> DeliveredResult:
            snapshot = attempt.snapshot
            assert snapshot is not None
            await self.owner.run_write(
                lambda: self._validate_provider_families(snapshot, job.plan),
                ctx,
                "interactive",
            )
            prepared, lexical_epoch, cache_hit = await self._cached_prepare(
                job, attempt, ctx
            )
            eligible = await self.owner.run_read(
                lambda tx: _delivered_eligibility(tx, attempt.view, prepared), ctx
            )
            delivered = self._deliver(
                request, granted, attempt, requirements, mode, prepared, eligible
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

            await self.owner.run_write(record, ctx, "interactive")
            return delivered

        async def supersede(delivered: DeliveredResult) -> None:
            await self.owner.run_write(
                lambda: self.history.supersede(delivered.result.run_id),
                ExecutionContext(),
                "interactive",
            )

        return await run_pinned_eligibility(
            AttemptHost(self.compiler, self.owner, ctx),
            EligibilityInputs(None, granted, request, self.owner.config),
            operation,
            on_invalidated=supersede,
        )

    def _result_key(
        self, request: QueryRequest, view: EligibilityView, lexical_epoch: int
    ) -> str:
        return result_cache_key(
            request,
            eligible_cache_key(view),
            lexical_epoch,
            self.owner.config.config_sha,
            self._provider_fingerprint(),
            self._projection_fingerprint(),
        )

    async def _cached_prepare(
        self, job: _QueryJob, attempt: EligibilityAttempt, ctx: ExecutionContext
    ) -> tuple[CachedSelection, int, bool]:
        """The cached entry for this request, or a fresh one cached when allowed.

        Returns the entry, the lexical epoch it was prepared at, and whether
        it came from the cache. An entry larger than the response bound plus
        ``CACHE_OVERHEAD_BYTES`` is served uncached, and so is one a failure
        made partial or unavailable (an unavailable channel or rerank), so a
        transient failure is never replayed. A complete entry whose channels a
        deterministic limit truncated is cached.
        """
        lexical_epoch = await self.owner.run_read(
            lambda tx: tx.workspace.lexical_epoch(), ctx
        )
        cached = self._cached_entry(
            self._result_key(job.request, attempt.view, lexical_epoch)
        )
        if cached is not None:
            return cached, lexical_epoch, True
        prepared, actual_epoch = await self._prepare(job, attempt, ctx)
        entry = prepared.to_bytes()
        bound = self.owner.config.values.limits.max_response_bytes
        if (
            not prepared.coverage.unavailable_channels
            and prepared.coverage.status != "unavailable"
            and len(entry) <= bound + CACHE_OVERHEAD_BYTES
        ):
            self.caches.put_result(
                self._result_key(job.request, attempt.view, actual_epoch), entry
            )
        return prepared, actual_epoch, False

    def _cached_entry(self, key: str) -> CachedSelection | None:
        """The entry under ``key``; an unreadable one is dropped and is a miss."""
        raw = self.caches.get_result(key)
        if raw is None:
            return None
        try:
            return CachedSelection.from_bytes(raw)
        except ValueError as error:
            self.caches.drop_result(key)
            diagnostic = uuid4().hex
            # The error text would quote cached evidence; log only its type.
            _LOG.warning(
                "Dropped an unreadable cached result [%s]: %s",
                diagnostic,
                type(error).__name__,
            )
            return None

    async def _prepare(
        self, job: _QueryJob, attempt: EligibilityAttempt, ctx: ExecutionContext
    ) -> tuple[CachedSelection, int]:
        request, scope, mode, plan = job.request, job.scope, job.mode, job.plan
        # Exact reads no lexical batch: a missing one skips lexical alone.
        incomplete: list[str] = []
        lexical_runs = plan.lexical and runs_lexical(request)
        lexical_missing = self._missing_batches(
            attempt.view, ("lexical",) if lexical_runs else (), incomplete
        )
        channels = (
            await self.owner.run_read(
                lambda tx: retrieve_exact_lexical(
                    tx,
                    attempt.view,
                    request,
                    self.registry,
                    80,
                    ctx,
                    stopwords=self.stopwords,
                    with_lexical=not lexical_missing,
                ),
                ctx,
            )
            if plan.lexical
            else ()
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
            lexical_epoch = await self.owner.run_read(
                lambda tx: tx.workspace.lexical_epoch(), ctx
            )
        assert lexical_epoch is not None
        incomplete.extend(
            channel.channel for channel in channels if not channel.complete
        )
        completed: list[str] = [
            channel.channel for channel in channels if channel.complete
        ]
        # A deterministic bound cut these short: reported, never refused.
        truncated: list[str] = [c.channel for c in channels if c.truncated]
        self._refuse_strict(incomplete, "channel.unavailable")
        lexical_votes: dict[str, tuple[ChannelCandidate, ...]] = {
            channel.channel: (*channel.source_candidates, *channel.asset_candidates)
            for channel in channels
        }
        votes = lexical_votes.copy() if plan.lexical else {}
        vector_calls: list[
            tuple[str, Callable[[], Awaitable[_VectorChannel | TychoraError]]]
        ] = []

        async def text_candidates() -> _VectorChannel | TychoraError:
            try:
                return await self._text_candidates(request, scope, attempt, ctx)
            except TychoraError as error:
                return error

        async def retrieve_visual_channel() -> _VectorChannel | TychoraError:
            try:
                return await self._visual_candidates(request, scope, attempt, ctx)
            except TychoraError as error:
                return error

        text_runs = (
            plan.text
            and needs_text(request)
            and self.owner.config.values.index.text.backend != "disabled"
        )
        image_runs = (
            plan.image
            and needs_visual(request)
            and self.owner.config.values.index.image.backend != "disabled"
        )
        # Strict mode refuses a missing batch; degraded mode records it.
        text_missing = self._missing_batches(
            attempt.view, ("text",) if text_runs else (), incomplete
        )
        image_missing = self._missing_batches(
            attempt.view, ("image",) if image_runs else (), incomplete
        )
        if text_runs and not text_missing:
            vector_calls.append(("text", text_candidates))
        if image_runs and not image_missing:
            vector_calls.append(("visual", retrieve_visual_channel))

        vector_results = await _run_independent_channels(
            tuple(call for _, call in vector_calls), ctx
        )
        for (channel, _), result in zip(vector_calls, vector_results, strict=True):
            if isinstance(result, TychoraError):
                if (
                    not self.owner.config.values.retrieval.allow_degraded
                    or result.code
                    not in {
                        "CAPABILITY_MISSING",
                        "INDEX_INCOMPLETE",
                        "PROVIDER_FAILURE",
                        "FORBIDDEN_SCOPE",
                    }
                ):
                    raise result
                incomplete.append(channel)
                continue
            votes[channel], cut = result
            if cut:
                truncated.append(channel)
            completed.append(channel)
        fused = fuse(votes)
        graph = self.graph
        graph_view, graph_candidates, graph_votes = await self._augment_graph(
            request, attempt, fused, plan, ctx
        )
        if graph_votes:
            votes["graph"] = graph_votes
            fused = fuse(votes)
        survivors = await self.owner.run_read(
            lambda tx: load_survivors(tx, attempt.view, fused), ctx
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
            survivors.candidates,
            tuple(item.key for item in channels[0].source_candidates)
            if channels and plan.lexical
            else (),
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
            lanes=survivors.lanes,
        )
        paths = tuple(
            path
            for item in (*graph_candidates.candidates, *graph_candidates.enrichments)
            for path in item.paths
        )
        facts = await prepare_rerank_facts(
            self.owner, attempt, admission, votes, ctx, paths=paths
        )
        if request.exact_only or not plan.rerank:
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
            reranker = self.reranker
            if (
                self.auto_rerank
                and reranker is not None
                and self.rerank_counter is not None
            ):
                from tychora.application.reranking import _create_injected_reranker

                reranker = await _create_injected_reranker(
                    self.owner,
                    runtime=reranker._runtime,
                    scope=scope,
                    counter=self.rerank_counter,
                    ctx=ctx,
                )
            ranked = await rerank_candidates(
                request,
                admission,
                RerankContext(
                    owner=self.owner,
                    attempt=attempt,
                    reranker=reranker,
                    counter=self.rerank_counter,
                    facts=cast(Any, facts),
                    execution=ctx,
                    allow_degraded=self.owner.config.values.retrieval.allow_degraded,
                    unavailable=self.rerank_unavailable,
                ),
            )
        relations = _graph_relations(paths, graph_view)
        selection = assemble_segments(
            ranked,
            attempt.view,
            self.owner.config,
            self.counter,
            graph_view=graph_view,
            continuation_paths=paths,
            historical_kind_roles=attempt.historical_kind_roles,
            total_tokens=request.budgets.max_tokens,
            result_limit=request.limit,
        )
        visual_candidates = await prepare_visual_candidates(
            self.owner, attempt, ranked, ctx
        )
        visuals = select_visuals(visual_candidates, selection.segments)
        records = await self.owner.run_read(
            lambda tx: prepare_records(tx, selection, ranked, attempt), ctx
        )
        checks = RelationChecks(status="not_evaluated")
        check_settings = self.owner.config.values.checks.relations
        if (
            not request.exact_only
            and check_settings.enabled
            and graph is not None
            and "graph" not in plan.skipped
        ):
            checks = await self.owner.run_write(
                lambda: inspect_relations(
                    graph,
                    graph_view,
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
                ),
                ctx,
                "interactive",
            )
        coverage = build_coverage(
            completed=completed,
            unavailable=incomplete,
            truncated=truncated,
            rerank_failed=ranked.failure is not None,
            graph_exhausted=graph_candidates.exhausted,
        ).model_copy(update={"skipped_channels": plan.skipped})
        return (
            CachedSelection.of(
                selection,
                PreparedEvidence(
                    records=records,
                    checks=checks,
                    relations=tuple(relations),
                    visuals=visuals,
                    coverage=coverage,
                    warnings=attempt.warnings,
                    decisions=_edition_decisions(attempt.decisions),
                    candidate_count=len(fused),
                ),
            ),
            lexical_epoch,
        )

    def _missing_batches(
        self,
        view: EligibilityView,
        slots: tuple[str, ...],
        incomplete: list[str] | None = None,
    ) -> list[str]:
        """Channels among ``slots`` missing a batch of an eligible revision.

        Strict mode refuses the query; degraded mode skips the channel and
        reports it unavailable, appended to ``incomplete`` when given.
        """
        missing = [
            _SLOT_CHANNELS.get(slot, slot) for slot in missing_batch_slots(view, slots)
        ]
        self._refuse_strict(missing, "batch.missing")
        if incomplete is not None:
            incomplete.extend(missing)
        return missing

    def _refuse_strict(self, channels: list[str], reason: str) -> None:
        """Refuse incomplete ``channels`` with ``reason`` unless degradation is allowed."""
        if channels and not self.owner.config.values.retrieval.allow_degraded:
            raise TychoraError(
                "INDEX_INCOMPLETE", _INCOMPLETE_MESSAGES[reason], reason=reason
            )

    async def _text_candidates(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        ctx: ExecutionContext,
    ) -> _VectorChannel:
        from tychora.contracts.requests import PassageInput, QuestionInput

        if not isinstance(request.input, (QuestionInput, PassageInput)):
            return (), False
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
        return await self._vector_channel(index, attempt, query_vector.values, ctx)

    async def _vector_channel(
        self,
        index: VectorIndex,
        attempt: EligibilityAttempt,
        values: tuple[float, ...],
        ctx: ExecutionContext,
        candidates: Callable[..., _VectorChannel] = vector_candidates,
    ) -> _VectorChannel:
        """Search through the view on the writer, then map owners in one pooled read."""
        payload = struct.pack("<" + "f" * len(values), *values)
        ranked = await self.owner.run_write(
            lambda: index.search(attempt.view, payload, CHANNEL_LIMIT, ctx),
            ctx,
            "interactive",
        )
        return await self.owner.run_read(
            lambda tx: candidates(tx, attempt.view, ranked, CHANNEL_LIMIT), ctx
        )

    async def _visual_candidates(
        self,
        request: QueryRequest,
        scope: RequestScope,
        attempt: EligibilityAttempt,
        ctx: ExecutionContext,
    ) -> _VectorChannel:
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
            query = await self.owner.run_write(
                lambda: self._registered_image_query(asset_ref, attempt),
                ctx,
                "interactive",
            )
        elif isinstance(request.input, (QuestionInput, PassageInput)):
            query = TextQuery(text=request.input.text)
        else:
            raise TychoraError("INVALID_REQUEST", "Visual query input is invalid.")
        vector = await attempt.owner_provider(
            lambda: self.owner.embed_visual_query(current, query, ctx, scope=scope)
        )
        return await self._vector_channel(
            index, attempt, vector.values, ctx, visual_candidates
        )

    async def _augment_graph(
        self,
        request: QueryRequest,
        attempt: EligibilityAttempt,
        fused: Sequence[FusedCandidate],
        plan: QueryPlan,
        ctx: ExecutionContext,
    ) -> tuple[GraphScope | None, GraphCandidates, tuple[ChannelCandidate, ...]]:
        """The graph scope, candidates and source votes, when a graph channel runs."""
        graph = self.graph
        if request.exact_only or graph is None or "graph" in plan.skipped:
            return None, GraphCandidates(), ()
        scoped = await self._graph_scope(attempt, ctx)
        if scoped is None:
            # Only a graph the request selected itself is refused.
            if "graph" in plan.explicit:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Pinned graph generation is unavailable",
                    reason="graph.disabled",
                )
            return None, GraphCandidates(), ()
        candidates = await self.owner.run_write(
            lambda: augment(
                scoped,
                fused,
                self.owner.config,
                graph,
                ctx,
                plan.graph_strategies,
            ),
            ctx,
            "interactive",
        )
        votes = tuple(
            ChannelCandidate(key=("unit", item.unit_id), rank=rank)
            for rank, item in enumerate(candidates.candidates, 1)
            if item.unit_id in scoped.source_unit_ids
        )
        return scoped, candidates, votes

    async def _graph_scope(
        self, attempt: EligibilityAttempt, ctx: ExecutionContext
    ) -> GraphScope | None:
        """The attempt's graph scope, derived once per cached view."""
        key = eligible_cache_key(attempt.view)
        return await self.owner.run_read(
            lambda tx: self.owner.eligibility_cache.graph_scope(
                key, lambda: graph_scope(tx, attempt.view)
            ),
            ctx,
        )

    def _registered_image_query(
        self, asset_id: str, attempt: EligibilityAttempt
    ) -> RegisteredImageQuery:
        view = attempt.view
        with self.owner.store.uow.read() as tx:
            _require_image_asset(tx, view, asset_id)
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
            source_rows = self.owner.db.execute(
                "SELECT u.unit_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE ua.asset_id=? AND ua.role='source' ORDER BY u.unit_id LIMIT 2049",
                (asset_id,),
            ).fetchall()
            if not source_rows or len(source_rows) > 2048:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Image query source links are invalid."
                )
            _require_image_sources(
                tx, view, row[8], tuple(item[0] for item in source_rows)
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
        dependencies = frozenset({asset_id, *(item[0] for item in source_rows)})
        ref = ArtifactRef.from_row((row[2], *row[9:12]))
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
        prepared: CachedSelection,
        eligible: tuple[frozenset[str], frozenset[str]],
    ) -> DeliveredResult:
        return self.assembler.assemble(
            prepared.selection(),
            prepared.records,
            prepared.checks,
            request,
            ResponseContext(
                run_id=str(uuid4()),
                snapshot_id=attempt.view.snapshot_id,
                scope=scope,
                counter=self.counter,
                profiles=self.profiles,
                eligible_unit_ids=eligible[0],
                eligible_asset_ids=eligible[1],
                relations=prepared.relations,
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


def _require_image_asset(
    tx: ReadTransaction, view: EligibilityView, asset_id: str
) -> None:
    """Refuse an image query asset the view does not admit."""
    fact = next(iter(tx.eligibility.owner_facts((asset_id,))), None)
    if fact is None or not is_eligible_asset(view, fact):
        raise TychoraError(
            "FORBIDDEN_SCOPE",
            "Image query asset is not eligible.",
            reason="scope.dependency_ineligible",
        )


def _require_image_sources(
    tx: ReadTransaction,
    view: EligibilityView,
    revision_id: str,
    unit_ids: tuple[str, ...],
) -> None:
    """Refuse an image query whose revision or any source unit the view does not admit."""
    facts = {f.owner_id: f for f in tx.eligibility.owner_facts(unit_ids)}
    if revision_id not in view.revisions or not all(
        (unit := facts.get(uid)) is not None and is_eligible_unit(view, unit)
        for uid in unit_ids
    ):
        raise TychoraError(
            "FORBIDDEN_SCOPE",
            "Image query source is not eligible.",
            reason="scope.dependency_ineligible",
        )


def _graph_relations(
    paths: tuple[GraphPath, ...], graph_view: GraphScope | None
) -> list[RelationPath]:
    """Each distinct expansion path as a relation; an ineligible one is refused."""
    eligible = graph_view.graph_eligible_unit_ids if graph_view else frozenset()
    blocked = graph_view.blocked_assertion_ids if graph_view else frozenset()
    relations = []
    seen_paths = set()
    for path in paths:
        if (
            not {path.source_unit_id, path.target_unit_id} <= eligible
            or not path.required_support_ids <= eligible
            or set(path.assertion_ids) & blocked
            or path.status not in {"recorded", "confirmed"}
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Graph expansion returned an ineligible path"
            )
        key = (path.assertion_ids, path.source_unit_id, path.target_unit_id)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        relations.append(
            RelationPath(
                unit_ids=(path.source_unit_id, path.target_unit_id),
                assertion_ids=path.assertion_ids,
                predicates=(path.predicate,),
                support_unit_ids=path.required_support_ids,
                provenance=FrozenJsonMap(
                    {
                        "status": path.status,
                        "confidence": path.confidence,
                    }
                ),
            )
        )
    return relations


def _delivered_eligibility(
    tx: ReadTransaction, view: EligibilityView, prepared: CachedSelection
) -> tuple[frozenset[str], frozenset[str]]:
    """The units and assets a delivery names that the view still admits."""
    units = {
        *(uid for record in prepared.records for uid in record.dependencies),
        *(uid for path in prepared.relations for uid in path.unit_ids),
        *(uid for path in prepared.relations for uid in path.support_unit_ids),
        *(
            uid
            for visual in prepared.visuals
            for proxy in visual.proxies
            for uid in proxy.dependencies
        ),
    }
    assets = {visual.asset_id for visual in prepared.visuals}
    facts = tx.eligibility.owner_facts(tuple(sorted(units | assets)))
    return (
        frozenset(f.owner_id for f in facts if is_eligible_unit(view, f)),
        frozenset(f.owner_id for f in facts if is_eligible_asset(view, f)),
    )


def _edition_decisions(
    decisions: tuple[EditionDecision, ...],
) -> tuple[DecisionRequirement, ...]:
    """A decision requirement for each edition decision selection left open."""
    return tuple(
        DecisionRequirement(
            requirement_id=digest(
                "edition-decision-v1",
                decision.reason,
                decision.source_id,
                list(decision.revision_ids),
            ),
            reason=decision.message(),
            revision_ids=decision.revision_ids,
        )
        for decision in decisions
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
