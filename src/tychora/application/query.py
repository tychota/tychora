"""Owner-aware evidence query composition over reviewed retrieval producers."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property, partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any, NamedTuple, cast
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
from tychora.configuration.models import EmbeddingSettings, ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.configuration.stopwords import load_stopwords
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidate, ChannelCandidates
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
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.graph import GraphCandidates, GraphPath, GraphScope
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
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
from tychora.contracts.snapshots import IndexFamily
from tychora.formatting.render import OutputProfile
from tychora.ports.execution import ExecutionContext
from tychora.ports.query import TextQueryProvider, VisualQueryProvider
from tychora.ports.repositories import ReadTransaction
from tychora.ports.tokens import TokenCounter
from tychora.ports.vectors import VectorIndex
from tychora.retrieval.admission import Admission, LanePolicy, admit
from tychora.retrieval.attempt import (
    AttemptHost,
    EligibilityAttempt,
    is_eligible_asset,
    is_eligible_unit,
    resolve_snapshot,
    run_pinned_eligibility,
)
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.cached_selection import (
    CACHE_OVERHEAD_BYTES,
    CachedSelection,
    PreparedEvidence,
    Selection,
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
    from PIL.Image import Image as PillowImage

    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.application.owner import WorkspaceOwner


_LOG = logging.getLogger(__name__)
# The visual channel reads the publication slot named ``image``.
_SLOT_CHANNELS = {"image": "visual"}
_INCOMPLETE_MESSAGES = {
    "channel.unavailable": "A retrieval channel is incomplete.",
    "batch.missing": "A batch of an eligible revision is missing.",
}

type _VectorChannel = tuple[tuple[ChannelCandidate, ...], bool]
type _VectorCall = Callable[
    [QueryRequest, RequestScope, EligibilityAttempt, ExecutionContext],
    Awaitable[_VectorChannel],
]
type _FactoryIdentity = tuple[EmbeddingCapabilities, str, bool, bool]

# A vector channel failure degraded mode reports instead of refusing.
_DEGRADABLE = frozenset(
    {"CAPABILITY_MISSING", "INDEX_INCOMPLETE", "PROVIDER_FAILURE", "FORBIDDEN_SCOPE"}
)
# A memo past this many entries starts over; its keys grow with snapshots.
_MEMO_LIMIT = 256


@dataclass(frozen=True)
class _QueryJob:
    """One authorized request with its mode, requirements and plan."""

    request: QueryRequest
    scope: RequestScope
    mode: FrozenJsonMap
    requirements: tuple[ResponseRequirement, ...]
    plan: QueryPlan
    started: float


@dataclass(frozen=True)
class _QueryInputs:
    """One job under its pinned attempt and execution context; each stage reads it."""

    job: _QueryJob
    attempt: EligibilityAttempt
    ctx: ExecutionContext


@dataclass(frozen=True)
class _Channels:
    """What the exact, lexical and vector channels returned, and how each ended."""

    exact_lexical: tuple[ChannelCandidates, ...]
    votes: Mapping[str, tuple[ChannelCandidate, ...]]
    completed: tuple[str, ...]
    incomplete: tuple[str, ...]
    truncated: tuple[str, ...]
    lexical_epoch: int


@dataclass(frozen=True)
class _Fused:
    """The fused candidates and their votes, with the graph that augmented them."""

    fused: tuple[FusedCandidate, ...]
    votes: Mapping[str, tuple[ChannelCandidate, ...]]
    graph: GraphScope | None
    graph_candidates: GraphCandidates
    paths: tuple[GraphPath, ...]


class _FamilyChannel(NamedTuple):
    """A vector channel's binding and configured settings, as the family check reads them."""

    channel: str
    provider: TextQueryProvider | VisualQueryProvider | None
    index: VectorIndex | None
    settings: EmbeddingSettings
    modality: str


@dataclass
class _Digests:
    """What a service computes once per configuration and binding.

    A shallow copy of the service shares it, so every key names the inputs
    that can vary between copies.
    """

    fingerprints: dict[tuple[object, ...], str] = field(default_factory=dict)
    family_settings: dict[tuple[str, str], JsonValue] = field(default_factory=dict)
    validated: set[tuple[object, ...]] = field(default_factory=set)


