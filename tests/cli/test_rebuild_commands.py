"""Rebuild sub-commands: native channels and whole workspaces."""

import json

from typer.testing import CliRunner

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text, make_format_one
from tychora import Tychora
from tychora.interfaces.cli import app


def test_rebuild_native_keeps_the_maintenance_request(tmp_path):
    result = CliRunner().invoke(
        app,
        ["--workspace", str(tmp_path / "ws"), "manage", "rebuild", "native", "text"],
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["recovery"]["native_pending_batches"] == 0


def test_rebuild_workspace_from_old(tmp_path, monkeypatch):
    monkeypatch.setattr("tychora.api._LazyOutputCounter", lambda _path: Counter())
    old = tmp_path / "old"
    with Tychora.open(old, token_counter=Counter()) as kb:
        add_text(kb, "alpha", "Alpha.")
    make_format_one(old)
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "new"),
            "manage",
            "rebuild",
            "workspace",
            "--from",
            str(old),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert [item["source_key"] for item in report["sources"]] == ["alpha"]


def test_rebuild_workspace_refuses_occupied_destination(tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "x").write_text("x")
    (tmp_path / "old").mkdir()
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(tmp_path / "new"),
            "manage",
            "rebuild",
            "workspace",
            "--from",
            str(tmp_path / "old"),
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["reason"] == "rebuild.destination_not_empty"
