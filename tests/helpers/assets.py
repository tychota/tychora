"""Provisioned test assets: declared with @pytest.mark.assets, read with asset_path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROVISION_HINT = "provision with packaging/provision_ci_assets.py"
ASSET_VARIABLES = frozenset(
    {
        "TYCHORA_TEST_MODEL_CACHE",
        "TYCHORA_TEST_DOCLING_MODELS",
        "TYCHORA_TEST_TOKENIZER_ASSETS",
        "TYCHORA_TEST_OCR_MODELS",
        "TYCHORA_TEST_CODE",
        "TYCHORA_TEST_CODE_CORPUS",
        "TYCHORA_TEST_PDF_RESPONSES",
    }
)
_declared: frozenset[str] = frozenset()


def declared_assets(item: pytest.Item) -> frozenset[str]:
    """Every variable named by the assets markers on a test, its class and module."""
    names: set[str] = set()
    for marker in item.iter_markers("assets"):
        names.update(marker.args)
    unknown = names - ASSET_VARIABLES
    if unknown:
        raise pytest.UsageError(
            f"unknown asset variables {sorted(unknown)} on {item.nodeid}"
        )
    return frozenset(names)


def missing_assets(item: pytest.Item) -> list[str]:
    """Declared variables that are unset or empty in this environment."""
    return sorted(name for name in declared_assets(item) if not os.environ.get(name))


def check_item(item: pytest.Item) -> None:
    """Before fixtures run: remember the declared assets, then skip or fail if any is missing.

    Ordinary tests skip with the variable names. Qualification and release tests fail,
    because a missing verified bundle is an unavailable qualification, not a pass.
    """
    global _declared
    _declared = declared_assets(item)
    missing = missing_assets(item)
    if not missing:
        return
    message = f"requires {', '.join(missing)} ({PROVISION_HINT})"
    if item.get_closest_marker("qualification") or item.get_closest_marker("release"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def end_test() -> None:
    """Called by tests/conftest.py after teardown."""
    global _declared
    _declared = frozenset()


def asset_path(name: str) -> Path:
    """Path of a provisioned asset that the running test declared with @pytest.mark.assets."""
    if name not in _declared:
        raise RuntimeError(
            f"asset_path({name!r}) needs @pytest.mark.assets({name!r}) on the test"
        )
    return Path(os.environ[name])
