"""Benchmark suites over a workspace client: ingestion, queries and scale."""

import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Protocol

from tychora.contracts.base import Contract, NonEmptyStr, NonNegativeInt
from tychora.contracts.operations import JobEvent
from tychora.ports.repositories import PublicationFootprint

from .budgets import INGEST_STAGE_BUDGETS_S, QUERY_STAGE_BUDGETS_MS
from .corpus import BenchCorpus, BenchQuery, Category, all_queries, document_bytes
from .results import (
    Assertion,
    BenchResult,
    CorpusIdentity,
    MachineFingerprint,
    Profile,
    ScalePoint,
    StageStats,
    Suite,
    nearest_rank,
    stage_stats,
)
from .stages import StageSource, ingest_bucket
from .synth import SynthDocument, SynthSpec, document_at, generate

SCALE_WINDOW = 20


class BenchSource(Contract):
    """One document to add: text or bytes with its registration fields."""

    source_key: NonEmptyStr
    title: NonEmptyStr
    kind: NonEmptyStr
    category: Category
    format: str | None = None
    media_type: str | None = None
    text: str | None = None
    data: bytes | None = None


class SearchOutcome(Contract):
    """The result of one search: its run, matched units, error and wall time."""

    run_id: str | None
    unit_ids: tuple[str, ...]
    error_code: str | None
    duration_ms: float


class Footprint(Contract):
    """Bytes a workspace occupies on disk."""

    total_bytes: NonNegativeInt
    largest_artifact_bytes: NonNegativeInt


class BenchClient(Protocol):
    """The workspace operations a benchmark needs."""

    def add(self, source: BenchSource) -> str:
        """Add one source and return its job id."""
        ...

    def wait(self, job_id: str) -> str:
        """Wait for the job to settle and return its final state."""
        ...

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        """Every event of the job, in order."""
        ...

    def search(self, question: str) -> SearchOutcome:
        """Run one search and time it."""
        ...

    def footprint(self) -> Footprint:
        """Measure the workspace's bytes on disk."""
        ...

    def publication(self) -> PublicationFootprint:
        """Measure what the latest publication wrote."""
        ...


class BenchUnavailable(Exception):
    """A suite this version cannot run."""


def require_available(suite: Suite) -> None:
    """Raise ``BenchUnavailable`` for a suite this version cannot run."""
    if suite == "families":
        raise BenchUnavailable(
            "The families suite needs vector family options, which this version does not have yet."
        )


@dataclass
class Samples:
    """Measurements collected by one suite."""

    stages: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    assertions: list[Assertion] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    scale: list[ScalePoint] = field(default_factory=list)
    ingested: set[str] = field(default_factory=set)
    aborted: bool = False


@dataclass(frozen=True)
class RunContext:
    """What a run is measured on and against, with its scale and time limits."""

    commit: str
    profile: Profile
    machine: MachineFingerprint
    reference: bool
    retained_sha: str
    checkpoints: tuple[int, ...] = (100, 1000, 10000)
    max_wall_seconds: float | None = None
    limit: int | None = None
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class SuiteInputs:
    """Where public documents are cached and where stage durations come from."""

    cache: Path
    stage_source: StageSource


def _synthetic(document: SynthDocument) -> BenchSource:
    return BenchSource(
        source_key=document.source_key,
        title=document.title,
        kind="document@1",
        category="synthetic",
        format=document.format,
        text=document.text,
    )


def iter_sources(
    corpus: BenchCorpus, cache: Path, limit: int | None = None
) -> Iterator[BenchSource]:
    """Synthetic documents first, then public ones read lazily from the cache."""

    def produce() -> Iterator[BenchSource]:
        for document in generate(corpus.synthetic):
            yield _synthetic(document)
        for document in corpus.documents:
            yield BenchSource(
                source_key=document.source_key,
                title=document.title,
                kind=document.kind,
                category=document.category,
                format=document.format,
                media_type=document.media_type,
                data=document_bytes(document, cache),
            )

    return islice(produce(), limit)


def run_ingest(
    client: BenchClient, sources: Iterable[BenchSource], stage_source: StageSource
) -> Samples:
    """Add each source and record its stage durations in seconds."""
    samples = Samples()
    for source in sources:
        job_id = client.add(source)
        state = client.wait(job_id)
        if state != "succeeded":
            samples.failures.append(f"{source.source_key}: job {state}")
            continue
        samples.ingested.add(source.source_key)
        per_job: dict[str, float] = defaultdict(float)
        for sample in stage_source.ingest_stages(job_id):
            per_job[ingest_bucket(sample.stage)] += sample.duration_ms / 1000
        for bucket, seconds in per_job.items():
            samples.stages[bucket].append(seconds)
    return samples


