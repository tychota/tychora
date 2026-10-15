"""The owner bearer token persists across restarts and fails closed when unsafe."""

import json
import os

import pytest
from typer.testing import CliRunner

from tychora.application.owner import WorkspaceOwner
from tychora.application.runtime import (
    OWNER_TOKEN_FILE,
    RuntimeIdentity,
    load_owner_token,
    rotate_owner_token,
)
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.interfaces.cli import app


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


@pytest.fixture
def scope():
    return RequestScope(principal_id="owner")


def _token(root, settings, scope):
    with WorkspaceOwner.open(root, settings, scope) as owner:
        record = json.loads((root / ".runtime/endpoint.json").read_text())
        assert record["token"] == owner._runtime.record["token"]
        return record["token"], owner._runtime.instance_secret


def test_token_is_persisted_owner_only_and_reused(tmp_path, settings, scope):
    root = tmp_path / "data"
    first, first_secret = _token(root, settings, scope)
    path = root / ".runtime" / OWNER_TOKEN_FILE
    assert path.read_text() == first
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(first) >= 43
    second, second_secret = _token(root, settings, scope)
    assert second == first
    # Owner-bound cursors still expire with the instance.
    assert second_secret != first_secret
    assert not [p for p in (root / ".runtime").iterdir() if p.name.startswith(".")]


@pytest.mark.parametrize(
    "tamper",
    ["mode", "symlink", "content", "short", "directory", "hardlink", "oversized"],
)
def test_unsafe_token_file_fails_closed_without_regeneration(
    tmp_path, settings, scope, tamper
):
    root = tmp_path / "data"
    original, _ = _token(root, settings, scope)
    path = root / ".runtime" / OWNER_TOKEN_FILE
    outside = tmp_path / "outside"
    outside.write_text(original)
    outside.chmod(0o600)
    if tamper == "mode":
        path.chmod(0o644)
    elif tamper == "symlink":
        path.unlink()
        path.symlink_to(outside)
    elif tamper == "content":
        path.write_text("x" * 43 + "\n")
    elif tamper == "short":
        path.write_text("abc")
    elif tamper == "directory":
        path.unlink()
        path.mkdir()
    elif tamper == "hardlink":
        os.link(path, tmp_path / "second-name")
    elif tamper == "oversized":
        path.write_text("a" * 300)
    with (
        pytest.raises(TychoraError) as error,
        WorkspaceOwner.open(root, settings, scope),
    ):
        pytest.fail("Unsafe token file was accepted")
    assert error.value.code == "INTEGRITY_ERROR"
    assert original not in error.value.message
    # The suspicious file is left for inspection, not silently replaced.
    assert path.is_symlink() or path.exists()
    assert outside.read_text() == original


def test_load_creates_once_and_reads_back(tmp_path):
    directory = tmp_path / ".runtime"
    directory.mkdir(mode=0o700)
    token = load_owner_token(directory)
    assert load_owner_token(directory) == token


def test_rotation_requires_stopped_owner_and_replaces_token(tmp_path, settings, scope):
    root = tmp_path / "data"
    first, _ = _token(root, settings, scope)
    with WorkspaceOwner.open(root, settings, scope):
        with pytest.raises(TychoraError) as busy:
            rotate_owner_token(root)
        assert busy.value.code == "WORKSPACE_BUSY"
        assert (root / ".runtime" / OWNER_TOKEN_FILE).read_text() == first
    rotate_owner_token(root)
    rotated, _ = _token(root, settings, scope)
    assert rotated != first
    # Rotation also replaces a corrupt file, which is the documented recovery.
    (root / ".runtime" / OWNER_TOKEN_FILE).chmod(0o644)
    rotate_owner_token(root)
    assert _token(root, settings, scope)[0] not in {first, rotated}


def test_rotate_token_cli(tmp_path, settings, scope):
    root = tmp_path / "data"
    first, _ = _token(root, settings, scope)
    result = CliRunner().invoke(
        app, ["--workspace", str(root), "manage", "rotate-token"]
    )
    assert result.exit_code == 0, result.output
    assert first not in result.output
    assert (root / ".runtime" / OWNER_TOKEN_FILE).read_text() != first
    identity = RuntimeIdentity(root)
    identity.acquire()
    try:
        with pytest.raises(TychoraError):
            # A live lock holder in any process blocks rotation.
            rotate_owner_token(root)
    finally:
        identity.close()
