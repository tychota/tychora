"""Safe lexical query construction and eligible SQLite ranking."""

import sqlite3

from tychora.adapters.sqlite.lexical import LexicalIndex
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import EligibilityView
from tychora.contracts.identity import digest
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.lexical_query import lexical_expression


def test_user_query_cannot_supply_fts_operators():
    expression = lexical_expression("alpha OR beta NEAR(secret)", frozenset())
    assert expression is not None
    assert "NEAR(" not in expression
    assert '"alpha"' in expression


def test_bounded_phrases_stopwords_and_diacritics():
    expression = lexical_expression(
        '"café au lait" alpha alpha the', frozenset({"the"})
    )
    assert expression is not None
    assert '"café au lait"' in expression
    assert '"the"' not in expression
    assert expression.count('"alpha"') == 1
    assert lexical_expression("the", frozenset({"the"})) is None
    assert (
        len(
            lexical_expression(
                " ".join(f"word{i}" for i in range(100)), frozenset()
            ).split(" OR ")
        )
        == 32
    )


def test_explicit_phrase_retains_internal_stopwords_for_adjacency():
    expression = lexical_expression('"alpha the beta"', frozenset({"the"}))
    assert expression is not None
    assert '"alpha the beta"' in expression


def _view(*, chunks, batches=("old",), sources=(), proxies=(), assets=()):
    return EligibilityView(
        snapshot_id="snapshot",
        policy_epoch=0,
        scope_digest=digest("scope"),
        eligibility_policy_digest=digest("policy"),
        eligibility_request_digest=digest("request"),
        selected_batch_ids=batches,
        revision_ids=frozenset({"revision"}),
        source_unit_ids=frozenset(sources),
        context_unit_ids=frozenset(sources) | frozenset(proxies),
        structural_unit_ids=frozenset(),
        proxy_unit_ids=frozenset(proxies),
        chunk_ids=frozenset(chunks),
        asset_ids=frozenset(assets),
        vector_record_ids=frozenset(),
        blocked_assertion_ids=frozenset(),
    )


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE workspaces(workspace_id TEXT, lexical_statistics_epoch INTEGER);
        INSERT INTO workspaces VALUES ('workspace', 7);
        CREATE TABLE snapshots(snapshot_id TEXT, workspace_id TEXT);
        INSERT INTO snapshots VALUES ('snapshot', 'workspace');
        CREATE TABLE snapshot_batches(snapshot_id TEXT, slot TEXT, batch_id TEXT);
        INSERT INTO snapshot_batches VALUES ('snapshot', 'lexical', 'old');
        CREATE TABLE chunks(chunk_id TEXT, unit_id TEXT);
        CREATE TABLE asset_proxies(proxy_unit_id TEXT, asset_id TEXT);
        CREATE TABLE lexical_records(rowid INTEGER PRIMARY KEY, batch_id TEXT,
          chunk_id TEXT, identifiers TEXT, title TEXT, headings TEXT, body TEXT, context TEXT);
        CREATE VIRTUAL TABLE lexical_fts USING fts5(identifiers,title,headings,body,context,
          content='lexical_records',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER lexical_insert AFTER INSERT ON lexical_records BEGIN
          INSERT INTO lexical_fts(rowid,identifiers,title,headings,body,context)
          VALUES(new.rowid,new.identifiers,new.title,new.headings,new.body,new.context);
        END;
    """)
    return db


def _record(db, batch, chunk, unit, body, asset=None):
    db.execute("INSERT INTO chunks VALUES (?,?)", (chunk, unit))
    if asset is not None:
        db.execute("INSERT INTO asset_proxies VALUES (?,?)", (unit, asset))
    db.execute(
        "INSERT INTO lexical_records(batch_id,chunk_id,identifiers,title,headings,body,context) VALUES (?,?,?,?,?,?,?)",
        (batch, chunk, "", "", "", body, ""),
    )


def test_eligible_rows_are_scored_before_owner_limits_and_epoch_is_captured():
    db = _db()
    for index in range(100):
        _record(db, "old", f"window-{index}", "repeated", "alpha")
    _record(db, "old", "end-window", "ending", "alpha beta")
    _record(db, "old", "hidden", "hidden-owner", "alpha " * 100)
    _record(db, "new", "unpublished", "new-owner", "alpha " * 100)
    view = _view(
        chunks=tuple(f"window-{i}" for i in range(100)) + ("end-window",),
        sources=("repeated", "ending"),
    )
    result = LexicalIndex(db).search(view, "alpha", 80, ExecutionContext())
    assert {candidate.key for candidate in result.source_candidates} == {
        ("unit", "repeated"),
        ("unit", "ending"),
    }
    assert (
        len(
            next(
                candidate.matching_window_ids
                for candidate in result.source_candidates
                if candidate.key[1] == "repeated"
            )
        )
        == 100
    )
    assert result.lexical_statistics_epoch == 7
    assert result.complete
    db.close()


def test_proxy_windows_rank_in_independent_asset_pool():
    db = _db()
    _record(db, "old", "source-window", "source", "café")
    _record(db, "old", "proxy-window", "proxy", "cafe", asset="asset")
    view = _view(
        chunks=("source-window", "proxy-window"),
        sources=("source",),
        proxies=("proxy",),
        assets=("asset",),
    )
    result = LexicalIndex(db).search(view, "cafe", 80, ExecutionContext())
    assert [candidate.key for candidate in result.source_candidates] == [
        ("unit", "source")
    ]
    assert [candidate.key for candidate in result.asset_candidates] == [
        ("asset", "asset")
    ]
    db.close()


def test_epoch_change_is_visible_on_repeated_search():
    db = _db()
    _record(db, "old", "window", "source", "alpha")
    view = _view(chunks=("window",), sources=("source",))
    index = LexicalIndex(db)
    first = index.search(view, "alpha", 80, ExecutionContext())
    db.execute("UPDATE workspaces SET lexical_statistics_epoch=8")
    second = index.search(view, "alpha", 80, ExecutionContext())
    assert first.lexical_statistics_epoch == 7
    assert second.lexical_statistics_epoch == 8
    assert second.source_candidates
    db.close()


def test_cancelled_search_clears_owner_connection_temp_rows():
    db = _db()
    _record(db, "old", "window", "source", "alpha")
    view = _view(chunks=("window",), sources=("source",))
    index = LexicalIndex(db)

    class CancelAfterMaterialization:
        calls = 0

        def checkpoint(self):
            self.calls += 1
            if self.calls == 2:
                raise TychoraError("CANCELLED", "Cancelled")

    try:
        index.search(view, "alpha", 80, CancelAfterMaterialization())
    except TychoraError as error:
        assert error.code == "CANCELLED"
    else:
        raise AssertionError("search should cancel")
    assert (
        db.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'eligible_%'"
        ).fetchall()
        == []
    )
    assert index.search(view, "alpha", 80, ExecutionContext()).source_candidates
    db.close()


def test_eighty_owner_caps_are_independent_and_ranks_are_dense():
    db = _db()
    source_chunks = []
    proxy_chunks = []
    for index in range(81):
        source_chunk = f"source-window-{index}"
        proxy_chunk = f"proxy-window-{index}"
        source_chunks.append(source_chunk)
        proxy_chunks.append(proxy_chunk)
        _record(db, "old", source_chunk, f"source-{index}", "alpha")
        _record(db, "old", proxy_chunk, f"proxy-{index}", "alpha", f"asset-{index}")
    view = _view(
        chunks=(*source_chunks, *proxy_chunks),
        sources=tuple(f"source-{index}" for index in range(81)),
        proxies=tuple(f"proxy-{index}" for index in range(81)),
        assets=tuple(f"asset-{index}" for index in range(81)),
    )
    result = LexicalIndex(db).search(view, "alpha", 80, ExecutionContext())
    assert len(result.source_candidates) == len(result.asset_candidates) == 80
    assert [item.rank for item in result.source_candidates] == list(range(1, 81))
    assert [item.rank for item in result.asset_candidates] == list(range(1, 81))
    assert result.distinct_candidate_count == 162
    assert not result.complete
    db.close()
