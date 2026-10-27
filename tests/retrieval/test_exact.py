"""Exact namespaced identifier retrieval."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.eligibility import revision_view
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.lexical_query import (
    extract_identifiers,
    normalize_identifier,
    retrieve_exact_lexical,
)


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE identifiers(namespace TEXT, normalized_value TEXT, unit_id TEXT);
        CREATE TABLE evidence_units(unit_id TEXT, extraction_id TEXT, citable INTEGER);
        CREATE TABLE asset_proxies(proxy_unit_id TEXT, asset_id TEXT);
        CREATE TABLE snapshot_members(snapshot_id TEXT, extraction_id TEXT);
        INSERT INTO snapshot_members VALUES ('snapshot','extraction');
        INSERT INTO evidence_units VALUES
          ('allowed','extraction',1),('hidden','extraction',1),('old','old',1),
          ('heading','extraction',0);
        INSERT INTO identifiers VALUES
          ('rule','STD-1.2','allowed'),('rule','STD-1.2','hidden'),
          ('rule','STD-1.2','old'),('rule','STD-1.2','heading'),
          ('code.qualified_name','Foo.bar','allowed');
    """)
    return db


class ForbiddenSearch(SqliteLexicalSearch):
    def search(self, view, query, limit, ctx):
        raise AssertionError("exact-only or invalid request reached lexical search")


def _tx(lexical):
    return SimpleNamespace(
        lexical=lexical, workspace=SimpleNamespace(lexical_epoch=lambda: 0)
    )


def _registry(tmp_path: Path) -> Registry:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    return Registry.from_config(resolve_config(config_path, "test", {}))


def test_exact_uses_namespace_equality_and_the_view():
    db = _db()
    view = revision_view(excluded_units=("hidden",))
    result = SqliteLexicalSearch(db).exact(
        view, ("rule:STD-1.2",), 32, ExecutionContext()
    )
    assert [o.key for o in result.owners] == [("unit", "allowed")]
    assert not result.truncated
    db.close()


def test_exact_absence_does_not_expand_or_use_case_insensitive_code_alias():
    db = _db()
    search = SqliteLexicalSearch(db)
    view = revision_view()

    def keys(identifier):
        return [
            o.key
            for o in search.exact(view, (identifier,), 32, ExecutionContext()).owners
        ]

    assert keys("rule:STD-9") == []
    assert keys("code.qualified_name:foo.bar") == []
    assert keys("code.qualified_name:Foo.bar") == [("unit", "allowed")]
    db.close()


def test_33_identifier_hits_are_complete_and_truncated():
    db = _db()
    for index in range(33):
        db.execute(
            "INSERT INTO evidence_units VALUES (?,?,1)", (f"u{index:02}", "extraction")
        )
        db.execute(
            "INSERT INTO identifiers VALUES ('rule','STD-9',?)", (f"u{index:02}",)
        )
    result = SqliteLexicalSearch(db).exact(
        revision_view(), ("rule:STD-9",), 32, ExecutionContext()
    )
    assert len(result.owners) == 32
    assert result.truncated
    assert result.detail["hits"] == 33
    db.close()


def test_unknown_namespace_is_rejected():
    with pytest.raises(ValueError):
        normalize_identifier("made.up:value")


def test_unknown_namespace_is_refused_with_a_reason():
    db = _db()
    with pytest.raises(TychoraError) as error:
        SqliteLexicalSearch(db).exact(
            revision_view(), ("made.up:value",), 32, ExecutionContext()
        )
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.reason == "identifier.namespace_unknown"
    db.close()


def test_registered_free_text_extractors_are_bounded(tmp_path: Path):
    registry = _registry(tmp_path)
    extracted = extract_identifiers("Find STD-1.2 and Foo.bar", registry)
    assert "rule:STD-1.2" in extracted
    assert "code.qualified_name:Foo.bar" in extracted
    assert (
        len(extract_identifiers(" ".join(f"STD-{i}" for i in range(100)), registry))
        <= 16
    )


def test_exact_only_absence_never_calls_lexical(tmp_path: Path):
    db = _db()
    channels = retrieve_exact_lexical(
        _tx(ForbiddenSearch(db)),
        revision_view(),
        QueryRequest.from_mapping({"question": "Find STD-9", "exact_only": True}),
        _registry(tmp_path),
        32,
        ExecutionContext(),
    )
    assert len(channels) == 1
    assert channels[0].channel == "exact"
    assert channels[0].source_candidates == ()
    assert channels[0].complete
    db.close()


def test_explicit_identifiers_over_limit_are_rejected(tmp_path: Path):
    db = _db()
    request = QueryRequest.from_mapping(
        {
            "question": "Find exact identifiers",
            "exact_identifiers": tuple(f"rule:STD-{number}" for number in range(17)),
            "exact_only": True,
        }
    )
    with pytest.raises(TychoraError) as error:
        retrieve_exact_lexical(
            _tx(ForbiddenSearch(db)),
            revision_view(),
            request,
            _registry(tmp_path),
            32,
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"
    db.close()
