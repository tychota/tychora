"""Gates backed by external tools: each runner calls the tool, each parser reduces its output."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health.model import Metric

COMPLEXITY_LIMIT = 15
VULTURE_FOUND = 3
JSCPD_SCOPES: dict[str, tuple[str, list[str]]] = {
    "python": ("python", ["src/tychora"]),
    "typescript": ("typescript,tsx", ["dashboard/src", "dashboard/e2e"]),
    "tests": ("python", ["tests"]),
}
VULTURE_LINE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+): unused "
    r"(?P<kind>function|method|class|property|attribute|import) '(?P<name>[^']+)'"
)


class MissingReport(RuntimeError):
    """A report file a gate reads was not produced by the step that should write it."""


def node_tool(root: Path, name: str) -> str:
    """Executable installed by `npm ci` in dashboard/, as an absolute path."""
    return str(root.resolve() / "dashboard" / "node_modules" / ".bin" / name)


def venv_tool(name: str) -> str:
    """Console script installed next to the running interpreter."""
    return str(Path(sys.executable).parent / name)


def run(
    command: list[str], root: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a tool from the given directory, capturing its output as text."""
    return subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def read_report(path: Path) -> str:
    """Read a report written by an earlier CI step, or fail naming the file."""
    if not path.is_file():
        raise MissingReport(
            f"{path} is missing; run the step that writes it (see .github/workflows)"
        )
    return path.read_text(encoding="utf-8")


def json_output(result: subprocess.CompletedProcess[str], tool: str) -> Any:
    """Parse a tool's JSON stdout, or fail with its own message when it printed none."""
    start = min(
        (i for i in (result.stdout.find("["), result.stdout.find("{")) if i >= 0),
        default=-1,
    )
    if start < 0:
        raise RuntimeError(f"{tool} failed: {result.stdout}{result.stderr}")
    return json.loads(result.stdout[start:])


def command_gate(name: str, commands: list[list[str]], root: Path) -> Metric:
    """Hard gate: the number of commands that exit non-zero (output is printed)."""
    failures = 0
    for command in commands:
        result = run(command, root)
        if result.returncode != 0:
            failures += 1
            print(
                f"[{name}] {' '.join(command)} failed:\n{result.stdout}{result.stderr}"
            )
    return Metric(name, failures, kind="hard")


def lint(root: Path) -> list[Metric]:
    """G1: ruff with the configured rule set."""
    return [command_gate("lint", [[venv_tool("ruff"), "check", "."]], root)]


def format_check(root: Path) -> list[Metric]:
    """G2: ruff format and Prettier report no changes."""
    commands = [
        [venv_tool("ruff"), "format", "--check", "."],
        ["npm", "--prefix", "dashboard", "run", "format:check"],
    ]
    return [command_gate("format", commands, root)]


def types(root: Path) -> list[Metric]:
    """G3: ty over Python sources, tests and tools; tsc over the dashboard."""
    commands = [
        [venv_tool("ty"), "check", "src", "tests", "tools"],
        ["npm", "--prefix", "dashboard", "run", "typecheck"],
    ]
    return [command_gate("types", commands, root)]


def ruff_counts(output: str, root: Path, *, by_rule: bool) -> dict[str, float]:
    """Per-file (or per-file-and-rule) counts from `ruff --output-format json`."""
    counts: Counter[str] = Counter()
    for item in json.loads(output):
        path = Path(item["filename"])
        rel = (
            path.relative_to(root.resolve()).as_posix()
            if path.is_absolute()
            else path.as_posix()
        )
        counts[f"{rel}::{item['code']}" if by_rule else rel] += 1
    return dict(sorted(counts.items()))


