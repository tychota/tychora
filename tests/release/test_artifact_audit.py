"""Distribution boundaries, including objects that a later commit deleted."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path
from stat import S_IFLNK

import pytest


def _auditor():
    path = Path(__file__).parents[2] / "packaging/check_artifact.py"
    assert path.is_file(), "release artifact auditor is missing"
    spec = importlib.util.spec_from_file_location("release_artifact_auditor", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return path


def test_release_build_proves_private_paths_denied(artifacts):
    proof = json.loads(artifacts.isolation_proof.read_text())
    assert proof == {
        "checkout_denied": True,
        "git_common_dir_denied": True,
        "private_fixture_denied": True,
        "public_stage_readable": True,
        "build_under_same_profile": True,
    }
    assert artifacts.wheel.is_file() and artifacts.sdist.is_file()


def test_hosted_deliverable_uses_same_public_stage_boundary():
    workflow = (Path(__file__).parents[2] / ".github/workflows/build.yml").read_text()
    assert "python packaging/build_public_stage.py" in workflow
    assert '--source-root "$RUNNER_TEMP/tychora-public-stage"' in workflow
    assert "uv build --out-dir dist" not in workflow


def test_wheel_rejects_traversal_even_when_member_looks_like_source(tmp_path: Path):
    wheel = _wheel(
        tmp_path / "tychora-0.1.0a1-py3-none-any.whl",
        {"../src/tychora/api.py": b"pass"},
    )
    with pytest.raises(ValueError, match="Unexpected distribution member"):
        _auditor().check_artifact(wheel)


def test_wheel_rejects_sensitive_member_and_content_without_echoing_value(
    tmp_path: Path,
):
    path = tmp_path / "tychora-0.1.0a1-py3-none-any.whl"
    _wheel(path, {"tychora/.env": b"SECRET_VALUE=must-not-echo"})
    with pytest.raises(ValueError) as failure:
        _auditor().check_artifact(path)
    assert "must-not-echo" not in str(failure.value)
    private_path = b"/" + b"Users/" + b"privateperson/Documents/Code"
    _wheel(path, {"tychora/README.md": private_path})
    with pytest.raises(ValueError) as failure:
        _auditor().check_artifact(path)
    assert "privateperson" not in str(failure.value)


def test_sdist_rejects_symlinks_and_private_reports(tmp_path: Path):
    path = tmp_path / "tychora-0.1.0a1.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("tychora-0.1.0a1/src/tychora/api.py")
        info.type = tarfile.SYMTYPE
        info.linkname = "/private/tmp/secret"
        archive.addfile(info)
    with pytest.raises(ValueError, match="Unexpected distribution member"):
        _auditor().check_artifact(path)
    with tarfile.open(path, "w:gz") as archive:
        data = b"synthetic"
        info = tarfile.TarInfo("tychora-0.1.0a1/.superpowers/sdd/report.md")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="Unexpected distribution member"):
        _auditor().check_artifact(path)


def test_wheel_rejects_symlink_member(tmp_path: Path):
    path = tmp_path / "tychora-0.1.0a1-py3-none-any.whl"
    link = zipfile.ZipInfo("tychora/resources/linked.txt")
    link.create_system = 3
    link.external_attr = (S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(link, "/private/tmp/local")
    with pytest.raises(ValueError, match="Unexpected distribution member"):
        _auditor().check_artifact(path)


def test_history_audit_detects_ignored_report_even_after_removal(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic")
    (repo / ".gitignore").write_text("reports/\n")
    git("add", ".gitignore")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "reports").mkdir()
    (repo / "reports/local.md").write_text("synthetic private report")
    git("add", "-f", "reports/local.md")
    git("commit", "-qm", "mistake")
    git("rm", "reports/local.md")
    git("commit", "-qm", "delete")
    with pytest.raises(ValueError, match="reports/local.md"):
        _auditor().audit_outgoing_history(repo, base)


def test_history_audit_checks_private_path_when_blob_already_exists_at_base(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Synthetic")
    (repo / ".gitignore").write_text("reports/\n")
    (repo / "public.txt").write_text("shared synthetic bytes")
    git("add", ".gitignore", "public.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "reports").mkdir()
    (repo / "reports/local.md").write_bytes((repo / "public.txt").read_bytes())
    git("add", "-f", "reports/local.md")
    git("commit", "-qm", "private path reuses public blob")
    git("rm", "reports/local.md")
    git("commit", "-qm", "remove private path")
    with pytest.raises(ValueError, match="reports/local.md"):
        _auditor().audit_outgoing_history(repo, base)
