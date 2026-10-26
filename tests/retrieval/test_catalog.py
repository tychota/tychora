"""Snapshot views come from SQL once per snapshot and carry no members."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_rows
from tests.helpers.publication import members_of
from tychora import Tychora
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.repositories import SqliteSnapshotRepository
from tychora.adapters.sqlite.repositories import snapshots as snapshot_rows
from tychora.application.snapshot_gc import SnapshotGc
from tychora.contracts.errors import TychoraError
from tychora.contracts.publication import SnapshotView
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import Cursor
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


def _snapshot_ids(owner):
    rows = owner_rows(owner, "SELECT snapshot_id FROM snapshots ORDER BY published_at")
    return [row[0] for row in rows]


def test_catalog_evicts_the_least_recently_used_view(kb, monkeypatch):
    _add(kb, "c.md", "# Gauges\n\nCalibrate gauges monthly.\n")
    first, second, third = _snapshot_ids(kb._workspace.owner)
    calls = []
    original = SqliteSnapshotRepository.view

    def counted(self, snapshot_id):
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SqliteSnapshotRepository, "view", counted)
    single = SnapshotCatalog(capacity=1)
    _read(kb, lambda tx: [single.get(tx, sid) for sid in (first, second, first)])
    assert calls == [first, second, first]
    calls.clear()
    pair = SnapshotCatalog(capacity=2)
    # Touching the first view makes the second the least recently used one.
    order = (first, second, first, third, first, second)
    _read(kb, lambda tx: [pair.get(tx, sid) for sid in order])
    assert calls == [first, second, third, second]


def test_snapshot_gc_discards_collected_views(kb, monkeypatch):
    owner = kb._workspace.owner
    _add(kb, "c.md", "# Gauges\n\nCalibrate gauges monthly.\n")
    oldest = _snapshot_ids(owner)[0]
    view = _read(kb, lambda tx: owner.catalog.get(tx, oldest))
    assert view.snapshot_id == oldest
    monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 0)
    owner_rows(owner, "DELETE FROM query_runs")
    summary = on_owner(owner, lambda: SnapshotGc(owner).collect(ExecutionContext()))
    assert summary.snapshots >= 1 and oldest not in _snapshot_ids(owner)
    with pytest.raises(TychoraError) as error:
        _read(kb, lambda tx: owner.catalog.get(tx, oldest))
    assert error.value.code == "NOT_FOUND"


def _members_page(owner, **options):
    def read():
        with owner.store.uow.read() as tx:
            return tx.snapshots.members(tx.snapshots.active_id(), **options)

    return on_owner(owner, read)


@pytest.mark.parametrize("limit", [0, 10_001])
def test_members_refuse_a_page_size_out_of_range(kb, limit):
    with pytest.raises(TychoraError) as error:
        _members_page(kb._workspace.owner, limit=limit)
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "cursor.invalid",
    )


def test_members_refuse_a_malformed_cursor(kb):
    malformed = Cursor(sort_key=("only-source",), last_id="extraction")
    with pytest.raises(TychoraError) as error:
        _members_page(kb._workspace.owner, after=malformed)
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "cursor.invalid",
    )


def test_members_page_between_revisions_of_one_source(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for edition in ("1", "2"):
            job = kb.sources.add(
                {
                    "type": "text",
                    "source_key": "manual.txt",
                    "collection_id": "default",
                    "kind": "manual@1",
                    "format": "text",
                    "text": f"Cobalt edition {edition}.",
                    "metadata": {
                        "title": "Manual",
                        "publisher": "Example",
                        "edition": edition,
                        "approval": "accepted",
                    },
                }
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        first, cursor = _members_page(owner, limit=1)
        second, end = _members_page(owner, after=cursor, limit=1)
    assert cursor is not None and end is None
    assert first[0].source_id == second[0].source_id
    assert first[0].revision_id < second[0].revision_id


def test_member_refuses_a_collected_snapshot(kb):
    owner = kb._workspace.owner

    def read():
        with owner.store.uow.read() as tx:
            return tx.snapshots.member("collected-snapshot", "extraction")

    with pytest.raises(TychoraError) as error:
        on_owner(owner, read)
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "snapshot.absent")


def test_all_members_refuse_a_count_that_differs_from_the_row(kb):
    owner = kb._workspace.owner
    view = _read(kb, lambda tx: tx.snapshots.view(tx.snapshots.active_id()))
    owner_rows(
        owner,
        "UPDATE snapshots SET member_count=member_count+1 WHERE snapshot_id=?",
        (view.snapshot_id,),
    )
    with pytest.raises(TychoraError) as error:
        on_owner(owner, lambda: members_of(owner.store, view))
    assert error.value.code == "INTEGRITY_ERROR"


def test_full_member_reads_use_primary_keys_without_sorting(kb):
    owner = kb._workspace.owner
    for statement in (snapshot_rows.PAGE_IDS, snapshot_rows.PAGE_MEMBERS):
        plan = " ".join(
            str(row[-1])
            for row in owner_rows(owner, f"EXPLAIN QUERY PLAN {statement}", ("x",))
        )
        assert "TEMP B-TREE" not in plan, plan
        assert "USING" in plan and "INDEX" in plan, plan
