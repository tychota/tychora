"""Unretained snapshots, orphan pages and orphan batches are collected."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.api.test_public_sync import CharacterCounter
from tychora import SourceInput, Tychora
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.adapters.sqlite.repositories import batches as batch_rows
from tychora.adapters.sqlite.repositories import snapshots as snapshot_rows
from tychora.application.removal import RemovalService
from tychora.application.snapshot_gc import SnapshotGc
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


class _Rows:
    """Rows fetched on the owner thread."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _OwnerDb:
    """Runs statements on the owner thread, which owns the connection."""

    def __init__(self, owner) -> None:
        self._owner = owner

    def execute(self, sql: str, args: tuple = ()) -> _Rows:
        return _Rows(
            _on_owner(self._owner, lambda: self._owner.db.execute(sql, args).fetchall())
        )

    def commit(self) -> None:
        _on_owner(self._owner, self._owner.db.commit)


def _on_owner(owner, call):
    return owner.run_blocking_sync(call, ExecutionContext())


def _db(owner) -> _OwnerDb:
    return _OwnerDb(owner)


def _active(owner) -> str:
    snapshot_id = _on_owner(owner, owner.store.active_snapshot_id)
    assert snapshot_id is not None
    return snapshot_id


@contextmanager
def _pinned(owner, snapshot_id: str):
    """Hold a reader's pin of one snapshot, as a query does."""
    pin = owner.store.pin_snapshot(snapshot_id)
    view = _on_owner(owner, pin.__enter__)
    try:
        yield view
    finally:
        pin.__exit__(None, None, None)


ORPHAN_BATCHES = (
    "SELECT COUNT(*) FROM index_batches b WHERE NOT EXISTS "
    "(SELECT 1 FROM snapshot_batches s WHERE s.batch_id=b.batch_id)"
)


def _add(kb, key: str, text: str):
    job = kb.sources.add(
        SourceInput(
            type="text",
            source_key=key,
            collection_id=kb.default_collection_id,
            kind="manual@1",
            format="text",
            metadata={"title": key, "publisher": "Example Team", "edition": "1"},
            text=text,
        )
    )
    return kb.jobs.wait(job.id)


def _count(db, sql, args=()):
    return db.execute(sql, args).fetchone()[0]


def _snapshots(db) -> list[str]:
    return [
        row[0]
        for row in db.execute(
            "SELECT snapshot_id FROM snapshots ORDER BY published_at,snapshot_id"
        )
    ]


def _retained_shas(db) -> set[str]:
    """Every artifact a stored snapshot, page or batch row binds."""
    return {
        row[0]
        for row in db.execute(
            "SELECT manifest_sha FROM snapshots UNION SELECT manifest_sha "
            "FROM membership_pages UNION SELECT input_manifest_sha FROM index_batches "
            "UNION SELECT vectors_sha FROM index_batches WHERE vectors_sha IS NOT NULL"
        )
    }


def _payload_state(db, sha: str) -> str:
    return db.execute(
        "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
    ).fetchone()[0]


def _storage_path(owner, sha: str) -> Path:
    key = (
        _db(owner)
        .execute("SELECT storage_key FROM artifacts WHERE artifact_sha=?", (sha,))
        .fetchone()[0]
    )
    return owner.artifacts.root / key


def _collect(owner, **options):
    return owner.run_blocking_sync(
        lambda: SnapshotGc(owner).collect(ExecutionContext(), **options),
        ExecutionContext(),
    )


def _assert_retained_payloads_present(owner) -> None:
    for sha in _retained_shas(_db(owner)):
        assert _payload_state(_db(owner), sha) == "present", sha


