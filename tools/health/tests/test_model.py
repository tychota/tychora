"""Ratchet rules: global and per-key regressions, new keys, stale baselines."""

import json

import pytest

from tools.health.model import (
    Metric,
    UpdateRefused,
    compare,
    load_baseline,
    save_baseline,
    updated_entry,
)


def entry(value, per_key=None):
    data = {"value": value, "direction": "lower", "measured_at": "abc"}
    if per_key is not None:
        data["per_key"] = per_key
    return data


def test_hard_metric_fails_on_any_finding_and_ignores_the_baseline():
    assert compare(Metric("plan_references", 0, kind="hard"), None) == []
    [finding] = compare(Metric("plan_references", 2, kind="hard"), entry(5))
    assert "allows none" in finding.message
    assert not finding.stale


def test_global_regression_fails():
    [finding] = compare(Metric("raw_sql", 238), entry(237))
    assert finding.message == "238 is worse than baseline 237"
    assert not finding.stale


def test_higher_is_better_metric_fails_when_it_drops():
    metric = Metric("coverage_line", 85.0, direction="higher", tolerance=0.1)
    [finding] = compare(metric, {"value": 85.25, "measured_at": "abc"})
    assert "worse" in finding.message


def test_tolerance_absorbs_noise_in_both_directions():
    base = {"value": 85.25, "measured_at": "abc"}
    for value in (85.16, 85.25, 85.34):
        metric = Metric("coverage_line", value, direction="higher", tolerance=0.1)
        assert compare(metric, base) == []


def test_relative_tolerance_allows_fifteen_percent_slower_runs():
    metric = Metric(
        "test_runtime_seconds",
        1150,
        relative_tolerance=0.15,
        stale_on_improvement=False,
    )
    assert compare(metric, entry(1000)) == []
    assert compare(
        Metric("test_runtime_seconds", 1151, relative_tolerance=0.15), entry(1000)
    )
    faster = Metric(
        "test_runtime_seconds", 500, relative_tolerance=0.15, stale_on_improvement=False
    )
    assert compare(faster, entry(1000)) == []


def test_stale_baseline_fails_with_update_instruction():
    [finding] = compare(Metric("raw_sql", 230), entry(237))
    assert finding.stale
    assert "python -m tools.health update" in finding.message


def test_per_key_growth_fails_even_when_the_total_improves():
    metric = Metric("function_length", 2, per_key={"a.py::f": 120, "b.py::g": 81})
    findings = compare(metric, entry(3, {"a.py::f": 100, "b.py::g": 81, "c.py::h": 90}))
    messages = [f.message for f in findings if not f.stale]
    assert messages == ["a.py::f: 120 is worse than 100"]


def test_new_key_with_a_value_fails():
    metric = Metric("function_length", 1, per_key={"new.py::long": 90})
    [finding] = [f for f in compare(metric, entry(1, {})) if not f.stale]
    assert finding.message == "new.py::long: new entry with value 90"


def test_new_key_allowed_for_global_only_metrics():
    base = {"value": 90, "measured_at": "x", "per_key": {}}

    def metric(allowed):
        return Metric(
            "coverage_line_core",
            90,
            direction="higher",
            per_key={"new.py": 10},
            new_keys_allowed=allowed,
        )

    assert compare(metric(True), base) == []
    [finding] = compare(metric(False), base)
    assert finding.message == "new.py: new entry with value 10"


def test_higher_metric_per_key_worse_value_fails():
    metric = Metric("cov", 90, direction="higher", per_key={"a.py": 70})
    [finding] = compare(metric, entry(90, {"a.py": 80}))
    assert finding.message == "a.py: 70 is worse than 80"


def test_higher_metric_new_key_at_or_above_the_global_value_passes():
    metric = Metric("cov", 90, direction="higher", per_key={"a.py": 95})
    assert compare(metric, entry(90, {})) == []


def test_higher_metric_removed_key_is_stale():
    metric = Metric("cov", 90, direction="higher", per_key={})
    [finding] = compare(metric, entry(90, {"gone.py": 50}))
    assert finding.stale


