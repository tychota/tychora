"""Source withdrawal and original binding release, using synthetic workspaces."""

import pytest

from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import MaintenanceRequest


class _Counter:
    model = "synthetic-output-test-counter"
    tokenizer_id = "cl100k_base"

    def count(self, value: str) -> int:
        return len(value)


def _add(kb: Tychora, key: str) -> tuple[str, str]:
    from tychora.contracts.identity import source_id

    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "manual@1",
            "format": "text",
            "text": "shared original",
            "metadata": {
                "title": "Synthetic record",
                "publisher": "Example Team",
                "edition": "1",
            },
        }
    )
    status = kb.jobs.wait(job.id)
    assert status.state == "succeeded", status
    sid = source_id(kb.default_collection_id, key)
    revisions = kb.sources.revisions(sid)
    return sid, revisions.revisions[0].revision_id


def _original_sha(kb: Tychora, revision: str) -> str:
    from tychora.ports.execution import ExecutionContext

    owner = kb._workspace.owner
    return kb._workspace.bridge.call(
        owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT original_sha FROM revisions WHERE revision_id=?", (revision,)
            ).fetchone()[0],
            ExecutionContext(),
        )
    )


def test_purge_requires_source_and_explicit_confirmation_contract():
    request = MaintenanceRequest.from_mapping(
        {
            "operation": "purge-originals",
            "source_id": "synthetic-source",
            "revision_ids": ["synthetic-revision"],
            "confirm_original_removal": True,
        }
    )
    assert request.root.source_id == "synthetic-source"
    assert request.root.confirm_original_removal is True


def test_withdraw_and_purge_preserve_shared_original_binding(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source_a, revision_a = _add(kb, "record-a")
        _, revision_b = _add(kb, "record-b")
        removed = kb.management.run({"operation": "remove", "source_ids": [source_a]})
        assert removed.job is not None and removed.job.state == "succeeded"
        with pytest.raises(TychoraError) as withdrawn:
            kb.sources.sync(source_a)
        assert withdrawn.value.code == "FORBIDDEN_SCOPE"
        preview = kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source_a,
                "revision_ids": [revision_a],
            }
        )
        assert preview.job is None
        result = kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source_a,
                "revision_ids": [revision_a],
                "confirm_original_removal": True,
            }
        )
        assert result.job is not None and result.job.state == "succeeded"
        assert result.shared_payloads >= 1
        with pytest.raises(TychoraError) as purged:
            kb.inspect({"subject": "revisions", "id": revision_a, "original": True})
        assert purged.value.code == "ORIGINAL_REMOVED"
        assert (
            kb.inspect(
                {"subject": "revisions", "id": revision_b, "original": True}
            ).payload
            == b"shared original"
        )


def test_remove_requires_one_explicit_source_per_job(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        first, _ = _add(kb, "first-removal")
        second, _ = _add(kb, "second-removal")
        with pytest.raises(TychoraError) as error:
            kb.management.run({"operation": "remove", "source_ids": [first, second]})
        assert error.value.code == "INVALID_REQUEST"
        assert kb.sources.revisions(first).revisions
        assert kb.sources.revisions(second).revisions


def test_removal_job_authorization_binds_only_target_collection(tmp_path):
    from tychora.api import DEFAULT_COLLECTION_ID
    from tychora.contracts.requests import RequestScope

    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, token_counter=_Counter()) as kb:
        source, _ = _add(kb, "one-collection")
    scope = RequestScope(
        principal_id="local-owner",
        collection_ids=frozenset(
            {
                str(DEFAULT_COLLECTION_ID),
                "00000000-0000-4000-8000-000000000099",
            }
        ),
        access_ceiling=0,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "write", "manage"}),
    )
    with Tychora.open(workspace, scope=scope, token_counter=_Counter()) as kb:
        removed = kb.management.run({"operation": "remove", "source_ids": [source]})
        assert removed.job is not None and removed.job.state == "succeeded"


def test_unshared_original_is_tombstoned_and_physically_deleted(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "one-record")
        db = kb._workspace.owner.db
        sha = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: db.execute(
                    "SELECT original_sha FROM revisions WHERE revision_id=?",
                    (revision,),
                ).fetchone()[0],
                __import__(
                    "tychora.ports.execution", fromlist=["ExecutionContext"]
                ).ExecutionContext(),
            )
        )
        kb.management.run({"operation": "remove", "source_ids": [source]})
        result = kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source,
                "revision_ids": [revision],
                "confirm_original_removal": True,
            }
        )
        assert result.deleted_payloads == 1
        assert not (kb._workspace.owner.path / "artifacts" / sha).exists()
        state = kb._workspace.bridge.call(
            kb._workspace.owner.run_blocking(
                lambda: db.execute(
                    "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
                ).fetchone()[0],
                __import__(
                    "tychora.ports.execution", fromlist=["ExecutionContext"]
                ).ExecutionContext(),
            )
        )
        assert state == "deleted"


