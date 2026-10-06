"""Hosted setup verifies every selected public asset before exposing it to tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
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
