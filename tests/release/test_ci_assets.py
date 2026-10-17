"""Hosted setup verifies every selected public asset before exposing it to tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def _provisioner():
    path = Path(__file__).parents[2] / "packaging/provision_ci_assets.py"
    spec = importlib.util.spec_from_file_location("provision_ci_assets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_asset_verification_rejects_corrupt_bytes(tmp_path: Path):
    cache = tmp_path / "cache"
    asset = cache / "tiktoken/0.14.0/cl100k_base.tiktoken"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"pinned public test bytes")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": "cl100k-base-ranks",
                        "selection": "output-tokenizer",
                        "path": "tiktoken/0.14.0/cl100k_base.tiktoken",
                        "size_bytes": asset.stat().st_size,
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    }
                ]
            }
        )
    )
    assert _provisioner().verify_selected(cache, manifest) == {
        "cl100k-base-ranks": asset
    }
    asset.write_bytes(b"tampered public test bytes")
    with pytest.raises(ValueError, match="checksum mismatch"):
        _provisioner().verify_selected(cache, manifest)


def test_ci_provisions_selected_standard_reranker_manifest():
    assert "rerank-2.5" in _provisioner().SELECTIONS


def _setup_result(code: str, retryable: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["uv", "run"],
        1,
        stdout="",
        stderr=json.dumps(
            {
                "schema_version": "1",
                "code": code,
                "message": "Synthetic setup failure",
                "retryable": retryable,
            }
        )
        + "\n",
    )


def test_ci_setup_retries_explicit_transient_failure_then_succeeds(monkeypatch):
    provisioner = _provisioner()
    results = iter(
        (
            _setup_result("PROVIDER_FAILURE", True),
            subprocess.CompletedProcess(["uv", "run"], 0, "{}\n", ""),
        )
    )
    calls = []
    delays = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return next(results)

    monkeypatch.setattr(provisioner.subprocess, "run", run)
    monkeypatch.setattr("time.sleep", delays.append)
    provisioner.run_setup(["uv", "run"], {"TYCHORA_MODEL_CACHE": "cache"})

    assert len(calls) == 2
    assert all(call[1]["capture_output"] and call[1]["text"] for call in calls)
    assert len(delays) == 1 and 0 < delays[0] <= 10


@pytest.mark.parametrize(
    "failure",
    [
        _setup_result("INTEGRITY_ERROR", True),
        _setup_result("INVALID_REQUEST", True),
        _setup_result("PROVIDER_FAILURE", False),
        subprocess.CompletedProcess(["uv", "run"], 1, "", "unstructured failure"),
    ],
)
def test_ci_setup_does_not_retry_integrity_or_permanent_failures(monkeypatch, failure):
    provisioner = _provisioner()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return failure

    monkeypatch.setattr(provisioner.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        provisioner.run_setup(["uv", "run"], {})
    assert len(calls) == 1


def test_ci_setup_stops_after_bounded_transient_failures(monkeypatch):
    provisioner = _provisioner()
    calls = []
    delays = []

    def run(command, **kwargs):
        calls.append(command)
        return _setup_result("PROVIDER_FAILURE", True)

    monkeypatch.setattr(provisioner.subprocess, "run", run)
    monkeypatch.setattr("time.sleep", delays.append)
    with pytest.raises(subprocess.CalledProcessError):
        provisioner.run_setup(["uv", "run"], {})
    assert len(calls) == 5
    assert delays == [5, 10, 20, 40]


def test_code_corpus_export_is_pinned_by_git_tree(tmp_path: Path):
    module = _provisioner()
    repo = tmp_path / "repo"
    (repo / "src/tychora").mkdir(parents=True)
    (repo / "src/tychora/a.py").write_text("VALUE = 1\n")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")
    commit, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD:src/tychora")
    exported = module.provision_code_corpus(
        repo, tmp_path / "corpus", commit=commit, tree=tree
    )
    assert (exported / "src/tychora/a.py").read_text() == "VALUE = 1\n"
    with pytest.raises(SystemExit, match="differs from the pinned tree"):
        module.provision_code_corpus(
            repo, tmp_path / "other", commit=commit, tree="0" * 40
        )
