"""Release inventory binds the actual artifacts to the locked installed stacks."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest


def _manifest_module(monkeypatch):
    scripts = Path(__file__).parents[2] / "packaging"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "release_manifest_builder", scripts / "build_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_provenance_rejects_arbitrary_commit_and_records_dirty_inputs(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "source"
    (repo / "src/tychora").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='tychora'\nversion='0.0.0'\n")
    source = repo / "src/tychora/api.py"
    source.write_text("VERSION = 1\n")

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic")
    git("add", ".")
    git("commit", "-qm", "base")
    commit = git("rev-parse", "HEAD")

    def archive() -> Path:
        path = tmp_path / "tychora-0.0.0.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for name in ("pyproject.toml", "src/tychora/api.py"):
                data = (repo / name).read_bytes()
                member = tarfile.TarInfo("tychora-0.0.0/" + name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        return path

    builder = _manifest_module(monkeypatch)
    clean = builder.source_provenance(
        repo, repo, commit, builder.check_artifact(archive())
    )
    assert clean["dirty"] is False
    with pytest.raises(ValueError, match="commit"):
        builder.source_provenance(
            repo, repo, "a" * 40, builder.check_artifact(archive())
        )
    source.write_text("VERSION = 2\n")
    dirty = builder.source_provenance(
        repo, repo, commit, builder.check_artifact(archive())
    )
    assert dirty["dirty"] is True
    assert dirty["tree_sha256"] != clean["tree_sha256"]


def test_manifest_generator_is_present():
    assert (Path(__file__).parents[2] / "packaging/build_manifest.py").is_file()


def test_manifest_records_artifacts_features_and_resolved_licenses(
    artifacts, installed_base, installed_full, tmp_path: Path
):
    output = tmp_path / "manifest.json"
    script = artifacts.source / "packaging/build_manifest.py"
    assert script.is_file(), "release manifest generator is missing"
    result = subprocess.run(
        [
            str(installed_base.python),
            str(script),
            "--wheel",
            str(artifacts.wheel),
            "--sdist",
            str(artifacts.sdist),
            "--lock",
            str(artifacts.source / "uv.lock"),
            "--project",
            str(artifacts.source / "pyproject.toml"),
            "--base-python",
            str(installed_base.python),
            "--full-python",
            str(installed_full.python),
            "--commit",
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=Path(__file__).parents[2], text=True
            ).strip(),
            "--repository",
            str(Path(__file__).parents[2]),
            "--source-root",
            str(artifacts.source),
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        env=installed_base.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert manifest["package"]["version"] == "0.1.0a1"
    assert "documents" in manifest["features"]
    assert "full" in manifest["features"]
    assert len(manifest["lock"]["sha256"]) == 64
    assert len(manifest["artifacts"]["wheel"]["sha256"]) == 64
    assert manifest["source"]["commit"] == manifest["commit"]
    assert len(manifest["source"]["tree_sha256"]) == 64
    assert (
        "tychora/resources/migrations/0001.sql"
        in manifest["artifacts"]["wheel"]["members"]
    )
    assert any(row["name"] == "pydantic" for row in manifest["resolved"]["base"])
    assert any(row["name"] == "docling" for row in manifest["resolved"]["full"])
    assert all("license" in row for row in manifest["resolved"]["full"])
    chromadb = next(
        row for row in manifest["resolved"]["full"] if row["name"] == "chromadb"
    )
    assert chromadb["license_files"], (
        "installed third-party notices must be inventoried"
    )
