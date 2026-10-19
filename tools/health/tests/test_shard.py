"""The CI shard plugin splits a selection into disjoint, duration-balanced shards."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools.health import shard
from tools.health.__main__ import main

NODE_IDS = [f"tests/pkg/test_m{i % 37}.py::test_case[{i}]" for i in range(3000)]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_hash_shards_partition_every_test_exactly_once(count):
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


def test_junit_id_matches_pytest_junit_writer():
    assert shard.junit_id("tests/api/test_x.py::test_y") == "tests.api.test_x::test_y"
    assert (
        shard.junit_id("tests/a/test_b.py::test_c[x/y.py::z]")
        == "tests.a.test_b::test_c[x/y.py::z]"
    )


def test_greedy_assignment_balances_recorded_durations():
    ids = [f"tests/test_m.py::test_{i}" for i in range(7)]
    durations = {
        shard.junit_id(n): seconds
        for n, seconds in zip(ids, [300, 120, 100, 90, 60, 30, 20], strict=True)
    }
    assigned = shard.assign(ids, durations, 3)
    loads = {
        k: sum(durations[shard.junit_id(n)] for n in ids if assigned[n] == k)
        for k in (1, 2, 3)
    }
    # Longest first to the least-loaded shard: 300 | 120+60+30 | 100+90+20.
    assert loads == {1: 300, 2: 210, 3: 210}
    assert shard.assign(list(reversed(ids)), durations, 3) == assigned


def test_unknown_tests_fall_back_to_the_hash_shard():
    ids = ["tests/test_m.py::test_known", "tests/test_m.py::test_new"]
    assigned = shard.assign(ids, {"tests.test_m::test_known": 5.0}, 4)
    assert assigned["tests/test_m.py::test_new"] == shard.shard_of(ids[1], 4)
    assert set(assigned) == set(ids)


@pytest.mark.parametrize("known", [0, 1500, 3000])
@pytest.mark.parametrize("count", [2, 3, 5])
def test_assignment_is_exactly_once_with_any_mix_of_records(known, count):
    durations = {
        shard.junit_id(n): (i % 13) * 0.7 for i, n in enumerate(NODE_IDS[:known])
    }
    assigned = shard.assign(NODE_IDS, durations, count)
    assert set(assigned) == set(NODE_IDS)
    assert set(assigned.values()) <= set(range(1, count + 1))


@pytest.mark.parametrize("spec", ["0/3", "4/3", "1", "a/b", "1/0"])
def test_malformed_shard_is_a_usage_error(spec):
    with pytest.raises(pytest.UsageError):
        shard.parse_shard(spec)


class _Hook:
    def __init__(self):
        self.deselected = []

    def pytest_deselected(self, items):
        self.deselected.extend(items)


def _config(spec, durations_file):
    options = {"shard": spec, "shard_durations": str(durations_file)}
    return SimpleNamespace(
        getoption=options.__getitem__, hook=_Hook(), rootpath=Path("/nonexistent")
    )


def test_collection_keeps_this_shard_and_runs_long_tests_first(tmp_path):
    ids = NODE_IDS[:200]
    durations = {shard.junit_id(n): 0.1 for n in ids}
    durations[shard.junit_id(ids[150])] = 40.0
    durations[shard.junit_id(ids[120])] = 15.0
    path = tmp_path / "durations.json"
    path.write_text(json.dumps({"schema": 1, "durations": durations}))
    items = [SimpleNamespace(nodeid=n) for n in ids]
    kept_per_shard = []
    for k in (1, 2, 3):
        selected: list[Any] = list(items)
        config = _config(f"{k}/3", path)
        shard.pytest_collection_modifyitems(config, selected)
        assert len(selected) + len(config.hook.deselected) == len(items)
        long = [i.nodeid for i in selected if durations[shard.junit_id(i.nodeid)] >= 10]
        assert [i.nodeid for i in selected[: len(long)]] == long
        kept_per_shard.append(selected)
    assert sorted(i.nodeid for part in kept_per_shard for i in part) == sorted(ids)


def test_without_shard_nothing_is_deselected(tmp_path):
    items: list[Any] = [SimpleNamespace(nodeid=n) for n in NODE_IDS[:10]]
    config = _config(None, tmp_path / "absent.json")
    shard.pytest_collection_modifyitems(config, items)
    assert len(items) == 10 and config.hook.deselected == []


def test_record_writes_durations_from_junit(tmp_path):
    first = tmp_path / "a.xml"
    first.write_text(
        '<testsuites><testsuite time="3"><testcase classname="tests.a" name="test_x" time="2.345"/>'
        "</testsuite></testsuites>"
    )
    second = tmp_path / "b.xml"
    second.write_text(
        '<testsuite time="1"><testcase classname="tests.a" name="test_x" time="9"/>'
        '<testcase classname="tests.b" name="test_y" time="0.5"/></testsuite>'
    )
    output = tmp_path / "durations.json"
    assert (
        main(["shard", "--record", str(first), str(second), "--output", str(output)])
        == 0
    )
    assert shard.load_durations(output) == {
        "tests.a::test_x": 2.35,
        "tests.b::test_y": 0.5,
    }
    assert shard.load_durations(tmp_path / "absent.json") == {}
