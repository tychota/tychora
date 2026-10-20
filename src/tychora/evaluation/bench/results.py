"""Benchmark result documents: percentiles, verdicts, machine and corpus identity."""

import json
import math
import os
import platform
import sqlite3
import uuid
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
from tychora.contracts.errors import TychoraError
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
    quantile: float
    asserted: float
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
    publication_bytes_per_add: NonNegativeInt = 0
    largest_snapshot_artifact_bytes: NonNegativeInt = 0
    publication_p90_ms: float = 0.0


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
        quantile=quantile,
        asserted=asserted,
        unit=unit,
        budget=None if budget is None else float(budget),
        verdict=verdict(asserted, budget, reference=reference)
        if samples
        else "no_budget",
    )


_REGRESSION_FLOOR = {"ms": 1.0, "s": 0.001}


def _require_comparable(current: BenchResult, previous: BenchResult) -> None:
    """Refuse to compare runs of different suites, profiles or machines."""
    same = (current.suite, current.profile, current.reference_machine) == (
        previous.suite,
        previous.profile,
        previous.reference_machine,
    ) and (current.machine.cpu, current.machine.cores, current.machine.os) == (
        previous.machine.cpu,
        previous.machine.cores,
        previous.machine.os,
    )
    if not same:
        raise TychoraError(
            "INVALID_REQUEST",
            "Results come from different suites, profiles or machines.",
            reason="bench.incomparable",
        )


def regressions(
    current: BenchResult,
    previous: BenchResult,
    tolerance: float = SHARED_RUNNER_REGRESSION,
) -> tuple[str, ...]:
    """Stages whose asserted quantile grew by more than ``tolerance``.

    Both runs must share suite, profile, reference flag and machine class. A
    stage never regresses below an absolute floor (1 ms, or 0.001 s), so a zero
    baseline does not flag every positive value.
    """
    _require_comparable(current, previous)
    found = []
    for name, stats in sorted(current.stages.items()):
        earlier = previous.stages.get(name)
        if earlier is None or not earlier.n:
            continue
        if (earlier.unit, earlier.quantile) != (stats.unit, stats.quantile):
            raise TychoraError(
                "INVALID_REQUEST",
                f"Stage {name} differs in unit or quantile between runs.",
                reason="bench.incomparable",
            )
        ceiling = round(
            max(earlier.asserted * (1 + tolerance), _REGRESSION_FLOOR[stats.unit]), 3
        )
        if stats.asserted > ceiling:
            label = f"p{round(stats.quantile * 100)}"
            found.append(
                f"{name}: {label} {stats.asserted} {stats.unit} > {ceiling} {stats.unit}"
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


def _sync_directory(directory: Path) -> None:
    """Flush a directory entry to disk where the platform allows opening one."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def write_result(result: BenchResult, out_dir: Path) -> Path:
    """Write ``<out_dir>/<commit>/<suite>-<profile>.json`` atomically.

    The temp file is created with the umask-adjusted default mode and removed
    when any step fails, so the target is either the old or the new document.
    """
    path = out_dir / result.commit / f"{result.suite}-{result.profile}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(result.document()) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)
    return path


def load_result(path: Path) -> BenchResult:
    """Read a result document written by ``write_result``."""
    document = json.loads(path.read_bytes())
    if (
        document.pop("schema", None) != "tychora.bench-result"
        or document.pop("version", None) != 1
    ):
        raise ValueError(f"{path} is not a tychora.bench-result version 1 document")
    return BenchResult.model_validate(document, strict=False)
