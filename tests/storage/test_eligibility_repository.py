"""Store-side eligibility reads: one row per member, exclusions, closures, owner facts."""

import json

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.adapters.sqlite.repositories.eligibility import _ASSET_SOURCES
from tychora.configuration.global_facets import ROLE as GLOBAL_FACET_ROLE
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import TextLocator
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
    insert = (
        "INSERT INTO representation_dependencies(owner_id,dependency_id) VALUES (?,?)"
    )
    owner_rows(owner, insert, (middle, seed))
    owner_rows(owner, insert, (last, middle))
    expected = {seed: "unit", middle: "unit", last: "unit"}
    closure = _read(kb, lambda tx: tx.eligibility.dependents((seed,)))
    assert closure == expected
    # A cycle back to the seed terminates with the same closure.
    owner_rows(owner, insert, (seed, middle))
    cyclic = _read(kb, lambda tx: tx.eligibility.dependents((seed,)))
    assert cyclic == expected


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


def test_blocked_assertions_are_those_decided_after_the_watermark(kb):
    owner = kb._workspace.owner
    (sha,) = _row(owner, "SELECT artifact_sha FROM artifacts LIMIT 1")

    def decide() -> list[int]:
        ids = []
        for assertion in ("early", "late", "late"):
            cursor = owner.db.execute(
                "INSERT INTO assertion_decisions(assertion_id,provenance_artifact_sha,"
                "provenance_record_id,state,actor_id,created_at) "
                "VALUES (?,?,'record','recorded','actor','2025-01-01')",
                (assertion, sha),
            )
            ids.append(cursor.lastrowid)
        owner.db.commit()
        return ids

    first, second, _ = on_owner(owner, decide)
    assert _read(kb, lambda tx: tx.eligibility.blocked_assertions(first - 1)) == {
        "early",
        "late",
    }
    assert _read(kb, lambda tx: tx.eligibility.blocked_assertions(first)) == {"late"}
    assert _read(kb, lambda tx: tx.eligibility.blocked_assertions(second + 1)) == set()


def test_kind_definitions_and_facets_bind_json_lists(kb):
    def read(tx):
        kinds = tx.eligibility.kind_definitions(("document@1",))
        return kinds, tx.eligibility.revision_facets((), ("scope.project",))

    kinds, facets = _read(kb, read)
    (kind_id, definition_hash, definition) = kinds[0]
    assert kind_id == "document@1" and len(definition_hash) == 64
    assert json.loads(definition)["kind"]
    assert facets == ()


def _first_extraction(owner) -> tuple[str, str, str]:
    """``(extraction_id, revision_id, original_sha)`` of the first committed source."""
    return _row(
        owner,
        "SELECT e.extraction_id,e.revision_id,ra.artifact_sha FROM extractions e "
        "JOIN revision_artifacts ra ON ra.revision_id=e.revision_id "
        "AND ra.role='original' ORDER BY e.extraction_id LIMIT 1",
    )


def _add_asset(owner) -> dict[str, str]:
    """Give the first extraction an asset with a source unit, a proxy unit and a
    unit declared ``needs_review``.

    These tests read stored rows, so the asset's set and the proxy's added asset
    dependency are persisted directly rather than recomputed by the indexer.
    """
    extraction_id, revision_id, sha = _first_extraction(owner)
    proxy, source, declared = (
        row[0]
        for row in owner_rows(
            owner,
            "SELECT unit_id FROM evidence_units WHERE extraction_id=? "
            "ORDER BY unit_id LIMIT 3",
            (extraction_id,),
        )
    )
    asset = "asset-figure-1"
    locator = TextLocator(
        artifact_ref=sha, native_ref="figure:1", char_start=0, char_end=0
    ).model_dump_json()

    def write() -> None:
        db = owner.store.db
        with owner.store.immediate_transaction():
            db.execute(
                "INSERT INTO assets(asset_id,extraction_id,artifact_sha,asset_type,"
                "native_ref,locator_json,declared_review_state) "
                "VALUES (?,?,?,'image','figure:1',?,'needs_review')",
                (asset, extraction_id, sha, locator),
            )
            db.execute(
                "INSERT INTO unit_assets(unit_id,asset_id,role) VALUES (?,?,'source')",
                (source, asset),
            )
            db.execute(
                "INSERT INTO asset_proxies(proxy_unit_id,asset_id,origin_type,"
                "derivation_sha) VALUES (?,?,'caption_copy',?)",
                (proxy, asset, sha),
            )
            db.executemany(
                "INSERT INTO representation_dependencies(owner_id,dependency_id) "
                "VALUES (?,?)",
                [
                    (asset, revision_id),
                    (asset, asset),
                    (asset, source),
                    (proxy, asset),
                    (proxy, source),
                ],
            )
            db.execute(
                "UPDATE evidence_units SET declared_review_state='needs_review' "
                "WHERE unit_id=?",
                (declared,),
            )

    on_owner(owner, write)
    return {
        "extraction": extraction_id,
        "asset": asset,
        "proxy": proxy,
        "source": source,
        "declared": declared,
    }


