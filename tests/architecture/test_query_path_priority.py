"""Query-path writer tasks name their lane; the default lane is background.

``WorkspaceOwner.run_write`` and ``run_write_sync`` default to the background
lane, where a query would wait behind ingestion. Every call from a module on
the query path (and from discovery, whose reads are user-facing) must pass a
priority explicitly, so a new call cannot fall back to background silently.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
QUERY_PATH_MODULES: tuple[str, ...] = (
    "application/query.py",
    "application/reranking.py",
    "application/visual.py",
    "application/discovery.py",
    "adapters/voyage/query.py",
    *sorted(
        str(path.relative_to(SOURCE)) for path in (SOURCE / "retrieval").glob("*.py")
    ),
)
WRITER_CALLS = {"run_write", "run_write_sync"}


def _violations(path: Path) -> list[str]:
    """Writer calls that pass neither a third argument nor ``priority=``."""
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITER_CALLS
            and len(node.args) < 3
            and not any(keyword.arg == "priority" for keyword in node.keywords)
        ):
            found.append(f"{path.name}:{node.lineno} .{node.func.attr}(")
    return found


def test_query_path_writer_calls_name_their_priority():
    violations = []
    for relative in QUERY_PATH_MODULES:
        path = SOURCE / relative
        assert path.is_file(), relative
        violations.extend(_violations(path))
    assert violations == []


def test_guard_detects_a_call_at_the_default_priority(tmp_path):
    sample = tmp_path / "bad.py"
    sample.write_text(
        "async def f(owner, call, ctx):\n"
        "    await owner.run_write(call, ctx)\n"
        "    owner.run_write_sync(call, ctx)\n"
        "    await owner.run_write(call, ctx, 'interactive')\n"
        "    owner.run_write_sync(call, ctx, priority='interactive')\n"
    )
    assert len(_violations(sample)) == 2
