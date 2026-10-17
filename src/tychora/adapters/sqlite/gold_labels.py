"""Resolve gold labels against one pinned snapshot's canonical rows."""

import sqlite3

from tychora.contracts.identity import normalize_source_key


class SqliteGoldLabelResolver:
    """Canonical lookups for label resolution; call on the owner thread."""

    def __init__(self, db: sqlite3.Connection, snapshot_id: str) -> None:
        self._db = db
        self._snapshot_id = snapshot_id

    def _ids(self, statement: str, parameters: tuple[object, ...]) -> tuple[str, ...]:
        return tuple(row[0] for row in self._db.execute(statement, parameters))

    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]:
        """Return the live sources of a collection carrying this source key."""
        try:
            key = normalize_source_key(source_key)
        except ValueError:
            return ()
        return self._ids(
            "SELECT s.source_id FROM sources s JOIN collections c USING(collection_id) "
            "WHERE (c.slug=? OR c.collection_id=?) AND s.source_key=? "
            "AND s.withdrawn_at IS NULL ORDER BY s.source_id",
            (collection, collection, key),
        )

    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]:
        """Return the snapshot revisions of a source, the default one without edition."""
        if edition_key is None:
            return self._ids(
                "SELECT revision_id FROM snapshot_members "
                "WHERE snapshot_id=? AND source_id=? AND is_default=1",
                (self._snapshot_id, source_id),
            )
        return self._ids(
            "SELECT m.revision_id FROM snapshot_members m "
            "JOIN revisions r USING(revision_id) "
            "WHERE m.snapshot_id=? AND m.source_id=? AND r.edition_key=? "
            "ORDER BY m.revision_id",
            (self._snapshot_id, source_id, edition_key),
        )

    def _extraction(self, revision_id: str) -> str | None:
        row = self._db.execute(
            "SELECT extraction_id FROM snapshot_members "
            "WHERE snapshot_id=? AND revision_id=?",
            (self._snapshot_id, revision_id),
        ).fetchone()
        return None if row is None else row[0]

    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]:
        """Return the units of the pinned extraction carrying this native reference."""
        return self._ids(
            "SELECT unit_id FROM evidence_units WHERE extraction_id=? AND native_ref=? "
            "ORDER BY ordinal",
            (self._extraction(revision_id), native_ref),
        )

    def units_by_char_range(
        self, revision_id: str, char_start: int, char_end: int
    ) -> tuple[str, ...]:
        """Return the units whose text locators overlap the codepoint range."""
        return self._ids(
            "SELECT u.unit_id FROM evidence_units u WHERE u.extraction_id=? AND EXISTS("
            "SELECT 1 FROM locators l WHERE l.unit_id=u.unit_id "
            "AND l.locator_type IN ('text','code') "
            "AND json_extract(l.details_json,'$.char_start')<? "
            "AND json_extract(l.details_json,'$.char_end')>?) ORDER BY u.ordinal",
            (self._extraction(revision_id), char_end, char_start),
        )

    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]:
        """Return the units located on this physical page."""
        return self._ids(
            "SELECT u.unit_id FROM evidence_units u WHERE u.extraction_id=? AND EXISTS("
            "SELECT 1 FROM locators l WHERE l.unit_id=u.unit_id AND l.physical_page=?) "
            "ORDER BY u.ordinal",
            (self._extraction(revision_id), page),
        )
