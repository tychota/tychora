"""Manifest closures scale with the workspace, not with a fixed per-root cap.

A snapshot root reaches every retained index input, so a fixed cap on one
closure is met by an ordinary corpus. The fixture lowers the former cap below
its own snapshot closure and checks every caller still works.
"""

import shutil
from collections import Counter

import pytest

from tests.api.test_public_sync import CharacterCounter
from tests.operations.export_support import add_texts, owner_rows
from tychora import Tychora
from tychora.adapters.files import references
from tychora.application.removal import RemovalService
from tychora.application.restore import RestoreService
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

FORMER_CAP = 50
DOCUMENTS = 60


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """A closed workspace whose snapshot closure exceeds ``FORMER_CAP``."""
    root = tmp_path_factory.mktemp("closure") / "kb"
    with Tychora.open(root, token_counter=CharacterCounter()) as kb:
        add_texts(kb, DOCUMENTS, "Bound")
        inputs = owner_rows(kb, "SELECT COUNT(*) FROM index_batches")[0][0]
        assert inputs > FORMER_CAP
    return root


@pytest.fixture
def copied(corpus, tmp_path):
    copy = tmp_path / "kb"
    shutil.copytree(corpus, copy)
    return copy


@pytest.fixture
def workspace(copied, monkeypatch):
    """The copy, with the former fixed per-root cap lowered below its closure.

    The floors are lowered too, so only the bound derived from the
    workspace's own artifact rows applies.
    """
    monkeypatch.setattr(references, "MAX_CLOSURE_MANIFESTS", FORMER_CAP, raising=False)
    monkeypatch.setattr(references, "MIN_CLOSURE_MANIFESTS", 1, raising=False)
    monkeypatch.setattr(references, "MIN_CLOSURE_REFERENCES", 1, raising=False)
    return copied


def _on_owner(kb, call):
    owner = kb._workspace.owner
    return owner.run_blocking_sync(call, ExecutionContext())


def _mark_rooted_pending(kb) -> str:
    """Leave a rooted manifest delete_pending, as a crash mid-collection does."""
    owner = kb._workspace.owner
    (sha,) = owner_rows(kb, "SELECT input_manifest_sha FROM index_batches LIMIT 1")[0]

    def mark():
        owner.db.execute(
            "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=?",
            (sha,),
        )
        owner.db.commit()

    _on_owner(kb, mark)
    return sha


def _state(kb, sha: str) -> str:
    return owner_rows(
        kb, "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
    )[0][0]


def test_open_reconciles_a_corpus_above_the_former_cap(workspace):
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        sha = _mark_rooted_pending(kb)
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        assert _state(kb, sha) == "present"
        assert kb.search({"question": "outcome", "modalities": ["lexical"]}).segments


def test_gc_reconciles_a_corpus_above_the_former_cap(workspace):
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        sha = _mark_rooted_pending(kb)
        assert kb.management.run({"operation": "gc"}).gc is not None
        assert _state(kb, sha) == "present"


def test_export_and_import_a_corpus_above_the_former_cap(workspace, tmp_path):
    archive = tmp_path / "archive"
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        kb.management.run({"operation": "export", "destination": str(archive)})
        pages = owner_rows(kb, "SELECT COUNT(*) FROM snapshot_pages")
    receipt = RestoreService().import_backup(
        archive, tmp_path / "restored", None, ExecutionContext()
    )
    with Tychora.open(
        tmp_path / "restored",
        scope=receipt.opening_scope,
        token_counter=CharacterCounter(),
    ) as restored:
        assert owner_rows(restored, "SELECT COUNT(*) FROM snapshot_pages") == pages


def test_an_archive_above_the_closure_ceiling_fails_with_a_registered_reason(
    workspace, tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        kb.management.run({"operation": "export", "destination": str(archive)})
    monkeypatch.setattr(references, "CLOSURE_MANIFEST_CEILING", FORMER_CAP)
    with pytest.raises(TychoraError) as error:
        RestoreService().import_backup(
            archive, tmp_path / "restored", None, ExecutionContext()
        )
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason == "closure.limit_exceeded"
    assert not (tmp_path / "restored").exists()


def test_one_reconcile_pass_reads_each_manifest_once(copied, monkeypatch):
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        _mark_rooted_pending(kb)
        owner = kb._workspace.owner
        reads: Counter[str] = Counter()
        artifacts = type(owner.artifacts)
        open_verified = artifacts.open_verified

        def counting(self, ref, *args, **kwargs):
            reads[ref.sha256] += 1
            return open_verified(self, ref, *args, **kwargs)

        monkeypatch.setattr(artifacts, "open_verified", counting)
        service = RemovalService(owner, kb._workspace.services.jobs.repository)
        _on_owner(kb, service.reconcile_pending_deletions)
        captures = owner_rows(
            kb,
            "SELECT COUNT(*) FROM jobs WHERE captured_inputs_sha IS NOT NULL "
            "AND state='complete' AND result_snapshot_id IS NOT NULL",
        )[0][0]
        assert captures
        assert reads
        assert max(reads.values()) == 1, reads.most_common(3)
