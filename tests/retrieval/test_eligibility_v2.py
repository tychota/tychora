"""The revision-level view admits exactly what the unit-level compiler admitted."""

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import grimp
import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tests.retrieval.eligibility_corpus import (
    GLOBAL_KEY,
    MODES,
    SOURCES,
    Corpus,
    add_text,
    build,
    global_metadata,
    own_states,
    snapshot_facts,
)
from tychora import Tychora
from tychora.configuration.registry import compile_filter
from tychora.contracts.collections import CollectionCreate, CollectionEdit
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_admitted_record,
    is_candidate_asset,
    is_candidate_proxy,
    is_context_unit,
    is_eligible_asset,
    is_eligible_unit,
    is_source_unit,
    is_structural_unit,
)
from tychora.retrieval.cache import EligibilityCache
from tychora.retrieval.compiler import EligibilityCompiler
from tychora.retrieval.eligibility import LegacyEligibilityCompiler

ITERATIONS = 500
_REVIEW_STATES = ("unreviewed", "needs_review", "accepted", "rejected")
_FILTERS = (
    {"op": "eq", "field": "project", "value": "alpha"},
    {"op": "in", "field": "project", "values": ["alpha", "beta"]},
    {"op": "eq", "field": "language", "value": "python"},
    {"op": "eq", "field": "symbol_kind", "value": "function"},
    {"op": "range", "field": "published", "lower": "2021-01-01", "upper": "2023-01-01"},
    {
        "op": "and",
        "children": [
            {"op": "in", "field": "project", "values": ["alpha", "gamma"]},
            {"op": "eq", "field": "symbol_kind", "value": "class"},
        ],
    },
)
# Each must hold in at least this many compared iterations (errors: per reason).
_MINIMUMS = {
    "compared": 300,
    "excluded_units": 50,
    "unit_filter": 20,
    "assets": 30,
    "proxies": 20,
    "candidate_proxies": 10,
    "structural": 20,
    "records": 100,
    "closure": 10,
    "rejected_revision_proxy": 3,
    "request.limit_exceeded": 3,
    "mode.unavailable": 3,
}


# ---------------------------------------------------------------------------
# Randomized requests and production policy writes


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
    value = {"question": "seal inspection", "mode": rng.choice(MODES)}
    if rng.random() < 0.45:
        value["filters"] = rng.choice(_FILTERS)
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
        value["revision_ids"] = [rng.choice(revision_ids)]
    draw = rng.random()
    if draw < 0.025:
        value["limit"] = 50  # the profile allows 8
    elif draw < 0.05:
        value["mode"] = "unregistered"
    return QueryRequest.from_mapping(value)


@dataclass
class _Run:
    """The workspace under test, its pinned snapshot and what the loop observed."""

    corpus: Corpus
    rng: random.Random
    snapshot: object = None
    facts: dict = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    withdrawn: bool = False
    archived: bool = False
    edition: int = 1

    @property
    def owner(self):
        return self.corpus.kb._workspace.owner

    def call(self, coroutine):
        return self.corpus.kb._workspace.bridge.call(coroutine)

    def write(self, call):
        return self.owner.run_write_sync(call, ExecutionContext())

    def refresh(self):
        """Pin the active snapshot and read its owners' facts again."""
        owner = self.owner
        self.snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
        snapshot_id = self.snapshot.snapshot_id
        self.facts = self.call(
            owner.run_read(
                lambda tx: snapshot_facts(tx, snapshot_id), ExecutionContext()
            )
        )


# Identifiers derive from random collection IDs, so every draw is over rows in
# a stable order (collection slug, source key, edition, ordinal) and a seed
# replays the same run.
_SOURCES_SQL = (
    "SELECT s.source_id,s.access_floor FROM sources s "
    "JOIN collections c USING(collection_id) ORDER BY c.slug,s.source_key"
)
_REVISIONS_SQL = (
    "SELECT r.revision_id FROM revisions r JOIN sources s USING(source_id) "
    "JOIN collections c USING(collection_id) ORDER BY c.slug,s.source_key,r.edition_key"
)
_UNITS_SQL = (
    "SELECT u.unit_id FROM evidence_units u JOIN extractions e USING(extraction_id) "
    "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
    "JOIN collections c USING(collection_id) "
    "ORDER BY c.slug,s.source_key,r.edition_key,u.ordinal"
)


def _ids(run, sql):
    return [row[0] for row in owner_rows(run.owner, sql)]