def _record_query(
    samples: Samples, outcome: SearchOutcome, stage_source: StageSource
) -> None:
    stages = stage_source.query_stages(outcome.run_id) if outcome.run_id else ()
    for sample in stages:
        samples.stages[sample.stage].append(sample.duration_ms)
    if not any(sample.stage == "total" for sample in stages):
        samples.stages["total"].append(outcome.duration_ms)


def run_queries(
    client: BenchClient, queries: Iterable[BenchQuery], stage_source: StageSource
) -> Samples:
    """Warm each query once, then record per-stage and total durations in milliseconds."""
    selected = tuple(queries)
    for query in selected:
        client.search(query.question)
    samples = Samples()
    for query in selected:
        outcome = client.search(query.question)
        if outcome.error_code:
            samples.failures.append(f"{query.query_id}: {outcome.error_code}")
        else:
            _record_query(samples, outcome, stage_source)
        if query.probe:
            samples.assertions.append(
                Assertion(
                    name=query.query_id,
                    passed=outcome.error_code is None and bool(outcome.unit_ids),
                    detail=outcome.error_code or "",
                )
            )
    return samples


class _Growth:
    """A synthetic workspace growing under a wall-clock limit."""

    def __init__(
        self, client: BenchClient, spec: SynthSpec, context: RunContext
    ) -> None:
        self.client = client
        self.spec = spec
        self.context = context
        self.started = context.clock()
        self.aborted = False
        self.failures: list[str] = []
        self.jobs: list[str] = []

    def grow(self, span: tuple[int, int]) -> list[float]:
        """Add the documents of ``span`` and return each add's latency in milliseconds."""
        latencies = []
        self.jobs = []
        limit = self.context.max_wall_seconds
        for index in range(*span):
            if limit is not None and self.context.clock() - self.started > limit:
                self.aborted = True
                break
            began = time.perf_counter()
            job_id = self.client.add(_synthetic(document_at(self.spec, index)))
            state = self.client.wait(job_id)
            self.jobs.append(job_id)
            latencies.append((time.perf_counter() - began) * 1000)
            if state != "succeeded":
                self.failures.append(f"synthetic/{index:06d}: job {state}")
        return latencies


def _search_latencies(
    client: BenchClient, questions: list[str], failures: list[str]
) -> list[float]:
    """Latencies of the searches that succeeded; failed ones are recorded as failures."""
    latencies = []
    for question in questions:
        outcome = client.search(question)
        if outcome.error_code:
            failures.append(f"search {question}: {outcome.error_code}")
        else:
            latencies.append(outcome.duration_ms)
    return latencies


def _publication_p90_ms(stage_source: StageSource, jobs: list[str]) -> float:
    """Nearest-rank p90 of the ``publish`` stage over the jobs of one window."""
    durations = [
        sum(
            sample.duration_ms
            for sample in stage_source.ingest_stages(job_id)
            if sample.stage == "publish"
        )
        for job_id in jobs
    ]
    return nearest_rank(durations, 0.90) if durations else 0.0


def _checkpoint(
    client: BenchClient,
    documents: int,
    latencies: list[float],
    before: Footprint,
    searches: list[float],
) -> ScalePoint:
    after = client.footprint()
    searches = searches or [0.0]
    return ScalePoint(
        documents=documents,
        add_p50_ms=nearest_rank(latencies, 0.5),
        bytes_per_add=(after.total_bytes - before.total_bytes) / len(latencies),
        search_p50_ms=nearest_rank(searches, 0.5),
        search_p95_ms=nearest_rank(searches, 0.95),
        largest_artifact_bytes=after.largest_artifact_bytes,
        footprint_bytes=after.total_bytes,
    )


def _with_publication(
    point: ScalePoint,
    client: BenchClient,
    stage_source: StageSource,
    jobs: list[str],
) -> ScalePoint:
    """Add what the checkpoint's last publication wrote and how long publishing took."""
    footprint = client.publication()
    return point.model_copy(
        update={
            "publication_bytes_per_add": footprint.root_bytes
            + footprint.new_page_bytes,
            "largest_snapshot_artifact_bytes": footprint.largest_artifact_bytes,
            "publication_p90_ms": _publication_p90_ms(stage_source, jobs),
        }
    )


E3_ARTIFACT_CEILING_BYTES = 1024 * 1024
E3_BYTES_RATIO = 2
E3_PUBLICATION_P90_MS = 500.0


