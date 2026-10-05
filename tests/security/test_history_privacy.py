"""Persisted query history exposes only a scope-bound summary."""

import os
import subprocess
import sys

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.contracts.operations import DiscoverRequest, InspectRequest
from tychora.ports.execution import ExecutionContext


class _Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


def test_run_summary_omits_question_and_rejects_changed_scope(tmp_path):
    question = "synthetic-private-query-marker"
    with Tychora.open(tmp_path / "workspace", token_counter=_Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "history",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "The synthetic-private-query-marker appears in this fixture.",
                "metadata": {
                    "title": "History fixture",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id).state == "succeeded"
        result = kb.search({"question": question, "modalities": ["lexical"]})
        own = kb.inspect({"subject": "runs", "id": result.run_id})
        assert own.records[0].record_type == "run_summary"
        assert question not in own.model_dump_json()
        assert "segments" not in own.model_dump_json()
        narrowed = kb.scope.model_copy(update={"collection_ids": frozenset()})
        with pytest.raises(TychoraError) as denied:
            kb._workspace.bridge.call(
                kb._workspace.services.inspection.inspect(
                    InspectRequest(subject="runs", id=result.run_id),
                    narrowed,
                    ExecutionContext(),
                )
            )
        assert denied.value.code == "FORBIDDEN_SCOPE"


def test_run_discovery_paginates_in_sql_and_definition_reads_one_id(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=_Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "run-pages",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Run history pagination fixture.",
                "metadata": {
                    "title": "History pages",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id).state == "succeeded"
        base = kb.search({"question": "history", "modalities": ["lexical"]}).run_id
        owner = kb._workspace.owner
        statements = []

        def seed():
            for index in range(300):
                owner.db.execute(
                    "INSERT INTO query_runs SELECT ?,snapshot_id,principal_id,"
                    "scope_digest,policy_epoch,lexical_statistics_epoch,config_sha,"
                    "state,metrics_json,result_ids_json,retained_payload_sha,"
                    "created_at,expires_at FROM query_runs WHERE run_id=?",
                    (f"synthetic-run-{index:04d}", base),
                )
            owner.db.execute(
                "UPDATE query_runs SET principal_id='other' "
                "WHERE run_id BETWEEN 'synthetic-run-0200' AND 'synthetic-run-0299'"
            )
            owner.db.set_trace_callback(statements.append)

        kb._workspace.bridge.call(owner.run_blocking(seed, ExecutionContext()))
        try:
            first = kb.discover(DiscoverRequest(subject="runs", limit=1))
            assert len(first.definitions) == 1
            assert first.next_cursor is not None
            assert first.definitions[0].id != "synthetic-run-0200"
            second = kb.discover(
                DiscoverRequest(subject="runs", limit=1, cursor=first.next_cursor)
            )
            assert len(second.definitions) == 1
            assert second.definitions[0].id > first.definitions[0].id
            assert (
                kb._workspace.services.discovery.definition(
                    "runs", "synthetic-run-0001", kb.scope
                ).id
                == "synthetic-run-0001"
            )
            with pytest.raises(TychoraError) as hidden:
                kb._workspace.services.discovery.definition(
                    "runs", "synthetic-run-0200", kb.scope
                )
            assert hidden.value.code == "NOT_FOUND"
        finally:
            kb._workspace.bridge.call(
                owner.run_blocking(
                    lambda: owner.db.set_trace_callback(None), ExecutionContext()
                )
            )
        run_reads = [
            sql
            for sql in statements
            if "FROM query_runs" in sql and sql.lstrip().startswith("SELECT")
        ]
        assert len(run_reads) == 4
        assert all("LIMIT" in sql for sql in run_reads[:2])
        assert all("run_id=" in sql for sql in run_reads[2:])


def test_new_run_history_reopens_and_replays_across_opposite_hash_seeds(tmp_path):
    root = tmp_path / "workspace"
    for mode, seed in (("create", "1"), ("verify", "2")):
        completed = subprocess.run(
            [sys.executable, "-m", "tests.security.history_probe", mode, str(root)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_legacy_scope_digest_matches_exact_projection_only(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=_Counter()) as kb:
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "legacy-run",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "Legacy run identity fixture.",
                "metadata": {
                    "title": "Legacy run",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id).state == "succeeded"
        run_id = kb.search({"question": "legacy run", "modalities": ["lexical"]}).run_id
        owner = kb._workspace.owner
        current = kb.scope.model_dump(mode="json")
        exact = digest("query-run-scope-v1", current)
        alternate = dict(current)
        alternate["capabilities"] = list(reversed(current["capabilities"]))
        assert alternate != current
        mismatch = digest("query-run-scope-v1", alternate)

        def set_digest(value):
            owner.db.execute(
                "UPDATE query_runs SET scope_digest=? WHERE run_id=?", (value, run_id)
            )

        kb._workspace.bridge.call(
            owner.run_blocking(lambda: set_digest(exact), ExecutionContext())
        )
        assert kb.inspect({"subject": "runs", "id": run_id}).id == run_id
        kb._workspace.bridge.call(
            owner.run_blocking(lambda: set_digest(mismatch), ExecutionContext())
        )
        with pytest.raises(TychoraError) as denied:
            kb.inspect({"subject": "runs", "id": run_id})
        assert denied.value.code == "FORBIDDEN_SCOPE"
        assert run_id not in {
            entry.id
            for entry in kb.discover({"subject": "runs", "limit": 1000}).definitions
        }