def test_pinned_original_remains_pending_until_owner_reconciliation(tmp_path):
    from tychora.application.removal import RemovalService
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "leased-record")
        owner = kb._workspace.owner
        sha = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT original_sha FROM revisions WHERE revision_id=?",
                    (revision,),
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        kb.management.run({"operation": "remove", "source_ids": [source]})
        with owner.artifacts.pin_payloads(frozenset({sha})):
            result = kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
            assert result.pending_payloads == 1
            assert (owner.path / "artifacts" / sha).exists()
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["deleted_payloads"] == 1
        assert not (owner.path / "artifacts" / sha).exists()


def test_crash_after_unlink_reconciles_pending_absent(tmp_path, monkeypatch):
    from tychora.application.removal import RemovalService
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "crash-record")
        owner = kb._workspace.owner
        sha = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT original_sha FROM revisions WHERE revision_id=?",
                    (revision,),
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        kb.management.run({"operation": "remove", "source_ids": [source]})
        unlink = owner.artifacts.unlink_registered

        def interrupted(ref):
            unlink(ref)
            raise RuntimeError("synthetic interruption after unlink")

        monkeypatch.setattr(owner.artifacts, "unlink_registered", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
        assert not (owner.path / "artifacts" / sha).exists()
        monkeypatch.setattr(owner.artifacts, "unlink_registered", unlink)
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["deleted_payloads"] == 1


def test_interruption_between_unlink_and_directory_fsync_resumes(tmp_path, monkeypatch):
    from tychora.application.removal import RemovalService
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "pre-fsync-record")
        owner = kb._workspace.owner
        sha = _original_sha(kb, revision)
        kb.management.run({"operation": "remove", "source_ids": [source]})
        unlink = owner.artifacts.unlink_registered

        def interrupted(_ref):
            # Synthetic crash cut: the path is gone while the pending SQL state
            # has not reached its final update or directory durability step.
            (owner.path / "artifacts" / sha).unlink()
            raise RuntimeError("synthetic interruption before directory fsync")

        monkeypatch.setattr(owner.artifacts, "unlink_registered", interrupted)
        with pytest.raises(RuntimeError, match="before directory fsync"):
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
        monkeypatch.setattr(owner.artifacts, "unlink_registered", unlink)
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["deleted_payloads"] == 1


def test_interruption_before_tombstone_keeps_binding_intact(tmp_path, monkeypatch):
    from tychora.application.removal import RemovalService

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "pre-tombstone-record")
        kb.management.run({"operation": "remove", "source_ids": [source]})
        original = RemovalService._bindings
        calls = 0

        def interrupted(self, source_id, revision_ids, scope):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic interruption before tombstone")
            return original(self, source_id, revision_ids, scope)

        monkeypatch.setattr(RemovalService, "_bindings", interrupted)
        with pytest.raises(RuntimeError, match="before tombstone"):
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
        with __import__("sqlite3").connect(
            kb._workspace.owner.path / "corpus.sqlite3"
        ) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM original_payload_tombstones WHERE revision_id=?",
                    (revision,),
                ).fetchone()[0]
                == 0
            )


def test_interruption_after_tombstone_before_unlink_resumes(tmp_path, monkeypatch):
    from tychora.application.removal import RemovalService
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "post-tombstone-record")
        owner = kb._workspace.owner
        sha = _original_sha(kb, revision)
        kb.management.run({"operation": "remove", "source_ids": [source]})
        unlink = owner.artifacts.unlink_registered

        def interrupted(_ref):
            raise RuntimeError("synthetic interruption before unlink")

        monkeypatch.setattr(owner.artifacts, "unlink_registered", interrupted)
        with pytest.raises(RuntimeError, match="before unlink"):
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
        assert (owner.path / "artifacts" / sha).exists()
        monkeypatch.setattr(owner.artifacts, "unlink_registered", unlink)
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["deleted_payloads"] == 1
        assert not (owner.path / "artifacts" / sha).exists()


def test_snapshot_lease_defers_original_deletion(tmp_path):
    from tychora.application.removal import RemovalService
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "snapshot-lease-record")
        owner = kb._workspace.owner
        kb.management.run({"operation": "remove", "source_ids": [source]})
        snapshot = kb._workspace.bridge.call(
            owner.run_blocking(
                owner.store.active_snapshot_id,
                ExecutionContext(),
            )
        )
        assert snapshot is not None
        with owner.leases.pin(frozenset({snapshot})):
            result = kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
            assert result.pending_payloads == 1
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["deleted_payloads"] == 1


