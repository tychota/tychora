import os
import subprocess
from pathlib import Path

import pytest

from tests.code.conftest import git, make_capture, make_repo
from tychora.adapters.code.capture import RepositoryCapture
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def capture(tmp_path):
    return make_capture(tmp_path)


def test_whole_capture_never_runs_git_helpers(repo, capture, monkeypatch):
    markers = {}

    def helper(name):
        marker = repo.parent / f"{name}-ran"
        executable = repo.parent / f"record-{name}"
        executable.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\nexit 1\n")
        executable.chmod(0o755)
        markers[name] = marker
        return executable

    filter_helper = helper("filter")
    process_helper = helper("process")
    fsmonitor_helper = helper("fsmonitor")
    hook_helper = helper("hook")
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    (hooks / "post-index-change").write_text(hook_helper.read_text())
    (hooks / "post-index-change").chmod(0o755)
    (repo / ".gitattributes").write_text("*.py filter=guard\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-qm", "add attributes fixture")
    git(repo, "config", "filter.guard.clean", str(filter_helper))
    git(repo, "config", "filter.guard.smudge", str(filter_helper))
    git(repo, "config", "filter.guard.process", str(process_helper))
    git(repo, "config", "core.fsmonitor", str(fsmonitor_helper))
    git(repo, "config", "core.hooksPath", str(hooks))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(filter_helper))
    monkeypatch.setenv("GIT_DIR", str(repo.parent / "wrong-git-dir"))

    result = capture(repo)
    assert result.commit is not None
    assert all(marker.exists() is False for marker in markers.values())


def test_missing_promised_tree_never_runs_transport_helper(repo, capture):
    marker = repo.parent / "transport-ran"
    helper = repo.parent / "transport-helper"
    helper.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\nexit 1\n")
    helper.chmod(0o755)
    tree_oid = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree_object = repo / ".git" / "objects" / tree_oid[:2] / tree_oid[2:]
    assert tree_object.is_file()
    tree_object.unlink()
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    git(repo, "config", "remote.origin.url", f"ext::{helper}")
    git(repo, "config", "protocol.ext.allow", "always")

    with pytest.raises(TychoraError) as exc:
        capture(repo)
    assert exc.value.code == "INVALID_REQUEST"
    assert marker.exists() is False


def test_symlink_is_rejected_without_reading_its_target(repo, capture):
    outside = repo.parent / "outside.txt"
    outside.write_text("must not be captured\n")
    (repo / "linked.txt").symlink_to(outside)
    with pytest.raises(TychoraError) as exc:
        capture(repo)
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_nested_repository_gitlink_is_rejected(repo, capture):
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "child.py").write_text("value = 1\n")
    git(nested, "config", "user.name", "Synthetic Author")
    git(nested, "config", "user.email", "synthetic@example.invalid")
    git(nested, "add", "child.py")
    git(nested, "commit", "-qm", "nested fixture")
    git(repo, "add", "nested")
    with pytest.raises(TychoraError) as exc:
        capture(repo)
    assert exc.value.code == "FORBIDDEN_SCOPE"


def test_unsupported_filename_encoding_is_an_actionable_error(repo, capture):
    raw_root = os.fsencode(repo)
    raw_name = b"unsupported-\xff.py"
    adapter = RepositoryCapture(capture.artifacts)
    try:
        descriptor = os.open(
            raw_root + b"/" + raw_name, os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError:
        with pytest.raises(TychoraError) as exc:
            adapter._paths(raw_name + b"\0", ())
        assert exc.value.code == "INVALID_REQUEST"
        assert "represented safely" in exc.value.message
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"value = 1\n")
    subprocess.run(
        [b"/usr/bin/git", b"-C", raw_root, b"add", b"--", raw_name],
        check=True,
        capture_output=True,
    )
    with pytest.raises(TychoraError) as exc:
        capture(repo)
    assert exc.value.code == "INVALID_REQUEST"
    assert "represented safely" in exc.value.message


def test_git_capability_is_lazy_and_only_repository_capture_needs_it(
    repo, capture, monkeypatch
):
    from uuid import UUID

    from tychora.contracts import SourceInput

    original = capture.artifacts
    missing = RepositoryCapture(original, git_executable=Path("/synthetic/missing/git"))
    monkeypatch.setattr(capture.acquirer, "_repositories", missing)
    document = capture.acquirer.capture(
        SourceInput.model_validate(
            {
                "type": "text",
                "source_key": "document",
                "collection_id": UUID("00000000-0000-4000-8000-000000000015"),
                "kind": "manual@1",
                "text": "synthetic document",
            }
        ),
        ExecutionContext(),
    )
    assert document.original_ref.size_bytes == len(b"synthetic document")
    with pytest.raises(TychoraError) as exc:
        missing.snapshot(repo, (), ExecutionContext(), max_files=10)
    assert exc.value.code == "CAPABILITY_MISSING"


def test_cancelled_repository_capture_starts_no_git_process(repo, capture, monkeypatch):
    started = False
    popen = subprocess.Popen

    def record_start(*args, **kwargs):
        nonlocal started
        started = True
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_start)
    context = ExecutionContext()
    context.cancelled.set()
    with pytest.raises(TychoraError) as exc:
        RepositoryCapture(capture.artifacts).snapshot(repo, (), context, max_files=10)
    assert exc.value.code == "CANCELLED"
    assert started is False


def test_capture_retries_then_rejects_a_persistently_moving_file(
    repo, capture, monkeypatch
):
    path = repo / "module.py"
    put = capture.artifacts.put_stream
    movements = 0

    def move_during_file_store(stream, media_type):
        nonlocal movements
        ref = put(stream, media_type)
        if media_type == "application/octet-stream" and movements < 3:
            movements += 1
            path.write_text(f"movement = {movements}\n")
        return ref

    monkeypatch.setattr(capture.artifacts, "put_stream", move_during_file_store)
    with pytest.raises(TychoraError) as exc:
        capture(repo)
    assert exc.value.code == "INTEGRITY_ERROR"
    assert movements == 3


def test_unborn_repository_captures_without_false_head_attribution(tmp_path, capture):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-q")
    (root / "first.py").write_text("value = 1\n")
    result = capture(root)
    assert result.commit is None
    assert result.dirty is True


def test_default_secret_excludes_do_not_affect_capture_dirty_scope(repo, capture):
    (repo / ".env").write_text("TOKEN=synthetic\n")
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-qm", "add excluded secret fixture")
    baseline = capture(repo)
    (repo / ".env").write_text("TOKEN=changed\n")
    changed = capture(repo)
    assert baseline.original_sha == changed.original_sha
    assert changed.dirty is False
    assert ".env" not in {entry.relative_path for entry in changed.entries}