def _review(run):
    rng, figure = run.rng, run.corpus.figure
    draw = rng.random()
    if draw < 0.25:
        # The figure's revision alternates between rejected and readable, so a
        # mode admitting rejected originals meets its proxy under a rejection.
        state = rng.choice(("rejected", "accepted", "unreviewed"))
        target = ReviewTarget(revision_id=figure.revision)
        run.write(lambda: run.owner.store.record_review(target, state, "equivalence"))
        return
    if draw < 0.45:
        units = [figure.source, figure.proxy, figure.declared]
        target = ReviewTarget(unit_id=rng.choice(units))
    elif draw < 0.5:
        unit = rng.choice(_ids(run, _UNITS_SQL))
        target = ReviewTarget(unit_id=unit)
    elif draw < 0.65:
        target = ReviewTarget(asset_id=figure.asset)
    else:
        revision = rng.choice(_ids(run, _REVISIONS_SQL))
        target = ReviewTarget(revision_id=revision)
    # Every state is drawn; admissible ones more often, so exclusions also recover.
    state = rng.choices(_REVIEW_STATES, (3, 2, 3, 1))[0]
    run.write(lambda: run.owner.store.record_review(target, state, "equivalence"))


def _source_floor(run):
    source, floor = run.rng.choice(owner_rows(run.owner, _SOURCES_SQL))
    revision = owner_row(
        run.owner, "SELECT revision_id FROM revisions WHERE source_id=?", (source,)
    )
    assert revision is not None
    rank = min(3, floor + run.rng.randint(0, 1))
    run.write(
        lambda: run.owner.store.raise_access_floor(
            ReviewTarget(revision_id=revision[0]), rank
        )
    )


def _override(run):
    source = run.rng.choice(_ids(run, _SOURCES_SQL))
    override = run.rng.choice(["never_generate", "reference_only", None, None])
    run.write(lambda: run.owner.store.set_allowed_uses_override(source, override))


def _withdraw(run):
    if run.withdrawn:
        return _override(run)
    run.withdrawn = True
    source = run.rng.choice(_ids(run, _SOURCES_SQL))
    return run.write(lambda: run.owner.store.withdraw_source(source))


def _archive(run):
    run.archived = not run.archived
    services = run.corpus.kb._workspace.services
    run.call(
        services.collections.set_archived(
            run.corpus.collections["second"],
            run.archived,
            run.corpus.kb.scope,
            ExecutionContext(),
        )
    )


def _collection_policy(run):
    services = run.corpus.kb._workspace.services
    kind = run.rng.choice([None, "manual@1", "guidance@1"])
    run.call(
        services.collections.edit(
            run.corpus.collections["second"],
            CollectionEdit(title="Second", slug="second", default_kind_id=kind),
            run.corpus.kb.scope,
            ExecutionContext(),
        )
    )


def _consent(run):
    action = run.rng.choice(["grant", "revoke"])
    run.corpus.kb.management.run(
        {
            "operation": "provider-upload-consent",
            "collection_id": run.corpus.collections["second"],
            "provider": "voyage",
            "action": action,
        }
    )


def _facet_binding(run):
    """Publish a new edition whose global-facet binding flips; the snapshot moves."""
    run.edition += 1
    add_text(
        run.corpus.kb,
        run.corpus.collections["second"],
        GLOBAL_KEY,
        "manual@1",
        f"# Shared\n\nSeals apply to every project, edition {run.edition}.\n",
        global_metadata(run.edition, run.edition % 2 == 1),
    )
    run.refresh()


_MUTATIONS = (
    (_review, 8),
    (_source_floor, 2),
    (_override, 3),
    (_withdraw, 1),
    (_archive, 2),
    (_collection_policy, 1),
    (_consent, 1),
    (_facet_binding, 2),
)


def _mutate(run):
    names, weights = zip(*_MUTATIONS, strict=True)
    mutation = run.rng.choices(names, weights)[0]
    if mutation is _facet_binding and (run.edition >= 4 or run.archived):
        mutation = _review  # an archived collection admits no new edition
    mutation(run)
    run.counts[f"mutation:{mutation.__name__}"] += 1


# ---------------------------------------------------------------------------
# Comparison


