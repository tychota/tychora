"""Exercise built artifacts using interpreters whose cwd is outside the source tree."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.helpers.timing import scaled


def _python(installation, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(installation.python), "-c", code],
        cwd=installation.working_dir,
        env=installation.env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.release
def test_minimal_installed_wheel_import_help_lexical_and_collision(installed_base):
    code = """import importlib.util, sqlite3, sys
from pathlib import Path
import tychora
assert all(importlib.util.find_spec(name) is None for name in ("chromadb", "docling", "fastapi", "mcp", "voyageai", "anthropic", "PIL"))
assert "chromadb" not in sys.modules
assert "docling" not in sys.modules
assert "fastapi" not in sys.modules
from tychora import Tychora, TychoraError
class Counter:
    model = "synthetic-release-counter"
    tokenizer_id = "cl100k_base"
    def count(self, text): return len(text)
workspace = Path("knowledge")
with Tychora.open(workspace, token_counter=Counter()) as kb:
    job = kb.sources.add({"type":"text", "source_key":"release-manual", "collection_id":"default", "kind":"manual@1", "format":"text", "text":"Synthetic release evidence.", "metadata":{"title":"Release", "publisher":"Example", "edition":"1"}})
    assert kb.jobs.wait(job.id, timeout=WAIT_SECONDS).state == "succeeded"
    assert kb.search({"question":"release", "modalities":["lexical"]}).segments
db = workspace / "corpus.sqlite3"
with sqlite3.connect(db) as conn:
    conn.execute("UPDATE collections SET collection_id=? WHERE slug='default'", ("00000000-0000-4000-8000-000000000099",))
try:
    Tychora.open(workspace, token_counter=Counter())
except TychoraError as error:
    assert error.code == "PROFILE_INCOMPATIBLE", error.code
else:
    raise AssertionError("default collection collision was accepted")
with sqlite3.connect(db) as conn:
    assert conn.execute("SELECT collection_id FROM collections WHERE slug='default'").fetchone()[0] == "00000000-0000-4000-8000-000000000099"
"""
    # The subprocess runs the installed wheel without the test package.
    code = code.replace("WAIT_SECONDS", str(scaled(10)))
    result = _python(installed_base, code)
    assert result.returncode == 0, result.stderr
    help_result = subprocess.run(
        [str(installed_base.python), "-m", "tychora", "--help"],
        cwd=installed_base.working_dir,
        env=installed_base.env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "Usage" in help_result.stdout


@pytest.mark.release
def test_full_installed_wheel_native_sdk_document_http_mcp_and_restore(
    installed_full, artifacts
):
    root = installed_full.working_dir
    source_tests = artifacts.source / "tests"
    target_tests = root / "tests"
    (target_tests / "qualification").mkdir(parents=True)
    (target_tests / "helpers").mkdir(parents=True)
    (target_tests / "cli").mkdir(parents=True)
    (target_tests / "fixtures").mkdir(parents=True)
    (target_tests / "configuration").mkdir(parents=True)
    for name in (
        "test_native_stack.py",
        "graph_probe.py",
        "test_sdk_surface.py",
        "test_docling.py",
    ):
        shutil.copy2(
            source_tests / "qualification" / name, target_tests / "qualification" / name
        )
    shutil.copy2(source_tests / "__init__.py", target_tests / "__init__.py")
    for name in ("__init__.py", "ladybug.py", "timing.py"):
        shutil.copy2(source_tests / "helpers" / name, target_tests / "helpers" / name)
    shutil.copy2(
        source_tests / "cli/test_commands.py", target_tests / "cli/test_commands.py"
    )
    shutil.copytree(
        source_tests / "fixtures/documents", target_tests / "fixtures/documents"
    )
    shutil.copytree(
        source_tests / "configuration/fixtures", target_tests / "configuration/fixtures"
    )
    env = installed_full.env.copy()
    model_cache = Path(
        env.get(
            "TYCHORA_TEST_MODEL_CACHE",
            Path.home() / "Library/Caches/tychora-development/models",
        )
    )
    assert model_cache.is_dir(), (
        "Set TYCHORA_TEST_MODEL_CACHE to the verified public model bundle"
    )
    env["TYCHORA_TEST_DOCLING_MODELS"] = str(model_cache)
    env["TYCHORA_TEST_MODEL_CACHE"] = str(model_cache)
    env["TYCHORA_TEST_OCR_MODELS"] = str(model_cache / "rapidocr/3.9.2")
    env["TYCHORA_TEST_TOKENIZER_ASSETS"] = str(
        model_cache.parent / "tokenizer-test-assets"
    )
    result = subprocess.run(
        [
            str(installed_full.python),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--confcutdir",
            str(root),
            str(target_tests / "qualification/test_native_stack.py"),
            str(target_tests / "qualification/test_sdk_surface.py"),
            str(
                target_tests
                / "qualification/test_docling.py::test_real_docling_retains_document_and_locators"
            ),
            str(
                target_tests
                / "cli/test_commands.py::test_manage_import_bootstraps_without_original_or_control_workspace"
            ),
            str(
                target_tests
                / "cli/test_commands.py::test_setup_http_uses_same_verified_job_and_scope"
            ),
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.release
def test_sdist_derived_wheel_import_and_resources(artifacts, tmp_path):
    from tests.release.conftest import UV, _clean_env, _run

    with tarfile.open(artifacts.sdist, "r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    extracted = next(tmp_path.glob("tychora-*"))
    dist = tmp_path / "rebuilt"
    _run([UV, "build", "--wheel", "--out-dir", dist], cwd=extracted, env=_clean_env())
    wheel = next(dist.glob("*.whl"))
    _run(
        [sys.executable, artifacts.source / "packaging/check_artifact.py", wheel],
        cwd=tmp_path,
        env=_clean_env(),
    )
    venv = tmp_path / "venv"
    _run([UV, "venv", venv, "--python", "3.12"], cwd=tmp_path, env=_clean_env())
    python = venv / "bin/python"
    _run(
        [UV, "pip", "install", "--python", python, "--no-deps", wheel],
        cwd=tmp_path,
        env=_clean_env(),
    )
    result = subprocess.run(
        [
            str(python),
            "-c",
            'import tychora; from importlib.resources import files; assert files("tychora").joinpath("resources/skills/query/SKILL.md").is_file()',
        ],
        cwd=tmp_path,
        env=_clean_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
