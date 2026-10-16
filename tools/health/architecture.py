"""Architecture report: coupling, cycles, complexity, cohesion, depth, co-change, pass-through."""

from __future__ import annotations

import ast
import json
import statistics
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from tools.health import graph
from tools.health.external import (
    complexity_from_json,
    complexity_items,
    run,
    venv_tool,
)
from tools.health.static import python_files, relative

ABSTRACT_BASES = frozenset({"Protocol", "ABC"})


def _bases(node: ast.ClassDef) -> set[str]:
    return {getattr(base, "id", getattr(base, "attr", "")) for base in node.bases}


def martin_metrics(root: Path) -> list[dict[str, Any]]:
    """Ca, Ce, instability I, abstractness A and distance D per component."""
    imports = graph.import_graph(root, top_level_only=False)
    coupling = graph.coupling(imports)
    classes: Counter[str] = Counter()
    abstract: Counter[str] = Counter()
    modules: Counter[str] = Counter()
    for path in python_files(root):
        name = graph.component(graph.module_name(root, path))
        modules[name] += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                classes[name] += 1
                abstract[name] += bool(_bases(node) & ABSTRACT_BASES)
    rows = []
    for name, (ca, ce) in coupling.items():
        instability = ce / (ca + ce) if ca + ce else 0.0
        abstractness = abstract[name] / classes[name] if classes[name] else 0.0
        rows.append(
            {
                "component": name,
                "modules": modules[name],
                "ca": ca,
                "ce": ce,
                "i": round(instability, 2),
                "a": round(abstractness, 2),
                "d": round(abs(abstractness + instability - 1), 2),
            }
        )
    return rows


def _find(parent: dict[str, str], name: str) -> str:
    while parent[name] != name:
        parent[name] = parent[parent[name]]
        name = parent[name]
    return name


def _methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        item
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
        and not (item.name.startswith("__") and item.name.endswith("__"))
        and not any(getattr(d, "id", "") == "staticmethod" for d in item.decorator_list)
    ]


