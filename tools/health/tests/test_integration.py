"""S1: a throwaway package with three injected regressions makes `check` fail on each."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.health.__main__ import main
from tools.health.gates import Gate
from tools.health.model import Metric

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


def require_jscpd() -> None:
    """Skip locally when jscpd is not installed; fail under CI where it must be."""
    if JSCPD.exists():
        return
    message = "jscpd is missing: run `npm ci` in dashboard/"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def test_three_injected_regressions_fail_with_named_gates(project, capsys):
    require_jscpd()
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
    assert "1 finding(s); this gate allows none" in output
    assert status("G9-G11", "new_clones_python") == "FAIL"
    assert output.count("1 finding(s); this gate allows none") == 2
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


def test_initial_update_refuses_when_a_baseline_exists(project, capsys):
    require_jscpd()
    gates = "G9-G11"
    assert main(["update", "--initial", "--gates", gates, "--root", str(project)]) == 0
    allowlists = {
        path: path.read_text()
        for path in (project / "quality" / "jscpd").glob("*.json")
    }
    assert allowlists
    write(
        project,
        "src/tychora/contracts/copy.py",
        f'"""Copy."""\n\n\ndef again(values):\n{BLOCK}    return 0\n',
    )
    capsys.readouterr()

    assert main(["update", "--initial", "--gates", gates, "--root", str(project)]) == 1
    assert "update refused" in capsys.readouterr().out
    assert {
        path: path.read_text()
        for path in (project / "quality" / "jscpd").glob("*.json")
    } == allowlists


def test_a_failing_gate_prints_error_and_others_still_report(
    tmp_path, capsys, monkeypatch
):
    def broken(root):
        raise RuntimeError("tool crashed")

    gates = [
        Gate("G90", "Broken", "pr", broken),
        Gate("G91", "Fine", "pr", lambda root: [Metric("fine_metric", 0.0)]),
    ]
    monkeypatch.setattr("tools.health.__main__.select", lambda *a, **k: gates)
    assert main(["check", "--root", str(tmp_path)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert any(l.startswith("G90") and l.endswith("ERROR") for l in lines)
    assert "        - RuntimeError: tool crashed" in lines
    assert any(l.startswith("G91") and "fine_metric" in l for l in lines)
