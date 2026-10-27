"""Persisted dependency sets and ownership intervals of committed extractions."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping

from pydantic import ValidationError

from tychora.contracts.base import Contract
from tychora.contracts.corpus import RepresentationSlice
from tychora.contracts.errors import TychoraError
from tychora.contracts.indexing import IntervalRow, RepresentationFacts
from tychora.ports.repositories import ChunkRow, UnitRow

from ..retained_bindings import BindingScope
from .slices import prepared_slice, require_bounded, retained_rows
from .workspace import require_write

_IN_IDS = "IN (SELECT value FROM json_each(?))"
_UNITS = (
    "SELECT unit_id,extraction_id,boundary_key,ordinal,text_sha,"
    "length(text) AS char_count "
    f"FROM evidence_units WHERE unit_id {_IN_IDS} ORDER BY extraction_id,ordinal"
)
_CHUNKS = (
    "SELECT chunk_id,unit_id,extraction_id,char_start,char_end,context_group_key "
    f"FROM chunks WHERE chunk_id {_IN_IDS} ORDER BY extraction_id,unit_id,char_start"
)
_DEPENDENCIES = (
    "SELECT owner_id,dependency_id FROM representation_dependencies "
    f"WHERE owner_id {_IN_IDS}"
)
# Document order: the unit's extraction and ordinal, then the interval's start.
_INTERVALS = (
    "SELECT i.unit_id AS unit_id,i.char_start AS char_start,i.char_end AS char_end,"
    "i.dependency_digest AS dependency_digest FROM ownership_intervals i "
    f"JOIN evidence_units u USING(unit_id) WHERE i.unit_id {_IN_IDS} "
    "ORDER BY u.extraction_id,u.ordinal,i.char_start"
)

# The unit's own extraction and boundary key; ``radius`` NULL keeps all of it.
# Served by the ``units_extraction_boundary`` index.
_NEIGHBOURS = (
    "SELECT n.unit_id,n.extraction_id,n.boundary_key,n.ordinal,n.text_sha,"
    "length(n.text) AS char_count FROM evidence_units u JOIN evidence_units n "
    "ON n.extraction_id=u.extraction_id AND n.boundary_key=u.boundary_key "
    "WHERE u.unit_id=? AND (? IS NULL OR abs(n.ordinal-u.ordinal)<=?) "
    "ORDER BY n.ordinal"
)

_CODE_PATH = (
    "SELECT source_ref FROM locators WHERE unit_id=? AND locator_type='code' "
    "ORDER BY fragment_ordinal LIMIT 1"
)


def _ids(values: tuple[str, ...]) -> str:
    return json.dumps(sorted(set(values)))


def rows_by_name[T: Contract](model: type[T], cursor: sqlite3.Cursor) -> tuple[T, ...]:
    """Validate each row as the model, binding every column to the field it names."""
    names = tuple(column[0] for column in cursor.description)
    try:
        return tuple(model(**dict(zip(names, row, strict=True))) for row in cursor)
    except ValidationError:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Representation row is invalid.",
            reason="canonical.row_invalid",
        ) from None


def _differs(owner: str) -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "Stored representation facts differ for " + owner,
        reason="canonical.binding_mismatch",
    )


def _slice_differs(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


class SqliteRepresentationRepository:
    """Reads by ID tuples and idempotent writes of representation facts."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db
        self._writable = writable

    def _select[T: Contract](
        self, model: type[T], query: str, ids: tuple[str, ...]
    ) -> tuple[T, ...]:
        return rows_by_name(model, self._db.execute(query, (_ids(ids),)))

    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]:
        """Return the stored units among these IDs; unknown IDs are skipped."""
        return self._select(UnitRow, _UNITS, unit_ids)

    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]:
        """Return the stored chunks among these IDs; unknown IDs are skipped."""
        return self._select(ChunkRow, _CHUNKS, chunk_ids)

    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]:
        """Return the dependency set of each stored owner among these IDs."""
        found: dict[str, set[str]] = defaultdict(set)
        for owner, dependency in self._db.execute(_DEPENDENCIES, (_ids(owner_ids),)):
            found[owner].add(dependency)
        return {owner: frozenset(dependencies) for owner, dependencies in found.items()}

    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]:
        """Return the ownership intervals of these units in unit and start order."""
        return self._select(IntervalRow, _INTERVALS, unit_ids)

    def neighbours(self, unit_id: str, radius: int | None) -> tuple[UnitRow, ...]:
        """Units of the same extraction and boundary key within ``radius`` ordinals, by ordinal."""
        return rows_by_name(
            UnitRow, self._db.execute(_NEIGHBOURS, (unit_id, radius, radius))
        )

    def code_path(self, unit_id: str) -> str | None:
        """The file a unit's first code locator names, or ``None`` for a non-code unit."""
        row = self._db.execute(_CODE_PATH, (unit_id,)).fetchone()
        return None if row is None else row[0]

    def slice(
        self,
        extraction_id: str,
        *,
        unit_ids: tuple[str, ...] = (),
        relative_path: str | None = None,
    ) -> RepresentationSlice:
        """Retained rows and persisted ownership of the listed units or of one code file.

        An empty request is refused, as is a listed unit the extraction lacks; a
        scope over the slice bounds is ``request.limit_exceeded``.
        """
        if not unit_ids and relative_path is None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "A slice names no unit and no file.",
                reason="invariant.unexpected",
            )
        row = self._db.execute(
            "SELECT revision_id FROM extractions WHERE extraction_id=?",
            (extraction_id,),
        ).fetchone()
        if row is None:
            raise _slice_differs("Slice extraction is not registered.")
        scope = BindingScope(
            extraction_id,
            row[0],
            relative_path,
            None if relative_path is not None else unit_ids,
        )
        footprint = require_bounded(self._db, scope)
        rows = retained_rows(self._db, scope)
        expected = len(set(unit_ids)) if relative_path is None else None
        if not rows.units or expected not in (None, len(rows.units)):
            raise _slice_differs("Slice units differ from the request.")
        intervals = self.intervals(tuple(unit.unit_id for unit in rows.units))
        sets = self.dependencies(tuple({item.dependency_digest for item in intervals}))
        return RepresentationSlice(
            rows=rows,
            prepared=prepared_slice(rows, intervals, sets),
            footprint=footprint,
        )

    def write(self, facts: RepresentationFacts) -> None:
        """Store facts once; a repeat is a no-op and a different set is refused."""
        require_write(self._db, self._writable)
        expected = facts.dependency_map()
        stored = self.dependencies(tuple(expected))
        for owner, dependencies in stored.items():
            if dependencies != expected[owner]:
                raise _differs(owner)
        self._db.executemany(
            "INSERT INTO representation_dependencies(owner_id,dependency_id) VALUES (?,?)",
            [
                (owner, dependency)
                for owner, dependencies in sorted(expected.items())
                if owner not in stored
                for dependency in sorted(dependencies)
            ],
        )
        existing = {
            (row.unit_id, row.char_start): row
            for row in self.intervals(tuple({row.unit_id for row in facts.intervals}))
        }
        for row in facts.intervals:
            current = existing.get((row.unit_id, row.char_start))
            if current is not None and current != row:
                raise _differs(row.unit_id)
        self._db.executemany(
            "INSERT INTO ownership_intervals(unit_id,char_start,char_end,dependency_digest) "
            "VALUES (?,?,?,?)",
            [
                (row.unit_id, row.char_start, row.char_end, row.dependency_digest)
                for row in facts.intervals
                if (row.unit_id, row.char_start) not in existing
            ],
        )
