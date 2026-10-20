"""Shared setup for export/import tests: real text vector batches and owner reads."""

import json
from types import SimpleNamespace

from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.configuration.resolve import resolve_config
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

DEFAULT_COLLECTION = "00000000-0000-4000-8000-000000000001"


def owner_rows(kb, sql, params=()):
    """Run a read on the owner thread and return plain tuples."""
    owner = kb._workspace.owner
    return owner.run_blocking_sync(
        lambda: [tuple(r) for r in owner.db.execute(sql, params).fetchall()],
        ExecutionContext(),
    )


def add_texts(kb, count, prefix):
    for index in range(count):
        job = kb.sources.add(
            {
                "type": "text",
                "text": f"{prefix} outcome {index}.",
                "format": "text",
                "kind": "manual@1",
                "source_key": f"{prefix}{index}.md",
                "collection_id": "default",
                "metadata": {"title": "T", "publisher": "P", "edition": "1"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"


def vector_config(tmp_path, monkeypatch):
    """Voyage text provider with chroma, stubbed at the transport and tokenizer."""

    class Tokenizer:
        def encode(self, text):
            return SimpleNamespace(ids=list(range(len(text))))

    class Transport:
        def __init__(self, *, api_key):
            pass

        def post_json(self, path, body, timeout):
            vector = [1.0] + [0.0] * 1023
            data = [
                {
                    "index": o,
                    "data": [
                        {"index": i, "embedding": vector} for i, _ in enumerate(b)
                    ],
                }
                for o, b in enumerate(json.loads(body)["inputs"])
            ]
            return RawVoyageResponse(200, json.dumps({"data": data}).encode())

        def close(self):
            pass

    base = "tychora.application.default_bindings."
    monkeypatch.setattr(base + "load_voyage_tokenizer", lambda root, model: Tokenizer())
    monkeypatch.setattr(base + "HttpxVoyageTransport", Transport)
    monkeypatch.setenv("TEST_VOYAGE_KEY", "synthetic-test-key")
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=false\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:TEST_VOYAGE_KEY"\n'
        '[configuration.index.text]\nbackend="chroma"\n'
    )
    return resolve_config(settings, "test", {})


def upload_scope():
    return RequestScope(
        principal_id="writer",
        collection_ids=frozenset({DEFAULT_COLLECTION}),
        capabilities=frozenset({"read", "write", "manage"}),
        uses=frozenset({"lookup", "compose"}),
        upload_providers=frozenset({"voyage"}),
    )


def grant_upload(kb):
    owner = kb._workspace.owner
    kb._workspace.bridge.call(
        owner.run_blocking(
            lambda: owner.db.execute(
                'UPDATE collections SET policy_json=? WHERE slug="default"',
                ('{"schema_version":1,"upload_providers":["voyage"]}',),
            ),
            ExecutionContext(),
        )
    )