def test_update_higher_metric_refuses_a_new_key_below_the_global_value():
    metric = Metric("cov", 90, direction="higher", per_key={"new.py": 10})
    with pytest.raises(UpdateRefused, match="new entries"):
        updated_entry(metric, entry(90, {}), "c", initial=False)
    ok = Metric("cov", 90, direction="higher", per_key={"new.py": 95})
    result = updated_entry(ok, entry(90, {"gone.py": 50}), "c", initial=False)
    assert result["per_key"] == {"new.py": 95}


def test_relative_tolerance_applies_per_key():
    metric = Metric(
        "t",
        1000,
        relative_tolerance=0.15,
        per_key={"a": 115},
        stale_on_improvement=False,
    )
    assert compare(metric, entry(1000, {"a": 100})) == []
    worse = Metric("t", 1000, relative_tolerance=0.15, per_key={"a": 116})
    assert [f for f in compare(worse, entry(1000, {"a": 100})) if not f.stale]


def test_update_twice_is_idempotent():
    metric = Metric("m", 5, per_key={"a": 3, "b": 2})
    base = entry(5, {"a": 3, "b": 2})
    once = updated_entry(metric, base, "new", initial=False)
    twice = updated_entry(metric, once, "newer", initial=False)
    assert json.dumps(once) == json.dumps(twice) == json.dumps(base)


def test_value_exactly_at_the_tolerance_edge_is_not_a_regression():
    metric = Metric("coverage_line", 85.05, direction="higher", tolerance=0.1)
    assert compare(metric, {"value": 85.15, "measured_at": "a"}) == []


def test_removed_key_is_a_stale_improvement():
    metric = Metric("function_length", 0, per_key={})
    findings = compare(metric, entry(0, {"gone.py::f": 95}))
    assert [f.stale for f in findings] == [True]


def test_missing_entry_asks_for_an_update():
    [finding] = compare(Metric("raw_sql", 1), None)
    assert "no baseline entry" in finding.message


def test_update_records_improvements_and_keeps_unchanged_commit():
    improved = updated_entry(Metric("raw_sql", 230), entry(237), "new", initial=False)
    assert improved["value"] == 230
    assert improved["measured_at"] == "new"
    same = updated_entry(Metric("raw_sql", 237), entry(237), "new", initial=False)
    assert same["measured_at"] == "abc"


def test_update_refuses_a_regression():
    with pytest.raises(UpdateRefused, match="worse"):
        updated_entry(Metric("raw_sql", 238), entry(237), "new", initial=False)


def test_update_accepts_a_rename_but_not_a_new_long_function():
    renamed = Metric("function_length", 1, per_key={"a.py::new_name": 90})
    result = updated_entry(
        renamed, entry(1, {"a.py::old_name": 90}), "c", initial=False
    )
    assert result["per_key"] == {"a.py::new_name": 90}
    grown = Metric("function_length", 2, per_key={"a.py::f": 90, "a.py::g": 85})
    with pytest.raises(UpdateRefused, match="only renames"):
        updated_entry(
            grown, entry(2, {"a.py::f": 90, "b.py::x": 81}), "c", initial=False
        )


def test_update_without_baseline_requires_initial():
    with pytest.raises(UpdateRefused, match="--initial"):
        updated_entry(Metric("raw_sql", 1), None, "c", initial=False)
    assert updated_entry(Metric("raw_sql", 1), None, "c", initial=True)["value"] == 1


def test_hand_edited_worse_value_passes_only_when_it_matches_the_current_value():
    # A reviewer sees the hand edit in the diff; the checker cannot tell it apart.
    assert compare(Metric("raw_sql", 240), entry(240)) == []
    [finding] = compare(Metric("raw_sql", 239), entry(240))
    assert finding.stale


def test_baseline_round_trip_is_sorted_and_versioned(tmp_path):
    path = tmp_path / "quality" / "baseline.json"
    save_baseline(path, {"b": entry(1), "a": entry(2, {"z": 1, "y": 2})})
    assert load_baseline(path)["a"]["per_key"] == {"y": 2, "z": 1}
    assert path.read_text().index('"a"') < path.read_text().index('"b"')
    assert load_baseline(tmp_path / "missing.json") == {}
    path.write_text('{"schema": 2, "metrics": {}}')
    with pytest.raises(ValueError, match="schema"):
        load_baseline(path)
