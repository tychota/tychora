"""Representation facts written at commit and read back by ID tuples."""

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteRepresentationRepository
from tychora.adapters.sqlite.repositories.representations import rows_by_name
from tychora.contracts.errors import TychoraError
from tychora.contracts.indexing import (
    IntervalRow,
    OwnerDependencies,
    RepresentationFacts,
)
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ports.execution import ExecutionContext


def _facts(bundle):
    return representation_facts(
        bundle,
        prepare_extraction(bundle),
        (
            ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end)
            for c in bundle.chunks
        ),
    )


@pytest.fixture
def store(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    yield CorpusStore(db, registry=registry(), artifacts=reader())
    db.close()


def test_commit_writes_dependencies_and_intervals(store):
    bundle = seed_bundle("Seal checks are quarterly.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    with store.uow.read() as tx:
        stored = tx.representations.dependencies(tuple(facts.dependency_map()))
        intervals = tx.representations.intervals(tuple(u.unit_id for u in bundle.units))
    assert stored == facts.dependency_map()
    assert intervals == facts.intervals


def test_recommit_is_idempotent(store):
    bundle = seed_bundle("Record the result.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    store.commit_extraction(bundle, facts)
    with store.uow.write() as tx:
        tx.representations.write(facts)
    count = store.db.execute(
        "SELECT COUNT(*) FROM representation_dependencies"
    ).fetchone()[0]
    assert count == sum(len(item.dependency_ids) for item in facts.dependencies)
    intervals = store.db.execute("SELECT COUNT(*) FROM ownership_intervals").fetchone()[
        0
    ]
    assert intervals == len(facts.intervals)


def test_conflicting_facts_are_refused(store):
    bundle = seed_bundle("Record the result.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    changed = RepresentationFacts(
        extraction_id=facts.extraction_id,
        dependencies=(
            OwnerDependencies(
                owner_id=bundle.units[0].unit_id, dependency_ids=frozenset({"other"})
            ),
        ),
    )
    with pytest.raises(TychoraError) as error, store.uow.write() as tx:
        tx.representations.write(changed)
    assert error.value.reason == "canonical.binding_mismatch"


def test_facts_of_another_extraction_are_refused(store):
    with pytest.raises(TychoraError) as error:
        store.commit_extraction(
            seed_bundle("Record the result."), _facts(seed_bundle("Different text."))
        )
    assert error.value.reason == "canonical.binding_mismatch"
    assert store.db.execute("SELECT COUNT(*) FROM evidence_units").fetchone()[0] == 0


def test_a_failure_after_the_facts_rolls_the_extraction_back(store, monkeypatch):
    bundle = seed_bundle("Record the result.")
    facts = _facts(bundle)
    write = SqliteRepresentationRepository.write

    def write_then_crash(self, facts):
        write(self, facts)
        raise RuntimeError("crash after the facts")

    monkeypatch.setattr(SqliteRepresentationRepository, "write", write_then_crash)
    with pytest.raises(RuntimeError):
        store.commit_extraction(bundle, facts)
    for table in (
        "evidence_units",
        "representation_dependencies",
        "ownership_intervals",
    ):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(SqliteRepresentationRepository, "write", write)
    store.commit_extraction(bundle, facts)
    with store.uow.read() as tx:
        assert tx.representations.dependencies(tuple(facts.dependency_map())) == (
            facts.dependency_map()
        )


def test_write_requires_a_write_transaction(store):
    bundle = seed_bundle("Record the result.")
    with pytest.raises(TychoraError) as error, store.uow.read() as tx:
        tx.representations.write(_facts(bundle))
    assert error.value.reason == "invariant.unexpected"


def test_repository_reads_bind_id_tuples(store):
    bundle = seed_bundle("Seal checks are quarterly.")
    store.commit_extraction(bundle, _facts(bundle))
    unit, chunk = bundle.units[0], bundle.chunks[0]
    padding = tuple(f"missing-{index}" for index in range(999))
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        with store.uow.read() as tx:
            (unit_row,) = tx.representations.units((unit.unit_id, *padding))
            (chunk_row,) = tx.representations.chunks((chunk.chunk_id, *padding))
            dependencies = tx.representations.dependencies((unit.unit_id, *padding))
    finally:
        store.db.set_trace_callback(None)
    assert (
        unit_row.boundary_key,
        unit_row.ordinal,
        unit_row.text_sha,
        unit_row.char_count,
    ) == (
        unit.boundary_key,
        unit.ordinal,
        unit.text_sha,
        len(unit.text),
    )
    assert (chunk_row.unit_id, chunk_row.char_start, chunk_row.char_end) == (
        chunk.unit_id,
        chunk.char_start,
        chunk.char_end,
    )
    assert chunk_row.context_group_key == chunk.context_group_key
    assert set(dependencies) == {unit.unit_id}
    assert sum("json_each" in statement for statement in statements) == 3


def test_rows_are_built_by_column_name(store):
    bundle = seed_bundle("Seal checks are quarterly.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    reordered = store.db.execute(
        "SELECT dependency_digest,char_end,char_start,unit_id FROM ownership_intervals"
    )
    assert rows_by_name(IntervalRow, reordered) == facts.intervals
    renamed = store.db.execute(
        "SELECT unit_id,char_start,char_end,dependency_digest AS digest "
        "FROM ownership_intervals"
    )
    with pytest.raises(TychoraError) as error:
        rows_by_name(IntervalRow, renamed)
    assert error.value.reason == "canonical.row_invalid"


_MANUAL = (
    "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
    "# Valves\n\nReplace worn valves.\n"
)


def _add_manual(kb):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": "manual.md",
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": _MANUAL,
            "metadata": {"title": "Pumps"},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_intervals_read_back_in_document_order(tmp_path, monkeypatch):
    committed: list[RepresentationFacts | None] = []
    commit = CorpusStore.commit_extraction

    def recording_commit(self, bundle, facts=None):
        committed.append(facts)
        return commit(self, bundle, facts)

    monkeypatch.setattr(CorpusStore, "commit_extraction", recording_commit)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_manual(kb)
        owner = kb._workspace.owner
        (facts,) = committed
        assert facts is not None
        units = tuple(dict.fromkeys(row.unit_id for row in facts.intervals))

        def read():
            with owner.store.uow.read() as tx:
                return tx.representations.intervals(tuple(sorted(units)))

        intervals = owner.run_blocking_sync(read, ExecutionContext())
    assert len(units) > 2
    assert list(units) != sorted(units)
    assert intervals == facts.intervals


def test_published_add_persists_what_eligibility_derives(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_manual(kb)
        owner = kb._workspace.owner

        def read():
            store = owner.store
            facts = store.read_eligibility_facts(store.resolve_snapshot(None))
            owners = (
                *(fact.unit_id for fact in facts.units),
                *(fact.chunk_id for fact in facts.chunks),
                *(fact.asset_id for fact in facts.assets),
            )
            with store.uow.read() as tx:
                return facts, tx.representations.dependencies(owners)

        facts, stored = owner.run_blocking_sync(read, ExecutionContext())
    assert facts.units and facts.chunks
    for fact in facts.units:
        assert stored[fact.unit_id] == fact.dependency_ids
    for fact in facts.chunks:
        assert stored[fact.chunk_id] == fact.dependency_ids
    for fact in facts.assets:
        assert stored[fact.asset_id] == fact.dependency_ids
