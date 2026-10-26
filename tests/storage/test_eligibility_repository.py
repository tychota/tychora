"""Store-side eligibility reads: one row per member, exclusions, closures, owner facts."""

import json

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

_TEXT = (
    "# Pumps\n\nInspect the pump seal quarterly.\n\n# Valves\n\nReplace worn valves.\n"
)


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        for key in ("a.md", "b.md"):
            job = opened.sources.add(
                {
                    "type": "text",
                    "source_key": key,
                    "collection_id": "default",
                    "kind": "document@1",
                    "format": "markdown",
                    "text": _TEXT,
                    "metadata": {"title": key},
                }
            )
            assert opened.jobs.wait(job.id).state == "succeeded"
        yield opened


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def _row(owner, sql: str, args: tuple = ()) -> tuple:
    row = owner_row(owner, sql, args)
    assert row is not None
    return row


def test_member_rows_are_one_statement(kb):
    owner = kb._workspace.owner
    statements = []

    def read(tx):
        snapshot_id = tx.snapshots.active_id()
        db = tx.eligibility._db
        db.set_trace_callback(statements.append)
        try:
            return tx.eligibility.members(snapshot_id)
        finally:
            db.set_trace_callback(None)

    rows = _read(kb, read)
    assert len(rows) == 2
    assert len(statements) == 1
    assert {row.review_state for row in rows} == {"unreviewed"}
    assert all(row.workspace_id == owner.workspace_id for row in rows)


def test_review_rows_report_reviewed_and_declared_states(kb):
    owner = kb._workspace.owner
    unit_id, extraction_id = _row(
        owner,
        "SELECT unit_id,extraction_id FROM evidence_units ORDER BY unit_id LIMIT 1",
    )
    on_owner(
        owner,
        lambda: owner.store.record_review(
            ReviewTarget(unit_id=unit_id), "rejected", "r"
        ),
    )
    rows = _read(
        kb,
        lambda tx: tx.eligibility.review_rows(
            (extraction_id,), ("needs_review", "rejected")
        ),
    )
    assert [(row.target_id, row.kind, row.state, row.proxy) for row in rows] == [
        (unit_id, "unit", "rejected", False)
    ]


def test_dependents_close_over_units_that_depend_on_a_seed(kb):
    owner = kb._workspace.owner
    (extraction_id,) = _row(
        owner, "SELECT extraction_id FROM evidence_units ORDER BY extraction_id LIMIT 1"
    )
    seed, middle, last = (
        row[0]
        for row in owner_rows(
            owner,
            "SELECT unit_id FROM evidence_units WHERE extraction_id=? "
            "ORDER BY unit_id LIMIT 3",
            (extraction_id,),
        )
    )
    # Markdown text units own disjoint ranges, so seed a two-step chain the way
    # overlapping or proxy units persist one: each owner names the unit it reads.
    owner_rows(
        owner,
        "INSERT INTO representation_dependencies(owner_id,dependency_id) "
        "VALUES (?,?),(?,?)",
        (middle, seed, last, middle),
    )
    row = owner_row(
        owner,
        "SELECT d.owner_id,d.dependency_id FROM representation_dependencies d "
        "JOIN evidence_units u ON u.unit_id=d.owner_id "
        "WHERE d.dependency_id<>d.owner_id AND d.dependency_id IN "
        "(SELECT unit_id FROM evidence_units) ORDER BY d.dependency_id=? DESC LIMIT 1",
        (seed,),
    )
    assert row == (middle, seed)
    closure = _read(kb, lambda tx: tx.eligibility.dependents((seed,)))
    assert closure == {seed: "unit", middle: "unit", last: "unit"}


def test_owner_facts_read_units_chunks_and_sets(kb):
    owner = kb._workspace.owner
    unit_id, chunk_id = _row(
        owner, "SELECT unit_id,chunk_id FROM chunks ORDER BY chunk_id LIMIT 1"
    )
    facts = {
        fact.owner_id: fact
        for fact in _read(
            kb, lambda tx: tx.eligibility.owner_facts((unit_id, chunk_id))
        )
    }
    assert facts[unit_id].kind == "unit" and facts[unit_id].citable in {True, False}
    assert facts[chunk_id].kind == "chunk" and facts[chunk_id].unit_id == unit_id
    assert unit_id in facts[chunk_id].dependency_ids


def test_owner_facts_refuse_a_missing_set(kb):
    owner = kb._workspace.owner
    (unit_id,) = _row(owner, "SELECT unit_id FROM evidence_units LIMIT 1")
    owner_rows(
        owner, "DELETE FROM representation_dependencies WHERE owner_id=?", (unit_id,)
    )
    with pytest.raises(TychoraError) as error:
        _read(kb, lambda tx: tx.eligibility.owner_facts((unit_id,)))
    assert error.value.reason == "canonical.facts_missing"


def test_batches_and_unit_flags(kb):
    def read(tx):
        snapshot = tx.snapshots.active_id()
        batches = tx.eligibility.batches(snapshot)
        extractions = tuple({row[2] for row in batches if row[2] is not None})
        return batches, tx.eligibility.unit_flags(extractions)

    batches, flags = _read(kb, read)
    assert {slot for slot, _, _ in batches} == {"lexical"}
    assert flags and all(not row.proxy for row in flags)


def test_kind_definitions_and_facets_bind_json_lists(kb):
    def read(tx):
        kinds = tx.eligibility.kind_definitions(("document@1",))
        return kinds, tx.eligibility.revision_facets((), ("scope.project",))

    kinds, facets = _read(kb, read)
    (kind_id, definition_hash, definition) = kinds[0]
    assert kind_id == "document@1" and len(definition_hash) == 64
    assert json.loads(definition)["kind"]
    assert facets == ()
