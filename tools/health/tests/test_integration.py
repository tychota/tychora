"""S1: a throwaway package with three injected regressions makes `check` fail on each."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.health.__main__ import main

REPO = Path(__file__).resolve().parents[3]
JSCPD = REPO / "dashboard" / "node_modules" / ".bin" / "jscpd"

PYPROJECT = """
[tool.importlinter]
root_package = "tychora"

[[tool.importlinter.contracts]]
id = "layers"
name = "Layers"
type = "layers"
layers = ["tychora.application", "tychora.contracts"]
"""

BLOCK = "".join(
    f"    total_{i} = sum(value * {i} for value in values if value > {i})\n"
    for i in range(12)
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    write(tmp_path, "pyproject.toml", PYPROJECT)
    write(tmp_path, ".jscpd.json", (REPO / ".jscpd.json").read_text())
    for package in ("", "contracts/", "application/"):
        write(tmp_path, f"src/tychora/{package}__init__.py", '"""Package."""\n')
    write(tmp_path, "src/tychora/contracts/ids.py", '"""Ids."""\n\nVALUE = 1\n')
    write(
        tmp_path,
        "src/tychora/application/service.py",
        '"""Service."""\n\nfrom tychora.contracts.ids import VALUE\n\n\n'
        f"def compute(values):\n{BLOCK}    return VALUE\n",
    )
    write(tmp_path, "dashboard/src/index.ts", "export const one = 1;\n")
    write(tmp_path, "dashboard/e2e/smoke.spec.ts", "export const two = 2;\n")
    write(tmp_path, "tests/test_smoke.py", "def test_smoke():\n    assert True\n")
    (tmp_path / "dashboard" / "node_modules" / ".bin").mkdir(parents=True)
    os.symlink(JSCPD, tmp_path / "dashboard" / "node_modules" / ".bin" / "jscpd")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


@pytest.mark.skipif(
    not JSCPD.exists(), reason="run `npm ci` in dashboard/ to install jscpd"
)
def test_three_injected_regressions_fail_with_named_gates(project, capsys):
    gates = "G5,G7,G9-G11"
    assert main(["update", "--initial", "--gates", gates, "--root", str(project)]) == 0
    assert main(["check", "--gates", gates, "--root", str(project)]) == 0
    capsys.readouterr()

    write(
        project,
        "src/tychora/contracts/long.py",
        '"""Long."""\n\n\ndef long():\n'
        + "".join(f"    x{i} = {i}\n" for i in range(88))
        + "    return 0\n",
    )
    write(
        project,
        "src/tychora/contracts/upward.py",
        '"""Upward."""\n\nfrom tychora.application.service import compute\n',
    )
    write(
        project,
        "src/tychora/contracts/copy.py",
        f'"""Copy."""\n\n\ndef again(values):\n{BLOCK}    return 0\n',
    )

    assert main(["check", "--gates", gates, "--root", str(project)]) == 1
    output = capsys.readouterr().out
    lines = output.splitlines()

    def status(gate: str, metric: str) -> str:
        (line,) = [
            line for line in lines if line.startswith(gate) and f" {metric} " in line
        ]
        return line.split()[-1]

    assert status("G5", "function_length") == "FAIL"
    assert "src/tychora/contracts/long.py::long: new entry with value 90" in output
    assert status("G7", "layering") == "FAIL"
    assert status("G9-G11", "new_clones_python") == "FAIL"
    assert status("G9-G11", "new_clones_typescript") == "ok"


def test_module_entry_point_prints_usage():
    result = subprocess.run(
        [sys.executable, "-m", "tools.health", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "check" in result.stdout and "update" in result.stdout
