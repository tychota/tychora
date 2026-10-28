"""The revision-level view admits exactly what the unit-level compiler admitted."""

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import grimp
import pytest

from tests.helpers.eligibility_v1_oracle import OracleCompiler
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
from tychora.adapters.sqlite.repositories.eligibility import SqliteEligibilityRepository
from tychora.configuration.registry import compile_filter
from tychora.contracts.collections import CollectionCreate, CollectionEdit
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.eligibility import GraphBinding
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import digest
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_admitted_record,
    is_candidate_asset,
    is_candidate_proxy,
    is_eligible_asset,
    is_eligible_unit,
    is_source_unit,
)
from tychora.retrieval.cache import EligibilityCache
from tychora.retrieval.compiler import (
    EligibilityCompiler,
    EligibilityInputs,
    split_revision_filter,
)
from tychora.retrieval.graph_scope import graph_scope

ITERATIONS = 500
_EDITIONS = 8  # the shared source's last edition a facet-binding mutation adds
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
_MIXED_FILTERS = (
    # A metadata conjunct drops revisions; the unit conjunct stays per unit.
    {
        "op": "and",
        "children": [
            {"op": "in", "field": "project", "values": ["alpha", "beta"]},
            {"op": "eq", "field": "language", "value": "python"},
        ],
    },
    # Metadata leaves under or are decided per revision...
    {
        "op": "or",
        "children": [
            {"op": "eq", "field": "project", "value": "beta"},
            {"op": "range", "field": "published", "lower": "2022-01-01"},
        ],
    },
    # ...but an or mixing a unit leaf falls back to the per-unit path.
    {
        "op": "or",
        "children": [
            {"op": "eq", "field": "project", "value": "alpha"},
            {"op": "eq", "field": "language", "value": "python"},
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
    "filtered_revisions": 20,
    "unreached_refused": 10,
    "empty_revision_dropped": 10,
    "rejected_revision_proxy": 3,
    "request.limit_exceeded": 3,
    "mode.unavailable": 3,
    "edition.as_of_required": 3,
    "decision:undated": 30,  # tie, overlap and applicable: tests/retrieval/test_temporal.py
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
        if rng.random() < 0.4:
            value["filters"] = rng.choice(_MIXED_FILTERS)
    if rng.random() < 0.3:
        value["as_of"] = date(
            rng.randint(2019, 2025), rng.randint(1, 12), 1
        ).isoformat()
    if rng.random() < 0.2:
        value["cutoff"] = date(rng.randint(2020, 2025), 6, 1).isoformat()
    selector_draw = rng.random()
    if selector_draw < 0.12:
        value["revision_selector"] = "all"
    elif selector_draw < 0.2:
        value["revision_selector"] = "applicable"  # refused without as_of
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
    "SELECT s.source_id,s.access_floor,s.source_key FROM sources s "
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
    # The shared source keeps its floor: above the caller's ceiling, the
    # facet-binding mutation could no longer publish its next edition.
    sources = [
        row for row in owner_rows(run.owner, _SOURCES_SQL) if row[2] != GLOBAL_KEY
    ]
    source, floor, _ = run.rng.choice(sources)
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
    # Withdrawing the figure's source would starve every figure counter for
    # the rest of the run, and a withdrawn shared source takes no new edition;
    # any other source exercises withdrawal.
    figure = owner_row(
        run.owner,
        "SELECT source_id FROM revisions WHERE revision_id=?",
        (run.corpus.figure.revision,),
    )
    source = run.rng.choice(
        [
            item
            for item, _, key in owner_rows(run.owner, _SOURCES_SQL)
            if (item,) != figure and key != GLOBAL_KEY
        ]
    )
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
    if mutation is _facet_binding and (run.edition >= _EDITIONS or run.archived):
        mutation = _review  # an archived collection admits no new edition
    mutation(run)
    run.counts[f"mutation:{mutation.__name__}"] += 1


# ---------------------------------------------------------------------------
# Comparison


_RECEIPT = GraphBatchReceipt(
    generation_id="generation",
    family_id=digest("graph-family"),
    config_sha=digest("graph-config"),
    decision_watermark=0,
    manifest_digest=digest("graph-manifest"),
    entity_count=0,
    evidence_count=0,
    assertion_count=0,
)
# The corpus has no graph: its views gain this binding to derive a graph scope.
_GRAPH = GraphBinding(
    family_id=_RECEIPT.family_id, all_eligible=False, receipt=_RECEIPT
)


def _unreached(v1, units, assets, records):
    """Owners no unit reaches, of revisions whose metadata fails the filter.

    These are the unlinked assets, the records over them and every owner of a
    revision without units. v1 admitted them; v2 judges them by their
    revision's metadata alone. Also returns the failing revisions without units.
    """
    failing = v1.metadata_failing_revision_ids
    unit_ids = {f.owner_id for f in units}
    with_units = {r for f in units for r in f.dependency_ids & failing}
    unlinked = {
        f.owner_id
        for f in assets
        if f.dependency_ids & failing and not f.dependency_ids & unit_ids
    }
    over = {
        f.owner_id for f in records if f.chunk_id is None and f.asset_id in unlinked
    }
    return frozenset(unlinked | over), frozenset(failing - with_units)


def _compare(run, v1, v2, request, registry, context):
    facts = run.facts
    units = [f for f in facts.values() if f.kind == "unit"]
    chunks = [f for f in facts.values() if f.kind == "chunk"]
    assets = [f for f in facts.values() if f.kind == "asset"]
    records = [f for f in facts.values() if f.kind == "record"]

    def admitted(predicate, items):
        return {f.owner_id for f in items if predicate(v2, f, facts)}

    # v1 keeps a revision whose every unit the filter rejects and excludes those
    # units; v2 drops a revision whose metadata fails the filter. Admission is
    # compared owner by owner below.
    selected = compile_filter(request.filters, registry) if request.filters else None
    unreached, empty = _unreached(v1, units, assets, records)
    dropped = v1.revision_ids - set(v2.revisions)
    assert set(v2.revisions) <= v1.revision_ids, context
    assert empty <= dropped <= v1.filter_excluded_revision_ids | empty, context
    if selected is not None and selected.all_units is not None:
        _, unit_part = split_revision_filter(selected.all_units, registry)
        if unit_part is None:
            assert dropped == v1.filter_excluded_revision_ids | empty, context
    run.counts["filtered_revisions"] += bool(dropped)
    run.counts["unreached_refused"] += bool(unreached & v1.asset_ids)
    run.counts["empty_revision_dropped"] += bool(empty)
    assert admitted(is_source_unit, units) == v1.source_unit_ids, context
    # The graph scope a traversal reads splits the eligible units as v1 did.
    scope = run.call(
        run.owner.run_read(
            lambda tx: graph_scope(tx, v2.model_copy(update={"graph": _GRAPH})),
            ExecutionContext(),
        )
    )
    assert scope.source_unit_ids == v1.source_unit_ids, context
    assert scope.context_unit_ids == v1.context_unit_ids, context
    assert scope.structural_unit_ids == v1.structural_unit_ids, context
    eligible_proxies = {
        f.owner_id for f in units if f.proxy and is_eligible_unit(v2, f)
    }
    assert eligible_proxies == v1.proxy_unit_ids, context
    assert admitted(is_admitted_chunk, chunks) == v1.chunk_ids, context
    assert admitted(is_eligible_asset, assets) == v1.asset_ids - unreached, context
    assert admitted(is_admitted_record, records) == v1.vector_record_ids - unreached, (
        context
    )
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
    assert admitted(is_candidate_asset, assets) == v1_candidate_assets - unreached, (
        context
    )
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
            lambda: legacy.compile_eligibility(snapshot, scope, request, owner.config),
            ExecutionContext(),
        )
    except TychoraError as error:
        with pytest.raises(TychoraError) as v2_error:
            run.call(
                owner.run_read(
                    lambda tx: compiler.compile_cached(
                        tx,
                        cache,
                        EligibilityInputs(snapshot, scope, request, owner.config),
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
    compiled = run.call(
        owner.run_read(
            lambda tx: compiler.compile_cached(
                tx,
                cache,
                EligibilityInputs(snapshot, scope, request, owner.config),
            ),
            ExecutionContext(),
        )
    )
    # Edition decisions are new in v2 (spec 4.9). Each names editions v1 also
    # selected, except an overlap, which names its one winner beside the older
    # editions that declare they still apply.
    for decision in compiled.decisions:
        named = set(decision.revision_ids)
        chosen = named & v1.revision_ids
        assert len(chosen) == 1 if decision.reason == "overlap" else chosen == named, (
            decision,
            request,
        )
        run.counts[f"decision:{decision.reason}"] += 1
    return v1, compiled.view


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """The rich corpus the equivalence test mutates; no other test reads it."""
    built = build(tmp_path_factory.mktemp("equivalence"))
    with built.kb:
        yield built


def test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures(corpus):
    owner = corpus.kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    legacy = OracleCompiler(owner.store)
    cache = EligibilityCache()
    run = _Run(corpus, random.Random(20261030))
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
                tx, cache, EligibilityInputs(snapshot, scope, request, owner.config)
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
                compiler.compile(
                    tx, EligibilityInputs(snapshot, owner.scope, request, owner.config)
                ).view
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
                        EligibilityInputs(snapshot, owner.scope, request, owner.config),
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


# ---------------------------------------------------------------------------
# Revision-level metadata filters


def _filtered_view(kb, collections, monkeypatch, filters):
    """Compile ``filters`` over every edition; record each unit_flags read."""
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    scope = owner.scope.model_copy(
        update={"collection_ids": frozenset(collections.values()), "access_ceiling": 3}
    )
    request = QueryRequest.from_mapping(
        {"question": "seal", "revision_selector": "all", "filters": filters}
    )
    reads: list[tuple[str, ...]] = []
    original = SqliteEligibilityRepository.unit_flags

    def counted(self, extraction_ids):
        reads.append(extraction_ids)
        return original(self, extraction_ids)

    monkeypatch.setattr(SqliteEligibilityRepository, "unit_flags", counted)
    view = kb._workspace.bridge.call(
        owner.run_read(
            lambda tx: (
                compiler.compile(
                    tx, EligibilityInputs(snapshot, scope, request, owner.config)
                ).view
            ),
            ExecutionContext(),
        )
    )
    monkeypatch.undo()
    units_of = {}
    for revision, unit in owner_rows(
        owner,
        "SELECT e.revision_id,u.unit_id FROM evidence_units u "
        "JOIN extractions e USING(extraction_id)",
    ):
        units_of.setdefault(revision, set()).add(unit)
    return view, reads, units_of


def _project_revisions(kb, value):
    return {
        revision
        for (revision,) in owner_rows(
            kb._workspace.owner,
            "SELECT revision_id FROM facet_values "
            "WHERE facet_key='scope.project' AND text_value=?",
            (value,),
        )
    }


def test_metadata_filter_drops_revisions_without_enumerating_units(stable, monkeypatch):
    kb, collections = stable
    plain, _, units_of = _filtered_view(
        kb, collections, monkeypatch, {"op": "eq", "field": "language", "value": "x"}
    )
    view, reads, _ = _filtered_view(
        kb, collections, monkeypatch, {"op": "eq", "field": "project", "value": "alpha"}
    )
    alpha = _project_revisions(kb, "alpha")
    dropped = set(plain.revisions) - alpha
    # N revisions of M units each fail the filter.
    assert len(dropped) >= 3
    assert sum(len(units_of[r]) for r in dropped) > len(dropped)
    assert set(view.revisions) == set(plain.revisions) & alpha
    assert reads == []
    assert not view.excluded_unit_ids & set().union(*(units_of[r] for r in dropped))
    assert view.unit_filter is None


def test_metadata_conjunct_drops_revisions_and_unit_conjunct_stays_per_unit(
    stable, monkeypatch
):
    kb, collections = stable
    view, reads, units_of = _filtered_view(
        kb,
        collections,
        monkeypatch,
        {
            "op": "and",
            "children": [
                {"op": "eq", "field": "project", "value": "alpha"},
                {"op": "eq", "field": "language", "value": "python"},
            ],
        },
    )
    alpha = _project_revisions(kb, "alpha")
    assert set(view.revisions) <= alpha and view.revisions
    # Only the surviving revisions' units are read for the unit conjunct.
    assert reads == [tuple(sorted(view.extraction_ids))]
    kept_units = set().union(*(units_of[r] for r in view.revisions))
    assert kept_units <= view.excluded_unit_ids


def test_mixed_or_falls_back_to_the_per_unit_path(stable, monkeypatch):
    kb, collections = stable
    view, reads, units_of = _filtered_view(
        kb,
        collections,
        monkeypatch,
        {
            "op": "or",
            "children": [
                {"op": "eq", "field": "project", "value": "alpha"},
                {"op": "eq", "field": "language", "value": "python"},
            ],
        },
    )
    alpha = _project_revisions(kb, "alpha")
    assert reads == [tuple(sorted(view.extraction_ids))]
    assert set(view.revisions) - alpha
    for revision in set(view.revisions) - alpha:
        assert units_of[revision] <= view.excluded_unit_ids


def test_unit_value_for_a_metadata_facet_keeps_the_per_unit_path(stable, monkeypatch):
    """One unit's own failing project value falls back to the per-unit path."""
    kb, collections = stable
    owner = kb._workspace.owner
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    # A unit of an alpha edition the unfiltered compile selects.
    plain, _, units_of = _filtered_view(
        kb, collections, monkeypatch, {"op": "eq", "field": "language", "value": "x"}
    )
    selected = sorted(set(plain.revisions) & _project_revisions(kb, "alpha"))
    row = (min(units_of[selected[0]]),) if selected else None
    assert row is not None
    (unit,) = row

    def write(sql, args):
        with owner.store.immediate_transaction():
            owner.store.db.execute(sql, args)

    on_owner(
        owner,
        lambda: write(
            "INSERT INTO facet_values(unit_id,facet_key,text_value) "
            "VALUES (?,'scope.project','beta')",
            (unit,),
        ),
    )
    try:
        filters = {"op": "eq", "field": "project", "value": "alpha"}
        view, reads, _ = _filtered_view(kb, collections, monkeypatch, filters)
        scope = owner.scope.model_copy(
            update={
                "collection_ids": frozenset(collections.values()),
                "access_ceiling": 3,
            }
        )
        request = QueryRequest.from_mapping(
            {"question": "seal", "revision_selector": "all", "filters": filters}
        )
        v1 = owner.run_write_sync(
            lambda: OracleCompiler(owner.store).compile_eligibility(
                snapshot, scope, request, owner.config
            ),
            ExecutionContext(),
        )
        facts = kb._workspace.bridge.call(
            owner.run_read(
                lambda tx: snapshot_facts(tx, snapshot.snapshot_id),
                ExecutionContext(),
            )
        )
    finally:
        on_owner(
            owner,
            lambda: write(
                "DELETE FROM facet_values WHERE unit_id=? AND facet_key='scope.project'",
                (unit,),
            ),
        )
    units = [f for f in facts.values() if f.kind == "unit"]
    assert set(view.revisions) == v1.revision_ids
    assert {f.owner_id for f in units if is_source_unit(view, f, facts)} == (
        v1.source_unit_ids
    )
    assert {f.owner_id for f in units if is_eligible_unit(view, f)} == (
        v1.context_unit_ids | v1.structural_unit_ids
    )
    assert unit in view.excluded_unit_ids
    assert reads == [tuple(sorted(view.extraction_ids))]
