"""Declared synthetic corpus; labels resolve anchors from canonical SQL, never search output."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from tychora import Tychora
from tychora.contracts.identity import canonical_bytes
from tychora.ports.execution import ExecutionContext

CORPUS = Path(__file__).parent.parent / "fixtures" / "evaluation" / "corpus.json"


class SyntheticCounter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


@pytest.fixture
def executable_integrity_corpus(tmp_path):
    declaration = json.loads(CORPUS.read_text())
    assert declaration["format"] == "tychora-synthetic-corpus@1"
    workspace = tmp_path / "workspace"
    with Tychora.open(workspace, token_counter=SyntheticCounter()) as kb:
        for source in declaration["sources"]:
            handle = kb.sources.add(
                {
                    "type": "text",
                    "source_key": source["source_key"],
                    "collection_id": "default",
                    "kind": source.get("kind", "manual@1"),
                    "format": source.get("format", "text"),
                    "text": source["text"],
                    "metadata": {
                        "title": source["title"],
                        "publisher": "Synthetic fixture",
                        "edition": "1",
                        **(
                            {"issuer": "Synthetic fixture"}
                            if source.get("kind") == "standard@1"
                            else {}
                        ),
                    },
                }
            )
            status = kb.jobs.wait(handle.id, timeout=10)
            assert status.state == "succeeded", (
                source["source_key"],
                status.error_code,
            )

        owner = kb._workspace.owner

        def canonical_labels():
            return {
                source["source_key"]: owner.db.execute(
                    "SELECT s.source_id,r.revision_id,u.unit_id FROM evidence_units u "
                    "JOIN extractions e USING(extraction_id) JOIN revisions r USING(revision_id) "
                    "JOIN sources s USING(source_id) "
                    "WHERE s.source_key=? AND instr(u.text,?)>0",
                    (source["source_key"], source["anchor"]),
                ).fetchall()
                for source in declaration["sources"]
            }

        labels = kb._workspace.bridge.call(
            owner.run_blocking(canonical_labels, ExecutionContext())
        )
        assert all(len(rows) == 1 for rows in labels.values())
        cases = []
        for source in declaration["sources"]:
            if source.get("case", True) is False:
                continue
            source_id, revision_id, unit_id = labels[source["source_key"]][0]
            query = dict(
                source.get(
                    "query",
                    {
                        "question": source["anchor"],
                        "modalities": ["lexical"],
                    },
                )
            )
            if source.get("restrict_to_revision"):
                query["revision_ids"] = [revision_id]
            cases.append(
                {
                    "case_id": source["source_key"],
                    "family_key": source["source_key"],
                    "split": "test",
                    "benchmark": "integrity",
                    "label_provenance": "declared synthetic source anchor and canonical SQL identity",
                    "query": query,
                    "expected_source_ids": [source_id],
                    "expected_revision_ids": [revision_id],
                    "expected_unit_ids": [unit_id],
                    "forbidden_source_ids": [
                        labels[other["source_key"]][0][0]
                        for other in declaration["sources"]
                        if other is not source
                    ],
                }
            )
        gold = tmp_path / "gold"
        gold.mkdir()
        payload = b"".join(canonical_bytes(case) + b"\n" for case in cases)
        (gold / "cases.jsonl").write_bytes(payload)
        (gold / "manifest.json").write_bytes(
            canonical_bytes(
                {
                    "format": "tychora-gold@1",
                    "dataset_id": "synthetic-executable-v1",
                    "name": "Declared synthetic lexical integrity",
                    "version_sha": sha256(payload).hexdigest(),
                    "case_count": len(cases),
                }
            )
        )
        yield kb, gold, tuple(case["case_id"] for case in cases)
