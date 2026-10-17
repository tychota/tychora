"""Format-2 workspaces open; format-1 workspaces and archives are refused untouched."""

import shutil
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import make_format_one, make_format_one_database
from tychora import Tychora
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.migrations import (
    FORMAT_ONE_CHECKSUM,
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    migration_sql,
    open_verified_import,
)
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _format_version(kb) -> int:
    owner = kb._workspace.owner

    def read() -> int:
        with owner.store.uow.read() as tx:
            return tx.workspace.format_version()

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


def _fingerprint(root: Path) -> dict[str, str]:
    """Hash the database and its log; the shared-memory index only counts as present."""
    return {
        path.name: "present"
        if path.name.endswith("-shm")
        else sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("corpus.sqlite3*"))
    }


def _crashed_copy(root: Path, destination: Path) -> Path:
    """Copy a workspace as a crashed owner leaves it: format 1 with a live -wal."""
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
    return destination


def test_packaged_migration_is_format_two():
    assert SCHEMA_VERSION == 2
    assert "schema_version INTEGER NOT NULL CHECK(schema_version=2)" in migration_sql()
    assert sha256(migration_sql().encode()).hexdigest() == SCHEMA_CHECKSUM


def test_new_workspace_records_format_two(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=Counter()) as kb:
        assert _format_version(kb) == 2


@pytest.mark.parametrize("crashed", [False, True])
def test_format_one_workspace_is_refused_untouched(tmp_path, crashed):
    root = tmp_path / "ws"
    with Tychora.open(root, token_counter=Counter()):
        pass
    if crashed:
        root = _crashed_copy(root, tmp_path / "crashed")
        assert (root / "corpus.sqlite3-wal").stat().st_size > 0
    else:
        make_format_one(root)
    before = _fingerprint(root)
    with pytest.raises(TychoraError) as error:
        Tychora.open(root, token_counter=Counter())
    assert (error.value.code, error.value.reason) == (
        "PROFILE_INCOMPATIBLE",
        "workspace.format_old",
    )
    assert "manage rebuild workspace --from" in error.value.message
    assert _fingerprint(root) == before


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