def _compare(run, v1, v2, request, registry, context):
    facts = run.facts
    units = [f for f in facts.values() if f.kind == "unit"]
    chunks = [f for f in facts.values() if f.kind == "chunk"]
    assets = [f for f in facts.values() if f.kind == "asset"]
    records = [f for f in facts.values() if f.kind == "record"]

    def admitted(predicate, items):
        return {f.owner_id for f in items if predicate(v2, f, facts)}

    assert set(v2.revisions) == v1.revision_ids, context
    assert admitted(is_source_unit, units) == v1.source_unit_ids, context
    assert admitted(is_context_unit, units) == v1.context_unit_ids, context
    assert admitted(is_structural_unit, units) == v1.structural_unit_ids, context
    eligible_proxies = {
        f.owner_id for f in units if f.proxy and is_eligible_unit(v2, f)
    }
    assert eligible_proxies == v1.proxy_unit_ids, context
    assert admitted(is_admitted_chunk, chunks) == v1.chunk_ids, context
    assert admitted(is_eligible_asset, assets) == v1.asset_ids, context
    assert admitted(is_admitted_record, records) == v1.vector_record_ids, context
    # v1 keeps candidate proxies and assets internal: a candidate proxy is one
    # whose windows it admits, and a candidate asset has an eligible source
    # unit passing the candidate filter (the figure's source unit is citable).
    windowed = {facts[c.unit_id].owner_id for c in chunks if c.unit_id in facts}
    v1_candidate_proxies = {
        facts[chunk].unit_id
        for chunk in v1.chunk_ids
        if facts[facts[chunk].unit_id].proxy
    }
    candidate_proxies = admitted(is_candidate_proxy, units) & windowed
    assert candidate_proxies == v1_candidate_proxies, context
    selected = compile_filter(request.filters, registry) if request.filters else None
    v1_candidate_assets = {
        a.owner_id
        for a in assets
        if a.owner_id in v1.asset_ids
        and (
            selected is None
            or selected.matched_candidates is None
            or any(
                owner in a.dependency_ids and owner in v1.source_unit_ids
                for owner in a.source_owner_ids
            )
        )
    }
    assert admitted(is_candidate_asset, assets) == v1_candidate_assets, context
    return candidate_proxies


def _count(run, v1, v2, candidate_proxies, mode, *, filtered):
    counts, figure = run.counts, run.corpus.figure
    counts["compared"] += 1
    counts["excluded_units"] += bool(v2.excluded_unit_ids)
    counts["unit_filter"] += v2.unit_filter is not None
    counts["assets"] += bool(v1.asset_ids)
    counts["proxies"] += bool(v1.proxy_unit_ids)
    counts["candidate_proxies"] += bool(candidate_proxies)
    counts["structural"] += bool(v1.structural_unit_ids)
    counts["records"] += bool(v1.vector_record_ids)
    # The closure is larger than its seeds when the figure's asset is excluded
    # only because its source unit is, its own state being admissible.
    states = own_states(run.owner, figure)
    asset_admissible = states[figure.asset] in mode.original_reviews
    counts["closure"] += (
        figure.source in v2.excluded_unit_ids
        and figure.asset in v2.excluded_asset_ids
        and asset_admissible
    )
    # A mode admitting rejected originals reads a rejected revision, whose proxy
    # is excluded by the revision's rejection alone: its own state is admissible
    # and no filter applies.
    counts["rejected_revision_proxy"] += bool(
        figure.revision in v2.revisions
        and states[figure.revision] == "rejected"
        and states[figure.proxy] in mode.proxy_reviews
        and not mode.proxy_disabled
        and not filtered
    )


def _compile_both(run, legacy, compiler, cache, scope, request):
    owner = run.owner
    snapshot = run.snapshot
    try:
        v1 = owner.run_write_sync(
            lambda: legacy.compile_attempt(snapshot, scope, request, owner.config).view,
            ExecutionContext(),
        )
    except TychoraError as error:
        with pytest.raises(TychoraError) as v2_error:
            run.call(
                owner.run_read(
                    lambda tx: compiler.compile_cached(
                        tx, cache, snapshot, scope, request, owner.config
                    ),
                    ExecutionContext(),
                )
            )
        assert (v2_error.value.code, v2_error.value.reason) == (
            error.code,
            error.reason,
        ), request
        run.counts[error.reason or error.code] += 1
        return None
    v2 = run.call(
        owner.run_read(
            lambda tx: (
                compiler.compile_cached(
                    tx, cache, snapshot, scope, request, owner.config
                ).view
            ),
            ExecutionContext(),
        )
    )
    return v1, v2


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """The rich corpus the equivalence test mutates; no other test reads it."""
    built = build(tmp_path_factory.mktemp("equivalence"))
    with built.kb:
        yield built


