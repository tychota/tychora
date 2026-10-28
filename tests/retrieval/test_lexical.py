"""Safe lexical query construction and eligible SQLite ranking."""

import sqlite3

import pytest

from tests.helpers.eligibility import revision_view
from tychora.adapters.sqlite import lexical as lexical_module
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch
from tychora.contracts.errors import TychoraError
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
    assert lexical_expression("the", frozenset({"the"})) == '"the"'
    bounded = lexical_expression(" ".join(f"word{i}" for i in range(100)), frozenset())
    assert bounded is not None
    assert len(bounded.split(" OR ")) == 32


def test_explicit_phrase_retains_internal_stopwords_for_adjacency():
    expression = lexical_expression('"alpha the beta"', frozenset({"the"}))
    assert expression is not None
    assert '"alpha the beta"' in expression


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE snapshot_batches(snapshot_id TEXT, slot TEXT, batch_id TEXT);
        INSERT INTO snapshot_batches VALUES ('snapshot', 'lexical', 'old');
        CREATE TABLE evidence_units(unit_id TEXT PRIMARY KEY, extraction_id TEXT, citable INTEGER);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, unit_id TEXT, extraction_id TEXT);
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


def _record(
    db, batch, chunk, unit, body, *, asset=None, extraction="extraction", citable=1
):
    db.execute(
        "INSERT OR IGNORE INTO evidence_units VALUES (?,?,?)",
        (unit, extraction, 0 if asset is not None else citable),
    )
    db.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk, unit, extraction))
    if asset is not None:
        db.execute("INSERT INTO asset_proxies VALUES (?,?)", (unit, asset))
    db.execute(
        "INSERT INTO lexical_records(batch_id,chunk_id,identifiers,title,headings,body,context) VALUES (?,?,?,?,?,?,?)",
        (batch, chunk, "", "", "", body, ""),
    )


def test_joins_restrict_to_snapshot_batches_eligible_extractions_and_exclusions():
    db = _db()
    for index in range(100):
        _record(db, "old", f"window-{index}", "repeated", "alpha")
    _record(db, "old", "end-window", "ending", "alpha beta")
    _record(db, "old", "hidden", "hidden-owner", "alpha " * 100)
    _record(db, "old", "other", "other-owner", "alpha " * 50, extraction="other")
    _record(db, "old", "heading", "heading-owner", "alpha " * 50, citable=0)
    _record(db, "new", "unpublished", "new-owner", "alpha " * 100)
    view = revision_view(excluded_units=("hidden-owner",), all_members=False)
    result = SqliteLexicalSearch(db).search(view, "alpha", 80, ExecutionContext())
    assert {owner.key for owner in result.owners} == {
        ("unit", "repeated"),
        ("unit", "ending"),
    }
    repeated = next(o for o in result.owners if o.key[1] == "repeated")
    assert len(repeated.window_ids) == 100
    assert not result.truncated
    db.close()


def test_proxy_windows_rank_in_an_independent_asset_pool():
    db = _db()
    _record(db, "old", "source-window", "source", "café")
    _record(db, "old", "proxy-window", "proxy", "cafe", asset="asset")
    result = SqliteLexicalSearch(db).search(
        revision_view(), "cafe", 80, ExecutionContext()
    )
    assert [o.key for o in result.owners if o.key[0] == "unit"] == [("unit", "source")]
    assert [o.key for o in result.owners if o.key[0] == "asset"] == [("asset", "asset")]
    db.close()


def test_lexical_ranks_survive_owner_grouping():
    db = _db()
    for index in range(81):
        _record(db, "old", f"source-window-{index}", f"source-{index}", "alpha")
        _record(
            db,
            "old",
            f"proxy-window-{index}",
            f"proxy-{index}",
            "alpha",
            asset=f"asset-{index}",
        )
    result = SqliteLexicalSearch(db).search(
        revision_view(), "alpha", 80, ExecutionContext()
    )
    units = [o for o in result.owners if o.key[0] == "unit"]
    assets = [o for o in result.owners if o.key[0] == "asset"]
    assert len(units) == len(assets) == 80
    assert [o.rank for o in units] == list(range(1, 81))
    assert [o.rank for o in assets] == list(range(1, 81))
    assert result.detail["owners"] == 162
    db.close()


