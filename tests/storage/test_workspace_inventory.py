"""An older workspace is enumerated read-only; collections seed a new one."""

import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text as _add
from tests.helpers.legacy import make_format_one
from tychora import Tychora
from tychora.adapters.sqlite import receipt
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.workspace_inventory import (
    InventoryCollection,
    read_inventory,
    read_original,
    seed_collections,
)
from tychora.contracts.errors import TychoraError

_PURGE = (
    "UPDATE artifacts SET payload_state='deleted', "
    "payload_deleted_at='2026-01-01T00:00:00+00:00' WHERE artifact_sha=?"
)


@pytest.fixture
def old_workspace(tmp_path):
    root = tmp_path / "old"
    with Tychora.open(root, token_counter=Counter()) as kb:
        _add(kb, "alpha", "Alpha text.")
        _add(kb, "beta", "Beta first edition.")
        _add(kb, "beta", "Beta second edition.")
    make_format_one(root)
    return root


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("corpus.sqlite3*"))
    }


def _crashed_with_purge(root: Path, destination: Path, sha: str) -> Path:
    """Copy a workspace as a crashed owner leaves it: the purge lives only in the -wal."""
    keeper = sqlite3.connect(root / "corpus.sqlite3")
    try:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(_PURGE, (sha,))
        keeper.commit()
        shutil.copytree(root, destination)
    finally:
        keeper.close()
    assert (destination / "corpus.sqlite3-wal").stat().st_size > 0
    return destination


def test_inventory_lists_sources_revisions_and_originals(old_workspace):
    before = sha256((old_workspace / "corpus.sqlite3").read_bytes()).hexdigest()
    inventory = read_inventory(old_workspace)
    keys = [(source.source_key, len(source.revisions)) for source in inventory.sources]
    assert keys == [("alpha", 1), ("beta", 2)]
    assert inventory.collections[0].slug == "default"
    first = inventory.sources[1].revisions[0]
    assert first.original is not None
    assert read_original(old_workspace, first.original) == b"Beta first edition."
    assert sha256((old_workspace / "corpus.sqlite3").read_bytes()).hexdigest() == before


def test_inventory_marks_purged_originals(old_workspace):
    inventory = read_inventory(old_workspace)
    purged = inventory.sources[1].revisions[0].original
    assert purged is not None
    with sqlite3.connect(old_workspace / "corpus.sqlite3") as db:
        db.execute(_PURGE, (purged.sha256,))
    assert read_inventory(old_workspace).sources[1].revisions[0].original is None


def test_inventory_includes_wal_only_commits_and_leaves_files_alone(
    old_workspace, tmp_path
):
    purged = read_inventory(old_workspace).sources[1].revisions[0].original
    assert purged is not None
    crashed = _crashed_with_purge(old_workspace, tmp_path / "crashed", purged.sha256)
    # The database file alone still shows the original as present.
    with closing(
        sqlite3.connect(
            (crashed / "corpus.sqlite3").as_uri() + "?immutable=1", uri=True
        )
    ) as plain:
        assert plain.execute(
            "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (purged.sha256,)
        ).fetchone() == ("present",)
    before = _fingerprint(crashed)
    names = sorted(path.name for path in crashed.rglob("*"))
    assert {"corpus.sqlite3-wal"} <= set(before)
    assert read_inventory(crashed).sources[1].revisions[0].original is None
    assert _fingerprint(crashed) == before
    assert sorted(path.name for path in crashed.rglob("*")) == names


def test_inventory_refuses_a_database_that_changes_while_it_is_copied(
    old_workspace, tmp_path, monkeypatch
):
    purged = read_inventory(old_workspace).sources[1].revisions[0].original
    assert purged is not None
    crashed = _crashed_with_purge(old_workspace, tmp_path / "crashed", purged.sha256)
    real_copy = shutil.copyfile

    def copy_then_append(source, destination, **kwargs):
        result = real_copy(source, destination, **kwargs)
        if str(source).endswith("-wal"):
            with open(source, "ab") as stream:
                stream.write(b"\0")
        return result

    monkeypatch.setattr(receipt.shutil, "copyfile", copy_then_append)
    with pytest.raises(TychoraError) as error:
        read_inventory(crashed)
    assert error.value.reason == "workspace.database_invalid"


def test_inventory_refuses_a_live_owner(tmp_path):
    root = tmp_path / "live"
    with (
        Tychora.open(root, token_counter=Counter()),
        pytest.raises(TychoraError) as error,
    ):
        read_inventory(root)
    assert error.value.reason == "owner.live"


def test_inventory_refuses_a_missing_database(tmp_path):
    with pytest.raises(TychoraError) as error:
        read_inventory(tmp_path)
    assert error.value.reason == "workspace.absent"


def test_read_original_reports_missing_and_damaged_payloads(old_workspace):
    original = read_inventory(old_workspace).sources[0].revisions[0].original
    assert original is not None
    payload = old_workspace / "artifacts" / original.storage_key
    damaged = b"X" * original.size_bytes
    payload.write_bytes(damaged)
    with pytest.raises(TychoraError) as error:
        read_original(old_workspace, original)
    assert error.value.reason == "payload.hash_mismatch"
    payload.unlink()
    assert read_original(old_workspace, original) is None
    with pytest.raises(TychoraError):
        read_original(old_workspace, original, max_bytes=1)


def test_seed_collections_inserts_and_refreshes(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    db.execute(
        "INSERT INTO workspaces(workspace_id,schema_version,created_at) "
        "VALUES ('ws',2,'2026-01-01T00:00:00+00:00')"
    )
    rows = (
        InventoryCollection(
            collection_id="00000000-0000-4000-8000-000000000001",
            slug="default",
            title="Default",
            access_floor=0,
            policy_json='{"schema_version":1,"upload_providers":["voyage"]}',
        ),
        InventoryCollection(
            collection_id="55825936-6a6a-4d29-945a-327d771a76b7",
            slug="manuals",
            title="Manuals",
            access_floor=1,
            policy_json='{"schema_version":2,"upload_providers":[]}',
        ),
    )
    assert seed_collections(db, "ws", rows) == 2
    assert seed_collections(db, "ws", rows) == 0
    assert db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0] == 2
    db.close()
