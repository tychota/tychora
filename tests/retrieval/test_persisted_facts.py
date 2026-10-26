"""Eligibility reads persisted representation facts and never derives them."""

import ast
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner
from tychora import Tychora
from tychora.contracts.errors import TychoraError

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def _facts(owner):
    store = owner.store
    return on_owner(
        owner, lambda: store.read_eligibility_facts(store.resolve_snapshot(None))
    )


def test_eligibility_reader_imports_no_indexing():
    tree = ast.parse((SOURCE / "adapters/sqlite/eligibility.py").read_text())
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in modules if m.startswith("tychora.indexing")}


@pytest.mark.parametrize(
    "owners",
    [
        "SELECT unit_id FROM evidence_units",
        "SELECT chunk_id FROM chunks",
    ],
)
def test_selected_extraction_without_facts_is_refused(tmp_path, owners):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pumps.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        owner = kb._workspace.owner
        on_owner(
            owner,
            lambda: owner.db.execute(
                f"DELETE FROM representation_dependencies WHERE owner_id IN ({owners})"
            ),
        )
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "pump seal"})
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "canonical.facts_missing",
    )


def test_eligibility_reads_no_artifact(tmp_path, monkeypatch):
    from tychora.adapters.files.cas import LocalArtifacts

    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pumps.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        owner = kb._workspace.owner
        snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
        opened = []
        original = LocalArtifacts.open_verified

        def recording(self, ref):
            opened.append(ref.sha256)
            return original(self, ref)

        monkeypatch.setattr(LocalArtifacts, "open_verified", recording)
        facts = _facts(owner)
    assert facts.units and facts.chunks
    assert snapshot.config_sha not in opened


def test_eligibility_narrows_revision_uses_by_the_source_override(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pumps.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        owner = kb._workspace.owner
        (before,) = _facts(owner).revisions
        assert {"compose", "lookup"} <= before.allowed_uses
        kb.sources.set_allowed_uses(before.source_id, "reference_only")
        (after,) = _facts(owner).revisions
        kb.sources.set_allowed_uses(before.source_id, None)
        (cleared,) = _facts(owner).revisions
    assert after.allowed_uses == frozenset({"lookup"})
    assert cleared.allowed_uses == before.allowed_uses
