"""Snapshot views come from SQL once per snapshot and carry no members."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner
from tests.helpers.publication import members_of
from tychora import Tychora
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.repositories import SqliteSnapshotRepository
from tychora.contracts.publication import SnapshotView
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.catalog import CATALOG_SIZE, SnapshotCatalog


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        _add(opened, "a.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        _add(opened, "b.md", "# Valves\n\nReplace worn valves yearly.\n")
        yield opened


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_snapshot_view_has_no_members():
    assert "members" not in SnapshotView.model_fields
    assert "lexical_statistics_epoch" not in SnapshotView.model_fields


def test_catalog_builds_view_from_sql_without_artifacts(kb, monkeypatch):
    owner = kb._workspace.owner

    def refuse(self, ref):
        raise AssertionError("the catalog opened an artifact")

    monkeypatch.setattr(LocalArtifacts, "open_verified", refuse)
    catalog = SnapshotCatalog()

    def read(tx):
        return catalog.get(tx, tx.snapshots.active_id())

    view = _read(kb, read)
    assert view.member_count == 2
    assert len(on_owner(owner, lambda: members_of(owner.store, view))) == 2


def test_catalog_reuses_views_and_evicts_least_recent(kb, monkeypatch):
    calls = []
    original = SqliteSnapshotRepository.view

    def counted(self, snapshot_id):
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SqliteSnapshotRepository, "view", counted)
    catalog = SnapshotCatalog(capacity=1)

    def read(tx):
        active = tx.snapshots.active_id()
        first = catalog.get(tx, active)
        again = catalog.get(tx, active)
        return first, again

    first, again = _read(kb, read)
    assert first is again
    assert len(calls) == 1
    catalog.discard((first.snapshot_id,))
    _read(kb, read)
    assert len(calls) == 2
    assert CATALOG_SIZE == 32


def test_query_resolves_snapshot_once(kb, monkeypatch):
    calls = []
    original = SqliteSnapshotRepository.view

    def counted(self, snapshot_id):
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SqliteSnapshotRepository, "view", counted)
    kb.search({"question": "pump seal"})
    assert len(calls) == 1
    kb.search({"question": "worn valves"})
    assert len(calls) == 1


def test_members_page_by_keyset(kb):
    owner = kb._workspace.owner

    def read():
        with owner.store.uow.read() as tx:
            active = tx.snapshots.active_id()
            first, cursor = tx.snapshots.members(active, limit=1)
            second, end = tx.snapshots.members(active, after=cursor, limit=1)
            member = tx.snapshots.member(active, second[0].extraction_id)
            missing = tx.snapshots.member(active, "missing")
        return first, cursor, second, end, member, missing

    first, cursor, second, end, member, missing = on_owner(owner, read)
    assert cursor is not None and end is None
    assert missing is None
    assert len(first) == len(second) == 1
    assert first[0] != second[0]
    assert member == second[0]
