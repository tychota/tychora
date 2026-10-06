"""Release inventory binds the actual artifacts to the locked installed stacks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
