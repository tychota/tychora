"""Controlled retrieval comparisons over owner-safe mutation admission."""

from __future__ import annotations

import asyncio
import platform
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from io import BytesIO
from threading import Lock
from typing import Literal

from tychora.adapters.sqlite.evaluations import EvaluationRepository
from tychora.adapters.sqlite.gold_labels import SqliteGoldLabelResolver
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import (
    CaseEvaluation,
    ConfigurationVariant,
    EvaluationReport,
    EvaluationRequest,
    JobHandle,
    MetricSet,
    MetricValue,
)
from tychora.contracts.requests import ChannelSelection, QueryRequest, RequestScope
from tychora.evaluation.dataset import (
    GoldCase,
    LabelFailure,
    bind_cases,
    load_dataset,
    load_dataset_bytes,
)
from tychora.evaluation.metrics import metrics
from tychora.evaluation.provenance import RetainedBindingCache, verify_result_locators
from tychora.ports.execution import ExecutionContext

_QUERY_CONTROLS = frozenset({"mode", "modalities", "exact_only", "limit", "hints"})
_EXECUTION_CONTROLS = frozenset({"channels", "rerank"})
_CHANNELS = frozenset(
    {"lexical", "text", "image", "structural_graph", "semantic_graph"}
)
MAX_SEARCHES = 1024
MAX_RETAINED_CASE_BYTES = 64 * 1024 * 1024


def validate_workload(case_count: int, variant_count: int, repetitions: int) -> int:
    searches = case_count * variant_count * repetitions
    if searches > MAX_SEARCHES:
        raise TychoraError("LIMIT_EXCEEDED", "Evaluation exceeds 1024 searches")
    return searches


def validate_retained_size(current_bytes: int, additional_bytes: int) -> int:
    total = current_bytes + additional_bytes
    if total > MAX_RETAINED_CASE_BYTES:
        raise TychoraError(
            "LIMIT_EXCEEDED", "Evaluation exceeds 64 MiB retained results"
        )
    return total


def variant_channels(
    variant: ConfigurationVariant,
) -> tuple[ChannelSelection | None, Literal["auto", "on", "off"]]:
    """Explicit ablation controls as request fields, never from a variant's label."""
    controls = dict(variant.overrides)
    if "channels" not in controls:
        if "rerank" in controls:
            raise ValueError("Rerank override requires explicit channels")
        return None, "auto"
    channels = controls["channels"]
    if (
        not isinstance(channels, tuple)
        or not channels
        or any(not isinstance(channel, str) for channel in channels)
        or len(set(channels)) != len(channels)
        or set(channels) - _CHANNELS
    ):
        raise ValueError("Unsupported or duplicate evaluation channels")
    rerank = controls.get("rerank", False)
    if type(rerank) is not bool:
        raise ValueError("Rerank override must be boolean")
    strategies = frozenset(
        name
        for name, channel in (
            ("structural", "structural_graph"),
            ("semantic", "semantic_graph"),
        )
        if channel in channels
    )
    return (
        ChannelSelection(
            lexical="lexical" in channels,
            text="text" in channels,
            image="image" in channels,
            graph=bool(strategies),
            graph_strategies=strategies or None,
        ),
        "on" if rerank else "off",
    )


def _ablation_modalities(
    selection: ChannelSelection, query_overrides: dict, exact_only: bool, rerank: str
) -> tuple[str, ...]:
    """The modalities an ablation's channels imply; a differing override is refused."""
    modalities = tuple(
        name
        for name, selected in (("text", selection.text), ("image", selection.image))
        if selected
    ) or ("lexical",)
    declared = query_overrides.get("modalities", modalities)
    if not isinstance(declared, tuple) or tuple(declared) != modalities:
        raise ValueError("Modalities override differs from selected ablation channels")
    if (
        selection.text or selection.image or selection.graph or rerank == "on"
    ) and exact_only:
        raise ValueError("Exact-only query cannot execute selected ablation channels")
    return modalities


def variant_query(
    case: GoldCase, variant: ConfigurationVariant, *, profile: str
) -> QueryRequest:
    """Keep gold intent fixed while varying explicitly supported retrieval controls."""
    if variant.profile != profile:
        raise ValueError("Variant profile differs from active workspace profile")
    overrides = dict(variant.overrides)
    if set(overrides) - (_QUERY_CONTROLS | _EXECUTION_CONTROLS):
        raise ValueError("Unsupported evaluation variant override")
    selection, rerank = variant_channels(variant)
    query_overrides = {
        key: value for key, value in overrides.items() if key in _QUERY_CONTROLS
    }
    if selection is not None:
        if selection.graph and not (
            selection.lexical or selection.text or selection.image
        ):
            raise ValueError("Graph ablation requires an explicit seed channel")
        query_overrides["modalities"] = _ablation_modalities(
            selection,
            query_overrides,
            bool(query_overrides.get("exact_only", case.query.exact_only)),
            rerank,
        )
        query_overrides["channels"] = selection.model_dump(mode="json")
        query_overrides["rerank"] = rerank
    return QueryRequest.from_mapping(
        {
            **case.query.model_dump(mode="json"),
            **query_overrides,
            "profile": profile,
        }
    )


