"""Build in a public-only staging tree and install into disposable clean venvs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).parents[2]
UV = Path(shutil.which("uv") or Path.home() / ".local/bin/uv")
_SOURCE_DIRS = ("src/tychora/", "tests/", "packaging/", "docs/", "examples/")
_SOURCE_FILES = {".gitignore", "README.md", "LICENSE", "pyproject.toml", "uv.lock"}


class Artifacts(NamedTuple):
    wheel: Path
    sdist: Path
    source: Path


class Installation(NamedTuple):
    python: Path
    working_dir: Path
    env: dict[str, str]


def _run(args: list[str | Path], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Command failed: {' '.join(str(arg) for arg in args)}\n"
        f"stdout: {completed.stdout[-3000:]}\nstderr: {completed.stderr[-3000:]}"
    )
    return completed.stdout


def _clean_env() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "UV_CACHE_DIR",
        "TYCHORA_TEST_MODEL_CACHE",
        "TYCHORA_TEST_DOCLING_MODELS",
        "TYCHORA_TEST_OCR_MODELS",
        "TYCHORA_TEST_TOKENIZER_ASSETS",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["PYTHONNOUSERSITE"] = "1"
    env["UV_CACHE_DIR"] = os.environ.get(
        "UV_CACHE_DIR", "/private/tmp/tychora-task45-uv"
    )
    return env


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory) -> Artifacts:
    root = tmp_path_factory.mktemp("artifact")
    source = root / "public-source"
    source.mkdir()
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in paths:
        if name not in _SOURCE_FILES and not name.startswith(_SOURCE_DIRS):
            continue
        original = ROOT / name
        if not original.is_file() or original.is_symlink():
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    assert not (source / ".superpowers").exists()
    assert not (source / "reports").exists()
    dist = root / "dist"
    _run([UV, "build", "--out-dir", dist], cwd=source, env=_clean_env())
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    # Inspect both complete archives before any installation.
    _run(
        [sys.executable, source / "packaging/check_artifact.py", wheel, sdist],
        cwd=root,
        env=_clean_env(),
    )
    return Artifacts(wheel, sdist, source)


def _install(artifacts: Artifacts, root: Path, *, full: bool) -> Installation:
    env = _clean_env()
    venv = root / "venv"
    _run([UV, "venv", venv, "--python", "3.12"], cwd=root, env=env)
    python = venv / "bin/python"
    requirements = root / "requirements.txt"
    export = [UV, "export", "--locked", "--no-default-groups", "--no-emit-project"]
    if full:
        export.extend(("--all-extras", "--group", "dev", "--group", "test-fixtures"))
    requirements.write_text(_run(export, cwd=artifacts.source, env=env))
    install = [
        UV,
        "pip",
        "install",
        "--python",
        python,
        "--require-hashes",
        "-r",
        requirements,
    ]
    if not full:
        install.append("--no-build")
    _run(install, cwd=root, env=env)
    _run(
        [UV, "pip", "install", "--python", python, "--no-deps", artifacts.wheel],
        cwd=root,
        env=env,
    )
    work = root / "work"
    work.mkdir()
    return Installation(python, work, env)


@pytest.fixture(scope="session")
def installed_base(artifacts: Artifacts, tmp_path_factory) -> Installation:
    return _install(artifacts, tmp_path_factory.mktemp("base"), full=False)


@pytest.fixture(scope="session")
def installed_full(artifacts: Artifacts, tmp_path_factory) -> Installation:
    return _install(artifacts, tmp_path_factory.mktemp("full"), full=True)
