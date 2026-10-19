"""Persisted family nonces make family identity independent of configuration alone."""

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.adapters.sqlite.repositories import SqliteUnitOfWork
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import family_id, new_family_nonce
from tychora.contracts.snapshots import IndexFamily


def _family(workspace_id: str, identity: str, nonce: str) -> IndexFamily:
    return IndexFamily(
        family_id=family_id(workspace_id, identity, nonce),
        channel="lexical",
        backend="sqlite-fts5",
        config_sha=identity,
        config_json=FrozenJsonMap({"effective_config_sha": identity}),
        backend_namespace="lexical",
        created_at="2026-10-16T00:00:00+00:00",
    )


@pytest.fixture
def seeded(tmp_path):
    db = open_corpus(tmp_path / "c.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    bundle = seed_bundle()
    store.commit_extraction(bundle)
    return SqliteUnitOfWork(db), bundle.workspace.workspace_id


def test_nonce_round_trip_and_idempotent_create(seeded):
    uow, workspace = seeded
    with uow.read() as tx:
        assert tx.families.nonce("lexical", "c" * 64) is None
        assert tx.families.get("f" * 64) is None
    nonce = new_family_nonce()
    family = _family(workspace, "c" * 64, nonce)
    with uow.write() as tx:
        tx.families.create(family, nonce, "c" * 64)
        tx.families.create(family, nonce, "c" * 64)
    with uow.read() as tx:
        assert tx.families.nonce("lexical", "c" * 64) == nonce
        assert tx.families.nonce("text", "c" * 64) is None
        assert tx.families.get(family.family_id) == family


def test_create_refuses_a_different_row_under_the_same_family_id(seeded):
    uow, workspace = seeded
    nonce = new_family_nonce()
    family = _family(workspace, "c" * 64, nonce)
    with uow.write() as tx:
        tx.families.create(family, nonce, "c" * 64)
    forged = family.model_copy(update={"backend_namespace": "forged"})
    with pytest.raises(TychoraError) as error, uow.write() as tx:
        tx.families.create(forged, nonce, "c" * 64)
    assert error.value.reason == "canonical.binding_mismatch"


def test_read_transaction_cannot_create_a_family(seeded):
    uow, workspace = seeded
    nonce = new_family_nonce()
    with pytest.raises(TychoraError), uow.read() as tx:
        tx.families.create(_family(workspace, "c" * 64, nonce), nonce, "c" * 64)


def test_two_nonces_give_two_families_for_one_configuration():
    first = _family("w", "c" * 64, new_family_nonce())
    second = _family("w", "c" * 64, new_family_nonce())
    assert first.family_id != second.family_id
