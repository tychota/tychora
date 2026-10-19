"""The CI shard plugin splits a selection into disjoint shards that cover it."""

from types import SimpleNamespace
from typing import Any

import pytest

from tools.health import shard

NODE_IDS = [f"tests/pkg/test_m{i % 37}.py::test_case[{i}]" for i in range(3000)]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_shards_partition_every_test_exactly_once(count):
    shards = {
        k: [n for n in NODE_IDS if shard.shard_of(n, count) == k]
        for k in range(1, count + 1)
    }
    assert sum(len(ids) for ids in shards.values()) == len(NODE_IDS)
    assert set().union(*map(set, shards.values())) == set(NODE_IDS)
    # A hash split balances by count: each shard is within 20% of an even share.
    assert all(
        abs(len(ids) - len(NODE_IDS) / count) < 0.2 * len(NODE_IDS) / count
        for ids in shards.values()
    )


def test_shard_of_is_pinned_so_every_runner_agrees():
    # SHA-256, not hash(): Python randomizes str hashes per process.
    assert [
        shard.shard_of("tests/api/test_public.py::test_query[1]", n) for n in (2, 3, 4)
    ] == [1, 2, 1]


@pytest.mark.parametrize("spec", ["0/3", "4/3", "1", "a/b", "1/0"])
def test_malformed_shard_is_a_usage_error(spec):
    with pytest.raises(pytest.UsageError):
        shard.parse_shard(spec)


class _Hook:
    def __init__(self):
        self.deselected = []

    def pytest_deselected(self, items):
        self.deselected.extend(items)


def _config(spec):
    return SimpleNamespace(getoption=lambda name: spec, hook=_Hook())


def test_collection_keeps_only_this_shard_in_order():
    items = [SimpleNamespace(nodeid=n) for n in NODE_IDS[:200]]
    kept_per_shard = []
    for k in (1, 2, 3):
        selected: list[Any] = list(items)
        config = _config(f"{k}/3")
        shard.pytest_collection_modifyitems(config, selected)
        assert [i.nodeid for i in selected] == [
            i.nodeid for i in items if shard.shard_of(i.nodeid, 3) == k
        ]
        assert len(selected) + len(config.hook.deselected) == len(items)
        kept_per_shard.append(selected)
    assert sorted(i.nodeid for part in kept_per_shard for i in part) == sorted(
        NODE_IDS[:200]
    )


def test_without_shard_nothing_is_deselected():
    items: list[Any] = [SimpleNamespace(nodeid=n) for n in NODE_IDS[:10]]
    config = _config(None)
    shard.pytest_collection_modifyitems(config, items)
    assert len(items) == 10 and config.hook.deselected == []