def test_reopen_reconciles_crash_pending_absent_state(tmp_path, monkeypatch):
    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, token_counter=_Counter()) as kb:
        source, revision = _add(kb, "restart-record")
        owner = kb._workspace.owner
        sha = _original_sha(kb, revision)
        kb.management.run({"operation": "remove", "source_ids": [source]})
        unlink = owner.artifacts.unlink_registered

        def interrupted(ref):
            unlink(ref)
            raise RuntimeError("synthetic crash before final SQL")

        monkeypatch.setattr(owner.artifacts, "unlink_registered", interrupted)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
    with Tychora.open(workspace, token_counter=_Counter()):
        pass
    with __import__("sqlite3").connect(workspace / "corpus.sqlite3") as db:
        assert db.execute(
            "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
        ).fetchone() == ("deleted",)


def test_identical_reingestion_restores_bytes_without_clearing_old_tombstone(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source_a, revision_a = _add(kb, "old-record")
        kb.management.run({"operation": "remove", "source_ids": [source_a]})
        deleted = kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source_a,
                "revision_ids": [revision_a],
                "confirm_original_removal": True,
            }
        )
        assert deleted.deleted_payloads == 1
        _, revision_b = _add(kb, "new-record")
        assert (
            kb.inspect(
                {"subject": "revisions", "id": revision_b, "original": True}
            ).payload
            == b"shared original"
        )
        with pytest.raises(TychoraError) as removed:
            kb.inspect({"subject": "revisions", "id": revision_a, "original": True})
        assert removed.value.code == "ORIGINAL_REMOVED"


def test_missing_present_original_is_corruption_before_tombstone(tmp_path):
    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "missing-record")
        sha = _original_sha(kb, revision)
        kb.management.run({"operation": "remove", "source_ids": [source]})
        (kb._workspace.owner.path / "artifacts" / sha).unlink()
        with pytest.raises(TychoraError) as error:
            kb.management.run(
                {
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                }
            )
        assert error.value.code == "INTEGRITY_ERROR"
        with __import__("sqlite3").connect(
            kb._workspace.owner.path / "corpus.sqlite3"
        ) as db:
            assert (
                db.execute(
                    "SELECT 1 FROM original_payload_tombstones WHERE revision_id=? AND artifact_sha=?",
                    (revision, sha),
                ).fetchone()
                is None
            )


def test_derived_extraction_reference_keeps_original_bytes(tmp_path):
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = _add(kb, "derived-record")
        sha = _original_sha(kb, revision)
        owner = kb._workspace.owner
        kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "UPDATE extractions SET document_artifact_sha=? WHERE revision_id=?",
                    (sha, revision),
                ),
                ExecutionContext(),
            )
        )
        kb.management.run({"operation": "remove", "source_ids": [source]})
        result = kb.management.run(
            {
                "operation": "purge-originals",
                "source_id": source,
                "revision_ids": [revision],
                "confirm_original_removal": True,
            }
        )
        assert result.shared_payloads == 1
        assert (owner.path / "artifacts" / sha).exists()


def test_cli_purge_previews_then_requires_confirmed_source_binding(tmp_path):
    import json

    from typer.testing import CliRunner

    from tychora.interfaces.cli import app

    workspace = tmp_path / "knowledge"
    with Tychora.open(workspace, token_counter=_Counter()) as kb:
        source, revision = _add(kb, "cli-record")
    runner = CliRunner()
    removed = runner.invoke(
        app, ["--workspace", str(workspace), "manage", "remove", source]
    )
    assert removed.exit_code == 0, removed.stderr
    preview = runner.invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "manage",
            "purge-originals",
            source,
            revision,
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.stderr
    assert json.loads(preview.stdout)["job"] is None
    confirmed = runner.invoke(
        app,
        [
            "--workspace",
            str(workspace),
            "manage",
            "purge-originals",
            source,
            revision,
            "--confirm",
            "--json",
        ],
    )
    assert confirmed.exit_code == 0, confirmed.stderr
    assert json.loads(confirmed.stdout)["job"]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_http_maintenance_uses_same_preview_and_confirm_handler(tmp_path):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from tychora.interfaces.http.app import create_app
    from tychora.interfaces.http.auth import LocalAuth

    with Tychora.open(tmp_path / "knowledge", token_counter=_Counter()) as kb:
        source, revision = await asyncio.to_thread(_add, kb, "http-record")
        owner = kb._workspace.owner
        app = create_app(
            owner,
            kb._workspace.services,
            LocalAuth({"manager": owner.scope}, authority="127.0.0.1:8765"),
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer manager"},
        ) as client:
            removed = await client.post(
                "/v1/jobs",
                json={
                    "operation": "remove",
                    "source_ids": [source],
                },
            )
            assert removed.status_code == 202, removed.text
            preview = await client.post(
                "/v1/jobs",
                json={
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                },
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["job"] is None
            confirmed = await client.post(
                "/v1/jobs",
                json={
                    "operation": "purge-originals",
                    "source_id": source,
                    "revision_ids": [revision],
                    "confirm_original_removal": True,
                },
            )
            assert confirmed.status_code == 202, confirmed.text
            assert confirmed.json()["job"]["state"] == "succeeded"
