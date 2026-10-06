"""Exercise the published offline recipe and Python examples."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _bundle(tmp_path: Path) -> Path:
    supplied = os.environ.get("TYCHORA_TEST_TOKENIZER_ASSETS")
    if not supplied:
        pytest.skip("requires the explicitly provisioned output tokenizer fixture")
    source = Path(supplied) / "cl100k_base.tiktoken"
    if not source.is_file():
        pytest.skip("output tokenizer fixture is absent")
    bundle = tmp_path / "model-bundle" / "tiktoken" / "0.14.0"
    bundle.mkdir(parents=True)
    shutil.copyfile(source, bundle / source.name)
    return bundle.parents[1]


def _environment(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TYCHORA_MODEL_BUNDLE"] = str(_bundle(tmp_path))
    env["TYCHORA_MODEL_CACHE"] = str(tmp_path / "model-cache")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


def test_literal_readme_offline_recipe(tmp_path: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    match = re.search(
        r"<!-- quickstart:start -->\n```bash\n(.*?)\n```\n<!-- quickstart:end -->",
        readme,
        re.DOTALL,
    )
    assert match, "README must expose one literal shell recipe"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", match.group(1)],
        cwd=tmp_path,
        env=_environment(tmp_path),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout
    assert (tmp_path / "knowledge" / "workspace.toml").is_file()


def test_public_python_quickstart(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    workspace = tmp_path / "python-knowledge"
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "quickstart.py"), str(workspace)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout


def test_public_async_python_quickstart(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    workspace = tmp_path / "async-knowledge"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "quickstart.py"),
            str(workspace),
            "--async",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Record verification outcomes." in result.stdout


def test_generic_external_pack_installs_and_survives_reopen(tmp_path: Path) -> None:
    pack = tmp_path / "knowledge" / "profiles" / "workbench"
    shutil.copytree(ROOT / "examples" / "profiles" / "workbench", pack)
    workspace = tmp_path / "knowledge"
    settings = workspace / "workspace.toml"
    settings.write_text(
        'schema_version = 1\n[profiles.workbench]\npath = "profiles/workbench"\n'
        '[defaults]\nprofile = "workbench"\n'
    )
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    initialized = subprocess.run(
        [
            "tychora",
            "--workspace",
            str(workspace),
            "--offline",
            "init",
            "--provider",
            "configured",
            "--config",
            str(settings),
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    discovered = subprocess.run(
        [
            "tychora",
            "--workspace",
            str(workspace),
            "--offline",
            "inspect",
            "relations",
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert discovered.returncode == 0, discovered.stderr
    assert "clarifies@1" in discovered.stdout


def test_public_relative_markdown_links_resolve() -> None:
    for page in (
        ROOT / "README.md",
        *(ROOT / "docs").glob("*.md"),
        ROOT / "examples" / "profiles" / "workbench" / "README.md",
    ):
        text = page.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#")):
                continue
            relative = target.split("#", 1)[0]
            assert (page.parent / relative).exists(), f"{page}: broken link {target}"
