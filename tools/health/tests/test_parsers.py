"""Recorded tool output reduced to metrics, without running the tools."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from tools.health import advisories, external, layering, testruns

ROOT = Path("/repo")


def test_ruff_counts_per_file_and_rule():
    output = json.dumps(
        [
            {"filename": "/repo/src/tychora/a.py", "code": "PLR0913"},
            {"filename": "/repo/src/tychora/a.py", "code": "PLR0913"},
            {"filename": "/repo/src/tychora/b.py", "code": "PLR0915"},
        ]
    )
    assert external.ruff_counts(output, ROOT, by_rule=True) == {
        "src/tychora/a.py::PLR0913": 2,
        "src/tychora/b.py::PLR0915": 1,
    }
    assert external.ruff_counts(output, ROOT, by_rule=False) == {
        "src/tychora/a.py": 2,
        "src/tychora/b.py": 1,
    }


def test_complexity_keeps_functions_above_fifteen():
    items = [
        {
            "path": "src/tychora/v.py",
            "function_name": "validate_bundle",
            "complexity": 240,
        },
        {"path": "src/tychora/v.py", "function_name": "small", "complexity": 15},
        {
            "path": "src/tychora/v.py",
            "function_name": "validate_bundle",
            "complexity": 16,
        },
    ]
    metric = external.complexity_from_json(items)
    assert metric.per_key == {
        "src/tychora/v.py::validate_bundle": 240,
        "src/tychora/v.py::validate_bundle#2": 16,
    }


def test_jscpd_report_gives_percentage_and_new_clones():
    report = {"statistics": {"total": {"percentage": 1.321182, "newClones": 1}}}
    duplication, new = external.jscpd_metrics("python", report)
    assert (duplication.name, duplication.value) == ("duplication_python", 1.32)
    assert (new.name, new.value, new.kind) == ("new_clones_python", 1, "hard")
    assert [m.name for m in external.jscpd_metrics("tests", report)] == [
        "duplication_tests"
    ]


def test_vulture_ignores_unused_variables():
    output = (
        "src/tychora/a.py:3: unused function 'old' (60% confidence)\n"
        "src/tychora/a.py:9: unused variable 'field' (60% confidence)\n"
        "src/tychora/b.py:1: unused method 'purge' (60% confidence)\n"
    )
    assert external.vulture_findings(output) == {
        "src/tychora/a.py::old": 1,
        "src/tychora/b.py::purge": 1,
    }
    assert external.whitelist_size("# header\n\nfoo  # reason\n_.bar  # reason\n") == 2


def test_knip_counts_every_listed_finding():
    report = {
        "issues": [
            {
                "file": "src/pages/Sources.tsx",
                "files": [{"name": "src/pages/Sources.tsx"}],
                "exports": [],
            },
            {"file": "package.json", "devDependencies": [{"name": "a"}, {"name": "b"}]},
            {
                "file": "src/api/contracts.ts",
                "types": [{"name": "Geometry"}],
                "exports": [{"name": "x"}],
            },
        ]
    }
    assert external.knip_count(report) == 5


def test_layering_exception_counts_per_contract():
    pyproject = {
        "tool": {
            "importlinter": {
                "contracts": [
                    {"id": "layers", "ignore_imports": ["a -> b", "c -> d"]},
                    {"id": "pure-contracts"},
                ]
            }
        }
    }
    assert layering.exception_counts(pyproject) == {"layers": 2, "pure-contracts": 0}


def coverage_report(covered_lines, branches=(1, 2)):
    files = {
        f"src/{module}": {
            "summary": {
                "covered_lines": 9,
                "num_statements": 10,
                "covered_branches": branches[0],
                "num_branches": branches[1],
            }
        }
        for module in testruns.CORE_MODULES
    }
    totals = {
        "covered_lines": covered_lines,
        "num_statements": 1000,
        "covered_branches": 700,
        "num_branches": 1000,
    }
    return {"totals": totals, "files": files}


def test_line_and_branch_coverage_with_core_module_floors():
    [line] = testruns.coverage_metrics(coverage_report(852), branch=False)
    assert (line.name, line.value, line.direction) == ("coverage_line", 85.2, "higher")
    assert line.per_key and line.per_key["tychora/contracts/identity.py"] == 90.0
    [branch] = testruns.coverage_metrics(coverage_report(852), branch=True)
    assert (branch.name, branch.value) == ("coverage_branch", 70.0)
    assert branch.per_key and branch.per_key["tychora/retrieval/fusion.py"] == 50.0


def test_missing_core_module_is_an_error_not_a_zero():
    report = coverage_report(1)
    del report["files"]["src/tychora/retrieval/fusion.py"]
    with pytest.raises(KeyError, match="fusion"):
        testruns.coverage_metrics(report, branch=False)


JUNIT = """<testsuites><testsuite name="pytest" time="221.5">
<testcase classname="tests.a" name="test_ok" time="1.0"/>
<testcase classname="tests.a" name="test_fail" time="40.2"><failure message="x"/></testcase>
<testcase classname="tests.b" name="test_skip" time="0.1"><skipped message="requires TYCHORA_TEST_X"/></testcase>
</testsuite></testsuites>"""


def test_junit_outcomes_runtime_and_slowest():
    assert testruns.outcomes(JUNIT) == {
        "tests.a::test_ok": "passed",
        "tests.a::test_fail": "failed",
        "tests.b::test_skip": "skipped",
    }
    assert testruns.suite_seconds(JUNIT) == 221.5
    assert testruns.slowest(JUNIT, 1) == [(40.2, "tests.a::test_fail")]


def _shard_report(time, *cases):
    body = "".join(
        f'<testcase classname="tests.{c}" name="test_{c}" time="1.0"/>' for c in cases
    )
    return f'<testsuites><testsuite name="pytest" time="{time}">{body}</testsuite></testsuites>'


def test_merged_shard_reports_sum_session_times_and_keep_every_outcome():
    merged = testruns.merge_junit(
        [_shard_report(100.5, "a", "b"), _shard_report(120.0, "c")]
    )
    # The runtime printed on pull requests is the sum of the shard sessions.
    assert testruns.suite_seconds(merged) == 220.5
    assert set(testruns.outcomes(merged)) == {
        "tests.a::test_a",
        "tests.b::test_b",
        "tests.c::test_c",
    }
    assert (
        testruns.merge_junit([JUNIT])
        and testruns.suite_seconds(testruns.merge_junit([JUNIT])) == 221.5
    )


def test_a_test_in_two_shard_reports_is_an_error():
    with pytest.raises(ValueError, match="tests.a::test_a"):
        testruns.merge_junit([_shard_report(1, "a"), _shard_report(1, "a")])
    with pytest.raises(ValueError, match="no test case"):
        testruns.merge_junit(["<testsuites/>"])


def test_flaky_tests_differ_between_seeds():
    other = JUNIT.replace('<failure message="x"/>', "")
    assert testruns.flaky_tests([JUNIT, other, JUNIT]) == ["tests.a::test_fail"]
    assert testruns.flaky_tests([JUNIT, JUNIT]) == []


def test_mutation_scores_exclude_documented_equivalents():
    results = (
        "    tychora.contracts.identity.x_digest__mutmut_1: killed\n"
        "    tychora.contracts.identity.x_digest__mutmut_2: survived\n"
        "    tychora.contracts.identity.x_canonical_bytes__mutmut_3: survived\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_1: timeout\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_2: no tests\n"
        "    tychora.retrieval.fusion.x_fuse__mutmut_3: not checked\n"
    )
    equivalent = {"tychora.contracts.identity.x_canonical_bytes__mutmut_3"}
    assert testruns.mutation_scores(results, equivalent) == {
        "tychora.contracts.identity": 50.0,
        "tychora.retrieval.fusion": 50.0,
    }


def test_missing_report_names_the_file(tmp_path):
    with pytest.raises(external.MissingReport, match="reports/junit.xml"):
        testruns.runtime(tmp_path)


def test_gold_report_is_keyed_by_variant_and_metric(tmp_path):
    report = tmp_path / "reports" / "code-gold.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps({"variants": {"lexical": {"file_recall": 0.5, "file_mrr": 0.4}}})
    )
    [metric] = testruns.gold_set(tmp_path)
    assert metric.value == 0.5
    assert metric.per_key == {"lexical::file_recall": 0.5, "lexical::file_mrr": 0.4}


REGISTER = """
[[advisory]]
id = "PYSEC-2026-311"
package = "chromadb"
reason = "Not reachable from the embedded PersistentClient."
reviewer = "Tycho Tatitscheff"
approved = 2026-10-16
expires = 2026-11-15
"""


def test_advisory_register_lists_unlisted_expired_and_obsolete_entries():
    waivers = advisories.load_register(REGISTER)
    reported = {"PYSEC-2026-311": "chromadb", "GHSA-new": "fastapi"}
    today = dt.date(2026, 10, 20)
    assert advisories.advisory_findings(reported, waivers, today) == {
        "fastapi:GHSA-new: not in quality/advisories.toml": 1
    }
    later = dt.date(2026, 11, 16)
    assert (
        "chromadb:PYSEC-2026-311: waiver expired 2026-11-15"
        in advisories.advisory_findings({"PYSEC-2026-311": "chromadb"}, waivers, later)
    )
    assert advisories.advisory_findings({}, waivers, today) == {
        "PYSEC-2026-311: no longer reported; remove it from quality/advisories.toml": 1
    }


def test_waiver_longer_than_thirty_days_is_rejected():
    with pytest.raises(ValueError, match="at most 30 days"):
        advisories.load_register(REGISTER.replace("2026-11-15", "2026-12-15"))


def test_pip_audit_and_npm_audit_parsing():
    pip = {
        "dependencies": [
            {
                "name": "chromadb",
                "version": "1.5.9",
                "vulns": [{"id": "PYSEC-2026-311"}],
            },
            {"name": "x", "vulns": []},
        ]
    }
    assert advisories.reported_advisories(pip) == {"PYSEC-2026-311": "chromadb"}
    assert (
        advisories.npm_vulnerabilities({"metadata": {"vulnerabilities": {"total": 0}}})
        == 0
    )


@pytest.mark.parametrize(
    ("gate", "report"),
    [
        (testruns.line_coverage, "reports/coverage.json"),
        (testruns.branch_coverage, "reports/coverage-branch.json"),
        (testruns.dashboard_coverage, "dashboard/coverage/coverage-summary.json"),
        (testruns.clean_suite, "reports/clean-junit.xml"),
        (testruns.mutation, "reports/mutmut-results.txt"),
        (testruns.gold_set, "reports/code-gold.json"),
    ],
)
def test_every_report_gate_names_its_missing_file(tmp_path, gate, report):
    with pytest.raises(external.MissingReport, match=report):
        gate(tmp_path)


def test_register_entry_with_a_missing_field_is_rejected():
    with pytest.raises(ValueError, match="PYSEC-2026-311: missing reason"):
        advisories.load_register(
            REGISTER.replace(
                'reason = "Not reachable from the embedded PersistentClient."\n', ""
            )
        )


@pytest.mark.parametrize("junit", ["<testsuites/>", '<testsuite time="1"/>'])
def test_empty_junit_report_is_an_error_not_a_clean_run(junit):
    with pytest.raises(ValueError, match="no test"):
        testruns.outcomes(junit)


def test_junit_without_testsuite_has_no_runtime():
    with pytest.raises(ValueError, match="no testsuite"):
        testruns.suite_seconds("<testsuites/>")


def test_gold_report_without_lexical_recall_is_an_error(tmp_path):
    report = tmp_path / "reports" / "code-gold.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"variants": {"dense": {"file_mrr": 0.4}}}))
    with pytest.raises(KeyError, match="file_recall"):
        testruns.gold_set(tmp_path)


def test_zero_coverage_totals_are_an_error_not_full_coverage():
    report = coverage_report(0)
    report["totals"]["num_statements"] = 0
    with pytest.raises(ValueError, match="coverage totals"):
        testruns.coverage_metrics(report, branch=False)
    report = coverage_report(852)
    next(iter(report["files"].values()))["summary"]["num_statements"] = 0
    with pytest.raises(ValueError, match="zero total"):
        testruns.coverage_metrics(report, branch=False)


def test_waiver_is_valid_through_its_expiry_day():
    waivers = advisories.load_register(REGISTER)
    reported = {"PYSEC-2026-311": "chromadb"}
    assert advisories.advisory_findings(reported, waivers, dt.date(2026, 11, 15)) == {}
    assert advisories.advisory_findings(reported, waivers, dt.date(2026, 11, 16)) == {
        "chromadb:PYSEC-2026-311: waiver expired 2026-11-15": 1
    }


def test_tool_without_json_output_fails_with_its_message():
    result = subprocess.CompletedProcess(
        ["knip"], 2, stdout="", stderr="boom: no config"
    )
    with pytest.raises(RuntimeError, match="knip failed:.*boom: no config"):
        external.json_output(result, "knip")


def test_dashboard_coverage_tolerates_the_timing_spread_of_repeated_runs(tmp_path):
    report = tmp_path / "dashboard" / "coverage" / "coverage-summary.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        '{"total": {"statements": {"pct": 81.67}, "branches": {"pct": 76.99}}}'
    )
    metrics = {m.name: m for m in testruns.dashboard_coverage(tmp_path)}
    # Observed spread over one commit: statements 81.54-81.67, branches 76.86-76.99.
    assert metrics["dashboard_coverage_statements"].tolerance >= 0.13
    assert metrics["dashboard_coverage_branches"].tolerance >= 0.13
