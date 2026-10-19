"""Gates that read reports written by test runs: coverage, runtime, clean suite, flakes, mutation, gold set."""

from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health.external import read_report
from tools.health.model import Metric

CORE_MODULES = (
    "tychora/contracts/identity.py",
    "tychora/contracts/publication.py",
    "tychora/adapters/sqlite/publication.py",
    "tychora/retrieval/eligibility.py",
    "tychora/adapters/sqlite/eligibility.py",
    "tychora/retrieval/lexical_query.py",
    "tychora/adapters/sqlite/lexical.py",
    "tychora/retrieval/fusion.py",
)
COVERAGE_TOLERANCE = 0.1
# Vitest timing-sensitive component tests cover a few more or fewer branches under load:
# statements ranged 81.54-81.67 and branches 76.86-76.99 over repeated runs of one commit.
DASHBOARD_COVERAGE_TOLERANCE = 0.15
RUNTIME_TOLERANCE = 0.15
DETECTED = frozenset({"killed", "timeout"})
NOT_RUN = frozenset({"not checked", "skipped"})


def _percent(covered: int, total: int, what: str = "report") -> float:
    if not total:
        raise ValueError(f"{what} has nothing to measure (zero total)")
    return round(100.0 * covered / total, 2)


def _core_file(files: dict[str, Any], module: str) -> dict[str, Any]:
    for path, data in files.items():
        if path.replace("\\", "/").endswith(module):
            return data["summary"]
    raise KeyError(f"{module} is missing from the coverage report")


def coverage_metrics(report: dict[str, Any], *, branch: bool) -> list[Metric]:
    """G16: total and per-core-module line (pull requests) or branch (nightly) coverage."""
    kind = "branch" if branch else "line"
    covered_key, total_key = (
        ("covered_branches", "num_branches")
        if branch
        else ("covered_lines", "num_statements")
    )
    totals = report["totals"]
    per_key: dict[str, float] = {}
    for module in CORE_MODULES:
        summary = _core_file(report["files"], module)
        per_key[module] = _percent(summary[covered_key], summary[total_key], module)
    return [
        Metric(
            f"coverage_{kind}",
            _percent(totals[covered_key], totals[total_key], "coverage totals"),
            direction="higher",
            tolerance=COVERAGE_TOLERANCE,
            per_key=per_key,
        )
    ]


def line_coverage(root: Path) -> list[Metric]:
    """Pull-request coverage from reports/coverage.json (sysmon, lines only)."""
    return coverage_metrics(
        json.loads(read_report(root / "reports/coverage.json")), branch=False
    )


def branch_coverage(root: Path) -> list[Metric]:
    """Nightly coverage from reports/coverage-branch.json."""
    report = json.loads(read_report(root / "reports/coverage-branch.json"))
    return coverage_metrics(report, branch=True)


def dashboard_coverage(root: Path) -> list[Metric]:
    """G16 (dashboard): Vitest V8 statement and branch percentages."""
    summary = json.loads(read_report(root / "dashboard/coverage/coverage-summary.json"))
    total = summary["total"]
    return [
        Metric(
            f"dashboard_coverage_{name}",
            total[name]["pct"],
            direction="higher",
            tolerance=DASHBOARD_COVERAGE_TOLERANCE,
        )
        for name in ("statements", "branches")
    ]


def outcomes(junit: str) -> dict[str, str]:
    """Test id to passed, failed or skipped from a JUnit XML report."""
    results: dict[str, str] = {}
    for case in ET.fromstring(junit).iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None or case.find("error") is not None:
            results[test_id] = "failed"
        elif case.find("skipped") is not None:
            results[test_id] = "skipped"
        else:
            results[test_id] = "passed"
    if not results:
        raise ValueError("the JUnit report contains no test case")
    return results


