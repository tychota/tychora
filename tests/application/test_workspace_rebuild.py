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


class _Wrapped:
    """A real rebuild target whose calls a test can intercept."""

    def __init__(self, target, *, fail_add_after=None, on_wait=None, hang=False):
        self._target = target
        self._adds = 0
        self._fail_add_after = fail_add_after
        self._on_wait = on_wait
        self._hang = hang
        self.cancelled: list[str] = []

    def __getattr__(self, name):
        return getattr(self._target, name)

    def add(self, source):
        if self._fail_add_after is not None and self._adds >= self._fail_add_after:
            raise RuntimeError("add failed")
        self._adds += 1
        return self._target.add(source)

    def wait(self, job_id):
        state = self._target.wait(job_id)
        if self._on_wait:
            self._on_wait()
        return "running" if self._hang else state

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return self._target.cancel(job_id)


def _strict_old(root):
    with _edit(root) as db:
        db.execute("UPDATE sources SET access_floor=2 WHERE source_key='alpha'")
        db.execute("UPDATE sources SET access_floor=3 WHERE source_key='beta'")


def _floors(destination: Path) -> dict[str, int]:
    """Read a destination whose rebuild stopped early; the marker is cleared first."""
    (destination / ".rebuild-incomplete").unlink(missing_ok=True)
    with Tychora.open(destination, token_counter=Counter()) as kb:
        return dict(_read(kb, "SELECT source_key,access_floor FROM sources"))


def _run(root, destination, wrap, ctx=None):

    from tychora.api import open_rebuild_target
    from tychora.application.workspace_rebuild import rebuild_workspace

    return rebuild_workspace(
        root,
        destination,
        ctx or ExecutionContext(),
        open_target=lambda path, scope: wrap(
            open_rebuild_target(path, scope, token_counter=Counter())
        ),
    )


def test_policy_is_restored_when_an_add_raises_mid_rebuild(old, tmp_path):
    root, _ = old
    _strict_old(root)
    with pytest.raises(RuntimeError, match="add failed"):
        _run(root, tmp_path / "new", lambda t: _Wrapped(t, fail_add_after=1))
    assert _floors(tmp_path / "new")["alpha"] == 2


def test_policy_is_restored_when_the_rebuild_is_cancelled(old, tmp_path):
    root, _ = old
    _strict_old(root)
    ctx = ExecutionContext()
    with pytest.raises(TychoraError) as error:
        _run(
            root,
            tmp_path / "new",
            lambda t: _Wrapped(t, on_wait=ctx.cancelled.set),
            ctx,
        )
    assert error.value.code == "CANCELLED"
    assert _floors(tmp_path / "new")["alpha"] == 2


def test_a_job_left_running_at_the_deadline_is_cancelled_and_reported(old, tmp_path):
    root, _ = old
    _strict_old(root)
    holder = []

    def wrap(target):
        holder.append(_Wrapped(target, hang=True))
        return holder[0]

    report = _run(root, tmp_path / "new", wrap)
    assert holder[0].cancelled
    assert ("revision", "timed_out") in {
        (item.subject, item.reason) for item in report.skipped
    }
    assert _floors(tmp_path / "new")["alpha"] == 2


def test_a_unit_rejected_in_the_old_workspace_stays_rejected(old, tmp_path):
    root, _ = old
    with _edit(root) as db:
        unit = db.execute(
            "SELECT e.unit_id FROM evidence_units e JOIN extractions x USING(extraction_id) "
            "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
            "WHERE s.source_key='alpha' ORDER BY e.unit_id LIMIT 1"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO review_events(unit_id,state,actor_id,created_at) VALUES "
            "(?,'rejected','reviewer','2026-01-01T00:00:00+00:00')",
            (unit,),
        )
        db.execute(
            "INSERT INTO review_events(asset_id,state,actor_id,created_at) VALUES "
            "('missing-asset','rejected','reviewer','2026-01-01T00:00:00+00:00')"
        )
    report = rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    with Tychora.open(tmp_path / "new", token_counter=Counter()) as kb:
        assert _read(
            kb,
            "SELECT state FROM review_events WHERE unit_id=? "
            "ORDER BY review_event_id DESC LIMIT 1",
            unit,
        ) == [("rejected",)]
    missing = [item for item in report.skipped if item.subject == "review"]
    assert [(item.key, item.reason) for item in missing] == [
        ("asset:missing-asset", "review_target_missing")
    ]


def test_a_symlink_to_the_old_workspace_is_refused(old, tmp_path):
    root, _ = old
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    before = _fingerprint(root)
    with pytest.raises(TychoraError) as error:
        rebuild_workspace_from(root, link, token_counter=Counter())
    assert error.value.reason == "rebuild.destination_not_empty"
    assert _fingerprint(root) == before