def test_window_limit_sets_truncated_not_incomplete(monkeypatch):
    monkeypatch.setattr(lexical_module, "WINDOW_LIMIT", 3)
    db = _db()
    for index in range(5):
        _record(db, "old", f"window-{index}", f"unit-{index}", "alpha")
    result = SqliteLexicalSearch(db).search(
        revision_view(), "alpha", 80, ExecutionContext()
    )
    assert result.truncated
    assert result.detail["windows"] == 3
    assert sum(len(o.window_ids) for o in result.owners) == 3
    db.close()


def test_eligible_extraction_binding_is_skipped_when_every_member_is_eligible():
    db = _db()
    _record(db, "old", "window", "unit", "alpha")
    statements = []
    db.set_trace_callback(statements.append)
    search = SqliteLexicalSearch(db)
    search.search(revision_view(all_members=True), "alpha", 80, ExecutionContext())
    search.search(revision_view(all_members=False), "alpha", 80, ExecutionContext())
    db.set_trace_callback(None)
    fts = [s for s in statements if "lexical_fts MATCH" in s]
    assert "json_each" not in fts[0]
    assert "json_each" in fts[1]
    db.close()


def test_eligible_extraction_binding_is_built_once_per_view(monkeypatch):
    from tychora.contracts import eligibility as eligibility_module

    built = []
    original = eligibility_module.json_array

    def counted(ids):
        built.append(ids)
        return original(ids)

    monkeypatch.setattr(eligibility_module, "json_array", counted)
    db = _db()
    db.executescript(
        "CREATE TABLE identifiers(unit_id TEXT, namespace TEXT, normalized_value TEXT);"
        "CREATE TABLE snapshot_members(snapshot_id TEXT, extraction_id TEXT);"
    )
    _record(db, "old", "window", "unit", "alpha")
    view = revision_view(
        excluded_units=("hidden",), unit_filter=("unit",), all_members=False
    )
    search = SqliteLexicalSearch(db)
    for _ in range(3):
        search.search(view, "alpha", 80, ExecutionContext())
        search.exact(view, ("rule:STD-1.2",), 16, ExecutionContext())
    # Extractions, exclusions and the unit filter: each serialized once.
    assert len(built) == 3
    db.close()


def test_window_limit_is_not_reached_by_exactly_that_many_windows(monkeypatch):
    monkeypatch.setattr(lexical_module, "WINDOW_LIMIT", 3)
    db = _db()
    for index in range(3):
        _record(db, "old", f"window-{index}", f"unit-{index}", "alpha")
    result = SqliteLexicalSearch(db).search(
        revision_view(), "alpha", 80, ExecutionContext()
    )
    assert not result.truncated
    assert result.detail["windows"] == 3
    db.close()


def test_candidate_filter_and_asset_exclusions_restrict_units_and_proxies():
    db = _db()
    _record(db, "old", "kept-window", "kept", "alpha")
    _record(db, "old", "filtered-window", "filtered", "alpha " * 20)
    _record(db, "old", "kept-proxy-window", "kept-proxy", "alpha", asset="kept-asset")
    _record(
        db,
        "old",
        "filtered-proxy-window",
        "filtered-proxy",
        "alpha " * 20,
        asset="filtered-asset",
    )
    _record(
        db,
        "old",
        "excluded-proxy-window",
        "excluded-proxy",
        "alpha " * 20,
        asset="excluded-asset",
    )
    view = revision_view(
        unit_filter=("kept", "kept-proxy", "excluded-proxy"),
        excluded_assets=("excluded-asset",),
    )
    result = SqliteLexicalSearch(db).search(view, "alpha", 80, ExecutionContext())
    assert {owner.key for owner in result.owners} == {
        ("unit", "kept"),
        ("asset", "kept-asset"),
    }
    db.close()


def test_a_cancelled_context_raises_before_the_search_runs():
    db = _db()
    _record(db, "old", "window", "unit", "alpha")
    statements = []
    db.set_trace_callback(statements.append)
    ctx = ExecutionContext()
    ctx.cancelled.set()
    with pytest.raises(TychoraError) as error:
        SqliteLexicalSearch(db).search(revision_view(), "alpha", 80, ctx)
    db.set_trace_callback(None)
    assert error.value.code == "CANCELLED"
    assert not [s for s in statements if "lexical_fts MATCH" in s]
    db.close()
