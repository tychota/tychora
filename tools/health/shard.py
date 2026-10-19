"""Pytest plugin that keeps one deterministic shard of the collected tests.

CI loads it with `python -m pytest -p tools.health.shard --shard K/N` on N parallel
runners. Each test goes to the shard given by a SHA-256 of its node id, so every
runner (and every xdist worker inside it) computes the same partition without
sharing state, and the N shards together hold each selected test exactly once.
"""

from __future__ import annotations

import hashlib

import pytest


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
    """1-based shard of a test; stable across processes, machines and Python versions."""
    digest = hashlib.sha256(nodeid.encode()).digest()
    return int.from_bytes(digest[:8], "big") % count + 1


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("shard").addoption(
        "--shard",
        default=None,
        metavar="K/N",
        help="run only shard K of N (1-based), split by a hash of each test id",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect the tests of the other shards; the relative order of the rest is kept."""
    spec = config.getoption("shard")
    if not spec:
        return
    index, count = parse_shard(spec)
    kept = [item for item in items if shard_of(item.nodeid, count) == index]
    dropped = [item for item in items if shard_of(item.nodeid, count) != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
