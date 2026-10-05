"""Measured four-state query benchmark over instrumented engine sessions."""

from __future__ import annotations

import math
import os
import platform
import resource
import sys
import tracemalloc
from dataclasses import dataclass, field, fields
from typing import Protocol

from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest

STATES = (
    "cold_owner",
    "warm_resources",
    "warm_query_embedding",
    "warm_result",
)


@dataclass(frozen=True)
class BenchmarkIdentity:
    corpus_sha: str
    config_sha: str
    output_cap: int


@dataclass(frozen=True)
class BenchmarkConditions:
    process_id: int
    process_start_id: str
    owner_session_id: str = ""
    process_isolated: bool = False
    os_page_cache: str = "unknown"
    provider_cache: str = "unknown"


@dataclass(frozen=True)
class BenchmarkTelemetry:
    query_embedding_calls: int = 0
    retrieval_calls: int = 0
    provider_calls: int = 0
    policy_revalidations: int = 0
    cache_hits: int = 0
    parsed_files: int = 0
    embedded_windows: int = 0
    projected_records: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_images: int = 0
    selected_units: int = 0
    index_bytes: int = 0
    stages_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkMemory:
    process_id: int
    traced_python_peak_bytes: int
    process_rss_high_water_bytes: int


def process_memory() -> BenchmarkMemory:
    """Observe this process only; RSS includes native allocations and prior peaks."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return BenchmarkMemory(
        process_id=os.getpid(),
        traced_python_peak_bytes=tracemalloc.get_traced_memory()[1]
        if tracemalloc.is_tracing()
        else 0,
        process_rss_high_water_bytes=int(
            rss if sys.platform == "darwin" else rss * 1024
        ),
    )


class BenchmarkClient(Protocol):
    identity: BenchmarkIdentity
    conditions: BenchmarkConditions

    def prepare(self, state: str) -> None: ...

    def counters(self) -> BenchmarkTelemetry: ...

    def search(self, request: QueryRequest) -> object: ...

    def memory(self) -> BenchmarkMemory: ...


class BenchmarkClock(Protocol):
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class BenchmarkState:
    name: str
    conditions: BenchmarkConditions
    request_count: int
    startup_ms: float
    total_ms: float
    p50_ms: float
    p95_ms: float
    samples_ms: tuple[float, ...]
    telemetry: BenchmarkTelemetry
    traced_python_peak_bytes: int
    process_rss_high_water_bytes: int
    memory_process_id: int
    throughput_queries_per_second: float

    @property
    def query_embedding_calls(self) -> int:
        return self.telemetry.query_embedding_calls

    @property
    def retrieval_calls(self) -> int:
        return self.telemetry.retrieval_calls

    @property
    def provider_calls(self) -> int:
        return self.telemetry.provider_calls

    @property
    def policy_revalidations(self) -> int:
        return self.telemetry.policy_revalidations


@dataclass(frozen=True)
class BenchmarkReport:
    request_sha: str
    identity: BenchmarkIdentity
    environment: dict[str, str]
    states: dict[str, BenchmarkState]
    quantile_method: str = "nearest rank, ceil(q*n)-1"

    def __getitem__(self, state: str) -> BenchmarkState:
        return self.states[state]


def _quantile(samples: tuple[float, ...], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _delta(before: BenchmarkTelemetry, after: BenchmarkTelemetry) -> BenchmarkTelemetry:
    values = {
        item.name: getattr(after, item.name) - getattr(before, item.name)
        for item in fields(BenchmarkTelemetry)
        if item.name not in {"index_bytes", "stages_ms"}
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("Benchmark counters must be monotonic")
    stages = {
        name: after.stages_ms.get(name, 0.0) - before.stages_ms.get(name, 0.0)
        for name in before.stages_ms.keys() | after.stages_ms.keys()
    }
    if any(value < 0 for value in stages.values()):
        raise ValueError("Benchmark stage times must be monotonic")
    return BenchmarkTelemetry(**values, index_bytes=after.index_bytes, stages_ms=stages)


def benchmark(
    requests: tuple[QueryRequest, ...],
    states: tuple[str, ...],
    client_factory,
    clock: BenchmarkClock,
) -> BenchmarkReport:
    """Measure actual session calls; the factory owns state preparation evidence.

    The cold session must be hosted in another process. An arbitrary factory's
    process metadata cannot be independently attested by this generic function.
    """
    if not requests or states != STATES:
        raise ValueError("Four ordered benchmark states and requests are required")
    request_sha = digest(
        "benchmark-requests-v1", [r.model_dump(mode="json") for r in requests]
    )
    results: dict[str, BenchmarkState] = {}
    identity: BenchmarkIdentity | None = None
    warm_conditions: BenchmarkConditions | None = None
    for state in states:
        entered = clock.monotonic()
        client: BenchmarkClient = client_factory(state)
        startup_ms = (clock.monotonic() - entered) * 1000
        if identity is None:
            identity = client.identity
        elif client.identity != identity:
            raise ValueError("Benchmark corpus/config/output identity differs")
        conditions = client.conditions
        if state == "cold_owner":
            if not conditions.process_isolated or conditions.process_id == os.getpid():
                raise ValueError("Cold owner requires a distinct process")
        elif warm_conditions is None:
            warm_conditions = conditions
        elif (
            conditions.process_id,
            conditions.process_start_id,
            conditions.owner_session_id,
        ) != (
            warm_conditions.process_id,
            warm_conditions.process_start_id,
            warm_conditions.owner_session_id,
        ):
            raise ValueError("Warm states must share one owner session")
        client.prepare(state)
        before = client.counters()
        samples: list[float] = []
        for request in requests:
            began = clock.monotonic()
            client.search(request)
            samples.append((clock.monotonic() - began) * 1000)
        observed = _delta(before, client.counters())
        elapsed_ms = (clock.monotonic() - entered) * 1000
        if state in {"cold_owner", "warm_resources"} and observed.cache_hits:
            raise ValueError("Uncached benchmark state returned a final-result hit")
        if state == "warm_query_embedding" and (
            observed.query_embedding_calls or not observed.retrieval_calls
        ):
            raise ValueError(
                "Warm query embedding must rerun retrieval without embedding"
            )
        if state == "warm_result" and (
            observed.retrieval_calls
            or observed.provider_calls
            or observed.policy_revalidations < len(requests)
            or observed.cache_hits < len(requests)
        ):
            raise ValueError("Warm result must revalidate policy without retrieval")
        memory = client.memory()
        if memory.process_id != conditions.process_id:
            raise ValueError("Benchmark memory belongs to another process")
        results[state] = BenchmarkState(
            name=state,
            conditions=conditions,
            request_count=len(requests),
            startup_ms=startup_ms,
            total_ms=elapsed_ms,
            p50_ms=_quantile(tuple(samples), 0.5),
            p95_ms=_quantile(tuple(samples), 0.95),
            samples_ms=tuple(samples),
            telemetry=observed,
            traced_python_peak_bytes=memory.traced_python_peak_bytes,
            process_rss_high_water_bytes=memory.process_rss_high_water_bytes,
            memory_process_id=memory.process_id,
            throughput_queries_per_second=len(requests) / (elapsed_ms / 1000),
        )
    assert identity is not None
    return BenchmarkReport(
        request_sha=request_sha,
        identity=identity,
        environment={
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "pid": str(os.getpid()),
        },
        states=results,
    )
