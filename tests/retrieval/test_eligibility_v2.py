"""The revision-level view admits exactly what the unit-level compiler admitted."""

import random
from datetime import date

import grimp
import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.collections import CollectionCreate
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_context_unit,
    is_eligible_asset,
    is_source_unit,
    is_structural_unit,
)
from tychora.retrieval.cache import EligibilityCache
from tychora.retrieval.compiler import EligibilityCompiler
from tychora.retrieval.eligibility import LegacyEligibilityCompiler

ITERATIONS = 500
MUTATE_EVERY = 25

_LAW = "Article 1\nThe operator inspects seals.\n\nArticle 2\nThe operator records results.\n"
_STANDARD = "STD-1 Seals are inspected quarterly.\nSTD-2 Results are recorded.\n"
_MANUAL = "# Pumps\n\nInspect the pump seal.\n\n## Valves\n\nReplace worn valves.\n"
_LAW_META = {"publisher": "State", "jurisdiction": "EU", "publication_date": None}
_SOURCES = (
    # (collection, key, kind, text, metadata); each kind's schema requires its keys.
    ("default", "law.txt", "law@1", _LAW, {**_LAW_META, "edition": "1", "effective_start": "2020-01-01", "effective_end": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "law.txt", "law@1", _LAW + "Article 3\nNew duty.\n", {**_LAW_META, "edition": "2", "effective_start": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "std.txt", "standard@1", _STANDARD, {"issuer": "ISO", "edition": "1", "publication_date": "2021-05-01", "approval": "accepted"}),
    ("default", "std.txt", "standard@1", _STANDARD + "STD-3 Added rule.\n", {"issuer": "ISO", "edition": "2", "publication_date": "2022-05-01"}),
    ("default", "manual.md", "manual@1", _MANUAL, {"publisher": "Acme", "edition": "1", "scope": {"project": "beta"}}),
    ("default", "undated.md", "manual@1", _MANUAL.replace("Pumps", "Fans"), {"publisher": "Acme", "edition": "1"}),
    ("second", "notes.md", "rationale@1", "# Why\n\nSeals fail early.\n", {"author": "Ops", "reference": "R-1", "scope": {"project": "alpha"}}),
    ("second", "guide.md", "guidance@1", "# Guide\n\nCheck seals.\n", {"publisher": "Acme", "edition": "1", "approval": "rejected"}),
    ("second", "manual.md", "manual@1", _MANUAL.replace("Valves", "Hoses"), {"publisher": "Acme", "edition": "1", "publication_date": "2024-02-01"}),
)  # fmt: skip


def _add(kb, collections, collection, key, kind, text, metadata):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": collections[collection],
            "kind": kind,
            "format": "markdown" if key.endswith(".md") else "text",
            "text": text,
            "metadata": {"title": key, **metadata},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def _build(path):
    kb = Tychora.open(path, token_counter=OutputCounter())
    second = kb._workspace.bridge.call(
        kb._workspace.services.collections.create(
            CollectionCreate(title="Second", slug="second"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
    )
    collections = {
        "default": str(kb._workspace.default_collection_id),
        "second": str(second.collection_id),
    }
    for row in _SOURCES:
        _add(kb, collections, *row)
    return kb, collections


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """The corpus the equivalence test mutates; no other test reads it."""
    kb, collections = _build(tmp_path_factory.mktemp("equivalence") / "ws")
    with kb:
        yield kb, collections


@pytest.fixture(scope="module")
def stable(tmp_path_factory):
    """A separate corpus for the cache tests, never mutated by the equivalence test."""
    kb, collections = _build(tmp_path_factory.mktemp("stable") / "ws")
    with kb:
        yield kb, collections


def _scope(rng, collections):
    chosen = frozenset(c for c in collections.values() if rng.random() < 0.8)
    return RequestScope(
        principal_id="equivalence",
        collection_ids=chosen or frozenset(collections.values()),
        access_ceiling=rng.randint(0, 3),
        uses=frozenset(u for u in ("compose", "audit", "lookup") if rng.random() < 0.7)
        or frozenset({"lookup"}),
        capabilities=frozenset({"read"}),
    )


def _request(rng, revision_ids):
    value = {
        "question": "seal inspection",
        "mode": rng.choice(["compose", "audit", "lookup"]),
    }
    if rng.random() < 0.3:
        value["filters"] = rng.choice(
            [
                {"op": "eq", "field": "project", "value": "alpha"},
                {"op": "in", "field": "project", "values": ["alpha", "beta"]},
            ]
        )
        value["include_global"] = rng.random() < 0.5
    if rng.random() < 0.3:
        value["as_of"] = date(
            rng.randint(2019, 2025), rng.randint(1, 12), 1
        ).isoformat()
    if rng.random() < 0.2:
        value["cutoff"] = date(rng.randint(2020, 2025), 6, 1).isoformat()
    if rng.random() < 0.2:
        value["revision_selector"] = "all"
    if rng.random() < 0.2:
        value["unknown_dates"] = "permissive"
    if rng.random() < 0.1 and revision_ids:
        value["revision_ids"] = rng.sample(sorted(revision_ids), 1)
    return QueryRequest.from_mapping(value)


def _raise_floor(owner, rng, revision):
    """Raise the revision's source floor; a floor never silently decreases."""
    (floor,) = owner.db.execute(
        "SELECT s.access_floor FROM sources s JOIN revisions r USING(source_id) "
        "WHERE r.revision_id=?",
        (revision,),
    ).fetchone()
    rank = rng.randint(1, 2)
    if rank >= floor:
        owner.store.raise_access_floor(ReviewTarget(revision_id=revision), rank)


def _mutate(rng, owner):
    store = owner.store
    if rng.random() < 0.5:
        unit_id = rng.choice(
            [r[0] for r in owner.db.execute("SELECT unit_id FROM evidence_units")]
        )
        target = ReviewTarget(unit_id=unit_id)
    else:
        revision = rng.choice(
            [r[0] for r in owner.db.execute("SELECT revision_id FROM revisions")]
        )
        target = ReviewTarget(revision_id=revision)
    choice = rng.random()
    if choice < 0.6:
        store.record_review(
            target, rng.choice(["accepted", "needs_review", "rejected"]), "eq"
        )
    elif choice < 0.8 and target.revision_id is not None:
        _raise_floor(owner, rng, target.revision_id)
    else:
        source = rng.choice(
            [r[0] for r in owner.db.execute("SELECT source_id FROM sources")]
        )
        store.set_allowed_uses_override(
            source, rng.choice(["never_generate", "reference_only", None])
        )


def _legacy(owner, legacy, snapshot, scope, request):
    return owner.run_write_sync(
        lambda: legacy.compile_eligibility(snapshot, scope, request, owner.config),
        ExecutionContext(),
    )


def test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures(corpus):
    kb, collections = corpus
    owner = kb._workspace.owner
    bridge = kb._workspace.bridge
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    legacy = LegacyEligibilityCompiler(owner.store)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))

    def facts(tx):
        units = tuple(
            r[0]
            for r in tx.eligibility._db.execute("SELECT unit_id FROM evidence_units")
        )
        chunks = tuple(
            r[0]
            for r in tx.eligibility._db.execute(
                "SELECT r.chunk_id FROM snapshot_batches sb JOIN lexical_records r "
                "ON r.batch_id=sb.batch_id WHERE sb.snapshot_id=? AND sb.slot='lexical'",
                (snapshot.snapshot_id,),
            )
        )
        return {f.owner_id: f for f in tx.eligibility.owner_facts(units + chunks)}

    by_id = bridge.call(owner.run_read(facts, ExecutionContext()))
    revision_ids = {
        r[0] for r in owner_rows(owner, "SELECT revision_id FROM revisions")
    }
    rng = random.Random(20261023)
    compared = 0
    for iteration in range(ITERATIONS):
        if iteration and iteration % MUTATE_EVERY == 0:
            owner.run_write_sync(lambda: _mutate(rng, owner), ExecutionContext())
        scope, request = _scope(rng, collections), _request(rng, revision_ids)
        try:
            v1 = _legacy(owner, legacy, snapshot, scope, request)
        except TychoraError as error:
            with pytest.raises(TychoraError) as v2_error:
                bridge.call(
                    owner.run_read(
                        lambda tx, scope=scope, request=request: compiler.compile(
                            tx, snapshot, scope, request, owner.config
                        ),
                        ExecutionContext(),
                    )
                )
            assert v2_error.value.code == error.code
            continue
        v2 = bridge.call(
            owner.run_read(
                lambda tx, scope=scope, request=request: (
                    compiler.compile(tx, snapshot, scope, request, owner.config).view
                ),
                ExecutionContext(),
            )
        )
        units = [f for f in by_id.values() if f.kind == "unit"]
        chunks = [f for f in by_id.values() if f.kind == "chunk"]
        assert set(v2.revisions) == v1.revision_ids, (iteration, request)
        assert {
            f.owner_id for f in units if is_source_unit(v2, f)
        } == v1.source_unit_ids
        assert {
            f.owner_id for f in units if is_context_unit(v2, f)
        } == v1.context_unit_ids
        assert {
            f.owner_id for f in units if is_structural_unit(v2, f)
        } == v1.structural_unit_ids
        assert {
            f.owner_id for f in chunks if is_admitted_chunk(v2, f, by_id)
        } == v1.chunk_ids
        assert {
            f.owner_id for f in by_id.values() if is_eligible_asset(v2, f)
        } == v1.asset_ids
        compared += 1
    assert compared > ITERATIONS // 2


def test_cache_hit_across_different_questions_same_filters(stable, monkeypatch):
    kb, _ = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    calls = []
    original = compiler._compile

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_compile", counted)
    for question in ("seal inspection", "worn valves"):
        request = QueryRequest.from_mapping({"question": question})
        kb._workspace.bridge.call(
            owner.run_read(
                lambda tx, request=request: compiler.compile_cached(
                    tx, cache, snapshot, owner.scope, request, owner.config
                ),
                ExecutionContext(),
            )
        )
    assert len(calls) == 1


def test_cache_miss_after_epoch_bump(stable):
    kb, _ = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping({"question": "seal"})

    def compiled():
        return kb._workspace.bridge.call(
            owner.run_read(
                lambda tx: compiler.compile_cached(
                    tx, cache, snapshot, owner.scope, request, owner.config
                ),
                ExecutionContext(),
            )
        )

    first = compiled()
    extraction = min(first.view.extraction_ids)
    row = owner_row(
        owner,
        "SELECT unit_id FROM evidence_units WHERE extraction_id=? ORDER BY unit_id LIMIT 1",
        (extraction,),
    )
    assert row is not None
    (unit_id,) = row
    on_owner(
        owner,
        lambda: owner.store.record_review(
            ReviewTarget(unit_id=unit_id), "rejected", "r"
        ),
    )
    second = compiled()
    assert second.view.policy_epoch == first.view.policy_epoch + 1
    assert unit_id in second.view.excluded_unit_ids
    assert unit_id not in first.view.excluded_unit_ids


def test_compiler_never_calls_prepare_extraction(stable, monkeypatch):
    from tychora.indexing import representations

    monkeypatch.setattr(
        representations,
        "prepare_extraction",
        lambda *a, **k: pytest.fail("compile derived representation facts"),
    )
    kb, _ = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping({"question": "seal"})
    view = kb._workspace.bridge.call(
        owner.run_read(
            lambda tx: (
                compiler.compile(tx, snapshot, owner.scope, request, owner.config).view
            ),
            ExecutionContext(),
        )
    )
    assert view.revisions


def test_compiler_never_imports_the_representation_derivation():
    graph = grimp.build_graph("tychora", exclude_type_checking_imports=True)
    assert not graph.chain_exists(
        importer="tychora.retrieval.compiler",
        imported="tychora.indexing.representations",
    )
