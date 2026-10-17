"""The epoch, watermark and artifact-row reads each have one implementation."""

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
OWNER = "adapters/sqlite/repositories/workspace.py"
PATTERNS = {
    re.compile(r"MAX\(decision_id\),0\)"): [OWNER],
    re.compile(r"SELECT policy_epoch(,lexical_statistics_epoch)? FROM workspaces"): [],
}


def test_epoch_and_watermark_statements_live_in_the_repository():
    for pattern, expected in PATTERNS.items():
        sites = sorted(
            str(path.relative_to(SOURCE))
            for path in SOURCE.rglob("*.py")
            if pattern.search(path.read_text())
        )
        assert sites == expected, (pattern.pattern, sites)


def _row_constructions(path: Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "ArtifactRef"
            and {kw.arg for kw in node.keywords}
            == {"sha256", "size_bytes", "media_type", "storage_key"}
            and any(
                isinstance(kw.value, ast.Subscript)
                and isinstance(kw.value.slice, ast.Constant)
                and isinstance(kw.value.slice.value, int)
                for kw in node.keywords
            )
        ):
            lines.append(node.lineno)
    return lines


def test_no_field_by_field_artifact_row_construction():
    offenders = {
        str(path.relative_to(SOURCE)): lines
        for path in SOURCE.rglob("*.py")
        if (lines := _row_constructions(path))
    }
    assert offenders == {}
