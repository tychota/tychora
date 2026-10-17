"""An older workspace is recreated from its originals; the old one is untouched."""

import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter as Counter
from tests.helpers.legacy import add_text as _add
from tests.helpers.legacy import make_format_one
from tychora import Tychora
from tychora.api import rebuild_workspace_from
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


def _read(kb, sql: str, *parameters):
    owner = kb._workspace.owner

    def read():
        return owner.db.execute(sql, parameters).fetchall()

    return kb._workspace.bridge.call(owner.run_blocking(read, ExecutionContext()))


def _revision_and_source(kb, key: str) -> tuple[str, str]:
    return tuple(
        _read(
            kb,
            "SELECT r.revision_id,s.source_id FROM revisions r JOIN sources s "
            "USING(source_id) WHERE s.source_key=? ORDER BY r.captured_at LIMIT 1",
            key,
        )[0]
    )


@contextmanager
def _edit(root: Path):
    """Edit a closed workspace's database and close the connection for good."""
    with closing(sqlite3.connect(root / "corpus.sqlite3")) as db, db:
        yield db


def _fingerprint(root: Path) -> dict[str, str]:
    """Hash of every file of the workspace, which a rebuild never touches."""
    return {
        str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else "dir"
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def old(tmp_path):
    root = tmp_path / "old"
    with Tychora.open(root, token_counter=Counter()) as kb:
        _add(kb, "alpha", "Alpha verification outcome.")
        _add(kb, "beta", "Beta first edition.")
        _add(kb, "beta", "Beta second edition.")
        _add(kb, "gamma", "Gamma withdrawn later.")
        alpha_revision, _ = _revision_and_source(kb, "alpha")
        beta_first, _ = _revision_and_source(kb, "beta")
        _, gamma_source = _revision_and_source(kb, "gamma")
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.store.record_review(
                    ReviewTarget(revision_id=alpha_revision), "accepted", "reviewer"
                ),
                ExecutionContext(),
            )
        )
    make_format_one(root)
    with _edit(root) as db:
        db.execute(
            "UPDATE sources SET withdrawn_at='2026-01-01T00:00:00+00:00' WHERE source_id=?",
            (gamma_source,),
        )
        db.execute(
            "UPDATE artifacts SET payload_state='deleted', "
            "payload_deleted_at='2026-01-01T00:00:00+00:00' "
            "WHERE artifact_sha=(SELECT original_sha FROM revisions WHERE revision_id=?)",
            (beta_first,),
        )
    return root, alpha_revision


def test_rebuild_reports_skipped_items_and_leaves_old_unchanged(old, tmp_path):
    root, alpha_revision = old
    before = _fingerprint(root)
    report = rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    assert _fingerprint(root) == before
    assert {(item.source_key, item.revisions) for item in report.sources} == {
        ("alpha", 1),
        ("beta", 1),
    }
    assert {(item.subject, item.reason) for item in report.skipped} == {
        ("source", "withdrawn"),
        ("revision", "original_removed"),
    }
    assert report.reviews_replayed == 1
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        result = kb.search({"question": "verification", "modalities": ["lexical"]})
        assert result.segments
        assert _revision_and_source(kb, "alpha")[0] == alpha_revision


def test_rebuild_applies_the_kind_map(old, tmp_path):
    root, _ = old
    report = rebuild_workspace_from(
        root,
        tmp_path / "new",
        kind_map={"document@1": "missing@1"},
        token_counter=Counter(),
    )
    assert report.sources == ()
    assert {item.reason for item in report.skipped} >= {"unsupported_kind"}


def test_rebuild_refuses_non_empty_or_same_destination(old, tmp_path):
    root, _ = old
    before = _fingerprint(root)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("x")
    for destination in (occupied, root, root / "nested"):
        with pytest.raises(TychoraError) as error:
            rebuild_workspace_from(root, destination, token_counter=Counter())
        assert error.value.reason == "rebuild.destination_not_empty"
    assert not (root / "nested").exists()
    assert _fingerprint(root) == before
    assert [path.name for path in occupied.iterdir()] == ["file"]


def test_rebuild_lists_a_missing_original_and_never_fabricates_it(old, tmp_path):
    root, _ = old
    with _edit(root) as db:
        sha = db.execute(
            "SELECT storage_key FROM artifacts a JOIN revisions r ON "
            "a.artifact_sha=r.original_sha JOIN sources s USING(source_id) "
            "WHERE s.source_key='alpha'"
        ).fetchone()[0]
    for path in (root / "artifacts").rglob("*"):
        if path.is_file() and sha.split("/")[-1] in path.name:
            path.unlink()
    report = rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    assert ("revision", "original_missing") in {
        (item.subject, item.reason) for item in report.skipped
    }
    assert "alpha" not in {item.source_key for item in report.sources}


def test_rebuild_never_lowers_access_floors_or_revision_policy(old, tmp_path):
    root, alpha_revision = old
    with _edit(root) as db:
        db.execute(
            "UPDATE sources SET access_floor=2 WHERE source_key='alpha'",
        )
        db.execute(
            "UPDATE revisions SET access_rank=1, allowed_uses_json='[\"lookup\"]', "
            "declared_review_state='needs_review' WHERE revision_id=?",
            (alpha_revision,),
        )
        db.execute("UPDATE collections SET access_floor=1 WHERE slug='default'")
    report = rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    assert "alpha" in {item.source_key for item in report.sources}
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        assert _read(
            kb, "SELECT access_floor FROM sources WHERE source_key='alpha'"
        ) == [(2,)]
        assert _read(
            kb, "SELECT access_floor FROM collections WHERE slug='default'"
        ) == [(1,)]
        assert _read(
            kb,
            "SELECT access_rank,allowed_uses_json,declared_review_state "
            "FROM revisions WHERE revision_id=?",
            alpha_revision,
        ) == [(1, '["lookup"]', "needs_review")]
        assert _read(
            kb, "SELECT access_floor FROM sources WHERE source_key='beta'"
        ) == [(0,)]
