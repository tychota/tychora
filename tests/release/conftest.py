"""Build in a public-only staging tree and install into disposable clean venvs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).parents[2]
UV = Path(shutil.which("uv") or Path.home() / ".local/bin/uv")


class Artifacts(NamedTuple):
    wheel: Path
    sdist: Path
    source: Path
    isolation_proof: Path


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
        "UV_CACHE_DIR", "/private/tmp/tychora-release-uv"
    )
    return env


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory) -> Artifacts:
    root = tmp_path_factory.mktemp("artifact")
    source = root / "public-source"
    dist = root / "dist"
    private = root / "private-fixture"
    proof = root / "build-isolation.json"
    _run(
        [
            sys.executable,
            ROOT / "packaging/build_public_stage.py",
            "--repository",
            ROOT,
            "--source",
            source,
            "--dist",
            dist,
            "--proof",
            proof,
            "--private",
            private,
            "--uv",
            UV,
        ],
        cwd=root,
        env=_clean_env(),
    )
    assert json.loads(proof.read_text()) == {
        "checkout_denied": True,
        "git_common_dir_denied": True,
        "private_fixture_denied": True,
        "public_stage_readable": True,
        "build_under_same_profile": True,
    }
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    # Inspect both complete archives before any installation.
    _run(
        [sys.executable, source / "packaging/check_artifact.py", wheel, sdist],
        cwd=root,
        env=_clean_env(),
    )
    return Artifacts(wheel, sdist, source, proof)


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
