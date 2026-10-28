"""Queries run as typed stages and never serialize the configuration."""

import ast
import copy
from collections import Counter
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.adapters.sqlite.history import HistoryRepository
from tychora.application import query as query_module
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest
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


def _count_digests(monkeypatch) -> Counter[str]:
    """Count the provider and projection digests the query module computes."""
    calls: Counter[str] = Counter()
    for name in ("_provider_digest", "_projection_digest"):
        original = getattr(query_module, name)

        def counted(*args, _name=name, _original=original):
            calls[_name] += 1
            return _original(*args)

        monkeypatch.setattr(query_module, name, counted)
    return calls


def test_query_never_dumps_configuration(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _ingest(kb)
        digests = _count_digests(monkeypatch)
        kb.search({"question": "warm the caches"})
        dumps = []
        original = ResolvedConfig.model_dump

        def counted(self, *args, **kwargs):
            dumps.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ResolvedConfig, "model_dump", counted)
        kb.search({"question": "verification outcome"})
    assert dumps == []
    # Each fingerprint is computed by the first query and reused by the second.
    assert digests == {"_provider_digest": 1, "_projection_digest": 1}


def test_a_copied_service_shares_the_memo(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _ingest(kb)
        service = kb._workspace.services.query
        digests = _count_digests(monkeypatch)
        for question in ("verification outcome", "warm the caches"):
            # The MCP tool copies the service per call, as here.
            copied = copy.copy(service)
            kb._workspace.bridge.call(
                copied.search(
                    QueryRequest.from_mapping({"question": question}),
                    kb.scope,
                    ExecutionContext(),
                )
            )
    # Read without the lazy fallback: the copies filled the base's own memo.
    assert vars(service)["_digests"].fingerprints
    assert digests == {"_provider_digest": 1, "_projection_digest": 1}


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

        views = []
        cached = owner.catalog.get

        def spied(tx, snapshot_id):
            views.append(cached(tx, snapshot_id))
            return views[-1]

        monkeypatch.setattr(owner.store, "resolve_snapshot", outside_catalog)
        monkeypatch.setattr(owner.catalog, "get", spied)
        replayed = kb._workspace.bridge.call(
            service.replay(retained.run_id, kb.scope, ExecutionContext())
        )
    assert replayed.result.snapshot_id == retained.snapshot_id
    assert replayed.result.segments == retained.segments
    # The retained request and the replayed search each read the one cached view.
    assert len(views) == 2
    assert views[0] is views[1]
    assert views[0].snapshot_id == retained.snapshot_id


def test_query_functions_stay_under_80_lines():
    tree = ast.parse((SOURCE / "application/query.py").read_text())
    long = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.end_lineno or node.lineno) - node.lineno + 1 > 80
    ]
    assert long == []


def _graph_workspace(tmp_path, *, degraded: bool):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.local]\nbuiltin="general-documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
        f"[configuration.retrieval]\nallow_degraded={str(degraded).lower()}\n"
    )
    return Tychora.open(
        tmp_path / "ws",
        config=resolve_config(settings, "local", {}),
        token_counter=OutputCounter(),
    )


def _no_pinned_generation(monkeypatch):
    """The configured graph is bound, but the attempt's view pins no generation."""

    async def no_scope(self, attempt, ctx):
        return None

    monkeypatch.setattr(query_module.QueryService, "_graph_scope", no_scope)


def test_an_unavailable_planned_graph_is_partial_when_degradation_is_allowed(
    tmp_path, monkeypatch
):
    with _graph_workspace(tmp_path, degraded=True) as kb:
        _ingest(kb)
        assert kb._workspace.services.query.bindings.graph is not None
        _no_pinned_generation(monkeypatch)
        result = kb.search({"question": "verification outcome"})
    assert "graph" in result.coverage.unavailable_channels
    assert result.coverage.status == "partial"


def test_an_unavailable_planned_graph_is_refused_in_strict_mode(tmp_path, monkeypatch):
    with _graph_workspace(tmp_path, degraded=False) as kb:
        _ingest(kb)
        complete = kb.search({"question": "pump seal"})
        _no_pinned_generation(monkeypatch)
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "verification outcome"})
    assert complete.coverage.status == "complete"
    assert (error.value.code, error.value.reason) == (
        "INDEX_INCOMPLETE",
        "channel.unavailable",
    )