def test_a_stricter_new_value_is_never_lowered(old, tmp_path):
    from tychora.adapters.sqlite.workspace_inventory import (
        read_inventory,
        restore_policy,
    )

    root, alpha_revision = old
    inventory = read_inventory(root)
    rebuild_workspace_from(root, tmp_path / "new", token_counter=Counter())
    with _edit(tmp_path / "new") as db:
        db.execute("UPDATE sources SET access_floor=3 WHERE source_key='alpha'")
        db.execute(
            "UPDATE revisions SET access_rank=2,allowed_uses_json='[]' "
            "WHERE revision_id=?",
            (alpha_revision,),
        )
    with closing(sqlite3.connect(tmp_path / "new" / "corpus.sqlite3")) as db:
        db.isolation_level = None
        workspace = db.execute("SELECT workspace_id FROM workspaces").fetchone()[0]
        restore_policy(db, workspace, inventory.sources)
        assert db.execute(
            "SELECT access_floor FROM sources WHERE source_key='alpha'"
        ).fetchone() == (3,)
        assert db.execute(
            "SELECT access_rank,allowed_uses_json FROM revisions WHERE revision_id=?",
            (alpha_revision,),
        ).fetchone() == (2, "[]")


def test_an_incomplete_marker_exists_during_the_rebuild_and_goes_on_success(
    old, tmp_path
):
    root, _ = old
    destination = tmp_path / "new"
    seen = []

    def wrap(target):
        return _Wrapped(
            target,
            on_wait=lambda: seen.append((destination / ".rebuild-incomplete").exists()),
        )

    _run(root, destination, wrap)
    assert seen
    assert all(seen)
    assert not (destination / ".rebuild-incomplete").exists()
    with Tychora.open(destination, token_counter=Counter()):
        pass


def test_a_rebuild_that_does_not_finish_leaves_a_workspace_that_will_not_open(
    old, tmp_path
):
    root, _ = old
    destination = tmp_path / "new"
    with pytest.raises(RuntimeError, match="add failed"):
        _run(root, destination, lambda t: _Wrapped(t, fail_add_after=1))
    assert (destination / ".rebuild-incomplete").exists()
    with pytest.raises(TychoraError) as error:
        Tychora.open(destination, token_counter=Counter())
    assert error.value.reason == "rebuild.incomplete"
    assert "Delete it and rerun" in str(error.value)


class _Failing(_Wrapped):
    """A rebuild target whose cleanup steps raise."""

    def __init__(self, target, *, fail=(), **kwargs):
        super().__init__(target, **kwargs)
        self._fail = fail
        self.calls: list[str] = []

    def stop_jobs(self):
        self.calls.append("stop_jobs")
        if "stop_jobs" in self._fail:
            raise OSError("stop boom")
        self._target.stop_jobs()

    def restore_policy(self, sources):
        self.calls.append("restore_policy")
        if "restore_policy" in self._fail:
            raise OSError("restore boom")
        return self._target.restore_policy(sources)

    def close(self):
        self.calls.append("close")
        self._target.close()
        if "close" in self._fail:
            raise OSError("close boom")


def test_the_original_error_stays_visible_when_cleanup_fails_too(old, tmp_path):
    root, _ = old
    with pytest.raises(RuntimeError, match="add failed") as error:
        _run(
            root,
            tmp_path / "new",
            lambda t: _Failing(t, fail=("restore_policy", "close"), fail_add_after=1),
        )
    assert error.value.__notes__ == [
        "cleanup also failed: OSError: restore boom",
        "cleanup also failed: OSError: close boom",
    ]
    assert (tmp_path / "new" / ".rebuild-incomplete").exists()


def test_a_cleanup_failure_after_a_good_rebuild_is_raised(old, tmp_path):
    root, _ = old
    with pytest.raises(OSError, match="restore boom"):
        _run(root, tmp_path / "new", lambda t: _Failing(t, fail=("restore_policy",)))
    assert (tmp_path / "new" / ".rebuild-incomplete").exists()


def test_the_job_service_stops_before_the_final_restore(old, tmp_path):
    root, _ = old
    holder = []

    def wrap(target):
        holder.append(_Failing(target))
        return holder[0]

    _run(root, tmp_path / "new", wrap)
    assert holder[0].calls == ["stop_jobs", "restore_policy", "close"]


def test_a_stopped_job_service_refuses_new_jobs_but_the_workspace_stays_usable(
    old, tmp_path
):
    from tests.helpers.legacy import (
        add_text,  # noqa: F401 - keeps the helper import local
    )
    from tychora.adapters.sqlite.workspace_inventory import read_inventory
    from tychora.api import open_rebuild_target
    from tychora.application.workspace_rebuild import rebuild_scope
    from tychora.contracts.requests import SourceInput

    root, _ = old
    inventory = read_inventory(root)
    target = open_rebuild_target(
        tmp_path / "new", rebuild_scope(inventory.collections), token_counter=Counter()
    )
    try:
        target.seed_collections(inventory.collections)
        target.stop_jobs()
        source = SourceInput.from_mapping(
            {
                "type": "text",
                "source_key": "late",
                "collection_id": inventory.collections[0].collection_id,
                "kind": "document@1",
                "format": "text",
                "text": "Late.",
            }
        )
        with pytest.raises(TychoraError) as error:
            target.add(source)
        assert error.value.code == "WORKSPACE_BUSY"
        assert target.restore_policy(inventory.sources) >= 0
    finally:
        target.close()
