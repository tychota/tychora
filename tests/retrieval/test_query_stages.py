"""Queries run as typed stages and never serialize the configuration."""

import ast
from pathlib import Path

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.adapters.sqlite.history import HistoryRepository
from tychora.configuration.models import ResolvedConfig
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


def _ingest(kb: Tychora) -> None:
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": "a.md",
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": "# A\n\nThe verification outcome.\n",
            "metadata": {"title": "A"},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_query_never_dumps_configuration(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _ingest(kb)
        kb.search({"question": "warm the caches"})
        dumps = []
        original = ResolvedConfig.model_dump

        def counted(self, *args, **kwargs):
            dumps.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ResolvedConfig, "model_dump", counted)
        kb.search({"question": "verification outcome"})
    assert dumps == []


def test_replay_resolves_its_snapshot_through_the_catalog(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _ingest(kb)
        service = kb._workspace.services.query
        owner = service.owner
        service.history = HistoryRepository(
            owner.db, artifacts=owner.artifacts, retain_payloads=True
        )
        retained = kb.search({"question": "verification outcome", "exact_only": True})

        def outside_catalog(selector):
            raise AssertionError("Replay read its snapshot outside the catalog")

        monkeypatch.setattr(owner.store, "resolve_snapshot", outside_catalog)
        replayed = kb._workspace.bridge.call(
            service.replay(retained.run_id, kb.scope, ExecutionContext())
        )
    assert replayed.result.snapshot_id == retained.snapshot_id
    assert replayed.result.segments == retained.segments


def test_query_functions_stay_under_80_lines():
    tree = ast.parse((SOURCE / "application/query.py").read_text())
    long = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.end_lineno or node.lineno) - node.lineno + 1 > 80
    ]
    assert long == []
