"""Format-2 workspaces open; format-1 workspaces and archives are refused untouched."""

import fcntl
import os
import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import make_format_one, make_format_one_database
from tychora import Tychora
from tychora.adapters.sqlite import connection
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.migrations import (
    FORMAT_ONE_CHECKSUM,
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    migration_sql,
    open_verified_import,
)
from tychora.adapters.sqlite.receipt import open_untouched
from tychora.contracts.errors import TychoraError


def _format_version(root: Path) -> int:
    with closing(sqlite3.connect(root / "corpus.sqlite3")) as db:
        return db.execute("SELECT schema_version FROM workspaces").fetchone()[0]


_SUFFIXES = {"": (), "-wal-shm": ("-wal", "-shm"), "-wal": ("-wal",), "-shm": ("-shm",)}


def _fingerprint(root: Path) -> dict[str, str]:
    """Hash the database, its -wal and its -shm; extra or missing files show up."""
    return {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("corpus.sqlite3*"))
    }


def _crashed_copy(root: Path, destination: Path, sidecars: str) -> Path:
    """Copy a workspace as a crashed owner leaves it: format 1 plus chosen sidecars."""
    keeper = sqlite3.connect(root / "corpus.sqlite3")
    try:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            "UPDATE schema_migrations SET version=1, checksum=?",
            (FORMAT_ONE_CHECKSUM,),
        )
        keeper.commit()
        shutil.copytree(root, destination)
    finally:
        keeper.close()
    shm = (destination / "corpus.sqlite3-shm").read_bytes()
    if "-wal" not in sidecars:
        # Without its -wal the receipt change is lost, so write it into the file.
        for suffix in ("-wal", "-shm"):
            (destination / f"corpus.sqlite3{suffix}").unlink(missing_ok=True)
        make_format_one(destination)
    if "-shm" in sidecars:
        (destination / "corpus.sqlite3-shm").write_bytes(shm)
    elif "-shm" not in sidecars:
        (destination / "corpus.sqlite3-shm").unlink(missing_ok=True)
    return destination


def test_packaged_migration_is_format_two():
    assert SCHEMA_VERSION == 2
    assert "schema_version INTEGER NOT NULL CHECK(schema_version=2)" in migration_sql()
    assert sha256(migration_sql().encode()).hexdigest() == SCHEMA_CHECKSUM


def test_new_workspace_records_format_two(tmp_path):
    root = tmp_path / "ws"
    with Tychora.open(root, token_counter=Counter()):
        pass
    assert _format_version(root) == 2


@pytest.mark.parametrize("sidecars", ["", "-wal-shm", "-wal", "-shm"])
def test_format_one_workspace_is_refused_untouched(tmp_path, sidecars):
    root = tmp_path / "ws"
    with Tychora.open(root, token_counter=Counter()):
        pass
    if sidecars:
        root = _crashed_copy(root, tmp_path / "crashed", sidecars)
    else:
        make_format_one(root)
    present = {path.name for path in root.glob("corpus.sqlite3-*")}
    assert present == {f"corpus.sqlite3{suffix}" for suffix in _SUFFIXES[sidecars]}
    before = _fingerprint(root)
    runtime_before = sorted(path.name for path in root.iterdir())
    with pytest.raises(TychoraError) as error:
        Tychora.open(root, token_counter=Counter())
    assert (error.value.code, error.value.reason) == (
        "PROFILE_INCOMPATIBLE",
        "workspace.format_old",
    )
    assert "manage rebuild workspace --from" in error.value.message
    assert _fingerprint(root) == before
    assert sorted(path.name for path in root.iterdir()) == runtime_before


def test_unreadable_database_is_refused_not_opened_read_write(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    path.write_bytes(b"not a database" * 100)
    before = _fingerprint(tmp_path)
    with pytest.raises(TychoraError) as error:
        open_corpus(path)
    assert error.value.reason == "workspace.database_invalid"
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize("genuine_ddl", [True, False])
def test_format_one_archive_import_is_refused(tmp_path, genuine_ddl):
    database = tmp_path / "corpus.sqlite3"
    if genuine_ddl:
        make_format_one_database(database)
    else:
        open_corpus(database, initialize=True).close()
        make_format_one(tmp_path)
    with pytest.raises(TychoraError) as error:
        open_verified_import(database)
    assert error.value.reason == "workspace.format_old"


def test_unknown_receipt_is_still_incompatible(tmp_path):
    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    db.execute("UPDATE schema_migrations SET version=7")
    db.close()
    with pytest.raises(TychoraError) as error:
        open_corpus(path)
    assert error.value.code == "PROFILE_INCOMPATIBLE"
    assert error.value.reason == "workspace.schema_mismatch"


def test_owner_starting_during_format_read_is_busy_not_integrity(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    with Tychora.open(root, token_counter=Counter()):
        pass
    held: list[int] = []

    def owner_starts_mid_read(path):
        fd = os.open(root / ".runtime" / "owner.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(fd)
        raise TychoraError(
            "INTEGRITY_ERROR",
            "The workspace database cannot be inspected.",
            reason="workspace.database_invalid",
        )

    monkeypatch.setattr(connection, "refuse_older_format", owner_starts_mid_read)
    try:
        with pytest.raises(TychoraError) as error:
            connection.refuse_owned_or_older(root)
        assert (error.value.code, error.value.reason) == (
            "WORKSPACE_BUSY",
            "owner.live",
        )
    finally:
        for fd in held:
            os.close(fd)


@pytest.mark.parametrize("sidecar", [False, True])
def test_caller_error_inside_untouched_read_keeps_its_type(tmp_path, sidecar):
    path = tmp_path / "corpus.sqlite3"
    open_corpus(path, initialize=True).close()
    if sidecar:
        (tmp_path / "corpus.sqlite3-shm").write_bytes(b"")
    with pytest.raises(PermissionError, match="caller"), open_untouched(path):
        raise PermissionError("caller")