class ComparisonAdmission:
    """Pause mutating operations while allowing owner query callbacks and CAS history."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._mutations = 0
        self._pending = False
        self._active = False

    @asynccontextmanager
    async def mutation(self, ctx=None):
        while True:
            if ctx is not None:
                ctx.checkpoint()
            with self._lock:
                if not self._pending and not self._active:
                    self._mutations += 1
                    break
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            with self._lock:
                self._mutations -= 1

    @contextmanager
    def mutation_nowait(self):
        with self._lock:
            if self._pending or self._active:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Evaluation comparison pauses mutations.",
                    retryable=True,
                )
            self._mutations += 1
        try:
            yield
        finally:
            with self._lock:
                self._mutations -= 1

    @asynccontextmanager
    async def comparison(self, ctx=None):
        with self._lock:
            if self._pending or self._active:
                raise TychoraError(
                    "WORKSPACE_BUSY",
                    "Another evaluation comparison is active.",
                    retryable=True,
                )
            self._pending = True
        try:
            while True:
                if ctx is not None:
                    ctx.checkpoint()
                with self._lock:
                    if self._mutations == 0:
                        self._active = True
                        break
                await asyncio.sleep(0.01)
            yield
        finally:
            with self._lock:
                self._active = False
                self._pending = False


def _aggregate(sets: list[MetricSet]) -> MetricSet:
    by_name: dict[str, list[MetricValue]] = {}
    for metric_set in sets:
        for value in metric_set.metrics:
            by_name.setdefault(value.name, []).append(value)
    aggregate = []
    for name, values in by_name.items():
        denominator = sum(value.denominator or 0 for value in values)
        weighted = sum(
            value.value * (value.denominator or 0)
            for value in values
            if value.value is not None
        )
        aggregate.append(
            MetricValue(
                name=name,
                value=weighted / denominator if denominator else None,
                sample_count=sum(value.sample_count for value in values),
                denominator=denominator,
                not_applicable_reason=None if denominator else "no judged items",
            )
        )
    return MetricSet(metrics=tuple(aggregate))


def _execution_plan(query: QueryRequest) -> dict | None:
    """The ablation a run executed, as its request selected it; none by default."""
    if query.channels is None:
        return None
    return {"channels": query.channels.model_dump(mode="json"), "rerank": query.rerank}


def _integrity_check(case: GoldCase, scored: MetricSet) -> None:
    values = {value.name: value.value for value in scored.metrics}
    if values["forbidden_leakage"] not in (0.0, None):
        raise TychoraError("INTEGRITY_ERROR", "Evaluation returned forbidden evidence")
    if case.expected_revision_ids and (
        values["revision_recall"] != 1.0 or values["version_precision"] != 1.0
    ):
        raise TychoraError("INTEGRITY_ERROR", "Evaluation selected a wrong revision")
    if case.query.exact_identifiers and any(
        values[f"{kind}_recall"] != 1.0
        for kind in ("source", "revision", "unit", "asset", "definition", "file")
        if getattr(case, f"expected_{kind}_ids")
    ):
        raise TychoraError("INTEGRITY_ERROR", "Evaluation missed an exact identifier")
    locator = next(
        value for value in scored.metrics if value.name == "locator_validity"
    )
    if locator.denominator and locator.value != 1.0:
        raise TychoraError("INTEGRITY_ERROR", "Evaluation returned an invalid locator")
    if case.expected_spans and values["span_completeness"] != 1.0:
        raise TychoraError("INTEGRITY_ERROR", "Evaluation missed an exact span")


class EvaluationService:
    """Run fixed local comparisons and persist only epoch-consistent results."""

    def __init__(self, owner, query, jobs) -> None:
        self.owner = owner
        self.query = query
        self.jobs = jobs
        self.repository = EvaluationRepository(owner)

    async def register_dataset(self, path, scope: RequestScope, ctx: ExecutionContext):
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Gold registration requires management grant"
            )
        dataset = await asyncio.to_thread(load_dataset, path)
        return await self.owner.run_write(
            lambda: self.repository.register(dataset, grant), ctx
        )

    async def register_dataset_bytes(
        self, manifest: bytes, cases: bytes, scope: RequestScope, ctx: ExecutionContext
    ):
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Gold registration requires management grant"
            )
        dataset = await asyncio.to_thread(load_dataset_bytes, manifest, cases)
        return await self.owner.run_write(
            lambda: self.repository.register(dataset, grant), ctx
        )

    def _epochs(self) -> tuple[int, int]:
        with self.owner.store.uow.read() as tx:
            return tx.workspace.policy_epoch(), tx.workspace.lexical_epoch()

    def _bind_labels(
        self, cases: tuple[GoldCase, ...], snapshot_id: str
    ) -> tuple[tuple[GoldCase, ...], tuple[LabelFailure, ...]]:
        """Resolve label-based cases on the owner thread; fail when none resolves."""
        bound, failures = bind_cases(
            cases, SqliteGoldLabelResolver(self.owner.db, snapshot_id)
        )
        if not bound:
            raise TychoraError(
                "INVALID_REQUEST",
                "No gold case of this split resolves in the workspace.",
                reason=failures[0].reason,
            )
        return bound, failures

    def _stage_config(self) -> str:
        """Store the registry snapshot of the running configuration as an artifact."""
        return self.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "registry",
                        "schema_version": "1",
                        "data": self.owner.config.model_dump(mode="json"),
                    }
                )
            ),
            "application/json",
        )

    def _hardware(self, request: EvaluationRequest, version_sha: str) -> dict:
        """Describe the machine, models and settings a comparison ran with."""
        return {
            "machine": platform.machine(),
            "system": platform.system(),
            "python": platform.python_version(),
            "output_model": getattr(self.query.counter, "model", "unknown"),
            "output_tokenizer": getattr(self.query.counter, "tokenizer_id", "unknown"),
            "text_model": getattr(
                getattr(self.query.text_provider, "capabilities", None),
                "model",
                None,
            ),
            "visual_model": getattr(
                getattr(self.query.visual_provider, "capabilities", None),
                "model",
                None,
            ),
            "seed": request.seed,
            "repetitions": request.repetitions,
            "offline": request.offline,
            "variants": [
                variant.model_dump(mode="json") for variant in request.variants
            ],
            "dataset_version_sha": version_sha,
        }

    async def run(
        self, request: EvaluationRequest, scope: RequestScope, ctx: ExecutionContext
    ) -> JobHandle:
        grant = self.owner.scope.narrow(scope)
        if "manage" not in grant.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Evaluation requires management grant"
            )
        if request.offline and not self.owner.config.values.offline:
            raise TychoraError(
                "CAPABILITY_MISSING", "Evaluation requires an offline workspace"
            )
        if len({variant.name for variant in request.variants}) != len(request.variants):
            raise TychoraError(
                "INVALID_REQUEST", "Evaluation variant names must differ"
            )
        async with self.owner.comparison_admission.comparison(ctx):

            def accept():
                dataset = self.repository.load(request.dataset_id, scope=grant)
                cases = tuple(
                    case for case in dataset.cases if case.split == request.split
                )
                if not cases:
                    raise TychoraError("INVALID_REQUEST", "Gold split has no cases")
                validate_workload(
                    len(cases), len(request.variants), request.repetitions
                )
                active, _, _ = self.owner.db.execute(
                    "SELECT active_snapshot_id,policy_epoch,lexical_statistics_epoch "
                    "FROM workspaces WHERE workspace_id=?",
                    (self.owner.workspace_id,),
                ).fetchone()
                if active != request.snapshot_id:
                    raise TychoraError(
                        "INVALID_REQUEST",
                        "Evaluation requires the active pinned snapshot",
                    )
                snapshot = self.owner.db.execute(
                    "SELECT config_sha FROM snapshots WHERE snapshot_id=?",
                    (request.snapshot_id,),
                ).fetchone()
                if snapshot is None:
                    raise TychoraError(
                        "NOT_FOUND", "Evaluation snapshot is unavailable"
                    )
                cases, label_failures = self._bind_labels(cases, request.snapshot_id)
                for case in cases:
                    for variant in request.variants:
                        variant_query(case, variant, profile=self.owner.config.profile)
                config_ref = self._stage_config()
                handle = self.jobs.accept(
                    "evaluate", grant, None, request.model_dump(mode="json"), config_ref
                )
                self.jobs.start(handle.id)
                return (
                    dataset,
                    cases,
                    label_failures,
                    self._epochs(),
                    snapshot[0],
                    handle,
                )

            (
                dataset,
                cases,
                label_failures,
                epochs,
                config_sha,
                handle,
            ) = await self.owner.run_write(accept, ctx)
            case_results: dict[str, list[dict]] = {case.case_id: [] for case in cases}
            retained_case_bytes = 0
            evaluations: list[CaseEvaluation] = []
            metric_sets: list[MetricSet] = []
            try:
                locator_cache = await self.owner.run_write(
                    lambda: RetainedBindingCache(self.owner, request.snapshot_id), ctx
                )
                for variant in request.variants:
                    for repetition in range(request.repetitions):
                        for case in cases:
                            ctx.checkpoint()
                            query = variant_query(
                                case, variant, profile=self.owner.config.profile
                            )
                            delivered = await self.query.search(query, grant, ctx)
                            if delivered.result.snapshot_id != request.snapshot_id:
                                raise TychoraError(
                                    "SCOPE_CHANGED", "Evaluation snapshot changed"
                                )
                            locator_assessment = await self.owner.run_write(
                                lambda result=delivered.result: verify_result_locators(
                                    self.owner, result, cache=locator_cache
                                ),
                                ctx,
                            )
                            scored = metrics(
                                case,
                                delivered.result,
                                locators=locator_assessment,
                                cutoff=query.limit,
                            )
                            if case.benchmark == "integrity":
                                _integrity_check(case, scored)
                            observed = await self.owner.run_write(self._epochs, ctx)
                            if observed != epochs:
                                raise TychoraError(
                                    "SCOPE_CHANGED",
                                    "Evaluation comparison epochs changed",
                                )
                            entry = {
                                "variant": variant.name,
                                "repetition": repetition,
                                "seed": request.seed + repetition,
                                "config_sha": config_sha,
                                "execution_plan": _execution_plan(query),
                                "request": query.model_dump(mode="json"),
                                "run_id": delivered.result.run_id,
                                "result": delivered.result.model_dump(mode="json"),
                                "metrics": scored.model_dump(mode="json"),
                                "usage": {
                                    "tokens": delivered.token_count,
                                    "bytes": delivered.byte_count,
                                },
                            }
                            retained_case_bytes = validate_retained_size(
                                retained_case_bytes, len(canonical_bytes(entry))
                            )
                            case_results[case.case_id].append(entry)
                            evaluations.append(
                                CaseEvaluation(
                                    case_id=case.case_id,
                                    variant=variant.name,
                                    run_id=delivered.result.run_id,
                                    metrics=scored,
                                )
                            )
                            metric_sets.append(scored)
                report = EvaluationReport(
                    dataset_id=dataset.dataset_id,
                    split=request.split,
                    snapshot_id=request.snapshot_id,
                    cases=tuple(evaluations),
                    aggregate=_aggregate(metric_sets),
                    label_failures=label_failures,
                    created_at=datetime.now(UTC),
                )
                validate_retained_size(
                    0,
                    sum(
                        len(
                            canonical_bytes(
                                {
                                    "schema_version": "1",
                                    "case_id": case_id,
                                    "runs": runs,
                                }
                            )
                        )
                        for case_id, runs in case_results.items()
                    ),
                )
                validate_retained_size(
                    0, len(canonical_bytes(report.model_dump(mode="json")))
                )
                hardware = self._hardware(request, dataset.version_sha)

                def finish():
                    self.repository.persist(
                        evaluation_id=handle.id,
                        request=request,
                        job_id=handle.id,
                        policy_epoch=epochs[0],
                        lexical_epoch=epochs[1],
                        config_sha=config_sha,
                        hardware=hardware,
                        case_results=case_results,
                        report=report,
                    )
                    self.jobs.complete(handle.id)

                await self.owner.run_write(finish, ctx)
            except BaseException as error:
                failure = (
                    error
                    if isinstance(error, TychoraError)
                    else TychoraError("JOB_INTERRUPTED", "Evaluation did not complete")
                )
                await self.owner.run_write(
                    lambda: self.jobs.fail(handle.id, failure), ExecutionContext()
                )
                raise
            return JobHandle(id=handle.id, operation="evaluate", state="succeeded")

    async def get(
        self, evaluation_id: str, scope: RequestScope, ctx: ExecutionContext
    ) -> dict:
        return await self.owner.run_write(
            lambda: self.repository.get(evaluation_id, scope, self.jobs), ctx
        )

    async def list_runs(self, scope: RequestScope, ctx: ExecutionContext) -> list[dict]:
        return await self.owner.run_write(
            lambda: self.repository.list_runs(scope, self.jobs), ctx
        )
