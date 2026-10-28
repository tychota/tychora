"""Run calls and statements on the workspace owner's writer thread from a test."""

from collections.abc import Callable, Sequence
from typing import Any

from tychora.application.owner import WorkspaceOwner
from tychora.ports.execution import ExecutionContext


def on_owner[T](owner: WorkspaceOwner, call: Callable[[], T]) -> T:
    """Run `call` on the writer thread, which owns the connection."""
    return owner.run_write_sync(call, ExecutionContext())


def owner_rows(
    owner: WorkspaceOwner, sql: str, args: Sequence[Any] = ()
) -> list[tuple]:
    """Every row of one statement, executed on the writer thread."""
    return on_owner(owner, lambda: [tuple(row) for row in owner.db.execute(sql, args)])


def owner_row(
    owner: WorkspaceOwner, sql: str, args: Sequence[Any] = ()
) -> tuple | None:
    """The first row of one statement, executed on the writer thread."""
    rows = owner_rows(owner, sql, args)
    return rows[0] if rows else None


def accept_source_units(owner: WorkspaceOwner, source_key: str) -> None:
    """Record an ``accepted`` review for every unit of one source, through the store."""
    from tychora.contracts.corpus import ReviewTarget

    units = owner_rows(
        owner,
        "SELECT u.unit_id FROM evidence_units u JOIN extractions e USING(extraction_id) "
        "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
        "WHERE s.source_key=?",
        (source_key,),
    )
    assert units
    for (unit_id,) in units:
        on_owner(
            owner,
            lambda unit_id=unit_id: owner.store.record_review(
                ReviewTarget(unit_id=unit_id), "accepted", "synthetic-reviewer"
            ),
        )
