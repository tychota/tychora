"""Binding-level original access and fail-closed upload consent."""

import sqlite3

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import source_id
from tychora.contracts.operations import InspectRequest
from tychora.ports.execution import ExecutionContext


class _Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, value: str) -> int:
        return len(value)


def test_unconfigured_upload_provider_never_changes_default_grant_or_policy(tmp_path):
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, token_counter=_Counter()) as kb:
        assert kb.scope.upload_providers == frozenset()
        with sqlite3.connect(workspace / "corpus.sqlite3") as db:
            before = db.execute(
                "SELECT policy_json FROM collections WHERE collection_id=?",
                (str(kb.default_collection_id),),
            ).fetchone()
        with pytest.raises(TychoraError) as error:
            kb.management.run(
                {
                    "operation": "provider-upload-consent",
                    "collection_id": str(kb.default_collection_id),
                    "provider": "voyage",
                    "action": "grant",
                }
            )
        assert error.value.code == "INVALID_REQUEST"
        assert kb.scope.upload_providers == frozenset()
        with sqlite3.connect(workspace / "corpus.sqlite3") as db:
            after = db.execute(
                "SELECT policy_json FROM collections WHERE collection_id=?",
                (str(kb.default_collection_id),),
            ).fetchone()
        assert before == after


def test_withdrawn_original_does_not_disclose_tombstone_to_reader(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "private-record",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "synthetic original",
                "metadata": {
                    "title": "Synthetic record",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        sid = source_id(kb.default_collection_id, "private-record")
        rid = kb.sources.revisions(sid).revisions[0].revision_id
        kb.management.run({"operation": "remove", "source_ids": [sid]})
        narrower = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})

        def inspect_narrow():
            return kb._workspace.bridge.call(
                kb._workspace.services.inspection.inspect(
                    InspectRequest(subject="revisions", id=rid, original=True),
                    narrower,
                    ExecutionContext(),
                )
            )

        with pytest.raises(TychoraError) as before:
            inspect_narrow()
        kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": sid,
                "revision_ids": [rid],
                "confirm_original_removal": True,
            }
        )
        with pytest.raises(TychoraError) as after:
            inspect_narrow()
        assert before.value.code == after.value.code == "FORBIDDEN_SCOPE"


def test_manager_cannot_preview_hidden_revision_bytes(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "high-rank-record",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "hidden synthetic original",
                "metadata": {
                    "title": "Synthetic record",
                    "publisher": "Example Team",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        sid = source_id(kb.default_collection_id, "high-rank-record")
        rid = kb.sources.revisions(sid).revisions[0].revision_id
        kb.management.run({"operation": "remove", "source_ids": [sid]})
        with sqlite3.connect(kb._workspace.owner.path / "corpus.sqlite3") as db:
            db.execute("UPDATE revisions SET access_rank=3 WHERE revision_id=?", (rid,))
        with pytest.raises(TychoraError) as error:
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": sid,
                    "revision_ids": [rid],
                }
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
