"""Benchmark result documents: percentiles, verdicts, machine and corpus identity."""

import json
import math
import os
import platform
import sqlite3
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime

from tychora.contracts.base import (
    Contract,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from tychora.contracts.identity import canonical_bytes

from .budgets import REFERENCE_TOLERANCE, SHARED_RUNNER_REGRESSION

Suite = Literal["ingest", "query", "scale", "families"]
Profile = Literal["offline", "online"]
Unit = Literal["ms", "s"]
Verdict = Literal["pass", "fail", "informational", "no_budget"]


class StageStats(Contract):
    """Percentiles, budget and verdict of one stage."""

    n: NonNegativeInt
    p50: float
    p90: float
    p95: float
    p99: float
    unit: Unit
    budget: float | None
    verdict: Verdict


class MachineFingerprint(Contract):
    """The measuring machine and its storage libraries."""

    cpu: NonEmptyStr
    cores: PositiveInt
    ram_bytes: NonNegativeInt
    os: NonEmptyStr
    python: NonEmptyStr
    sqlite: NonEmptyStr
    chroma: str | None


class CorpusIdentity(Contract):
    """The benchmark corpus a result was measured on."""

    name: NonEmptyStr
    digest: Sha256
    documents: NonNegativeInt
    queries: NonNegativeInt


class ScalePoint(Contract):
    """Add and search costs at one corpus size."""

    documents: PositiveInt
    add_p50_ms: float
    bytes_per_add: float
    search_p50_ms: float
    search_p95_ms: float
    largest_artifact_bytes: NonNegativeInt
    footprint_bytes: NonNegativeInt


class Assertion(Contract):
    """A named pass or fail check recorded with a result."""

    name: NonEmptyStr
    passed: bool
    detail: str = ""


class BenchResult(Contract):
    """One suite run on one profile, ready to serialise."""

    commit: NonEmptyStr
    suite: Suite
    profile: Profile
    corpus: CorpusIdentity
    machine: MachineFingerprint
    reference_machine: bool
    retained_sha: Sha256
    started_at: AwareDatetime
    finished_at: AwareDatetime
    stages: dict[str, StageStats]
    scale: tuple[ScalePoint, ...] = ()
    usage: dict[str, int]
    assertions: tuple[Assertion, ...] = ()
    failures: tuple[str, ...] = ()
    aborted: bool = False

    def document(self) -> dict:
        """The JSON document written to ``bench-results``."""
        return {
            "schema": "tychora.bench-result",
            "version": 1,
            **self.model_dump(mode="json"),
        }

    @property
    def failed(self) -> bool:
        """True when any stage verdict is a failure."""
        return any(stats.verdict == "fail" for stats in self.stages.values())


def nearest_rank(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the ceil(q*n)-th smallest sample."""
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def verdict(observed: float, budget: float | None, *, reference: bool) -> Verdict:
    """Budgets fail only on the reference machine, with a 10% tolerance."""
    if budget is None:
        return "no_budget"
    if not reference:
        return "informational"
    return "fail" if observed > budget * (1 + REFERENCE_TOLERANCE) else "pass"


def stage_stats(
    samples: Sequence[float],
    *,
    unit: Unit,
    budget: float | None,
    reference: bool,
    quantile: float,
) -> StageStats:
    """Percentiles of one stage and its verdict at the asserted quantile."""
    values = [float(value) for value in samples] or [0.0]
    asserted = nearest_rank(values, quantile)
    return StageStats(
        n=len(samples),
        p50=nearest_rank(values, 0.50),
        p90=nearest_rank(values, 0.90),
        p95=nearest_rank(values, 0.95),
        p99=nearest_rank(values, 0.99),
        unit=unit,
        budget=None if budget is None else float(budget),
        verdict=verdict(asserted, budget, reference=reference)
        if samples
        else "no_budget",
    )


def regressions(
    current: BenchResult,
    previous: BenchResult,
    tolerance: float = SHARED_RUNNER_REGRESSION,
) -> tuple[str, ...]:
    """Stages whose p95 grew by more than ``tolerance`` against an earlier run."""
    found = []
    for name, stats in sorted(current.stages.items()):
        earlier = previous.stages.get(name)
        if earlier is None or not earlier.n:
            continue
        ceiling = round(earlier.p95 * (1 + tolerance), 3)
        if stats.p95 > ceiling:
            found.append(
                f"{name}: p95 {stats.p95} {stats.unit} > {ceiling} {stats.unit}"
            )
    return tuple(found)


def machine_fingerprint() -> MachineFingerprint:
    """Describe the measuring machine and the storage libraries."""
    try:
        chroma = metadata.version("chromadb")
    except metadata.PackageNotFoundError:
        chroma = None
    return MachineFingerprint(
        cpu=platform.processor() or platform.machine() or "unknown",
        cores=os.cpu_count() or 1,
        ram_bytes=os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
        os=platform.platform(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        chroma=chroma,
    )


def is_reference(
    machine: MachineFingerprint, spec: Mapping[str, object], runner_class: str | None
) -> bool:
    """True on the declared reference runner class (budgets are asserted there)."""
    return (
        runner_class is not None
        and runner_class == spec.get("runner_class")
        and machine.os.startswith(str(spec.get("os_prefix", "")))
        and machine.cores >= int(str(spec.get("min_cores", 1)))
    )


def write_result(result: BenchResult, out_dir: Path) -> Path:
    """Write ``<out_dir>/<commit>/<suite>-<profile>.json`` and return its path."""
    path = out_dir / result.commit / f"{result.suite}-{result.profile}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(result.document()) + b"\n")
    return path


def load_result(path: Path) -> BenchResult:
    """Read a result document written by ``write_result``."""
    document = json.loads(path.read_bytes())
    if (
        document.pop("schema", None) != "tychora.bench-result"
        or document.pop("version", None) != 1
    ):
        raise ValueError(f"{path} is not a tychora.bench-result version 1 document")
    return BenchResult.model_validate_json(json.dumps(document))