def merge_junit(reports: list[str]) -> str:
    """One <testsuites> holding the <testsuite> of every shard report, unchanged.

    The shards partition the selection, so a test id present in two reports means
    the split is broken and is an error rather than a silently overwritten outcome.
    """
    merged = ET.Element("testsuites")
    seen: set[str] = set()
    for report in reports:
        root = ET.fromstring(report)
        for suite in (
            [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        ):
            for case in suite.iter("testcase"):
                test_id = f"{case.get('classname')}::{case.get('name')}"
                if test_id in seen:
                    raise ValueError(f"{test_id} is in more than one shard report")
                seen.add(test_id)
            merged.append(suite)
    if not seen:
        raise ValueError("the shard reports contain no test case")
    return ET.tostring(merged, encoding="unicode")


def suite_seconds(junit: str) -> float:
    """Session time recorded by pytest's JUnit writer, summed over the testsuites.

    A report merged from shards holds one testsuite per shard, so the value is the
    sum of the shard sessions (the test time of the whole selection), not the
    wall-clock of the parallel CI run.
    """
    root = ET.fromstring(junit)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ValueError("the JUnit report contains no testsuite")
    return round(sum(float(suite.get("time", 0)) for suite in suites), 1)


def slowest(junit: str, count: int = 20) -> list[tuple[float, str]]:
    """The slowest test cases, printed on every run so their cost stays visible."""
    cases = [
        (float(case.get("time", 0)), f"{case.get('classname')}::{case.get('name')}")
        for case in ET.fromstring(junit).iter("testcase")
    ]
    return sorted(cases, reverse=True)[:count]


def clean_suite(root: Path) -> list[Metric]:
    """G17: the default selection with no TYCHORA_TEST_* variable has no failure."""
    results = outcomes(read_report(root / "reports/clean-junit.xml"))
    failed = {test: 1.0 for test, outcome in results.items() if outcome == "failed"}
    return [Metric("clean_machine_failures", len(failed), kind="hard", per_key=failed)]


def runtime(root: Path) -> list[Metric]:
    """G18: session time of reports/junit.xml; nightly, at most 15% above the baseline.

    Nightly copies its seed-2 run (no coverage) to reports/junit.xml; pull requests
    print the merged shard report (sum of the shard sessions) for information only.
    """
    junit = read_report(root / "reports/junit.xml")
    for seconds, test in slowest(junit):
        print(f"[slowest] {seconds:7.1f}s {test}")
    return [
        Metric(
            "test_runtime_seconds",
            suite_seconds(junit),
            relative_tolerance=RUNTIME_TOLERANCE,
            stale_on_improvement=False,
        )
    ]


def flaky_tests(reports: list[str]) -> list[str]:
    """Tests whose outcome differs between runs with different random seeds."""
    runs = [outcomes(report) for report in reports]
    tests = set().union(*runs)
    return sorted(t for t in tests if len({run.get(t, "absent") for run in runs}) > 1)


def mutation_scores(results: str, equivalent: set[str]) -> dict[str, float]:
    """Score per module: killed or timed-out mutants over all non-equivalent ones."""
    detected: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for line in results.splitlines():
        name, _, status = line.strip().partition(": ")
        if not status or status in NOT_RUN or name in equivalent:
            continue
        module = name.rsplit(".", 1)[0]
        total[module] += 1
        detected[module] += status in DETECTED
    return {
        module: _percent(detected[module], total[module]) for module in sorted(total)
    }


def mutation(root: Path) -> list[Metric]:
    """G21: nightly mutation score per core module from `mutmut results --all true`."""
    results = read_report(root / "reports/mutmut-results.txt")
    register = root / "quality/equivalent-mutants.toml"
    equivalent_file = tomllib.loads(register.read_text()) if register.is_file() else {}
    equivalent = {entry["name"] for entry in equivalent_file.get("mutant", [])}
    scores = mutation_scores(results, equivalent)
    overall = round(sum(scores.values()) / len(scores), 2) if scores else 0.0
    return [
        Metric(
            "mutation_score",
            overall,
            direction="higher",
            tolerance=COVERAGE_TOLERANCE,
            per_key=scores,
            new_keys_allowed=True,
        )
    ]


def gold_set(root: Path) -> list[Metric]:
    """G22: mean code gold-set metrics per variant, written by the evaluation test."""
    report = json.loads(read_report(root / "reports/code-gold.json"))
    per_key = {
        f"{variant}::{metric}": value
        for variant, metrics in report["variants"].items()
        for metric, value in metrics.items()
    }
    overall = per_key["lexical::file_recall"]
    return [
        Metric(
            "code_gold",
            overall,
            direction="higher",
            tolerance=0.0,
            per_key=per_key,
            new_keys_allowed=True,
        )
    ]