def test_review_rows_report_proxies_declared_units_and_assets(kb):
    owner = kb._workspace.owner
    ids = _add_asset(owner)
    (proxy_state,) = _row(
        owner,
        "SELECT declared_review_state FROM evidence_units WHERE unit_id=?",
        (ids["proxy"],),
    )
    rows = _read(
        kb,
        lambda tx: tx.eligibility.review_rows(
            (ids["extraction"],), ("needs_review", "rejected")
        ),
    )
    assert set(rows) == {
        (ids["proxy"], "unit", proxy_state, True),
        (ids["declared"], "unit", "needs_review", False),
        (ids["asset"], "asset", "needs_review", False),
    }
    assert list(rows) == sorted(rows)


def test_owner_facts_read_assets_and_proxies(kb):
    owner = kb._workspace.owner
    ids = _add_asset(owner)
    facts = {
        fact.owner_id: fact
        for fact in _read(
            kb, lambda tx: tx.eligibility.owner_facts((ids["asset"], ids["proxy"]))
        )
    }
    asset, proxy = facts[ids["asset"]], facts[ids["proxy"]]
    assert asset.kind == "asset" and asset.asset_id == ids["asset"]
    assert asset.source_owner_ids == frozenset({ids["source"]})
    assert ids["source"] in asset.dependency_ids
    assert proxy.kind == "unit" and proxy.proxy and proxy.asset_id == ids["asset"]
    assert ids["asset"] in proxy.dependency_ids
    assert proxy.source_owner_ids == frozenset()


def test_asset_sources_read_uses_the_asset_index(kb):
    owner = kb._workspace.owner
    plan = owner_rows(
        owner, f"EXPLAIN QUERY PLAN {_ASSET_SOURCES}", (json.dumps(["asset"]),)
    )
    details = " ".join(str(row[-1]) for row in plan)
    assert "unit_assets_asset" in details, details


def test_owner_facts_resolve_a_chunk_record_from_its_group_set(kb):
    owner = kb._workspace.owner
    group_id, chunk_id, unit_id, batch_id = _row(
        owner,
        "SELECT d.owner_id,c.chunk_id,c.unit_id,b.batch_id "
        "FROM representation_dependencies d "
        "JOIN chunks c ON c.unit_id=d.dependency_id "
        "JOIN index_batches b ON b.extraction_id=c.extraction_id "
        "WHERE d.owner_id NOT IN (SELECT unit_id FROM evidence_units "
        "UNION SELECT chunk_id FROM chunks) ORDER BY d.owner_id LIMIT 1",
    )
    group_set = frozenset(
        row[0]
        for row in owner_rows(
            owner,
            "SELECT dependency_id FROM representation_dependencies WHERE owner_id=?",
            (group_id,),
        )
    )
    owner_rows(
        owner,
        "INSERT INTO vector_records(record_id,batch_id,chunk_id,input_sha,row_index,"
        "group_id) VALUES ('record-1',?,?,'input',0,?)",
        (batch_id, chunk_id, group_id),
    )
    (fact,) = _read(kb, lambda tx: tx.eligibility.owner_facts(("record-1",)))
    assert fact.kind == "record" and fact.batch_id == batch_id
    assert (fact.chunk_id, fact.unit_id) == (chunk_id, unit_id)
    assert fact.dependency_ids == group_set
    assert unit_id in group_set


