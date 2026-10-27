"""Per-candidate slices: neighbours by boundary key and persisted ownership."""

import pytest

from tests.helpers.corpus import add_manual
from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext

# Two paragraphs past radius 1 of "Inspect", so a radius-1 neighbourhood is a
# strict part of its boundary.
_MANUAL = (
    "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
    "Record the result.\n\nDrain the casing.\n\nRefill the reservoir.\n\n"
    "# Valves\n\nReplace worn valves.\n"
)


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        add_manual(opened, _MANUAL, title="Manual")
        yield opened


def _inspect_row(owner, columns):
    """The given columns of the "Inspect" paragraph's unit."""
    row = owner_row(
        owner, f"SELECT {columns} FROM evidence_units WHERE text LIKE 'Inspect%'"
    )
    assert row is not None
    return row


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_neighbours_stay_within_boundary_key(kb):
    owner = kb._workspace.owner
    rows = owner_rows(
        owner,
        "SELECT unit_id,boundary_key,ordinal FROM evidence_units ORDER BY ordinal",
    )
    unit_id, boundary, ordinal = _inspect_row(owner, "unit_id,boundary_key,ordinal")
    near = _read(kb, lambda tx: tx.representations.neighbours(unit_id, 1))
    every = _read(kb, lambda tx: tx.representations.neighbours(unit_id, None))
    assert all(row.boundary_key == boundary for row in near + every)
    assert all(abs(row.ordinal - ordinal) <= 1 for row in near)
    assert {row.unit_id for row in near} < {row.unit_id for row in every}
    assert {r[0] for r in rows if r[1] == boundary} == {row.unit_id for row in every}


def test_slice_ownership_equals_prepared_extraction(kb, monkeypatch):
    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    piece = _read(
        kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
    )
    full = on_owner(owner, lambda: prepare_extraction(_full_rows(owner, extraction_id)))
    assert piece.prepared.representation(unit_id) == full.representation(unit_id)
    assert [u.unit_id for u in piece.rows.units] == [unit_id]


def _full_rows(owner, extraction_id):
    """Runs on the writer thread (called inside `on_owner`)."""
    from tychora.adapters.sqlite.repositories.slices import retained_rows
    from tychora.adapters.sqlite.retained_bindings import BindingScope

    revision_id = owner.db.execute(
        "SELECT revision_id FROM extractions WHERE extraction_id=?", (extraction_id,)
    ).fetchone()[0]
    return retained_rows(owner.db, BindingScope(extraction_id, revision_id))


def test_slice_without_interval_sets_is_refused(kb):
    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    owner_rows(
        owner,
        "DELETE FROM representation_dependencies WHERE owner_id IN "
        "(SELECT dependency_digest FROM ownership_intervals WHERE unit_id=?)",
        (unit_id,),
    )
    with pytest.raises(TychoraError) as error:
        _read(
            kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
        )
    assert error.value.reason == "canonical.facts_missing"


@pytest.mark.xfail(
    strict=True,
    reason="inspection, retained content and visuals move in the next commit",
)
def test_no_read_path_calls_prepare_extraction():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "tychora"
    allowed = {
        "indexing/coordinator.py",
        "indexing/representations.py",
        "application/maintenance.py",
    }
    offenders = []
    for path in [
        *source.joinpath("application").rglob("*.py"),
        *source.joinpath("retrieval").rglob("*.py"),
    ]:
        relative = str(path.relative_to(source))
        if relative in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id == "prepare_extraction":
                offenders.append(relative)
    assert offenders == []
