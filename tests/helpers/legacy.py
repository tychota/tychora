"""Fabricate format-1 databases for refusal and rebuild tests."""

import sqlite3
from pathlib import Path

from tychora.adapters.sqlite.migrations import FORMAT_ONE_CHECKSUM, migration_sql


def make_format_one(root: Path) -> None:
    """Rewrite the migration receipt of a closed workspace to format 1."""
    db = sqlite3.connect(root / "corpus.sqlite3")
    try:
        db.execute(
            "UPDATE schema_migrations SET version=1, checksum=?", (FORMAT_ONE_CHECKSUM,)
        )
        db.commit()
    finally:
        db.close()


class OutputCounter:
    """Character-count output counter so tests need no tokenizer bundle."""

    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def add_text(kb, key: str, text: str):
    """Add one text document to the default collection and wait for success."""
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "text",
            "text": text,
            "metadata": {"title": key},
        }
    )
    status = kb.jobs.wait(job.id)
    assert status.state == "succeeded", status.error_code
    return status


def make_format_one_database(path: Path) -> None:
    """Create an empty database with the format-1 DDL and receipt."""
    legacy = migration_sql().replace(" CHECK(schema_version=2)", "")
    db = sqlite3.connect(path)
    try:
        db.executescript(legacy)
        db.execute(
            "INSERT INTO schema_migrations VALUES (1, ?, '2026-01-01T00:00:00+00:00')",
            (FORMAT_ONE_CHECKSUM,),
        )
        db.commit()
    finally:
        db.close()
