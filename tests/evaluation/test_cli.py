"""The local CLI runs a real lexical integrity case from a pinned gold directory."""

import json
from contextlib import contextmanager
from hashlib import sha256

from typer.testing import CliRunner

from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.interfaces.cli import app
from tychora.ports.execution import ExecutionContext


def test_cli_evaluates_explicit_local_gold_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"

    class Counter:
        model = "synthetic-output"
        tokenizer_id = "cl100k_base"

        def count(self, text):
            return len(text)

    with Tychora.open(workspace, token_counter=Counter()) as kb:
        accepted = kb.sources.add(
            {
                "type": "text",
                "source_key": "synthetic-note",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "The saffron marker identifies the selected note.",
                "metadata": {
                    "title": "Synthetic note",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(accepted.id, timeout=scaled(10))
        assert status.state == "succeeded", status.error_code
        owner = kb._workspace.owner
        source_id, revision_id, unit_id = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.db.execute(
                    "SELECT s.source_id,r.revision_id,u.unit_id FROM evidence_units u "
                    "JOIN extractions e USING(extraction_id) JOIN revisions r USING(revision_id) "
                    "JOIN sources s USING(source_id) WHERE s.source_key='synthetic-note' LIMIT 1"
                ).fetchone(),
                ExecutionContext(),
            )
        )
    gold = tmp_path / "gold"
    gold.mkdir()
    case = {
        "case_id": "saffron",
        "family_key": "saffron",
        "split": "test",
        "query": {"question": "saffron marker", "modalities": ["lexical"]},
        "expected_source_ids": [source_id],
        "expected_revision_ids": [revision_id],
        "expected_unit_ids": [unit_id],
    }
    cases = (json.dumps(case, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (gold / "cases.jsonl").write_bytes(cases)
    (gold / "manifest.json").write_text(
        json.dumps(
            {
                "format": "tychora-gold@1",
                "dataset_id": "synthetic-cli-v1",
                "name": "Synthetic lexical integrity",
                "version_sha": sha256(cases).hexdigest(),
                "case_count": 1,
            }
        )
    )

    @contextmanager
    def local_client(options):
        with Tychora.open(options.workspace, token_counter=Counter()) as kb:
            yield kb

    monkeypatch.setattr("tychora.interfaces.commands.manage.client", local_client)
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "manage",
            "evaluate",
            "synthetic-cli-v1",
            "test",
            "--dataset-path",
            str(gold),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["job"]["state"] == "succeeded"
    with Tychora.open(workspace) as kb:
        report = kb.evaluations.get(payload["job"]["id"])
        assert report["cases"]["saffron"]["runs"][0]["metrics"]["metrics"]