def _remember[K, V](memo: dict[K, V], key: K, compute: Callable[[], V]) -> V:
    """The value under ``key``, computed once; a full memo starts over.

    Owner and pool threads share the memo: a concurrent ``clear`` can drop
    an entry between the check and the read, so the hit path uses ``get`` and
    the miss path returns the value it computed.
    """
    value = memo.get(key)
    if value is None:
        value = compute()
        if len(memo) >= _MEMO_LIMIT:
            memo.clear()
        memo[key] = value
    return value


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
        self.auto_rerank = auto_rerank
        self.counter = output_counter
        self.caches = caches or QueryCaches()
        bindings.validate(owner.config, self.caches)
        self.history = history or HistoryRepository(owner.db, artifacts=owner.artifacts)
        self.profiles = profiles or (_default_profile(owner.config, self.registry),)
        self.assembler = ResponseAssembler()
        self.delivery_serializer = None
        self.delivery_serialization = None
        # Seeded here so a ``copy.copy`` of a fresh service shares the memo.
        self.__dict__["_digests"] = _Digests()

    @cached_property
    def _digests(self) -> _Digests:
        """The service's memo; ``__init__`` seeds it, this covers bare instances."""
        return _Digests()

    def _projection_fingerprint(self, config: ResolvedConfig | None = None) -> str:
        config = config or self.owner.config
        profiles, serialization = self.profiles, self.delivery_serialization
        return _remember(
            self._digests.fingerprints,
            ("projection", config.config_sha, profiles, serialization),
            lambda: _projection_digest(config, profiles, serialization),
        )

    def _provider_fingerprint(self, config: ResolvedConfig | None = None) -> str:
        config = config or self.owner.config
        bindings = self.bindings
        bound = (
            _factory_identity(bindings.text_provider),
            _factory_identity(bindings.visual_provider),
            bindings.text_index is not None,
            bindings.visual_index is not None,
            bindings.reranker.credential_binding_id if bindings.reranker else None,
        )
        return _remember(
            self._digests.fingerprints,
            ("provider", config.config_sha, config.query_fingerprint, bound),
            lambda: _provider_digest(config, bound),
        )

    def _family_channels(self) -> tuple[_FamilyChannel, ...]:
        """The text and image channels with their bindings and settings."""
        bindings, providers = self.bindings, self.owner.config.values.providers
        return (
            _FamilyChannel(
                "text",
                bindings.text_provider,
                bindings.text_index,
                providers.text,
                "text",
            ),
            _FamilyChannel(
                "image",
                bindings.visual_provider,
                bindings.visual_index,
                providers.image,
                "visual",
            ),
        )

    def _validate_provider_families(
        self, snapshot: SnapshotView, plan: QueryPlan
    ) -> None:
        """Check each planned channel's provider against its pinned family before cache reuse.

        The provider's own identity was checked once, by ``QueryBindings.validate``.
        A snapshot never changes, so a channel that passed for a snapshot, a
        configuration and a provider capability is not checked again.
        """
        retained: dict[str, ResolvedConfig] = {}
        validated = self._digests.validated
        for family in self._family_channels():
            if family.provider is None or not getattr(plan, family.channel):
                continue
            key = (
                snapshot.snapshot_id,
                family.channel,
                self.owner.config.config_sha,
                family.provider.capabilities,
                family.index is not None,
            )
            if key in validated:
                continue
            if self._check_family(family, snapshot, plan, retained):
                if len(validated) >= _MEMO_LIMIT:
                    validated.clear()
                validated.add(key)

    def _check_family(
        self,
        family: _FamilyChannel,
        snapshot: SnapshotView,
        plan: QueryPlan,
        retained: dict[str, ResolvedConfig],
    ) -> bool:
        """Check one channel's provider against its pinned family; ``False`` without one.

        Only a channel the request selected itself is refused for a missing family.
        """
        slot = snapshot.family(family.channel)
        if slot is None:
            if family.channel in plan.explicit:
                raise TychoraError(
                    "INDEX_INCOMPLETE",
                    "Pinned vector family is unavailable.",
                    reason="channel.unavailable",
                )
            return False
        if family.index is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Pinned vector index is unavailable."
            )
        assert family.provider is not None
        caps = family.provider.capabilities
        spec = _family_spec(slot.family)
        origin_sha = _family_origin(slot.family)
        if origin_sha not in retained:
            retained[origin_sha] = (
                self.owner.config
                if origin_sha == self.owner.config.retained_sha
                else self.owner.store.retained_config_sha(origin_sha)
            )
        raw_capabilities = slot.family.config_json.get("capabilities")
        if _retained_capabilities(raw_capabilities) != caps:
            raise _provider_differs()
        retained_settings = self._retained_settings(family, retained[origin_sha])
        expected = digest(
            f"{family.channel}-family-config-v1",
            origin_sha,
            _remember(
                self._digests.family_settings,
                (origin_sha, family.channel),
                lambda: retained_settings.model_dump(mode="json"),
            ),
            raw_capabilities,
        )
        if (
            spec.config_sha != expected
            or spec.family_id != slot.family.family_id
            or spec.modality != family.modality
            or (spec.provider, spec.model, spec.dimensions)
            != (caps.provider, caps.model, caps.dimensions)
            or spec.representation_fingerprint != caps.representation_fingerprint
        ):
            raise _provider_differs()
        return True

    def _retained_settings(
        self, family: _FamilyChannel, retained_config: ResolvedConfig
    ) -> EmbeddingSettings:
        """The channel's retained settings, refused unless the current ones match."""
        retained_settings = getattr(retained_config.values.providers, family.channel)
        unbound = {"credential_ref": None}
        if not pinned_runtime_compatible(
            retained_config, self.owner.config
        ) or family.settings.model_copy(update=unbound) != retained_settings.model_copy(
            update=unbound
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Query representation differs from pinned configuration.",
            )
        return retained_settings

    def _replay_ready(self, request: QueryRequest) -> bool:
        """Conservatively establish current local/remote capability before channels."""
        if request.exact_only:
            return True
        offline = self.owner.config.values.offline
        if (
            needs_text(request)
            and self.owner.config.values.index.text.backend != "disabled"
        ):
            provider = self.bindings.text_provider
            if (
                provider is None
                or self.bindings.text_index is None
                or provider.caches is not self.caches
                or not provider.available
                or (offline and provider.remote)
            ):
                return False
        if (
            needs_visual(request)
            and self.owner.config.values.index.image.backend != "disabled"
        ):
            visual = self.bindings.visual_provider
            if (
                visual is None
                or self.bindings.visual_index is None
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
                self.bindings.reranker is None
                or self.bindings.rerank_counter is None
                or self.bindings.rerank_unavailable is not None
                or not self.bindings.reranker.available
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

            with self.owner.store.uow.read() as tx:
                snapshot = resolve_snapshot(self.owner.catalog, tx, None)

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
        validate_query_text(request, self._query_counter())
        mode = _mode(self.registry, self.owner.config, request)
        requirements = tuple(
            ResponseRequirement.model_validate(value)
            for value in cast(tuple[object, ...], mode.get("requirements", ()))
        )
        active_requirements(requirements, request)
        job = _QueryJob(
            request,
            granted,
            mode,
            requirements,
            plan_for(request, self.owner.config, self.bindings),
            started,
        )
        return await run_pinned_eligibility(
            AttemptHost(self.compiler, self.owner, ctx),
            EligibilityInputs(None, granted, request, self.owner.config),
            lambda attempt: self._operation(_QueryInputs(job, attempt, ctx)),
            on_invalidated=self._supersede,
        )

    def _query_counter(self) -> TokenCounter:
        """The counter that bounds query text: the first bound provider's, else ours."""
        if self.bindings.text_provider is not None:
            return self.bindings.text_provider.counter
        if self.bindings.visual_provider is not None:
            return self.bindings.visual_provider.counter
        return self.bindings.rerank_counter or self.counter

    async def _operation(self, query: _QueryInputs) -> DeliveredResult:
        """Prepare or reuse the job's result under one attempt, deliver and record it."""
        job, attempt, ctx = query.job, query.attempt, query.ctx
        snapshot = attempt.snapshot
        assert snapshot is not None
        await self.owner.run_write(
            lambda: self._validate_provider_families(snapshot, job.plan),
            ctx,
            "interactive",
        )
        prepared, lexical_epoch, cache_hit = await self._cached_prepare(query)
        eligible = await self.owner.run_read(
            lambda tx: _delivered_eligibility(tx, attempt.view, prepared), ctx
        )
        delivered = self._deliver(job, attempt, prepared, eligible)
        metrics = RunMetrics(
            policy_epoch=attempt.view.policy_epoch,
            lexical_statistics_epoch=lexical_epoch,
            config_sha=self.owner.config.config_sha,
            request_fingerprint=digest(
                "query-request-v1", job.request.model_dump(mode="json")
            ),
            duration_ms=max(0, int((time.monotonic() - job.started) * 1000)),
            candidate_count=prepared.candidate_count,
            cache_hit=cache_hit,
        )
        await self.owner.run_write(
            lambda: self._record(job, delivered, metrics), ctx, "interactive"
        )
        return delivered

    def _record(
        self, job: _QueryJob, delivered: DeliveredResult, metrics: RunMetrics
    ) -> None:
        """Record the delivered run, retaining its request when history keeps them."""
        retained = (
            self.history.retain_request(
                delivered.result.run_id,
                job.request,
                scope=job.scope,
                snapshot_id=delivered.result.snapshot_id,
                config_sha=self.owner.config.config_sha,
                projection_fingerprint=self._projection_fingerprint(),
                provider_fingerprint=self._provider_fingerprint(),
            )
            if self.history.retains_requests
            else None
        )
        self.history.record(delivered, metrics, retained)

    async def _supersede(self, delivered: DeliveredResult) -> None:
        """Mark a run an invalidated attempt delivered as superseded."""
        await self.owner.run_write(
            lambda: self.history.supersede(delivered.result.run_id),
            ExecutionContext(),
            "interactive",
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
        self, query: _QueryInputs
    ) -> tuple[CachedSelection, int, bool]:
        """The cached entry for this request, or a fresh one cached when allowed.

        Returns the entry, the lexical epoch it was prepared at, and whether
        it came from the cache. An entry larger than the response bound plus
        ``CACHE_OVERHEAD_BYTES`` is served uncached, and so is one a failure
        made partial or unavailable (an unavailable channel or rerank), so a
        transient failure is never replayed. A complete entry whose channels a
        deterministic limit truncated is cached.
        """
        request, view = query.job.request, query.attempt.view
        lexical_epoch = await self.owner.run_read(
            lambda tx: tx.workspace.lexical_epoch(), query.ctx
        )
        cached = self._cached_entry(self._result_key(request, view, lexical_epoch))
        if cached is not None:
            return cached, lexical_epoch, True
        prepared, actual_epoch = await self._prepare(query)
        entry = prepared.to_bytes()
        bound = self.owner.config.values.limits.max_response_bytes
        if (
            not prepared.coverage.unavailable_channels
            and prepared.coverage.status != "unavailable"
            and len(entry) <= bound + CACHE_OVERHEAD_BYTES
        ):
            self.caches.put_result(self._result_key(request, view, actual_epoch), entry)
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

    async def _prepare(self, query: _QueryInputs) -> tuple[CachedSelection, int]:
        """Run the stages: channels, fusion, admission, reranking and assembly."""
        channels = await self._channels(query)
        fused = await self._fuse(query, channels)
        admission, unread = await self._admit(query, channels, fused)
        ranked = await self._rerank(query, admission, fused)
        selection, records, visuals, checks, relations = await self._assemble(
            query, ranked, fused
        )
        coverage = build_coverage(
            completed=channels.completed,
            unavailable=channels.incomplete,
            truncated=(*channels.truncated, *(("survivors",) if unread else ())),
            rerank_failed=ranked.failure is not None,
            graph_exhausted=fused.graph_candidates.exhausted,
        ).model_copy(update={"skipped_channels": query.job.plan.skipped})
        return (
            CachedSelection.of(
                selection,
                PreparedEvidence(
                    records=records,
                    checks=checks,
                    relations=relations,
                    visuals=visuals,
                    coverage=coverage,
                    warnings=query.attempt.warnings,
                    decisions=_edition_decisions(query.attempt.decisions),
                    candidate_count=len(fused.fused),
                ),
            ),
            channels.lexical_epoch,
        )

    async def _channels(self, query: _QueryInputs) -> _Channels:
        """Run the exact, lexical and vector channels; strict mode refuses a gap."""
        incomplete: list[str] = []
        exact_lexical = await self._exact_lexical(query, incomplete)
        lexical_epoch = await self._lexical_epoch(exact_lexical, query.ctx)
        incomplete.extend(c.channel for c in exact_lexical if not c.complete)
        completed: list[str] = [c.channel for c in exact_lexical if c.complete]
        # A deterministic bound cut these short: reported, never refused.
        truncated: list[str] = [c.channel for c in exact_lexical if c.truncated]
        self._refuse_strict(incomplete, "channel.unavailable")
        votes: dict[str, tuple[ChannelCandidate, ...]] = {
            c.channel: (*c.source_candidates, *c.asset_candidates)
            for c in exact_lexical
        }
        for channel, result in await self._vector_results(query, incomplete):
            if isinstance(result, TychoraError):
                self._refuse_failure(result)
                incomplete.append(channel)
                continue
            votes[channel], cut = result
            if cut:
                truncated.append(channel)
            completed.append(channel)
        return _Channels(
            exact_lexical,
            votes,
            tuple(completed),
            tuple(incomplete),
            tuple(truncated),
            lexical_epoch,
        )

    async def _exact_lexical(
        self, query: _QueryInputs, incomplete: list[str]
    ) -> tuple[ChannelCandidates, ...]:
        """The exact and lexical channels; a missing lexical batch skips lexical alone."""
        request, plan, ctx = query.job.request, query.job.plan, query.ctx
        view = query.attempt.view
        lexical_runs = plan.lexical and runs_lexical(request)
        lexical_missing = self._missing_batches(
            view, ("lexical",) if lexical_runs else (), incomplete
        )
        return await self.owner.run_read(
            lambda tx: retrieve_exact_lexical(
                tx,
                view,
                request,
                self.registry,
                80,
                ctx,
                stopwords=self.stopwords,
                with_lexical=plan.lexical and not lexical_missing,
            ),
            ctx,
        )

    async def _lexical_epoch(
        self, exact_lexical: tuple[ChannelCandidates, ...], ctx: ExecutionContext
    ) -> int:
        """The lexical statistics epoch the channels read, else the current one."""
        for channel in exact_lexical:
            if channel.lexical_statistics_epoch is not None:
                return channel.lexical_statistics_epoch
        return await self.owner.run_read(lambda tx: tx.workspace.lexical_epoch(), ctx)

    def _refuse_failure(self, error: TychoraError) -> None:
        """Raise a vector channel's failure unless degraded mode reports it."""
        if (
            not self.owner.config.values.retrieval.allow_degraded
            or error.code not in _DEGRADABLE
        ):
            raise error

    async def _vector_results(
        self, query: _QueryInputs, incomplete: list[str]
    ) -> tuple[tuple[str, _VectorChannel | TychoraError], ...]:
        """Run the text and visual channels side by side; a failure is returned."""
        request, plan, view = query.job.request, query.job.plan, query.attempt.view
        index = self.owner.config.values.index
        text_runs = (
            plan.text and needs_text(request) and index.text.backend != "disabled"
        )
        image_runs = (
            plan.image and needs_visual(request) and index.image.backend != "disabled"
        )
        # Strict mode refuses a missing batch; degraded mode records it.
        text_missing = self._missing_batches(
            view, ("text",) if text_runs else (), incomplete
        )
        image_missing = self._missing_batches(
            view, ("image",) if image_runs else (), incomplete
        )
        calls: list[tuple[str, _VectorCall]] = []
        if text_runs and not text_missing:
            calls.append(("text", self._text_candidates))
        if image_runs and not image_missing:
            calls.append(("visual", self._visual_candidates))
        results = await _run_independent_channels(
            tuple(partial(_captured, call, query) for _, call in calls), query.ctx
        )
        return tuple(zip((name for name, _ in calls), results, strict=True))

    async def _fuse(self, query: _QueryInputs, channels: _Channels) -> _Fused:
        """Fuse the channel votes, then again with the graph channel's when it runs."""
        votes = dict(channels.votes)
        fused = fuse(votes)
        graph, graph_candidates, graph_votes = await self._augment_graph(
            query.job.request, query.attempt, fused, query.job.plan, query.ctx
        )
        if graph_votes:
            votes["graph"] = graph_votes
            fused = fuse(votes)
        paths = tuple(
            path
            for item in (*graph_candidates.candidates, *graph_candidates.enrichments)
            for path in item.paths
        )
        return _Fused(fused, votes, graph, graph_candidates, paths)

    async def _admit(
        self, query: _QueryInputs, channels: _Channels, fused: _Fused
    ) -> tuple[Admission, int]:
        """Admit the fused survivors under the mode's and the request's lane needs.

        Returns the admission and how many fused candidates the survivor bound
        left unread; a positive count is reported as a ``survivors`` truncation.
        """
        request, attempt = query.job.request, query.attempt
        exact_keys = tuple(
            item.key for item in channels.exact_lexical[0].source_candidates
        )
        graph_lists = {
            strategy: tuple(
                ("unit", item.unit_id)
                for item in fused.graph_candidates.candidates
                if item.strategy == strategy
            )
            for strategy in ("structural", "semantic")
        }
        survivors = await self.owner.run_read(
            lambda tx: load_survivors(
                tx, attempt.view, fused.fused, exact_keys, graph_lists
            ),
            query.ctx,
        )
        required = cast(tuple[str, ...], query.job.mode.get("required_lanes", ()))
        active = active_requirements(query.job.requirements, request)
        required = tuple(
            dict.fromkeys((*required, *(r.value for r in active if r.target == "lane")))
        )
        lane_order = self.owner.config.values.output.lane_order or tuple(
            self.registry.lanes
        )
        admission = admit(
            survivors.candidates,
            exact_keys,
            graph_lists,
            LanePolicy(
                mode=self.owner.config.values.selection.lane_reservation,
                order=lane_order,
                required=required,
                known=tuple(self.registry.lanes),
            ),
            lanes=survivors.lanes,
        )
        return admission, survivors.beyond_limit

    async def _rerank(
        self, query: _QueryInputs, admission: Admission, fused: _Fused
    ) -> RankedEvidence:
        """Rerank the admitted candidates, or keep their order when reranking is off."""
        request, attempt, ctx = query.job.request, query.attempt, query.ctx
        facts = await prepare_rerank_facts(
            self.owner, attempt, admission, fused.votes, ctx, paths=fused.paths
        )
        if request.exact_only or not query.job.plan.rerank:
            return RankedEvidence(
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
        return await rerank_candidates(
            request,
            admission,
            RerankContext(
                owner=self.owner,
                attempt=attempt,
                reranker=await self._reranker(query),
                counter=self.bindings.rerank_counter,
                facts=cast(Any, facts),
                execution=ctx,
                allow_degraded=self.owner.config.values.retrieval.allow_degraded,
                unavailable=self.bindings.rerank_unavailable,
            ),
        )

    async def _reranker(self, query: _QueryInputs) -> VoyageReranker | None:
        """The bound reranker, rebound to the query's scope when it reranks itself."""
        reranker = self.bindings.reranker
        if (
            not self.auto_rerank
            or reranker is None
            or self.bindings.rerank_counter is None
        ):
            return reranker
        from tychora.application.reranking import _create_injected_reranker

        return await _create_injected_reranker(
            self.owner,
            runtime=reranker._runtime,
            scope=query.job.scope,
            counter=self.bindings.rerank_counter,
            ctx=query.ctx,
        )

    async def _assemble(
        self, query: _QueryInputs, ranked: RankedEvidence, fused: _Fused
    ) -> tuple[
        Selection,
        tuple[StructuredRecord, ...],
        tuple[VisualEvidence, ...],
        RelationChecks,
        tuple[RelationPath, ...],
    ]:
        """Select segments and visuals, prepare their records and check relations."""
        request, attempt, ctx = query.job.request, query.attempt, query.ctx
        relations = _graph_relations(fused.paths, fused.graph)
        selection = assemble_segments(
            ranked,
            attempt.view,
            self.owner.config,
            self.counter,
            graph_view=fused.graph,
            continuation_paths=fused.paths,
            historical_kind_roles=attempt.historical_kind_roles,
            total_tokens=request.budgets.max_tokens,
            result_limit=request.limit,
        )
        visual_options = await prepare_visual_candidates(
            self.owner, attempt, ranked, ctx
        )
        records = await self.owner.run_read(
            lambda tx: prepare_records(tx, selection, ranked, attempt), ctx
        )
        return (
            selection,
            records,
            select_visuals(visual_options, selection.segments),
            await self._relation_checks(query, selection, fused),
            tuple(relations),
        )

    async def _relation_checks(
        self, query: _QueryInputs, selection: Selection, fused: _Fused
    ) -> RelationChecks:
        """Check the selected units' relations when checks are on and a graph runs."""
        graph = self.bindings.graph
        settings = self.owner.config.values.checks.relations
        if (
            query.job.request.exact_only
            or not settings.enabled
            or graph is None
            or "graph" in query.job.plan.skipped
        ):
            return RelationChecks(status="not_evaluated")
        unit_ids = tuple(
            dict.fromkeys(
                uid
                for segment in selection.segments
                for uid in segment.matched_unit_ids
            )
        )
        return await self.owner.run_write(
            lambda: inspect_relations(
                graph,
                fused.graph,
                unit_ids,
                self.owner.config.values.retrieval.graph.predicates,
                query.ctx,
                expansion_inspected_rows=fused.graph_candidates.inspected_rows,
                registry=self.registry,
                max_rows=self.owner.config.values.limits.graph_work_limit,
                max_assertions=settings.max_assertions,
            ),
            query.ctx,
            "interactive",
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
        if not isinstance(request.input, (QuestionInput, PassageInput)):
            return (), False
        query_text = request.input.text
        base = self.bindings.text_provider
        index = self.bindings.text_index
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
        base = self.bindings.visual_provider
        index = self.bindings.visual_index
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
        graph = self.bindings.graph
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
        """The descriptor of a registered image query, from its verified pixels."""
        view = attempt.view
        with self.owner.store.uow.read() as tx:
            _require_image_asset(tx, view, asset_id)
            registered = tx.assets.registered(asset_id)
            if (
                registered is None
                or registered.payload_state != "present"
                or registered.ref.storage_key != registered.asset.artifact_sha
            ):
                raise TychoraError(
                    "ORIGINAL_REMOVED", "Image query original is unavailable."
                )
            sources = tx.assets.links(asset_id, ("source",), 2049)
            if not sources or len(sources) > 2048:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Image query source links are invalid."
                )
            source_ids = tuple(link.unit_id for link in sources)
            _require_image_sources(tx, view, registered.revision_id, source_ids)
        pixels = _registered_pixels(self.owner, registered.ref)
        width, height = pixels.size
        descriptor = VisualInput.from_registered(
            asset=registered.asset,
            artifact_ref=registered.ref,
            revision_id=registered.revision_id,
            context="",
            dependency_ids=frozenset({asset_id, *source_ids}),
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
        job: _QueryJob,
        attempt: EligibilityAttempt,
        prepared: CachedSelection,
        eligible: tuple[frozenset[str], frozenset[str]],
    ) -> DeliveredResult:
        return self.assembler.assemble(
            prepared.selection(),
            prepared.records,
            prepared.checks,
            job.request,
            ResponseContext(
                run_id=str(uuid4()),
                snapshot_id=attempt.view.snapshot_id,
                scope=job.scope,
                counter=self.counter,
                profiles=self.profiles,
                eligible_unit_ids=eligible[0],
                eligible_asset_ids=eligible[1],
                relations=prepared.relations,
                visuals=prepared.visuals,
                coverage=prepared.coverage,
                warnings=prepared.warnings,
                decision_requirements=prepared.decisions,
                required_lanes=cast(
                    tuple[str, ...], job.mode.get("required_lanes", ())
                ),
                requirements=job.requirements,
                checks_required=self.owner.config.values.checks.relations.required,
                max_tokens=self.owner.config.values.limits.max_tokens,
                max_response_bytes=self.owner.config.values.limits.max_response_bytes,
                delivery_serializer=self.delivery_serializer,
                delivery_serialization=self.delivery_serialization,
            ),
        )


async def _captured(
    call: _VectorCall, query: _QueryInputs
) -> _VectorChannel | TychoraError:
    """Run one vector channel, returning its typed failure instead of raising it."""
    try:
        return await call(query.job.request, query.job.scope, query.attempt, query.ctx)
    except TychoraError as error:
        return error


def _projection_digest(
    config: ResolvedConfig,
    profiles: tuple[OutputProfile, ...],
    serialization: str | None,
) -> str:
    """The projection identity of the output configuration, profiles and serialization."""
    parts = (
        config.output_fingerprint,
        tuple(profile.model_dump(mode="json") for profile in profiles),
    )
    return digest(
        "query-projection-v1",
        *(parts if serialization is None else (*parts, serialization)),
    )


def _factory_identity(
    provider: TextQueryProvider | VisualQueryProvider | None,
) -> _FactoryIdentity | None:
    """What of a bound provider factory changes a query's provider identity."""
    if provider is None:
        return None
    return (
        provider.capabilities,
        provider.credential_binding_id,
        provider.remote,
        provider.available,
    )


def _provider_digest(
    config: ResolvedConfig,
    bound: tuple[
        _FactoryIdentity | None, _FactoryIdentity | None, bool, bool, str | None
    ],
) -> str:
    """The provider identity of the configuration and the bound collaborators."""
    text, visual, text_index, visual_index, rerank_binding = bound
    return digest(
        "query-provider-v1",
        config.query_fingerprint,
        config.values.providers.model_dump(mode="json"),
        config.values.index.model_dump(mode="json"),
        _factory_document(text),
        _factory_document(visual),
        text_index,
        visual_index,
        rerank_binding,
    )


def _factory_document(identity: _FactoryIdentity | None) -> JsonValue:
    """A provider factory identity as the digest records it."""
    if identity is None:
        return None
    capabilities, credential_binding_id, remote, available = identity
    return {
        "capabilities": capabilities.model_dump(mode="json"),
        "credential_binding_id": credential_binding_id,
        "remote": remote,
        "available": available,
    }


def _provider_differs() -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR", "Query provider differs from pinned vector family."
    )


def _family_spec(family: IndexFamily) -> FamilySpec:
    """The family's recorded specification; an absent or invalid one is refused."""
    raw = family.config_json.get("spec")
    if not isinstance(raw, Mapping):
        raise TychoraError("INDEX_INCOMPLETE", "Pinned vector family is unavailable.")
    try:
        return FamilySpec.model_validate(dict(raw))
    except ValueError:
        raise TychoraError(
            "INDEX_INCOMPLETE", "Pinned vector family is invalid."
        ) from None


def _family_origin(family: IndexFamily) -> str:
    """The retained configuration digest the family was built under."""
    origin_sha = family.config_json.get("effective_config_sha")
    if not isinstance(origin_sha, str) or len(origin_sha) != 64:
        raise TychoraError("INTEGRITY_ERROR", "Pinned vector family origin is invalid.")
    return origin_sha


def _retained_capabilities(raw: JsonValue) -> EmbeddingCapabilities:
    """The capability proof the family retained; an absent one needs a new family."""
    if raw is None:
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Retained vector capability proof is absent; publish a new family and snapshot.",
        )
    try:
        return EmbeddingCapabilities.model_validate_json(canonical_bytes(raw))
    except (TypeError, ValueError):
        raise TychoraError(
            "INTEGRITY_ERROR", "Retained vector capability proof is invalid."
        ) from None


def _registered_pixels(owner: WorkspaceOwner, ref: ArtifactRef) -> PillowImage:
    """The verified image upright in RGB, refused above the pixel ceiling."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise TychoraError(
            "CAPABILITY_MISSING", "Image operations require the documents extra."
        ) from None
    with owner.artifacts.open_verified(ref) as stream, Image.open(stream) as encoded:
        if encoded.width * encoded.height > 40_000_000:
            raise TychoraError("INVALID_REQUEST", "Image query exceeds pixel ceiling.")
        pixels = ImageOps.exif_transpose(encoded).convert("RGB")
        pixels.load()
    return pixels


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
