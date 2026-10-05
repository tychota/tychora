"""Test-only cross-process run-history publication and replay probe."""

import json
import sys
from pathlib import Path

from tychora import Tychora
from tychora.adapters.sqlite.history import HistoryRepository
from tychora.contracts.operations import DiscoverRequest
from tychora.contracts.requests import QueryRequest
from tychora.ports.execution import ExecutionContext


class Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


def run(mode: str, root: Path) -> None:
    with Tychora.open(root, token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        if mode == "create":
            handle = kb.sources.add(
                {
                    "type": "text",
                    "source_key": "cross-process-history",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": "A synthetic history identity marker.",
                    "metadata": {
                        "title": "History marker",
                        "publisher": "Synthetic fixture",
                        "edition": "1",
                    },
                }
            )
            assert kb.jobs.wait(handle.id).state == "succeeded"
            request = QueryRequest.from_mapping(
                {"question": "identity marker", "modalities": ["lexical"]}
            )
            result = kb.search(request)

            def retain():
                history = HistoryRepository(
                    owner.db, artifacts=owner.artifacts, retain_payloads=True
                )
                ref = history.retain_request(
                    result.run_id,
                    request,
                    scope=result.applied_scope,
                    snapshot_id=result.snapshot_id,
                    config_sha=owner.config.config_sha,
                    projection_fingerprint="synthetic-projection",
                    provider_fingerprint="synthetic-provider",
                )
                owner.db.execute(
                    "UPDATE query_runs SET retained_payload_sha=? WHERE run_id=?",
                    (ref.sha256, result.run_id),
                )

            kb._workspace.bridge.call(owner.run_blocking(retain, ExecutionContext()))
            (root.parent / "history-receipt.json").write_text(
                json.dumps(
                    {
                        "run_id": result.run_id,
                        "snapshot_id": result.snapshot_id,
                        "config_sha": owner.config.config_sha,
                    }
                )
            )
        else:
            receipt = json.loads((root.parent / "history-receipt.json").read_text())
            run_id = receipt["run_id"]
            inspected = kb.inspect({"subject": "runs", "id": run_id})
            assert inspected.records[0].record_type == "run_summary"
            listed = kb.discover(DiscoverRequest(subject="runs", limit=1))
            assert [entry.id for entry in listed.definitions] == [run_id]

            def replay():
                history = HistoryRepository(
                    owner.db, artifacts=owner.artifacts, retain_payloads=True
                )
                return history.replay_request(
                    run_id,
                    scope=kb.scope,
                    snapshot_id=receipt["snapshot_id"],
                    config_sha=receipt["config_sha"],
                    projection_fingerprint="synthetic-projection",
                    provider_fingerprint="synthetic-provider",
                    providers_available=True,
                )

            replayed = kb._workspace.bridge.call(
                owner.run_blocking(replay, ExecutionContext())
            )
            assert replayed.input.text == "identity marker"


if __name__ == "__main__":
    run(sys.argv[1], Path(sys.argv[2]))