def e3_assertions(points: list[ScalePoint]) -> list[Assertion]:
    """The E3 verdicts: publication bytes stay flat, artifacts small, publication fast."""
    by_size = {point.documents: point for point in points}
    found = []
    if 1000 in by_size and 10_000 in by_size:
        small, large = (
            by_size[1000].publication_bytes_per_add,
            by_size[10_000].publication_bytes_per_add,
        )
        found.append(
            Assertion(
                name="E3.bytes_ratio",
                passed=large <= E3_BYTES_RATIO * small,
                detail=f"bytes per add: {small} at 1000, {large} at 10000",
            )
        )
    if points:
        largest = max(point.largest_snapshot_artifact_bytes for point in points)
        found.append(
            Assertion(
                name="E3.artifact_ceiling",
                passed=largest <= E3_ARTIFACT_CEILING_BYTES,
                detail=f"largest snapshot artifact: {largest} bytes",
            )
        )
    if 10_000 in by_size:
        p90 = by_size[10_000].publication_p90_ms
        found.append(
            Assertion(
                name="E3.publication_p90",
                passed=p90 <= E3_PUBLICATION_P90_MS,
                detail=f"publication p90 at 10000: {p90:.1f} ms",
            )
        )
    return found


def run_scale(
    client: BenchClient,
    spec: SynthSpec,
    context: RunContext,
    stage_source: StageSource,
) -> Samples:
    """Grow a synthetic workspace through the checkpoints, measuring each window."""
    growth, samples, added = _Growth(client, spec, context), Samples(), 0
    questions = [document_at(spec, index).marker for index in range(SCALE_WINDOW)]
    for checkpoint in sorted(context.checkpoints):
        window = max(added, checkpoint - SCALE_WINDOW)
        growth.grow((added, window))
        before = client.footprint()
        latencies = [] if growth.aborted else growth.grow((window, checkpoint))
        samples.failures.extend(growth.failures)
        growth.failures.clear()
        if growth.aborted:
            samples.aborted = True
            return samples
        added = checkpoint
        if latencies:
            point = _checkpoint(
                client,
                checkpoint,
                latencies,
                before,
                _search_latencies(
                    client,
                    questions[: min(checkpoint, SCALE_WINDOW)],
                    samples.failures,
                ),
            )
            samples.scale.append(
                _with_publication(point, client, stage_source, growth.jobs)
            )
    samples.assertions.extend(e3_assertions(samples.scale))
    return samples


def _budget(suite: Suite, profile: Profile, stage: str) -> float | None:
    if suite == "ingest":
        return INGEST_STAGE_BUDGETS_S.get(stage)
    if suite == "query" and stage == "total":
        return float(
            QUERY_STAGE_BUDGETS_MS["local_total" if profile == "offline" else "total"]
        )
    if suite == "query" and stage in QUERY_STAGE_BUDGETS_MS:
        return float(QUERY_STAGE_BUDGETS_MS[stage])
    return None


def _summarize(
    samples: Samples, suite: Suite, context: RunContext
) -> dict[str, StageStats]:
    unit = "s" if suite == "ingest" else "ms"
    quantile = 0.90 if suite == "ingest" else 0.95
    return {
        stage: stage_stats(
            values,
            unit=unit,
            budget=_budget(suite, context.profile, stage),
            reference=context.reference,
            quantile=quantile,
        )
        for stage, values in sorted(samples.stages.items())
    }


def _measure(
    client: BenchClient,
    corpus: BenchCorpus,
    suite: Suite,
    context: RunContext,
    inputs: SuiteInputs,
) -> Samples:
    if suite == "scale":
        return run_scale(client, corpus.synthetic, context, inputs.stage_source)
    sources = iter_sources(corpus, inputs.cache, context.limit)
    loaded = run_ingest(client, sources, inputs.stage_source)
    if suite == "ingest":
        return loaded
    queries = [
        query
        for query in all_queries(corpus)
        if any(label.source_key in loaded.ingested for label in query.labels)
    ]
    return run_queries(client, queries, inputs.stage_source)


def run_suite(
    client: BenchClient,
    corpus: BenchCorpus,
    suite: Suite,
    context: RunContext,
    inputs: SuiteInputs,
) -> BenchResult:
    """Run one suite and return its result document."""
    require_available(suite)
    started_at = datetime.now(UTC)
    samples = _measure(client, corpus, suite, context, inputs)
    return BenchResult(
        commit=context.commit,
        suite=suite,
        profile=context.profile,
        corpus=CorpusIdentity(
            name=corpus.name,
            digest=corpus.digest(),
            documents=corpus.synthetic.count + len(corpus.documents),
            queries=len(samples.stages["total"])
            if suite == "query"
            else len(all_queries(corpus)),
        ),
        machine=context.machine,
        reference_machine=context.reference,
        retained_sha=context.retained_sha,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        stages=_summarize(samples, suite, context),
        scale=tuple(samples.scale),
        usage={},
        assertions=tuple(samples.assertions),
        failures=tuple(samples.failures),
        aborted=samples.aborted,
    )