def test_owner_facts_map_a_proxy_window_record_to_its_asset(kb):
    owner = kb._workspace.owner
    ids = _add_asset(owner)
    chunk_id, batch_id = _row(
        owner,
        "SELECT c.chunk_id,b.batch_id FROM chunks c "
        "JOIN index_batches b ON b.extraction_id=c.extraction_id "
        "WHERE c.unit_id=? ORDER BY c.chunk_id LIMIT 1",
        (ids["proxy"],),
    )
    owner_rows(
        owner,
        "INSERT INTO vector_records(record_id,batch_id,chunk_id,input_sha,row_index,"
        "group_id) VALUES ('proxy-record',?,?,'input',0,?)",
        (batch_id, chunk_id, ids["proxy"]),
    )
    facts = {
        fact.owner_id: fact
        for fact in _read(
            kb, lambda tx: tx.eligibility.owner_facts(("proxy-record", chunk_id))
        )
    }
    record, window = facts["proxy-record"], facts[chunk_id]
    assert record.kind == "record" and record.asset_id == ids["asset"]
    assert record.proxy and record.unit_id == ids["proxy"]
    assert window.proxy and window.asset_id == ids["asset"]


def test_members_of_listed_extractions(kb):
    owner = kb._workspace.owner
    extraction_id, revision_id, _ = _first_extraction(owner)

    def read(tx):
        snapshot = tx.eligibility.members(tx.snapshots.active_id())
        return snapshot, tx.eligibility.members("unused", (extraction_id,))

    snapshot, listed = _read(kb, read)
    (row,) = listed
    assert (row.extraction_id, row.revision_id, row.is_default) == (
        extraction_id,
        revision_id,
        True,
    )
    assert row in snapshot


def test_unit_facets_metadata_and_global_facet_markers(kb):
    owner = kb._workspace.owner
    extraction_id, revision_id, sha = _first_extraction(owner)
    (unit_id,) = _row(
        owner,
        "SELECT unit_id FROM evidence_units WHERE extraction_id=? "
        "ORDER BY unit_id LIMIT 1",
        (extraction_id,),
    )

    def write() -> None:
        db = owner.store.db
        with owner.store.immediate_transaction():
            db.executemany(
                "INSERT INTO facet_definitions(facet_key,value_type,operators_json,"
                "definition_hash) VALUES (?,?,'[]',?)",
                [
                    ("scope.project", "text", "0" * 64),
                    ("part.count", "number", "1" * 64),
                ],
            )
            db.execute(
                "INSERT INTO facet_values(revision_id,facet_key,text_value) "
                "VALUES (?,'scope.project','pumps')",
                (revision_id,),
            )
            db.execute(
                "INSERT INTO facet_values(unit_id,facet_key,number_value) "
                "VALUES (?,'part.count',3)",
                (unit_id,),
            )
            db.execute(
                "INSERT INTO revision_artifacts(revision_id,role,source_ref,"
                "artifact_sha) VALUES (?,?,'facets.json',?)",
                (revision_id, GLOBAL_FACET_ROLE, sha),
            )

    on_owner(owner, write)

    def read(tx):
        eligibility = tx.eligibility
        return (
            eligibility.revision_facets((revision_id,), ("scope.project",)),
            eligibility.unit_facets((extraction_id,), ("part.count",)),
            eligibility.revision_metadata((revision_id,)),
            eligibility.global_facet_markers((revision_id,)),
        )

    revision_facets, unit_facets, metadata, markers = _read(kb, read)
    assert revision_facets == ((revision_id, "scope.project", "pumps"),)
    assert unit_facets == ((unit_id, "part.count", 3.0),)
    ((metadata_revision, metadata_json, metadata_sha),) = metadata
    assert metadata_revision == revision_id and len(metadata_sha) == 64
    (source_key,) = _row(
        owner,
        "SELECT s.source_key FROM revisions r JOIN sources s USING(source_id) "
        "WHERE r.revision_id=?",
        (revision_id,),
    )
    assert json.loads(metadata_json)["title"] == source_key
    ((marker_revision, source_ref, artifact),) = markers
    assert (marker_revision, source_ref) == (revision_id, "facets.json")
    assert artifact is not None and artifact.sha256 == sha
