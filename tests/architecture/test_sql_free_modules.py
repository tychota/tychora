"""Modules that must hold no raw SQL; later engine-core phases extend the list."""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
SQL_FREE_MODULES: tuple[str, ...] = (
    "ports/repositories.py",
    "application/workspace_rebuild.py",
    "evaluation/bench/budgets.py",
    "evaluation/bench/stages.py",
    "evaluation/bench/synth.py",
    "evaluation/bench/corpus.py",
    "evaluation/bench/harness.py",
    "interfaces/bench.py",
    "application/query.py",
    "application/reranking.py",
    "application/visual.py",
    "application/inspection.py",
    "retrieval/visuals.py",
    "application/owner.py",
    "application/removal.py",
    "application/restore.py",
)
SQL_CALLS = {"execute", "executemany", "executescript"}


def _violations(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SQL_CALLS
        ):
            found.append(f"{path.name}:{node.lineno} .{node.func.attr}(")
        if isinstance(node, ast.Import) and any(
            alias.name == "sqlite3" for alias in node.names
        ):
            found.append(f"{path.name}:{node.lineno} import sqlite3")
        if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            found.append(f"{path.name}:{node.lineno} from sqlite3")
    return found


def test_listed_modules_hold_no_sql():
    violations = []
    for relative in SQL_FREE_MODULES:
        path = SOURCE / relative
        assert path.is_file(), relative
        violations.extend(_violations(path))
    assert violations == []


def test_guard_detects_sql_calls_and_sqlite_imports(tmp_path):
    sample = tmp_path / "bad.py"
    sample.write_text(
        "import sqlite3\nfrom sqlite3 import Row\n\ndef f(c):\n    c.execute('x')\n"
    )
    assert len(_violations(sample)) == 3