def test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures(corpus):
    owner = corpus.kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    legacy = LegacyEligibilityCompiler(owner.store)
    cache = EligibilityCache()
    run = _Run(corpus, random.Random(20261023))
    run.refresh()
    next_mutation = run.rng.randint(3, 5)
    for iteration in range(ITERATIONS):
        if iteration == next_mutation:
            _mutate(run)
            next_mutation += run.rng.randint(3, 5)
        revision_ids = _ids(run, _REVISIONS_SQL)
        scope = _scope(run.rng, corpus.collections)
        request = _request(run.rng, revision_ids)
        both = _compile_both(run, legacy, compiler, cache, scope, request)
        if both is None:
            continue
        v1, v2 = both
        context = (iteration, request, scope)
        candidate_proxies = _compare(
            run, v1, v2, request, owner.store.registry, context
        )
        mode = compiler.mode(owner.config, request, scope)
        _count(run, v1, v2, candidate_proxies, mode, filtered=bool(request.filters))
    shortfalls = {
        name: (run.counts[name], minimum)
        for name, minimum in _MINIMUMS.items()
        if run.counts[name] < minimum
    }
    assert not shortfalls, (shortfalls, run.counts)
    for mutation, _ in _MUTATIONS:
        assert run.counts[f"mutation:{mutation.__name__}"], (mutation, run.counts)


# ---------------------------------------------------------------------------
# Cache and isolation, on a corpus the equivalence test never mutates


@pytest.fixture(scope="module")
def stable(tmp_path_factory):
    """A plain documents corpus for the cache tests."""
    path = tmp_path_factory.mktemp("stable") / "ws"
    with Tychora.open(path, token_counter=OutputCounter()) as kb:
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
        for collection, key, kind, text, metadata in SOURCES:
            add_text(kb, collections[collection], key, kind, text, metadata)
        yield kb, collections


def _counted(compiler, monkeypatch):
    calls = []
    original = compiler._compile

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, "_compile", counted)
    return calls


def _cached(kb, compiler, cache, snapshot, scope, request):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(
        owner.run_read(
            lambda tx: compiler.compile_cached(
                tx, cache, snapshot, scope, request, owner.config
            ),
            ExecutionContext(),
        )
    )


def test_cache_hit_across_different_questions_same_filters(stable, monkeypatch):
    kb, _ = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    calls = _counted(compiler, monkeypatch)
    for question in ("seal inspection", "worn valves"):
        request = QueryRequest.from_mapping({"question": question})
        _cached(kb, compiler, cache, snapshot, owner.scope, request)
    assert len(calls) == 1


def test_cache_misses_on_ceiling_collections_and_filter(stable, monkeypatch):
    kb, collections = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    calls = _counted(compiler, monkeypatch)
    base = owner.scope.model_copy(
        update={"collection_ids": frozenset(collections.values()), "access_ceiling": 3}
    )
    plain = QueryRequest.from_mapping({"question": "seal"})
    filtered = QueryRequest.from_mapping(
        {
            "question": "seal",
            "filters": {"op": "eq", "field": "project", "value": "alpha"},
        }
    )
    variants = (
        (base, plain),
        (base.model_copy(update={"access_ceiling": 1}), plain),
        (
            base.model_copy(
                update={"collection_ids": frozenset({collections["default"]})}
            ),
            plain,
        ),
        (base, filtered),
    )
    keys = set()
    for scope, request in variants:
        compiled = _cached(kb, compiler, cache, snapshot, scope, request)
        keys.add((compiled.view.scope_digest, compiled.view.eligibility_request_digest))
    assert len(calls) == len(variants)
    assert len(keys) == len(variants)
    for scope, request in variants:
        _cached(kb, compiler, cache, snapshot, scope, request)
    assert len(calls) == len(variants)


def test_cache_miss_after_epoch_bump(stable):
    kb, _ = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping({"question": "seal"})
    first = _cached(kb, compiler, cache, snapshot, owner.scope, request)
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
    second = _cached(kb, compiler, cache, snapshot, owner.scope, request)
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


def test_only_retained_inspection_admits_archived_collections(stable):
    kb, collections = stable
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping(
        {"question": "seal", "revision_selector": "all"}
    )
    archived_revisions = {
        revision
        for (revision,) in owner_rows(
            owner,
            "SELECT r.revision_id FROM revisions r JOIN sources s USING(source_id) "
            "WHERE s.collection_id=?",
            (collections["second"],),
        )
    }

    def archive(value):
        kb._workspace.bridge.call(
            kb._workspace.services.collections.set_archived(
                collections["second"], value, kb.scope, ExecutionContext()
            )
        )

    def compile_view(include_archived):
        return kb._workspace.bridge.call(
            owner.run_read(
                lambda tx: (
                    compiler.compile(
                        tx,
                        snapshot,
                        owner.scope,
                        request,
                        owner.config,
                        include_archived=include_archived,
                    ).view
                ),
                ExecutionContext(),
            )
        )

    readable = archived_revisions & set(compile_view(False).revisions)
    assert readable
    archive(True)
    try:
        plain = compile_view(False)
        inspected = compile_view(True)
    finally:
        archive(False)
    assert not archived_revisions & set(plain.revisions)
    assert readable <= set(inspected.revisions)
    assert inspected.eligibility_policy_digest != plain.eligibility_policy_digest
