"""Declared synthetic corpus; labels resolve anchors from canonical SQL, never search output."""

import json
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.timing import scaled
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.contracts.identity import canonical_bytes
from tychora.ports.execution import ExecutionContext

CORPUS = Path(__file__).parent.parent / "fixtures" / "evaluation" / "corpus.json"


class SyntheticCounter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def code_profile_config(tmp_path: Path, limits: str):
    """Resolve the built-in code profile with the given limits, acquiring under tmp_path."""
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="code@1"\n'
        f"[configuration.limits]\n{limits}\n"
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    return resolve_config(settings, "test", {})


def add_repository(kb: Tychora, repo: Path, source_key: str, title: str):
    """Register a git repository as a code source and return its job handle."""
    return kb.sources.add(
        {
            "type": "repository",
            "path": str(repo),
            "source_key": source_key,
            "collection_id": "default",
            "kind": "code_file@1",
            "metadata": {"title": title},
        }
    )


@contextmanager
def _integrity_corpus(tmp_path: Path):
    """Ingest the declared synthetic corpus and write its digest-pinned gold set."""
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
                    "text": "synthetic filler " * source.get("prefix_repeat", 0)
                    + source["text"],
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
            status = kb.jobs.wait(handle.id, timeout=scaled(10))
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


@pytest.fixture
def executable_integrity_corpus(tmp_path):
    """A fresh corpus for tests that register datasets or run evaluations."""
    with _integrity_corpus(tmp_path) as corpus:
        yield corpus


@pytest.fixture(scope="module")
def shared_integrity_corpus(tmp_path_factory):
    """One corpus per module for read-only tests; never register or run against it."""
    with _integrity_corpus(tmp_path_factory.mktemp("integrity-corpus")) as corpus:
        yield corpus
