"""Scoped source admission and stable client-held cursor identity."""

import json
import os
import subprocess
import sys

import pytest

from tychora import Tychora
from tychora.application.discovery import _scoped_after, _scoped_cursor
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope


class _Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


def test_public_source_job_rejects_path_outside_registered_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside synthetic payload")
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration.acquisition]\nroots=[" + json.dumps(str(allowed)) + "]\n"
    )
    with Tychora.open(
        tmp_path / "workspace",
        config=resolve_config(settings, "test", {}),
        token_counter=_Counter(),
    ) as kb:
        handle = kb.sources.add(
            {
                "type": "file",
                "source_key": "outside",
                "collection_id": "default",
                "kind": "manual@1",
                "path": str(outside),
                "metadata": {
                    "title": "Synthetic outside-root source",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        status = kb.jobs.wait(handle.id)
        assert status.state == "failed"
        assert status.error_code == "FORBIDDEN_SCOPE"
        assert kb.discover({"subject": "sources"}).definitions == ()


def test_source_cursor_uses_semantic_scope_and_rejects_narrowed_scope(tmp_path):
    with Tychora.open(tmp_path / "workspace", token_counter=_Counter()) as kb:
        a = kb.scope
        b = RequestScope(
            principal_id=a.principal_id,
            collection_ids=frozenset(sorted(a.collection_ids, reverse=True)),
            access_ceiling=a.access_ceiling,
            uses=frozenset(sorted(a.uses, reverse=True)),
            capabilities=frozenset(sorted(a.capabilities, reverse=True)),
            upload_providers=frozenset(sorted(a.upload_providers, reverse=True)),
        )
        assert a == b
        token = _scoped_cursor(kb._workspace.owner, "sources", "synthetic", a, 1)
        assert _scoped_after(kb._workspace.owner, token, "sources", b, 1) == "synthetic"
        narrow = b.model_copy(update={"uses": frozenset({"lookup"})})
        with pytest.raises(TychoraError) as denied:
            _scoped_after(kb._workspace.owner, token, "sources", narrow, 1)
        assert denied.value.code == "INVALID_REQUEST"


def test_scope_identity_domains_are_hash_seed_independent():
    code = (
        "from tychora.contracts.requests import RequestScope,canonical_scope_projection\n"
        "from tychora.contracts.identity import digest\n"
        "s=RequestScope(principal_id='synthetic',collection_ids=frozenset({'a','b','c'}),"
        "uses=frozenset({'lookup','compose','audit'}),"
        "capabilities=frozenset({'read','write','manage','vision'}),"
        "upload_providers=frozenset({'alpha','beta'}))\n"
        "p=canonical_scope_projection(s)\n"
        "print(digest('eligibility-scope-v2',p),digest('source-cursor-scope-v2',p))\n"
    )
    values = []
    for seed in ("1", "2"):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        values.append(completed.stdout.strip())
    assert values[0] == values[1]
