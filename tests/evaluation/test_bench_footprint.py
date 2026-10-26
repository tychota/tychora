"""Publication writes one page and one root, whose sizes are bounded."""

import pytest

from tests.api.test_public_sync import CharacterCounter
from tychora import SourceInput, Tychora
from tychora.contracts.errors import TychoraError
from tychora.evaluation.bench.footprint import publication_footprint
from tychora.ports.execution import ExecutionContext


def _add(kb, index):
    job = kb.sources.add(
        SourceInput(
            type="text",
            source_key=f"f{index}.md",
            collection_id=kb.default_collection_id,
            kind="manual@1",
            format="text",
            metadata={"title": "T", "publisher": "P", "edition": "1"},
            text=f"Footprint outcome {index}.",
        )
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def _footprint(kb, snapshot_id=None):
    owner = kb._workspace.owner

    def read():
        return publication_footprint(
            owner.db, snapshot_id or owner.store.active_snapshot_id()
        )

    return owner.run_write_sync(read, ExecutionContext())


def test_footprint_counts_root_and_new_page(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        for index in range(3):
            _add(kb, index)
        footprint = _footprint(kb)
    assert footprint.new_pages == 1
    assert 0 < footprint.root_bytes < 1024 * 1024
    assert 0 < footprint.new_page_bytes < 1024 * 1024
    assert footprint.largest_artifact_bytes == max(
        footprint.root_bytes, footprint.new_page_bytes
    )


def test_footprint_of_unknown_snapshot_is_refused(tmp_path):
    with (
        Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb,
        pytest.raises(TychoraError) as raised,
    ):
        _footprint(kb, "snapshot-missing")
    assert raised.value.reason == "snapshot.absent"
