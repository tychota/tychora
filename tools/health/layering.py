"""G7: import-linter contracts and the size of their exception lists."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tools.health.external import run, venv_tool
from tools.health.model import Metric


def exception_counts(pyproject: dict) -> dict[str, float]:
    """Number of ignore_imports entries per contract id."""
    contracts = pyproject.get("tool", {}).get("importlinter", {}).get("contracts", [])
    return {
        contract["id"]: float(len(contract.get("ignore_imports", [])))
        for contract in contracts
    }


def layering(root: Path) -> list[Metric]:
    """Broken contracts must be zero; exceptions may be removed, never added."""
    result = run(
        [venv_tool("lint-imports"), "--no-cache"],
        root,
        env={"PYTHONPATH": str(root / "src")},
    )
    if result.returncode != 0:
        print(result.stdout + result.stderr)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    per_key = exception_counts(pyproject)
    return [
        Metric("layering", 0 if result.returncode == 0 else 1, kind="hard"),
        Metric("layering_exceptions", sum(per_key.values()), per_key=per_key),
    ]
