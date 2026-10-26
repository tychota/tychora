"""Exact namespaced identifier retrieval."""

import sqlite3
from pathlib import Path

from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config
from tychora.contracts.candidates import ChannelCandidates
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.lexical_query import (
    extract_identifiers,
    normalize_identifier,
    retrieve_exact_lexical,
)


def _view(sources):
    return LegacyEligibilityView(
        snapshot_id="snapshot",
        policy_epoch=3,
        scope_digest=digest("scope"),
        eligibility_policy_digest=digest("policy"),
        eligibility_request_digest=digest("request"),
        selected_batch_ids=("batch",),
        revision_ids=frozenset({"revision"}),
        source_unit_ids=frozenset(sources),
        context_unit_ids=frozenset(sources),
        structural_unit_ids=frozenset(),
        proxy_unit_ids=frozenset(),
        chunk_ids=frozenset(),
        asset_ids=frozenset(),
        vector_record_ids=frozenset(),
        blocked_assertion_ids=frozenset(),
    )


def _store():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE identifiers(namespace TEXT, normalized_value TEXT, unit_id TEXT);
        CREATE TABLE evidence_units(unit_id TEXT, extraction_id TEXT);
        CREATE TABLE snapshot_members(snapshot_id TEXT, extraction_id TEXT);
        INSERT INTO snapshot_members VALUES ('snapshot','selected');
        INSERT INTO evidence_units VALUES ('allowed','selected'),('hidden','selected'),('old','old');
        INSERT INTO identifiers VALUES
          ('rule','STD-1.2','allowed'),('rule','STD-1.2','hidden'),
          ('rule','STD-1.2','old'),('code.qualified_name','Foo.bar','allowed');
    """)
    store = object.__new__(CorpusStore)
    store.db = db
    return store


class ForbiddenLexical:
    def search(
        self,
        view: LegacyEligibilityView,
        query: str,
        candidate_limit: int,
        ctx: ExecutionContext,
    ) -> ChannelCandidates:
        raise AssertionError("exact-only or invalid request reached lexical search")


def test_exact_punctuation_uses_namespace_equality_and_candidate_set():
    store = _store()
    result = store.lookup_identifiers(("rule:STD-1.2",), _view(("allowed",)), 32)
    assert [candidate.key for candidate in result.source_candidates] == [
        ("unit", "allowed")
    ]
    assert result.channel == "exact"
    assert result.policy_epoch == 3
    assert not result.asset_candidates
    store.db.close()


def test_exact_absence_does_not_expand_or_use_case_insensitive_code_alias():
    store = _store()
    view = _view(("allowed",))
    assert store.lookup_identifiers(("rule:STD-9",), view, 32).source_candidates == ()
    assert (
        store.lookup_identifiers(
            ("code.qualified_name:foo.bar",), view, 32
        ).source_candidates
        == ()
    )
    assert [
        c.key
        for c in store.lookup_identifiers(
            ("code.qualified_name:Foo.bar",), view, 32
        ).source_candidates
    ] == [("unit", "allowed")]
    store.db.close()


def test_unknown_namespace_is_rejected():
    try:
        normalize_identifier("made.up:value")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown namespace must fail")


def test_lookup_rejects_unknown_namespace_as_request_error():
    store = _store()
    try:
        store.lookup_identifiers(("made.up:value",), _view(("allowed",)), 32)
    except TychoraError as error:
        assert error.code == "INVALID_REQUEST"
    else:
        raise AssertionError("unknown namespace must fail as a request error")
    store.db.close()


def test_registered_free_text_extractors_are_bounded(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    registry = Registry.from_config(resolve_config(config_path, "test", {}))
    extracted = extract_identifiers("Find STD-1.2 and Foo.bar", registry)
    assert "rule:STD-1.2" in extracted
    assert "code.qualified_name:Foo.bar" in extracted
    assert (
        len(extract_identifiers(" ".join(f"STD-{i}" for i in range(100)), registry))
        <= 16
    )


def test_exact_only_absence_never_calls_lexical(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    registry = Registry.from_config(resolve_config(config_path, "test", {}))
    store = _store()

    channels = retrieve_exact_lexical(
        store,
        ForbiddenLexical(),
        _view(("allowed",)),
        QueryRequest.from_mapping({"question": "Find STD-9", "exact_only": True}),
        registry,
        32,
        ExecutionContext(),
    )
    assert len(channels) == 1
    assert channels[0].channel == "exact"
    assert channels[0].source_candidates == ()
    store.db.close()


def test_explicit_identifiers_over_limit_are_rejected(tmp_path: Path):
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    registry = Registry.from_config(resolve_config(config_path, "test", {}))
    store = _store()
    request = QueryRequest.from_mapping(
        {
            "question": "Find exact identifiers",
            "exact_identifiers": tuple(f"rule:STD-{number}" for number in range(17)),
            "exact_only": True,
        }
    )
    try:
        retrieve_exact_lexical(
            store,
            ForbiddenLexical(),
            _view(("allowed",)),
            request,
            registry,
            32,
            ExecutionContext(),
        )
    except TychoraError as error:
        assert error.code == "INVALID_REQUEST"
    else:
        raise AssertionError("excess explicit identifiers must be rejected")
    store.db.close()
