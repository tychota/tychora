"""Pytest plugin that keeps one deterministic, duration-balanced shard of the tests.

CI loads it with `python -m pytest -p tools.health.shard --shard K/N` on N parallel
runners. Every runner (and every xdist worker in it) computes the same partition
from the same inputs, without sharing state:

- tests with a recorded duration (quality/test-durations.json, written by
  `python -m tools.health shard --record <junit>...`) go longest first to the
  least-loaded shard;
- a test without a record (a new one) goes to the shard given by a SHA-256 of
  its node id and counts as the mean recorded duration.

Each selected test lands in exactly one shard. Inside a shard, tests recorded
as slower than LONG_TEST_SECONDS run first, longest first, so xdist starts
them on separate workers instead of leaving one to finish alone at the end.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

DURATIONS = Path("quality/test-durations.json")
LONG_TEST_SECONDS = 10.0


def parse_shard(spec: str) -> tuple[int, int]:
    """`K/N` to (K, N) with 1 <= K <= N; anything else is a usage error."""
    index, _, count = spec.partition("/")
    try:
        k, n = int(index), int(count)
    except ValueError:
        raise pytest.UsageError(f"--shard expects K/N, got {spec!r}") from None
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard {spec!r}: K must be between 1 and N")
    return k, n


def shard_of(nodeid: str, count: int) -> int:
    """1-based hash shard of a test; stable across processes, machines and Pythons."""
    digest = hashlib.sha256(nodeid.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def junit_id(nodeid: str) -> str:
    """`classname::name` as pytest's JUnit writer derives it from a node id."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return f"{'.'.join(names[:-1])}::{names[-1]}"


def assign(
    nodeids: Iterable[str], durations: Mapping[str, float], count: int
) -> dict[str, int]:
    """Shard (1-based) of every node id: greedy longest-first, hash for unknown tests."""
    ids = list(dict.fromkeys(nodeids))
    known = [(durations[junit_id(n)], n) for n in ids if junit_id(n) in durations]
    default = sum(seconds for seconds, _ in known) / len(known) if known else 1.0
    loads = [0.0] * count
    shards: dict[str, int] = {}
    for nodeid in ids:
        if junit_id(nodeid) not in durations:
            shards[nodeid] = shard_of(nodeid, count)
            loads[shards[nodeid] - 1] += default
    for seconds, nodeid in sorted(known, key=lambda pair: (-pair[0], pair[1])):
        target = min(range(count), key=lambda index: (loads[index], index))
        loads[target] += seconds
        shards[nodeid] = target + 1
    return shards


def load_durations(path: Path) -> dict[str, float]:
    """Recorded seconds per JUnit test id; an absent file means hash sharding only."""
    if not path.is_file():
        return {}
    return {
        str(k): float(v) for k, v in json.loads(path.read_text())["durations"].items()
    }


def record(reports: Iterable[str]) -> dict[str, object]:
    """The durations file content from JUnit reports; the first report naming a test wins."""
    durations: dict[str, float] = {}
    for report in reports:
        for case in ET.fromstring(report).iter("testcase"):
            test_id = f"{case.get('classname')}::{case.get('name')}"
            durations.setdefault(test_id, round(float(case.get("time", 0)), 2))
    if not durations:
        raise ValueError("the JUnit reports contain no test case")
    return {"schema": 1, "durations": dict(sorted(durations.items()))}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register --shard K/N and the durations file it balances with."""
    group = parser.getgroup("shard")
    group.addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only shard K of N (1-based), balanced by recorded test durations",
    )
    group.addoption(
        "--shard-durations",
        default=None,
        metavar="JSON",
        help=f"recorded durations (default: {DURATIONS} under the rootdir)",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect the tests of the other shards and start the long ones first.

    trylast: the partition is computed over the selection left by -m and every
    other plugin, the same on every runner.
    """
    spec = config.getoption("shard")
    if not spec:
        return
    index, count = parse_shard(spec)
    option = config.getoption("shard_durations")
    durations = load_durations(Path(option) if option else config.rootpath / DURATIONS)
    shards = assign((item.nodeid for item in items), durations, count)
    kept = [item for item in items if shards[item.nodeid] == index]
    dropped = [item for item in items if shards[item.nodeid] != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    long_first = sorted(
        (
            item
            for item in kept
            if durations.get(junit_id(item.nodeid), 0.0) >= LONG_TEST_SECONDS
        ),
        key=lambda item: -durations[junit_id(item.nodeid)],
    )
    front = {item.nodeid for item in long_first}
    items[:] = long_first + [item for item in kept if item.nodeid not in front]