def _ruff_select(root: Path, rules: str) -> str:
    result = run(
        [
            venv_tool("ruff"),
            "check",
            "--select",
            rules,
            "--output-format",
            "json",
            "--exit-zero",
            "src/tychora",
        ],
        root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ruff failed: {result.stderr}")
    return result.stdout


def size_rules(root: Path) -> list[Metric]:
    """G4: too many returns, arguments or statements, per file and rule."""
    per_key = ruff_counts(
        _ruff_select(root, "PLR0911,PLR0913,PLR0915"), root, by_rule=True
    )
    return [Metric("size_rules", sum(per_key.values()), per_key=per_key)]


def body_imports(root: Path) -> list[Metric]:
    """G6: imports inside function bodies (ruff PLC0415), per file."""
    per_key = ruff_counts(_ruff_select(root, "PLC0415"), root, by_rule=False)
    return [Metric("function_body_imports", sum(per_key.values()), per_key=per_key)]


def docstrings(root: Path) -> list[Metric]:
    """G14 (ratchet part): missing module, class, method, function, package docstrings."""
    rules = "D100,D101,D102,D103,D104,D419"
    per_key = ruff_counts(_ruff_select(root, rules), root, by_rule=True)
    return [Metric("missing_docstrings", sum(per_key.values()), per_key=per_key)]


def complexity_from_json(items: list[dict[str, Any]]) -> Metric:
    """G25: functions above cognitive complexity 15, keyed by path::function."""
    per_key: dict[str, float] = {}
    for item in items:
        if item["complexity"] > COMPLEXITY_LIMIT:
            key = f"{item['path']}::{item['function_name']}"
            suffix = 2
            while key in per_key:
                key = f"{item['path']}::{item['function_name']}#{suffix}"
                suffix += 1
            per_key[key] = item["complexity"]
    return Metric("cognitive_complexity", len(per_key), per_key=per_key)


@functools.cache
def complexity_items(root: Path) -> tuple[dict[str, Any], ...]:
    """Every function with its cognitive complexity; complexipy runs once per process.

    The limit passed to complexipy is far above any function so that its exit code
    reports tool failures only; the gate applies COMPLEXITY_LIMIT itself.
    """
    output = root / "reports" / "complexipy.json"
    output.parent.mkdir(exist_ok=True)
    result = run(
        [
            venv_tool("complexipy"),
            "src/tychora",
            "--output-format",
            "json",
            "--output",
            str(output),
            "--quiet",
            "--max-complexity-allowed",
            "1000000",
        ],
        root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"complexipy failed: {result.stdout}{result.stderr}")
    return tuple(json.loads(read_report(output)))


def complexity(root: Path) -> list[Metric]:
    """G25: the functions above the cognitive complexity limit."""
    return [complexity_from_json(list(complexity_items(root.resolve())))]


def jscpd_metrics(scope: str, report: dict[str, Any]) -> list[Metric]:
    """G9-G11: duplicated-line percentage, plus new clones for production scopes."""
    total = report["statistics"]["total"]
    metrics = [
        Metric(f"duplication_{scope}", round(total["percentage"], 2), tolerance=0.01)
    ]
    if scope != "tests":
        metrics.append(Metric(f"new_clones_{scope}", total["newClones"], kind="hard"))
    return metrics


def jscpd_command(root: Path, scope: str, *, update: bool = False) -> list[str]:
    """The jscpd invocation for one scope; update rewrites its fingerprint allow-list."""
    formats, paths = JSCPD_SCOPES[scope]
    command = [
        node_tool(root, "jscpd"),
        "--config",
        ".jscpd.json",
        "--format",
        formats,
        "--report-name",
        scope,
        "--baseline",
        f"quality/jscpd/{scope}.json",
        *paths,
    ]
    return [*command, "--update-baseline"] if update else command


def duplication(root: Path) -> list[Metric]:
    """G9, G10, G11: run jscpd per scope and read its JSON reports."""
    metrics: list[Metric] = []
    for scope in JSCPD_SCOPES:
        result = run(jscpd_command(root, scope), root)
        if result.returncode != 0:
            raise RuntimeError(f"jscpd {scope} failed: {result.stdout}{result.stderr}")
        report = json.loads(read_report(root / "reports" / "jscpd" / f"{scope}.json"))
        metrics.extend(jscpd_metrics(scope, report))
    return metrics


def vulture_findings(output: str) -> dict[str, float]:
    """Unused functions, methods, classes, properties and attributes (not variables)."""
    findings: dict[str, float] = {}
    for line in output.splitlines():
        match = VULTURE_LINE.match(line)
        if match:
            findings[f"{match['path']}::{match['name']}"] = 1
    return findings


def whitelist_size(text: str) -> int:
    """Entries in quality/vulture_whitelist.py (one name per non-comment line)."""
    return sum(
        1 for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


def dead_code(root: Path) -> list[Metric]:
    """G12: vulture with the reviewed whitelist; the whitelist may only shrink."""
    result = run([venv_tool("vulture")], root)
    if result.returncode not in (0, VULTURE_FOUND):
        raise RuntimeError(f"vulture failed: {result.stdout}{result.stderr}")
    findings = vulture_findings(result.stdout)
    whitelist = (root / "quality" / "vulture_whitelist.py").read_text()
    return [
        Metric("dead_code_python", len(findings), kind="hard", per_key=findings),
        Metric("dead_code_whitelist", whitelist_size(whitelist)),
    ]


def knip_count(report: dict[str, Any]) -> int:
    """Total knip findings: files, dependencies, exports, types and the rest."""
    return sum(
        len(value)
        for issue in report["issues"]
        for value in issue.values()
        if isinstance(value, list)
    )


def dashboard_dead_code(root: Path) -> list[Metric]:
    """G13: knip over the dashboard."""
    dashboard = root / "dashboard"
    result = run(
        [
            node_tool(root, "knip"),
            "--reporter",
            "json",
            "--no-progress",
        ],
        dashboard,
    )
    return [
        Metric(
            "dead_code_dashboard", knip_count(json_output(result, "knip")), kind="hard"
        )
    ]


def dashboard_cycles(root: Path) -> list[Metric]:
    """G28: madge import cycles in dashboard/src."""
    dashboard = root / "dashboard"
    result = run(
        [
            node_tool(root, "madge"),
            "--circular",
            "--json",
            "--extensions",
            "ts,tsx",
            "--ts-config",
            "tsconfig.json",
            "src",
        ],
        dashboard,
    )
    cycles = json_output(result, "madge")
    return [Metric("dashboard_import_cycles", len(cycles), kind="hard")]
