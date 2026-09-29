"""Public API behavior in base installs and pre-existing workspaces."""

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError


class OutputCounter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def test_base_import_and_lexical_operation_without_optional_pillow(tmp_path):
    script = """
import builtins
import sys

original_import = builtins.__import__
def without_pillow(name, *args, **kwargs):
    if name == "PIL" or name.startswith("PIL."):
        raise ModuleNotFoundError("No module named 'PIL'", name="PIL")
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_pillow

from tychora import Tychora

class Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"
    def count(self, text):
        return len(text)

with Tychora.open(sys.argv[1], token_counter=Counter()) as kb:
    job = kb.sources.add({
        "type": "text", "source_key": "manual", "collection_id": "default",
        "kind": "manual@1", "format": "text", "text": "Record verification outcomes.",
        "metadata": {"title": "Verification", "publisher": "Example Team", "edition": "1"},
    })
    assert kb.jobs.wait(job.id).state == "succeeded"
    assert kb.search({"question": "verification", "modalities": ["lexical"]}).segments
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "base-install")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_existing_default_slug_with_other_uuid_is_typed_incompatibility(tmp_path):
    root = tmp_path / "knowledge"
    with Tychora.open(root, token_counter=OutputCounter()) as kb:
        fixed_id = str(kb.default_collection_id)
    existing_id = str(uuid4())
    with sqlite3.connect(root / "corpus.sqlite3") as db:
        db.execute(
            "UPDATE collections SET collection_id=? WHERE collection_id=?",
            (existing_id, fixed_id),
        )
    reopened = None
    try:
        with pytest.raises(TychoraError) as conflict:
            reopened = Tychora.open(root, token_counter=OutputCounter())
    finally:
        if reopened is not None:
            reopened.close()
    assert conflict.value.code == "PROFILE_INCOMPATIBLE"
    with sqlite3.connect(root / "corpus.sqlite3") as db:
        assert db.execute(
            "SELECT collection_id FROM collections WHERE slug='default'"
        ).fetchone() == (existing_id,)
        assert (
            db.execute(
                "SELECT 1 FROM collections WHERE collection_id=?", (fixed_id,)
            ).fetchone()
            is None
        )
