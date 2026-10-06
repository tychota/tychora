"""Frozen acceptance harness must enforce its claimed isolation boundary."""

import runpy
import subprocess
from pathlib import Path

import pytest


def _harness():
    return runpy.run_path(str(Path(__file__).parents[2] / "packaging/test_frozen.py"))


def test_linux_network_denial_wraps_executable(monkeypatch, tmp_path):
    harness = _harness()
    called = []

    def record(command, **kwargs):
        called.append(command)
        return subprocess.CompletedProcess(command, 0, "Usage", "")

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/unshare")
    monkeypatch.setattr("subprocess.run", record)
    harness["run"](Path("/tmp/tychora"), "--help", cwd=tmp_path, network_denied=True)
    assert called[0][:4] == ["/usr/bin/unshare", "--user", "--map-root-user", "--net"]


def test_linux_network_denial_fails_if_unavailable(monkeypatch, tmp_path):
    harness = _harness()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(AssertionError, match="unshare"):
        harness["run"](
            Path("/tmp/tychora"), "--help", cwd=tmp_path, network_denied=True
        )