def _components(methods: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> int:
    """Connected components of methods linked by a shared attribute or a call."""
    names = {method.name for method in methods}
    uses = {
        method.name: {
            sub.attr
            for sub in ast.walk(method)
            if isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id in {"self", "cls"}
        }
        for method in methods
    }
    parent = {name: name for name in names}
    ordered = sorted(names)
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            shared = (uses[first] - names) & (uses[second] - names)
            if shared or second in uses[first] or first in uses[second]:
                parent[_find(parent, first)] = _find(parent, second)
    return len({_find(parent, name) for name in names})


def lcom4(root: Path) -> list[dict[str, Any]]:
    """Connected components of each non-protocol class with three or more methods."""
    rows = []
    for path in python_files(root):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or _bases(node) & ABSTRACT_BASES:
                continue
            methods = _methods(node)
            if len(methods) < 3:
                continue
            components = _components(methods)
            if components > 1:
                rows.append(
                    {
                        "class": f"{relative(root, path)}:{node.lineno} {node.name}",
                        "lcom4": components,
                        "methods": len(methods),
                    }
                )
    return sorted(rows, key=lambda row: (-row["lcom4"], row["class"]))


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = node.args
    names = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return [name for name in names if name not in {"self", "cls"}]


def _interface_elements(tree: ast.Module) -> int:
    """Public functions, classes, methods and their parameters in one module."""
    elements = 0
    for node in tree.body:
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef
        ) and not node.name.startswith("_"):
            elements += 1 + len(_parameters(node))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            elements += 1
            for item in node.body:
                if isinstance(
                    item, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not item.name.startswith("_"):
                    elements += 1 + len(_parameters(item))
    return elements


def depth_and_pass_through(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Lines per interface element per module, and exact forwarding functions."""
    depth, forwarding = [], []
    for path in python_files(root):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        code = sum(
            1
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        elements = _interface_elements(tree)
        if elements:
            depth.append(
                {
                    "module": relative(root, path),
                    "lines": code,
                    "depth": round(code / elements, 1),
                }
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _forwards(
                node
            ):
                forwarding.append(f"{relative(root, path)}:{node.lineno} {node.name}")
    return sorted(depth, key=lambda row: row["depth"]), forwarding


def _forwards(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [
        s
        for s in node.body
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return | ast.Expr):
        return False
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    parameters = _parameters(node)
    if not isinstance(value, ast.Call) or not parameters:
        return False
    passed = [a.id if isinstance(a, ast.Name) else None for a in value.args]
    passed += [
        k.value.id if isinstance(k.value, ast.Name) else None for k in value.keywords
    ]
    return None not in passed and sorted(p for p in passed if p) == sorted(parameters)


def co_change(
    root: Path, limit: int = 20
) -> tuple[dict[str, float], list[tuple[str, str, float]]]:
    """Share of commits touching three or more packages, and the strongest file pairs."""
    log = subprocess.run(
        ["git", "log", "--name-only", "--format=@@%n", "--", "src/tychora"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    commits = [
        sorted({line for line in chunk.splitlines() if line.startswith("src/tychora/")})
        for chunk in log.split("@@")
        if chunk.strip()
    ]
    commits = [files for files in commits if files]
    touches: Counter[str] = Counter(f for files in commits for f in files)
    pairs: Counter[tuple[str, str]] = Counter()
    for files in commits:
        for index, first in enumerate(files):
            for second in files[index + 1 :]:
                if first.split("/")[2] != second.split("/")[2]:
                    pairs[(first, second)] += 1
    wide = sum(1 for files in commits if len({f.split("/")[2] for f in files}) >= 3)
    summary = {
        "commits": len(commits),
        "median_files": statistics.median(len(files) for files in commits)
        if commits
        else 0,
        "share_three_or_more_packages": round(wide / len(commits), 2) if commits else 0,
    }
    strongest = sorted(
        (
            (a, b, round(n / min(touches[a], touches[b]), 2))
            for (a, b), n in pairs.items()
            if n >= 3
        ),
        key=lambda row: -row[2],
    )[:limit]
    return summary, strongest


def maintainability(root: Path) -> list[dict[str, Any]]:
    """Files ranked C by radon's maintainability index."""
    result = run([venv_tool("radon"), "mi", "-j", "src/tychora"], root)
    data = json.loads(result.stdout)
    return sorted(
        (
            {"file": path, "mi": round(item["mi"], 1)}
            for path, item in data.items()
            if item.get("rank") == "C"
        ),
        key=lambda row: row["mi"],
    )


def collect(root: Path) -> dict[str, Any]:
    """Every report section as plain data, also written next to the Markdown."""
    complexity = complexity_from_json(list(complexity_items(root.resolve())))
    cycles = graph.strongly_connected(
        graph.import_graph(root, top_level_only=False, include_type_checking=False)
    )
    depth, forwarding = depth_and_pass_through(root)
    summary, pairs = co_change(root)
    return {
        "coupling": martin_metrics(root),
        "cycles": [len(component) for component in cycles],
        "complexity": sorted((complexity.per_key or {}).items(), key=lambda kv: -kv[1])[
            :20
        ],
        "complexity_over_15": complexity.value,
        "lcom4": lcom4(root),
        "maintainability_c": maintainability(root),
        "depth_median": statistics.median(row["depth"] for row in depth),
        "shallowest": [row for row in depth if row["lines"] >= 40][:10],
        "co_change": summary,
        "co_change_pairs": pairs,
        "pass_through": forwarding,
    }


def _delta(current: float, previous: float | None) -> str:
    if previous is None:
        return ""
    change = round(current - previous, 2)
    return f" ({change:+g})" if change else " (=)"


def render(data: dict[str, Any], previous: dict[str, Any] | None) -> str:
    """Markdown with a delta against the previous report's JSON where one exists."""
    old = previous or {}
    old_cycles = sum(old["cycles"]) if "cycles" in old else None
    old_rows = {row["component"]: row for row in old.get("coupling", [])}
    lines = [
        "# Architecture quality report",
        "",
        "## Coupling (Martin metrics)",
        "",
        "| Component | Modules | Ca | Ce | I | A | D |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in data["coupling"]:
        before = old_rows.get(row["component"], {})
        lines.append(
            f"| `{row['component']}` | {row['modules']} | {row['ca']}{_delta(row['ca'], before.get('ca'))} "
            f"| {row['ce']}{_delta(row['ce'], before.get('ce'))} | {row['i']} | {row['a']} | {row['d']} |"
        )
    lines += [
        "",
        "## Cycles (runtime imports)",
        "",
        (
            f"{len(data['cycles'])} strongly connected components, sizes {data['cycles']}, "
            f"{sum(data['cycles'])} modules{_delta(sum(data['cycles']), old_cycles)}."
        ),
        "",
        "## Cognitive complexity (top 20)",
        "",
        f"Functions above 15: {data['complexity_over_15']}{_delta(data['complexity_over_15'], old.get('complexity_over_15'))}.",
        "",
    ]
    lines += [f"- `{key}`: {value:g}" for key, value in data["complexity"]]
    lines += ["", "## Class cohesion (LCOM4 > 1)", ""]
    lines += [
        f"- `{row['class']}`: LCOM4 {row['lcom4']} over {row['methods']} methods"
        for row in data["lcom4"]
    ]
    lines += ["", "## Maintainability index rank C", ""]
    lines += [f"- `{row['file']}`: {row['mi']}" for row in data["maintainability_c"]]
    lines += [
        "",
        "## Module depth",
        "",
        (
            f"Median lines per interface element: {data['depth_median']}"
            f"{_delta(data['depth_median'], old.get('depth_median'))}."
        ),
        "",
    ]
    lines += [
        f"- `{row['module']}`: {row['depth']} ({row['lines']} lines)"
        for row in data["shallowest"]
    ]
    summary = data["co_change"]
    lines += [
        "",
        "## Change amplification",
        "",
        (
            f"{summary['commits']} commits; median {summary['median_files']} files; "
            f"{summary['share_three_or_more_packages']:.0%} touch three or more packages."
        ),
        "",
    ]
    lines += [
        f"- `{a}` with `{b}`: {share:.0%}" for a, b, share in data["co_change_pairs"]
    ]
    lines += [
        "",
        "## Pass-through functions",
        "",
        f"{len(data['pass_through'])} exact forwarding functions.",
        "",
    ]
    lines += [f"- `{item}`" for item in data["pass_through"]]
    return "\n".join(lines) + "\n"


def write_report(root: Path, output: Path, previous: Path | None) -> None:
    """Write OUTPUT (Markdown) and OUTPUT with a .json suffix for the next delta."""
    data = collect(root)
    earlier = (
        json.loads(previous.with_suffix(".json").read_text()) if previous else None
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(data, earlier))
    output.with_suffix(".json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )
