"""Untrusted archive members and SQLite objects fail before activation."""

import sqlite3

import pytest

from tychora.adapters.files.archive import ArchiveReader, ArchiveWriter
from tychora.adapters.sqlite.migrations import initialize_schema, open_verified_import
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def test_imported_sqlite_rejects_unknown_trigger_before_table_reads(tmp_path):
    database = tmp_path / "corpus.sqlite3"
    db = sqlite3.connect(database)
    try:
        initialize_schema(db)
        db.execute(
            "CREATE TRIGGER archive_supplied AFTER INSERT ON workspaces "
            "BEGIN SELECT 1; END"
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(TychoraError) as error:
        open_verified_import(database)
    assert error.value.code == "PROFILE_INCOMPATIBLE"


def test_archive_reader_rejects_symlink_member_and_cleans_stage(tmp_path):
    source = tmp_path / "archive"
    source.mkdir()
    (source / "artifacts").mkdir()
    (source / "corpus.sqlite3").write_bytes(b"SQLite")
    (source / "backup-manifest.json").write_bytes(b"{}")
    (source / "backup-receipt.json").symlink_to(source / "backup-manifest.json")
    writer = ArchiveWriter(tmp_path / "restored")
    with pytest.raises(TychoraError) as error, writer:
        ArchiveReader(source).copy_to(writer.stage, ExecutionContext())
    assert error.value.code == "INTEGRITY_ERROR"
    assert not writer.stage.exists()
    assert not writer.destination.exists()


def test_archive_reader_enforces_lowered_byte_limit(tmp_path):
    source = tmp_path / "archive"
    source.mkdir()
    (source / "artifacts").mkdir()
    for name in ("corpus.sqlite3", "backup-manifest.json", "backup-receipt.json"):
        (source / name).write_bytes(b"x")
    writer = ArchiveWriter(tmp_path / "restored")
    with pytest.raises(TychoraError) as error, writer:
        ArchiveReader(source, max_entries=4, max_expanded_bytes=2).copy_to(
            writer.stage, ExecutionContext()
        )
    assert error.value.code == "INTEGRITY_ERROR"
    assert not writer.destination.exists()


def test_archive_reader_rejects_symlink_ancestor(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real = real_parent / "archive"
    real.mkdir()
    (real / "artifacts").mkdir()
    for name in ("corpus.sqlite3", "backup-manifest.json", "backup-receipt.json"):
        (real / name).write_bytes(b"x")
    (tmp_path / "alias").symlink_to(real_parent, target_is_directory=True)
    with (
        ArchiveWriter(tmp_path / "restored") as writer,
        pytest.raises(TychoraError) as error,
    ):
        ArchiveReader(tmp_path / "alias" / "archive").copy_to(
            writer.stage, ExecutionContext()
        )
    assert error.value.code == "INTEGRITY_ERROR"