def test_post_publication_step_keeps_the_last_sixteen(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        db = _db(owner)
        for index in range(20):
            assert (
                _add(
                    kb, "same.md", f"Version {index} of the verification outcome."
                ).state
                == "succeeded"
            )
        assert _count(db, "SELECT COUNT(*) FROM snapshots") == 16
        assert (
            _count(
                db,
                "SELECT COUNT(*) FROM membership_pages WHERE page_id NOT IN "
                "(SELECT page_id FROM snapshot_pages)",
            )
            == 0
        )
        assert _count(db, ORPHAN_BATCHES) == 0
        assert _count(db, "SELECT COUNT(*) FROM lexical_records") == _count(
            db,
            "SELECT COUNT(*) FROM lexical_records l WHERE l.batch_id IN "
            "(SELECT batch_id FROM snapshot_batches)",
        )
        # The post-publication step only marks released payloads; their files
        # stay until `manage gc` reclaims them.
        released = [
            row[0]
            for row in db.execute(
                "SELECT artifact_sha FROM artifacts WHERE payload_state='delete_pending'"
            )
        ]
        assert released
        assert all(_storage_path(owner, sha).exists() for sha in released)
        _assert_retained_payloads_present(owner)
        result = kb.management.run({"operation": "gc"})
        assert result.gc is not None and result.deleted_payloads > 0
        assert result.deleted_payloads + result.shared_payloads == len(released)
        for sha in released:
            deleted = _payload_state(db, sha) == "deleted"
            assert _storage_path(owner, sha).exists() is not deleted
        assert (
            _count(
                db,
                "SELECT COUNT(*) FROM artifacts WHERE payload_state='delete_pending'",
            )
            == 0
        )
        _assert_retained_payloads_present(owner)
        health = kb.management.run({"operation": "health", "verify_payloads": True})
        assert health.recovery is not None
        assert kb.search({"question": "Version 19", "modalities": ["lexical"]}).segments


def test_leased_snapshot_is_retained(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "First verification outcome.")
        first = _active(owner)
        for index in range(17):
            with _pinned(owner, first):
                _add(kb, "a.md", f"Later outcome {index}.")
        with _pinned(owner, first):
            _collect(owner, limit=1000)
            assert (
                _db(owner)
                .execute("SELECT 1 FROM snapshots WHERE snapshot_id=?", (first,))
                .fetchone()
            )


def test_pinned_reader_keeps_its_snapshot_and_payloads_through_manage_gc(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Pinned verification outcome.")
        pinned = _active(owner)
        for index in range(3):
            _add(kb, "a.md", f"Replacement outcome {index}.")
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        with _pinned(owner, pinned) as view:
            result = kb.management.run({"operation": "gc"})
            assert result.gc is not None and result.gc.snapshots == 1
            assert pinned in _snapshots(_db(owner))
            # A lease blocks every unlink, so released payloads wait.
            assert result.deleted_payloads == 0
            slots = owner.run_blocking_sync(
                lambda: owner.store.snapshot_slots(view, fresh=True),
                ExecutionContext(),
            )
            assert any(slot.batches for slot in slots)
            _assert_retained_payloads_present(owner)
        result = kb.management.run({"operation": "gc"})
        assert pinned not in _snapshots(_db(owner))
        assert result.deleted_payloads > 0
        _assert_retained_payloads_present(owner)


def test_retention_keeps_active_parent_queries_and_in_flight_bases(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        db = _db(owner)
        for index in range(6):
            _add(kb, "a.md", f"Retention outcome {index}.")
        ids = _snapshots(db)
        assert len(ids) == 6
        later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        db.execute(
            "INSERT INTO query_runs (run_id,snapshot_id,principal_id,scope_digest,"
            "policy_epoch,lexical_statistics_epoch,config_sha,state,metrics_json,"
            "result_ids_json,created_at,expires_at) VALUES "
            "('run-1',?,'p',?,0,0,?,'complete','{}','[]',?,?)",
            (ids[1], "a" * 64, "b" * 64, later, later),
        )
        job_id = db.execute(
            "SELECT job_id FROM jobs WHERE result_snapshot_id=?", (ids[3],)
        ).fetchone()[0]
        db.execute(
            "UPDATE jobs SET state='running',base_snapshot_id=? WHERE job_id=?",
            (ids[0], job_id),
        )
        db.commit()
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 0)
        summary = _collect(owner, limit=1000)
        # Active, the active's parent, an unexpired query run's snapshot and the
        # base of a running job stay.
        assert set(_snapshots(db)) == {ids[0], ids[1], ids[4], ids[5]}
        assert summary.snapshots == 2
        assert summary.unpublished_skipped is False
        assert _collect(owner, include_unpublished=True).unpublished_skipped is True
        db.execute("UPDATE jobs SET state='complete' WHERE job_id=?", (job_id,))
        db.commit()


def test_collection_honours_its_limit(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        for index in range(5):
            _add(kb, "a.md", f"Limited outcome {index}.")
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        assert _collect(owner, limit=2).snapshots == 2
        assert len(_snapshots(_db(owner))) == 3
        assert _collect(owner, limit=2).snapshots == 1


def test_leased_batch_is_not_collected(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Batch lease outcome.")
        (old_batch,) = {
            row[0]
            for row in _db(owner).execute(
                "SELECT batch_id FROM snapshot_batches WHERE slot='lexical'"
            )
        }
        _add(kb, "a.md", "Replacement batch lease outcome.")
        _add(kb, "a.md", "Second replacement batch lease outcome.")
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        with owner.store.leases.pin(frozenset({old_batch})):
            summary = _collect(owner)
            assert summary.snapshots == 1
            assert _db(owner).execute(
                "SELECT state FROM index_batches WHERE batch_id=?", (old_batch,)
            ).fetchone() == ("ready",)
        _collect(owner, include_unpublished=True)
        assert (
            _db(owner)
            .execute("SELECT 1 FROM index_batches WHERE batch_id=?", (old_batch,))
            .fetchone()
            is None
        )


def test_crash_between_batch_write_and_activation_is_collected(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Published verification outcome.")
        active = _active(owner)

        def crash(plan):
            raise RuntimeError("simulated crash before activation")

        monkeypatch.setattr(owner.store, "publish_snapshot", crash)
        assert (
            _add(kb, "b.md", "Unpublished verification outcome.").state != "succeeded"
        )
        monkeypatch.undo()
        assert _active(owner) == active
        assert _count(_db(owner), ORPHAN_BATCHES) > 0
        result = kb.management.run({"operation": "gc"})
        assert result.gc is not None and result.gc.batches > 0
        assert _count(_db(owner), ORPHAN_BATCHES) == 0
        assert (
            _add(kb, "b.md", "Unpublished verification outcome.").state == "succeeded"
        )
        result = kb.search({"question": "unpublished", "modalities": ["lexical"]})
        assert result.segments


def test_pending_orphan_batch_is_collected(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Pruned verification outcome.")
        (old_batch,) = {
            row[0]
            for row in _db(owner).execute(
                "SELECT batch_id FROM snapshot_batches WHERE slot='lexical'"
            )
        }
        _add(kb, "a.md", "Replacement of the pruned outcome.")
        _add(kb, "a.md", "Second replacement of the pruned outcome.")
        _db(owner).execute(
            "UPDATE index_batches SET state='pending' WHERE batch_id=?", (old_batch,)
        )
        _db(owner).commit()
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        summary = _collect(owner)
        assert summary.batches == 1
        assert (
            _db(owner)
            .execute("SELECT 1 FROM index_batches WHERE batch_id=?", (old_batch,))
            .fetchone()
            is None
        )


def test_collection_drops_cached_batches_and_bumps_the_lexical_epoch_once(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        for index in range(4):
            _add(kb, f"{index}.md", f"Cached outcome {index}.")
            kb.search({"question": "cached outcome", "modalities": ["lexical"]})
        for index in range(4):
            _add(kb, f"{index}.md", f"Cached replacement {index}.")
        _add(kb, "extra.md", "An unrelated outcome.")
        old = [
            row[0]
            for row in _db(owner).execute(
                "SELECT batch_id FROM index_batches WHERE batch_id NOT IN "
                "(SELECT batch_id FROM snapshot_batches s WHERE s.snapshot_id=?)",
                (_active(owner),),
            )
        ]
        assert len(old) == 4
        assert all(owner.store.batch_cache.get(batch) is not None for batch in old)
        # Searches recorded query runs that would retain the early snapshots.
        _db(owner).execute(
            "UPDATE query_runs SET expires_at='2000-01-01T00:00:00+00:00'"
        )
        _db(owner).commit()
        epoch = _count(_db(owner), "SELECT lexical_statistics_epoch FROM workspaces")
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        summary = _collect(owner, limit=1000)
        assert summary.batches == 4
        assert all(owner.store.batch_cache.get(batch) is None for batch in old)
        assert (
            _count(_db(owner), "SELECT lexical_statistics_epoch FROM workspaces")
            == epoch + 1
        )
        assert _collect(owner, limit=1000).batches == 0
        assert (
            _count(_db(owner), "SELECT lexical_statistics_epoch FROM workspaces")
            == epoch + 1
        )


def test_busy_database_after_publication_keeps_the_job_succeeded(
    tmp_path, monkeypatch, caplog
):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Before the busy collection.")

        def busy(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(snapshot_rows.SqliteSnapshotRepository, "collect", busy)
        with pytest.raises(TychoraError) as error:
            _collect(owner)
        assert error.value.code == "WORKSPACE_BUSY"
        assert error.value.reason == "storage.busy"
        assert error.value.retryable
        with caplog.at_level(logging.WARNING, logger="tychora.application.sources"):
            status = _add(kb, "a.md", "After the busy collection.")
        assert status.state == "succeeded"
        assert _active(owner) is not None
        assert "storage.busy" in caplog.text


def test_other_storage_failures_are_integrity_errors(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Before the damaged collection.")

        def damaged(self, *args, **kwargs):
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(snapshot_rows.SqliteSnapshotRepository, "collect", damaged)
        with pytest.raises(TychoraError) as error:
            _collect(owner)
        assert error.value.code == "INTEGRITY_ERROR"
        assert error.value.reason == "invariant.unexpected"


def test_receipt_of_a_collected_snapshot_still_resolves(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Receipt outcome.")
        first = _active(owner)
        job_id = (
            _db(owner)
            .execute("SELECT job_id FROM jobs WHERE result_snapshot_id=?", (first,))
            .fetchone()[0]
        )
        _add(kb, "a.md", "Receipt replacement.")
        _add(kb, "a.md", "Second receipt replacement.")
        monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
        _collect(owner)
        assert first not in _snapshots(_db(owner))
        receipt = owner.run_blocking_sync(
            lambda: PublicationReceipts(owner.store).lookup(job_id),
            ExecutionContext(),
        )
        assert receipt is not None and receipt.result_snapshot_id == first
        repository = kb._workspace.services.jobs.repository
        status = _on_owner(owner, lambda: repository.recover(job_id))
        assert status.state == "succeeded" and status.result_snapshot_id == first
        with pytest.raises(TychoraError) as error:
            _on_owner(owner, lambda: owner.store.resolve_snapshot(first))
        assert error.value.reason == "snapshot.absent"


def test_gc_requires_every_collection_grant(tmp_path):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as initial:
        narrowed = initial.scope.model_copy(update={"collection_ids": frozenset()})
    with Tychora.open(
        tmp_path / "kb", scope=narrowed, token_counter=CharacterCounter()
    ) as kb:
        with pytest.raises(TychoraError) as error:
            kb.management.run({"operation": "gc"})
        assert error.value.code == "FORBIDDEN_SCOPE"
        assert error.value.reason == "scope.whole_workspace"


def _stale_workspace(kb, monkeypatch):
    """Publish replacements, then lower retention so collection has work."""
    owner = kb._workspace.owner
    for index in range(4):
        _add(kb, "a.md", f"Crash outcome {index}.")
    monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
    return owner


def _assert_converged(kb, owner) -> None:
    result = kb.management.run({"operation": "gc"})
    assert result.gc is not None
    assert len(_snapshots(_db(owner))) == 2
    assert _count(_db(owner), ORPHAN_BATCHES) == 0
    assert (
        _count(
            _db(owner),
            "SELECT COUNT(*) FROM artifacts WHERE payload_state='delete_pending'",
        )
        == 0
    )
    for (sha,) in (
        _db(owner)
        .execute("SELECT artifact_sha FROM artifacts WHERE payload_state='deleted'")
        .fetchall()
    ):
        assert not _storage_path(owner, sha).exists()
    _assert_retained_payloads_present(owner)
    assert kb.search({"question": "outcome 3", "modalities": ["lexical"]}).segments
    assert _add(kb, "a.md", "Crash outcome 0.").state == "succeeded"


def test_crash_inside_the_marking_transaction_changes_nothing(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)
        before = _snapshots(_db(owner))

        def crash(self, *args):
            raise RuntimeError("simulated crash while marking")

        with monkeypatch.context() as patch:
            patch.setattr(snapshot_rows.SqliteSnapshotRepository, "_release", crash)
            with pytest.raises(RuntimeError):
                kb.management.run({"operation": "gc"})
        assert _snapshots(_db(owner)) == before
        _assert_converged(kb, owner)


def test_crash_after_marking_before_native_drop_converges(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)

        def crash(self, *args):
            raise RuntimeError("simulated crash after marking")

        with monkeypatch.context() as patch:
            patch.setattr(SnapshotGc, "_drop_native", crash)
            with pytest.raises(RuntimeError):
                kb.management.run({"operation": "gc"})
        assert len(_snapshots(_db(owner))) == 2
        assert (
            _count(
                _db(owner), "SELECT COUNT(*) FROM index_batches WHERE state='removed'"
            )
            == 2
        )
        _assert_converged(kb, owner)


def test_crash_while_purging_marked_batches_converges(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)

        def crash(self, batch_ids):
            raise RuntimeError("simulated crash while purging")

        with monkeypatch.context() as patch:
            patch.setattr(batch_rows.SqliteBatchRepository, "purge", crash)
            with pytest.raises(RuntimeError):
                kb.management.run({"operation": "gc"})
        assert (
            _count(
                _db(owner), "SELECT COUNT(*) FROM index_batches WHERE state='removed'"
            )
            == 2
        )
        _assert_converged(kb, owner)


def test_crash_between_mark_and_unlink_converges(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)

        def crash(self, candidates=None):
            raise RuntimeError("simulated crash before unlink")

        with monkeypatch.context() as patch:
            patch.setattr(RemovalService, "reconcile_pending_deletions", crash)
            with pytest.raises(RuntimeError):
                kb.management.run({"operation": "gc"})
        pending = [
            row[0]
            for row in _db(owner).execute(
                "SELECT artifact_sha FROM artifacts WHERE payload_state='delete_pending'"
            )
        ]
        assert pending
        assert all(_storage_path(owner, sha).exists() for sha in pending)
        _assert_converged(kb, owner)


def test_crash_after_unlink_before_its_row_update_converges(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)
        unlink = type(owner.artifacts).unlink_registered

        def crash(self, ref):
            unlink(self, ref)
            raise RuntimeError("simulated crash after unlink")

        with monkeypatch.context() as patch:
            patch.setattr(type(owner.artifacts), "unlink_registered", crash)
            with pytest.raises(RuntimeError):
                kb.management.run({"operation": "gc"})
        _assert_converged(kb, owner)


def test_reclaim_walks_the_roots_once_however_many_payloads_are_pending(
    tmp_path, monkeypatch
):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = _stale_workspace(kb, monkeypatch)
        _collect(owner, limit=1000)
        pending = [
            row[0]
            for row in _db(owner).execute(
                "SELECT artifact_sha FROM artifacts WHERE payload_state='delete_pending'"
            )
        ]
        assert len(pending) > 2
        walks = 0
        closure = RemovalService._manifest_closure

        def counting(self, *args, **kwargs):
            nonlocal walks
            walks += 1
            return closure(self, *args, **kwargs)

        monkeypatch.setattr(RemovalService, "_manifest_closure", counting)

        def reconcile(candidates):
            nonlocal walks
            walks = 0
            service = RemovalService(owner, kb._workspace.services.jobs.repository)
            counts = _on_owner(
                owner, lambda: service.reconcile_pending_deletions(candidates)
            )
            return walks, counts

        with owner.store.leases.pin(frozenset({"hold"})):
            one, _ = reconcile(frozenset(pending[:1]))
            every, counts = reconcile(None)
        assert counts["pending_payloads"] == len(pending)
        assert 0 < every == one
        every, counts = reconcile(None)
        assert counts["deleted_payloads"] == len(pending)
        assert every <= one


def test_jobs_orphaned_by_a_crash_are_interrupted_at_open(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        _add(kb, "a.md", "Published verification outcome.")

        def crash(plan):
            raise RuntimeError("simulated crash before activation")

        with monkeypatch.context() as patch:
            patch.setattr(owner.store, "publish_snapshot", crash)
            stopped = _add(kb, "b.md", "Unpublished verification outcome.")
        # The process dies mid-add: the job row still says it runs.
        _db(owner).execute(
            "UPDATE jobs SET state='running',error_code=NULL WHERE job_id=?",
            (stopped.id,),
        )
        _db(owner).commit()
    with Tychora.open(tmp_path / "kb", token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        assert _db(owner).execute(
            "SELECT state,error_code FROM jobs WHERE job_id=?", (stopped.id,)
        ).fetchone() == ("failed", "JOB_INTERRUPTED")
        result = kb.management.run({"operation": "gc"})
        assert result.gc is not None and result.gc.unpublished_skipped is False
        assert result.gc.batches > 0
        assert _count(_db(owner), ORPHAN_BATCHES) == 0
        assert kb.jobs.get(stopped.id).state == "interrupted"


def _graph_generations(owner) -> dict[str, str]:
    """Generation of every graph batch, by batch ID."""
    return {
        batch_id: json.loads(receipt)["generation_id"]
        for batch_id, receipt in _db(owner).execute(
            "SELECT b.batch_id,b.receipt_json FROM index_batches b "
            "JOIN index_families f USING(family_id) WHERE f.channel='graph'"
        )
    }


def test_graph_generations_of_collected_snapshots_drop_after_a_restart(
    tmp_path, monkeypatch
):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.index]\ngraph="ladybug"\n'
    )
    config = resolve_config(settings, "test", {})
    path = tmp_path / "kb"
    with Tychora.open(path, config=config, token_counter=CharacterCounter()) as kb:
        for index in range(4):
            assert _add(kb, "a.md", f"Graph outcome {index}.").state == "succeeded"
        before = _graph_generations(kb._workspace.owner)
    assert len(set(before.values())) == 4
    monkeypatch.setattr(snapshot_rows, "RETAINED_SNAPSHOTS", 1)
    with Tychora.open(path, config=config, token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        result = kb.management.run({"operation": "gc"})
        assert result.gc is not None
        assert result.gc.deferred_batches == 0
        after = _graph_generations(owner)
        assert len(after) == 2
        graph = owner._configured_graph_store
        for batch_id, generation in before.items():
            assert graph._state_path(generation).exists() is (batch_id in after)
        assert kb.search({"question": "graph outcome", "modalities": ["lexical"]})
