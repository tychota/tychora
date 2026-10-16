"""`python -m tools.health check|update|report|flaky|isolate`: the code-health command line."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from tools.health.architecture import write_report
from tools.health.external import JSCPD_SCOPES, jscpd_command, run
from tools.health.gates import Gate, select
from tools.health.model import (
    BASELINE,
    Finding,
    Metric,
    UpdateRefused,
    compare,
    load_baseline,
    save_baseline,
    updated_entry,
)
from tools.health.testruns import flaky_tests

DEFAULT_SELECTION = "not live and not qualification and not release"
ISOLATION_LIST = Path("tools/health/isolation.txt")


def commit(root: Path) -> str:
    """Short hash of HEAD, recorded as measured_at for changed baseline entries."""
    return subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def measure(root: Path, gates: list[Gate]) -> list[tuple[Gate, Metric]]:
    """Run every selected gate once and keep which gate produced each metric."""
    return [(gate, metric) for gate in gates for metric in gate.measure(root)]


def _measure_each(
    root: Path, gates: list[Gate]
) -> Iterator[tuple[Gate, Metric | None]]:
    """Yield each gate's metrics; a gate that raises prints an ERROR line and yields None."""
    for gate in gates:
        try:
            metrics = gate.measure(root)
        except Exception as error:  # noqa: BLE001 - any failure of one gate must not hide the others
            print(f"{gate.id:7} {'-':34} {'':>10} {'':>10} ERROR")
            print(f"        - {type(error).__name__}: {error}")
            yield gate, None
            continue
        for metric in metrics:
            yield gate, metric


def check(root: Path, gates: list[Gate]) -> int:
    """Print one line per metric and return 1 when any finding exists."""
    baseline = load_baseline(root / BASELINE)
    failed = False
    for gate, metric in _measure_each(root, gates):
        if metric is None:
            failed = True
            continue
        findings: list[Finding] = compare(metric, baseline.get(metric.name))
        entry = baseline.get(metric.name)
        base = "-" if metric.kind == "hard" or entry is None else f"{entry['value']:g}"
        status = (
            "ok"
            if not findings
            else ("stale" if all(f.stale for f in findings) else "FAIL")
        )
        print(f"{gate.id:7} {metric.name:34} {metric.value:>10g} {base:>10} {status}")
        for finding in findings:
            print(f"        - {finding.message}")
        failed = failed or bool(findings)
    return 1 if failed else 0


def refresh_clone_allowlists(root: Path, *, missing_only: bool = False) -> None:
    """Rewrite quality/jscpd/*.json; called only when no new clone exists."""
    (root / "quality" / "jscpd").mkdir(parents=True, exist_ok=True)
    for scope in JSCPD_SCOPES:
        if missing_only and (root / "quality" / "jscpd" / f"{scope}.json").exists():
            continue
        result = run(jscpd_command(root, scope, update=True), root)
        if result.returncode != 0:
            raise RuntimeError(f"jscpd {scope}: {result.stdout}{result.stderr}")


def update(root: Path, gates: list[Gate], *, initial: bool) -> int:
    """Record improvements; refuse regressions, unfixed hard gates and new over-limit keys."""
    duplication = any(gate.id == "G9-G11" for gate in gates)
    if initial and (root / BASELINE).exists():
        print(
            f"update refused: {BASELINE} already exists; run update without --initial"
        )
        return 1
    if initial and duplication:
        refresh_clone_allowlists(root, missing_only=True)
    measured = measure(root, gates)
    hard = [m for _, m in measured if m.kind == "hard" and m.value and not initial]
    if hard:
        print("fix the hard gates first: " + ", ".join(m.name for m in hard))
        return 1
    baseline = load_baseline(root / BASELINE)
    head = commit(root)
    new = dict(baseline)
    try:
        for _, metric in measured:
            if metric.kind == "ratchet":
                new[metric.name] = updated_entry(
                    metric, baseline.get(metric.name), head, initial=initial
                )
    except UpdateRefused as refused:
        print(f"update refused: {refused}")
        return 1
    if duplication and not initial:
        refresh_clone_allowlists(root)
    for name in sorted(new):
        before = baseline.get(name, {}).get("value")
        if before != new[name]["value"]:
            print(f"{name}: {before} -> {new[name]['value']}")
    save_baseline(root / BASELINE, new)
    return 0


def report(root: Path, gates: list[Gate]) -> int:
    """Print current values without comparing them; never fails on a value."""
    for gate, metric in measure(root, gates):
        print(f"{gate.id:7} {metric.name:34} {metric.value:>10g}")
    return 0


def isolate(root: Path) -> int:
    """Run each listed test file alone to prove it inherits no fixture by accident."""
    failures = 0
    for line in (root / ISOLATION_LIST).read_text().splitlines():
        test_file = line.strip()
        if not test_file or test_file.startswith("#"):
            continue
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                test_file,
                "-q",
                "-p",
                "no:randomly",
                "-m",
                DEFAULT_SELECTION,
            ],
            cwd=root,
            check=False,
        )
        failures += result.returncode not in (0, 5)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to one sub-command."""
    parser = argparse.ArgumentParser(prog="python -m tools.health")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "update", "report"):
        command = commands.add_parser(name)
        command.add_argument("--group", choices=("pr", "nightly", "all"), default="pr")
        command.add_argument(
            "--gates", default="", help="comma-separated ids, e.g. G5,G8"
        )
        command.add_argument("--root", type=Path, default=Path.cwd())
        command.add_argument(
            "--fast", action="store_true", help="G1-G3, G7 and G15 only"
        )
        if name == "update":
            command.add_argument("--initial", action="store_true")
        if name == "report":
            command.add_argument("--architecture", type=Path, metavar="MARKDOWN")
            command.add_argument("--previous", type=Path, metavar="MARKDOWN")
    flaky = commands.add_parser("flaky")
    flaky.add_argument("reports", nargs="+", type=Path)
    isolation = commands.add_parser("isolate")
    isolation.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.command == "flaky":
        differing = flaky_tests([path.read_text() for path in args.reports])
        for test in differing:
            print(f"flaky: {test}")
        return 1 if differing else 0
    if args.command == "isolate":
        return isolate(args.root)
    if args.command == "report" and args.architecture:
        write_report(args.root, args.architecture, args.previous)
        return 0
    gates = select(args.group, args.gates, fast=args.fast)
    if args.command == "check":
        return check(args.root, gates)
    if args.command == "update":
        return update(args.root, gates, initial=args.initial)
    return report(args.root, gates)


if __name__ == "__main__":
    raise SystemExit(main())
