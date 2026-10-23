"""Persisted dependency sets and ownership intervals of committed extractions."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping

from pydantic import ValidationError

from tychora.contracts.base import Contract
from tychora.contracts.errors import TychoraError
from tychora.contracts.indexing import IntervalRow, RepresentationFacts
from tychora.ports.repositories import ChunkRow, UnitRow

from .workspace import require_write

_IN_IDS = "IN (SELECT value FROM json_each(?))"
_UNITS = (
    "SELECT unit_id,extraction_id,boundary_key,ordinal,text_sha,length(text) "
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
_INTERVALS = (
    "SELECT unit_id,char_start,char_end,dependency_digest FROM ownership_intervals "
    f"WHERE unit_id {_IN_IDS} ORDER BY unit_id,char_start"
)


def _ids(values: tuple[str, ...]) -> str:
    return json.dumps(sorted(set(values)))


def _contracts[T: Contract](model: type[T], rows: Iterable[tuple]) -> tuple[T, ...]:
    """Validate rows whose columns follow the model's field order."""
    fields = tuple(model.model_fields)
    try:
        return tuple(model(**dict(zip(fields, row, strict=True))) for row in rows)
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


class SqliteRepresentationRepository:
    """Reads by ID tuples and idempotent writes of representation facts."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db
        self._writable = writable

    def _select[T: Contract](
        self, model: type[T], query: str, ids: tuple[str, ...]
    ) -> tuple[T, ...]:
        return _contracts(model, self._db.execute(query, (_ids(ids),)))

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
