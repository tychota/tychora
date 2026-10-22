"""One bound set per pass decides exactly as the per-payload row checks."""

import random
import sqlite3

import pytest

from tychora.adapters.sqlite import bindings

_REVISION_TABLES = (
    "CREATE TABLE revisions(revision_id TEXT, original_sha TEXT)",
    "CREATE TABLE revision_artifacts(revision_id TEXT, artifact_sha TEXT, role TEXT)",
    "CREATE TABLE original_payload_tombstones(revision_id TEXT, artifact_sha TEXT)",
    "CREATE TABLE locators(unit_id TEXT, artifact_sha TEXT)",
    "CREATE TABLE evidence_units(unit_id TEXT, extraction_id TEXT)",
)
ROLES = ("original", "source_file", "document", "proxy")


def _schema(db) -> None:
    columns: dict[str, list[str]] = {}
    for table, column in bindings.DIRECT_BINDINGS:
        columns.setdefault(table, []).append(column)
    columns["extractions"] = ["extraction_id", "revision_id", *columns["extractions"]]
    for table, names in columns.items():
        db.execute(f"CREATE TABLE {table}({','.join(names)})")
    for statement in _REVISION_TABLES:
        db.execute(statement)


def _scenario(seed: int):
    rng = random.Random(seed)
    db = sqlite3.connect(":memory:")
    _schema(db)
    shas = [f"{index:064x}" for index in range(60)]
    revisions = [f"r{index}" for index in range(12)]
    pick = rng.choice
    for _ in range(10):
        db.execute("INSERT INTO revisions VALUES (?,?)", (pick(revisions), pick(shas)))
    for _ in range(14):
        db.execute(
            "INSERT INTO revision_artifacts VALUES (?,?,?)",
            (pick(revisions), pick(shas), pick(ROLES)),
        )
    for _ in range(10):
        db.execute(
            "INSERT INTO original_payload_tombstones VALUES (?,?)",
            (pick(revisions), pick(shas)),
        )
    for index in range(8):
        db.execute(
            "INSERT INTO extractions(extraction_id,revision_id) VALUES (?,?)",
            (f"e{index}", pick(revisions)),
        )
        db.execute(
            "INSERT INTO evidence_units VALUES (?,?)", (f"u{index}", f"e{index}")
        )
    for _ in range(14):
        db.execute(
            "INSERT INTO locators VALUES (?,?)", (f"u{rng.randrange(8)}", pick(shas))
        )
    for table, column in bindings.DIRECT_BINDINGS:
        db.execute(f"INSERT INTO {table}({column}) VALUES (?)", (pick(shas),))
        db.execute(f"INSERT INTO {table}({column}) VALUES (NULL)")
    pairs = [(r, s) for r in revisions for s in shas]
    proposed = frozenset(rng.sample(pairs, 40))
    return db, shas, proposed


@pytest.mark.parametrize("seed", range(40))
def test_bound_set_decides_as_the_per_payload_checks(seed):
    db, shas, proposed = _scenario(seed)
    bound = bindings.BoundSet.load(db, frozenset(shas))
    for sha in shas:
        for cut in (frozenset(), proposed):
            assert bound.binds(sha, cut) == bindings.live_by_rows(db, sha, cut), (
                sha,
                cut == proposed,
            )


def test_every_binding_kind_binds_on_its_own():
    db = sqlite3.connect(":memory:")
    _schema(db)
    sha = "a" * 64
    bound = bindings.BoundSet.load(db, frozenset({sha}))
    assert not bound.binds(sha) and not bindings.live_by_rows(db, sha)
    for table, column in bindings.DIRECT_BINDINGS:
        db.execute(f"INSERT INTO {table}({column}) VALUES (?)", (sha,))
        assert bindings.BoundSet.load(db, frozenset({sha})).binds(sha), table
        assert bindings.live_by_rows(db, sha), table
        db.execute(f"DELETE FROM {table}")


def test_loading_reads_each_binding_table_a_fixed_number_of_times():
    db, shas, _ = _scenario(0)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    bindings.BoundSet.load(db, frozenset(shas[:1]))
    one = len(statements)
    statements.clear()
    bindings.BoundSet.load(db, frozenset(shas))
    assert len(statements) == one


def test_no_candidates_read_no_binding_table():
    db, _, _ = _scenario(0)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    assert not bindings.BoundSet.load(db, frozenset()).binds("a" * 64)
    assert statements == []
