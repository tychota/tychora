"""AST and text measurements: function length, raw SQL, integrity raises, docstrings, plan references."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from tools.health.model import Metric

SOURCE = Path("src/tychora")
FUNCTION_LINE_LIMIT = 80
SQL_METHODS = frozenset({"execute", "executemany", "executescript"})
PLAN_REFERENCE = re.compile(r"\b[Tt]ask ?[0-9]+\b")
PLAN_TEXT_ROOTS = frozenset({"src", "tests", "dashboard"})
PLAN_SKIPPED = ("docs/superpowers/", "dashboard/package-lock.json")
PUBLIC_API = ("src/tychora/api.py", "src/tychora/clients")

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def python_files(root: Path, base: Path = SOURCE) -> list[Path]:
    """Every Python file under base, in a stable order.

    A missing directory or an empty tree raises, so a wrong root cannot make a
    gate report a perfect zero.
    """
    if not (root / base).is_dir():
        raise FileNotFoundError(f"{root / base} is not a directory")
    files = sorted(
        path for path in (root / base).rglob("*.py") if "__pycache__" not in path.parts
    )
    if not files:
        raise FileNotFoundError(f"no Python files under {root / base}")
    return files


def relative(root: Path, path: Path) -> str:
    """POSIX path of path relative to root, used as a per-key name."""
    return path.relative_to(root).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def functions(tree: ast.AST) -> Iterator[tuple[str, FunctionNode]]:
    """Yield (qualified name, node) for every function, nested ones included."""

    def visit(node: ast.AST, prefix: str) -> Iterator[tuple[str, FunctionNode]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = prefix + child.name
                yield name, child
                yield from visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    seen: dict[str, int] = {}
    for name, node in visit(tree, ""):
        seen[name] = seen.get(name, 0) + 1
        yield (name if seen[name] == 1 else f"{name}#{seen[name]}"), node


def function_length(root: Path) -> list[Metric]:
    """G5: functions longer than 80 physical lines, keyed by path::qualified name."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        for name, node in functions(_parse(path)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > FUNCTION_LINE_LIMIT:
                per_key[f"{relative(root, path)}::{name}"] = length
    return [Metric("function_length", len(per_key), per_key=per_key)]


def raw_sql(root: Path) -> list[Metric]:
    """G8: .execute/.executemany/.executescript calls outside tychora/adapters."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        rel = relative(root, path)
        if rel.startswith("src/tychora/adapters/"):
            continue
        count = sum(
            1
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_METHODS
        )
        if count:
            per_key[rel] = count
    return [Metric("raw_sql_outside_adapters", sum(per_key.values()), per_key=per_key)]


def _called_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def integrity_raises(root: Path) -> list[Metric]:
    """G24: TychoraError("INTEGRITY_ERROR", ...) constructions without reason=."""
    per_key: dict[str, float] = {}
    for path in python_files(root):
        count = 0
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and _called_name(node.func) == "TychoraError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "INTEGRITY_ERROR"
                and not any(keyword.arg == "reason" for keyword in node.keywords)
            ):
                count += 1
        if count:
            per_key[relative(root, path)] = count
    total = sum(per_key.values())
    print(f"[G24] reason-less integrity raises by package: {by_package(per_key)}")
    return [Metric("integrity_raises_without_reason", total, per_key=per_key)]


def by_package(per_key: dict[str, float]) -> dict[str, float]:
    """Sum per-file counts by top-level tychora package, for the report."""
    totals: dict[str, float] = {}
    for key, value in per_key.items():
        parts = key.split("/")
        package = parts[2].removesuffix(".py") if len(parts) > 2 else key
        totals[package] = totals.get(package, 0) + value
    return dict(sorted(totals.items()))


def tracked_files(root: Path) -> list[str]:
    """Files Git tracks, so ignored build output never counts."""
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout
    return [name for name in output.decode().split("\0") if name]


def plan_references(root: Path, files: list[str] | None = None) -> list[Metric]:
    """G15: plan-task references in source, test and dashboard text and in file names."""
    per_key: dict[str, float] = {}
    for rel in tracked_files(root) if files is None else files:
        if rel.startswith(PLAN_SKIPPED):
            continue
        hits = 1 if PLAN_REFERENCE.search(Path(rel).name) else 0
        if rel.split("/", 1)[0] in PLAN_TEXT_ROOTS and (root / rel).is_file():
            try:
                text = (root / rel).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = ""
            hits += sum(1 for line in text.splitlines() if PLAN_REFERENCE.search(line))
        if hits:
            per_key[rel] = hits
    return [
        Metric("plan_references", sum(per_key.values()), kind="hard", per_key=per_key)
    ]


def _public_definitions(
    node: ast.AST, prefix: str = ""
) -> Iterator[tuple[str, ast.ClassDef | FunctionNode]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield prefix + child.name, child
            yield from _public_definitions(child, f"{prefix}{child.name}.")
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield prefix + child.name, child


def public_api_docstrings(root: Path) -> list[Metric]:
    """G14 (hard part): every public class, function and method of api.py and clients/.

    Methods of underscore-named resource classes count too: `kb.sources.add`
    is public even though its class is `_Sources`.
    """
    clients = root / PUBLIC_API[1]
    if not clients.is_dir():
        raise FileNotFoundError(f"{clients} is not a directory")
    paths = [root / PUBLIC_API[0], *sorted(clients.glob("*.py"))]
    missing: dict[str, float] = {}
    for path in paths:
        for name, node in _public_definitions(_parse(path)):
            if node.name.startswith("_"):
                continue
            if ast.get_docstring(node) is None:
                missing[f"{relative(root, path)}::{name}"] = 1
    return [Metric("public_api_docstrings", len(missing), kind="hard", per_key=missing)]


def quarantined_tests(root: Path) -> list[Metric]:
    """G23: tests carrying @pytest.mark.quarantine; the count may only fall."""
    per_key: dict[str, float] = {}
    for path in python_files(root, Path("tests")):
        count = sum(
            1
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Attribute)
            and node.attr == "quarantine"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        )
        if count:
            per_key[relative(root, path)] = count
    return [Metric("quarantined_tests", sum(per_key.values()), per_key=per_key)]
