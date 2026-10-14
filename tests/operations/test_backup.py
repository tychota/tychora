"""Canonical backup behavior over small synthetic owner workspaces."""

import json
import threading
from io import BytesIO

import pytest

from tychora.application.backup import BackupService
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def backup_case(tmp_path):
    settings_path = tmp_path / "workspace.toml"
    settings_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    settings = resolve_config(settings_path, "test", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    with WorkspaceOwner.open(tmp_path / "source", settings, scope) as owner:

        def seed():
            owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,policy_json) "
                "VALUES (?,?,?,?,?)",
                ("collection-one", owner.workspace_id, "one", "One", "{}"),
            )
            nested = owner.artifacts.put_stream(
                BytesIO(canonical_bytes({"type": "registry", "schema_version": "1"})),
                "application/json",
            )
            root = owner.artifacts.put_stream(
                BytesIO(
                    canonical_bytes(
                        {
                            "type": "snapshot",
                            "schema_version": "1",
                            "required_manifests": [
                                {
                                    "artifact": nested.model_dump(),
                                    "type": "registry",
                                    "schema_version": "1",
                                }
                            ],
                        }
                    )
                ),
                "application/json",
            )
            unreferenced = owner.artifacts.put_stream(
                BytesIO(b"retained bytes"), "application/octet-stream"
            )
            for ref in (nested, root, unreferenced):
                owner.db.execute(
                    "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        ref.sha256,
                        ref.size_bytes,
                        ref.media_type,
                        ref.storage_key,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            owner.db.execute(
                "INSERT INTO snapshots (snapshot_id,workspace_id,membership_sha,config_sha,manifest_sha,published_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "snapshot-one",
                    owner.workspace_id,
                    "membership",
                    "config",
                    root.sha256,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            return nested.sha256, unreferenced.sha256

        nested_sha, unreferenced_sha = owner.run_blocking_sync(seed, ExecutionContext())

        class Case:
            nested_schema_sha = nested_sha
            unreferenced_but_present_sha = unreferenced_sha
            destination = tmp_path / "backup"

            def __init__(self, owner: WorkspaceOwner, scope: RequestScope):
                self.owner = owner
                self.scope = scope

            def export(
                self, *, scope_override=None, mode="canonical", destination=None
            ):
                return BackupService(owner).export(
                    destination or self.destination,
                    scope_override or scope,
                    mode=mode,
                    ctx=ExecutionContext(),
                )

        case = Case(owner, scope)
        yield case


def test_backup_includes_nested_registry_and_retained_present_payload(backup_case):
    f = backup_case
    receipt = f.export()
    members = {a.sha256 for a in receipt.manifest.artifacts}
    assert f.nested_schema_sha in members
    assert f.unreferenced_but_present_sha in members
    assert receipt.manifest.omitted_channels == ("text", "image", "graph")
    assert (f.destination / "corpus.sqlite3").is_file()
    assert (
        json.loads((f.destination / "backup-receipt.json").read_text())["sqlite_sha256"]
        == receipt.sqlite_sha256
    )


def test_backup_rejects_partial_collection_authority(backup_case):
    f = backup_case
    partial = RequestScope(principal_id="owner", capabilities=frozenset({"manage"}))
    with pytest.raises(TychoraError) as error:
        f.export(scope_override=partial)
    assert error.value.code == "FORBIDDEN_SCOPE"
    assert not f.destination.exists()


def test_backup_never_overwrites_completed_destination(backup_case):
    f = backup_case
    f.export()
    before = (f.destination / "backup-receipt.json").read_bytes()
    with pytest.raises(TychoraError):
        f.export()
    assert (f.destination / "backup-receipt.json").read_bytes() == before


def test_backup_rejects_exact_native_replay(backup_case):
    f = backup_case
    with pytest.raises(TychoraError) as error:
        f.export(mode="exact-native-replay")
    assert error.value.code == "CAPABILITY_MISSING"
    assert not f.destination.exists()


def test_backup_rejects_missing_nested_required_payload(backup_case):
    f = backup_case
    (f.owner.path / "artifacts" / f.nested_schema_sha).unlink()
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "INTEGRITY_ERROR"
    assert not f.destination.exists()


def test_backup_rejects_corrupt_present_payload(backup_case):
    f = backup_case
    (f.owner.path / "artifacts" / f.nested_schema_sha).write_bytes(b"corrupt")
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "INTEGRITY_ERROR"
    assert not f.destination.exists()


def test_backup_reconciles_pending_but_absent_payload(backup_case):
    f = backup_case

    def pending_absent():
        from tychora.contracts.artifacts import ArtifactRef

        row = f.owner.db.execute(
            "SELECT size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
            (f.unreferenced_but_present_sha,),
        ).fetchone()
        ref = ArtifactRef(
            sha256=f.unreferenced_but_present_sha,
            size_bytes=row[0],
            media_type=row[1],
            storage_key=row[2],
        )
        f.owner.db.execute(
            "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=?",
            (ref.sha256,),
        )
        with f.owner.artifacts.payload_guard(ref.sha256):
            f.owner.artifacts.unlink_registered(ref)

    f.owner.run_blocking_sync(pending_absent, ExecutionContext())
    receipt = f.export()
    assert f.unreferenced_but_present_sha not in {
        a.sha256 for a in receipt.manifest.artifacts
    }
    assert (
        f.owner.run_blocking_sync(
            lambda: f.owner.db.execute(
                "SELECT payload_state FROM artifacts WHERE artifact_sha=?",
                (f.unreferenced_but_present_sha,),
            ).fetchone()[0],
            ExecutionContext(),
        )
        == "deleted"
    )


def test_backup_rejects_unknown_typed_schema(backup_case):
    f = backup_case

    def bad_manifest():
        ref = f.owner.artifacts.put_stream(
            BytesIO(canonical_bytes({"type": "snapshot", "schema_version": "2"})),
            "application/json",
        )
        f.owner.db.execute(
            "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                ref.sha256,
                ref.size_bytes,
                ref.media_type,
                ref.storage_key,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        f.owner.db.execute(
            "UPDATE snapshots SET manifest_sha=? WHERE snapshot_id='snapshot-one'",
            (ref.sha256,),
        )

    f.owner.run_blocking_sync(bad_manifest, ExecutionContext())
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "INTEGRITY_ERROR"
    assert not f.destination.exists()


def test_backup_holds_lease_and_owner_serialization_through_copy(
    backup_case, monkeypatch
):
    from tychora.adapters.files.archive import ArchiveWriter

    f = backup_case
    entered, release, mutation_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original = ArchiveWriter.copy_artifact

    def paused_copy(self, source, ref, ctx):
        entered.set()
        assert release.wait(5)
        return original(self, source, ref, ctx)

    monkeypatch.setattr(ArchiveWriter, "copy_artifact", paused_copy)
    result = []
    export_thread = threading.Thread(target=lambda: result.append(f.export()))
    export_thread.start()
    try:
        assert entered.wait(5)
        assert f.owner.artifacts.payload_is_pinned(f.nested_schema_sha)
        mutation_thread = threading.Thread(
            target=lambda: (
                f.owner.run_blocking_sync(
                    lambda: f.owner.db.execute(
                        "UPDATE workspaces SET policy_epoch=policy_epoch+1"
                    ),
                    ExecutionContext(),
                ),
                mutation_done.set(),
            )
        )
        mutation_thread.start()
        assert not mutation_done.wait(0.1)
    finally:
        release.set()
        export_thread.join(5)
        mutation_thread.join(5)
    assert result and mutation_done.is_set()
    assert result[0].manifest.policy_epoch == 0
    assert not f.owner.artifacts.payload_is_pinned(f.nested_schema_sha)


def test_cancelled_backup_drains_copy_before_unpin(backup_case, monkeypatch):
    from tychora.adapters.files.archive import ArchiveWriter

    f = backup_case
    entered, release = threading.Event(), threading.Event()
    original = ArchiveWriter.copy_artifact

    def paused_copy(self, source, ref, ctx):
        entered.set()
        assert release.wait(5)
        return original(self, source, ref, ctx)

    monkeypatch.setattr(ArchiveWriter, "copy_artifact", paused_copy)
    ctx = ExecutionContext()
    errors = []

    def export():
        try:
            BackupService(f.owner).export(f.destination, f.scope, ctx=ctx)
        except TychoraError as error:
            errors.append(error.code)

    thread = threading.Thread(target=export)
    thread.start()
    try:
        assert entered.wait(5)
        ctx.cancelled.set()
        assert f.owner.artifacts.payload_is_pinned(f.nested_schema_sha)
    finally:
        release.set()
        thread.join(5)
    assert errors == ["CANCELLED"]
    assert not f.destination.exists()
    assert not f.owner.artifacts.payload_is_pinned(f.nested_schema_sha)


def test_backup_rejects_new_collection_outside_owner_grant(backup_case):
    f = backup_case
    f.owner.run_blocking_sync(
        lambda: f.owner.db.execute(
            "INSERT INTO collections (collection_id,workspace_id,slug,title,policy_json) "
            "VALUES (?,?,?,?,?)",
            ("collection-two", f.owner.workspace_id, "two", "Two", "{}"),
        ),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "FORBIDDEN_SCOPE"
    assert not f.destination.exists()


def test_backup_barrier_waits_for_off_owner_cas_write(backup_case, monkeypatch):
    import tychora.application.backup as backup_module

    f = backup_case
    entered, release, put_done = threading.Event(), threading.Event(), threading.Event()
    original = backup_module.from_backup

    def paused_inventory(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(backup_module, "from_backup", paused_inventory)
    exported = []
    export_thread = threading.Thread(target=lambda: exported.append(f.export()))
    export_thread.start()
    try:
        assert entered.wait(5)
        writer = threading.Thread(
            target=lambda: (
                f.owner.artifacts.put_stream(
                    BytesIO(b"late bytes"), "application/octet-stream"
                ),
                put_done.set(),
            )
        )
        writer.start()
        assert not put_done.wait(0.1)
    finally:
        release.set()
        export_thread.join(5)
        writer.join(5)
    assert exported and put_done.is_set()


def test_backup_survives_source_workspace_removal(tmp_path):
    import hashlib
    import shutil
    import sqlite3

    settings_path = tmp_path / "workspace.toml"
    settings_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    settings = resolve_config(settings_path, "test", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    source = tmp_path / "source"
    destination = tmp_path / "backup"
    with WorkspaceOwner.open(source, settings, scope) as owner:

        def seed():
            ref = owner.artifacts.put_stream(
                BytesIO(b"portable payload"), "application/octet-stream"
            )
            owner.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            return ref.sha256

        payload_sha = owner.run_blocking_sync(seed, ExecutionContext())
        receipt = BackupService(owner).export(
            destination, scope, ctx=ExecutionContext()
        )
    shutil.rmtree(source)
    assert (destination / "artifacts" / payload_sha).read_bytes() == b"portable payload"
    assert (
        hashlib.sha256((destination / "corpus.sqlite3").read_bytes()).hexdigest()
        == receipt.sqlite_sha256
    )
    db = sqlite3.connect(destination / "corpus.sqlite3")
    try:
        assert db.execute("SELECT workspace_id FROM workspaces").fetchone() == (
            receipt.manifest.workspace_id,
        )
    finally:
        db.close()
    assert sorted(path.name for path in destination.iterdir()) == [
        "artifacts",
        "backup-manifest.json",
        "backup-receipt.json",
        "corpus.sqlite3",
        "resolved-config.json",
    ]


def test_backup_uses_trusted_published_capture_state_for_nested_originals(backup_case):
    f = backup_case

    def seed_published_capture():
        original = f.owner.artifacts.put_stream(
            BytesIO(b"released original"), "application/octet-stream"
        )
        child = f.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "repository_capture",
                        "schema_version": "1",
                        "original_artifacts": [original.model_dump()],
                    }
                )
            ),
            "application/json",
        )
        root = f.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": "job_capture",
                        "schema_version": "1",
                        "required_manifests": [
                            {
                                "artifact": child.model_dump(),
                                "type": "repository_capture",
                                "schema_version": "1",
                            }
                        ],
                    }
                )
            ),
            "application/json",
        )
        for ref, state in (
            (original, "deleted"),
            (child, "present"),
            (root, "present"),
        ):
            f.owner.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,payload_state,payload_deleted_at,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    state,
                    "2026-01-02T00:00:00+00:00" if state == "deleted" else None,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        with f.owner.artifacts.payload_guard(original.sha256):
            f.owner.artifacts.unlink_registered(original)
        f.owner.db.execute(
            "INSERT INTO jobs (job_id,operation,principal_id,request_digest,effective_config_sha,captured_inputs_sha,"
            "state,result_snapshot_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "job-one",
                "add",
                "owner",
                "a" * 64,
                f.nested_schema_sha,
                root.sha256,
                "complete",
                "snapshot-one",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        return original.sha256, child.sha256

    original_sha, child_sha = f.owner.run_blocking_sync(
        seed_published_capture, ExecutionContext()
    )
    receipt = f.export()
    members = {a.sha256 for a in receipt.manifest.artifacts}
    assert child_sha in members
    assert original_sha not in members
    assert not (f.destination / "artifacts" / original_sha).exists()


def test_backup_never_publishes_manifest_above_registered_reference_limit(
    backup_case, monkeypatch
):
    from tychora.adapters.files import references

    f = backup_case
    monkeypatch.setattr(references, "MAX_REFERENCES", 2)
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "INTEGRITY_ERROR"
    assert not f.destination.exists()


def test_backup_rejects_scope_below_source_access_floor(backup_case):
    f = backup_case
    f.owner.run_blocking_sync(
        lambda: f.owner.db.execute(
            "INSERT INTO sources (source_id,collection_id,source_key,source_type,registration_json,"
            "access_floor,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "private-source",
                "collection-one",
                "private",
                "document",
                "{}",
                3,
                "2026-01-01T00:00:00+00:00",
            ),
        ),
        ExecutionContext(),
    )
    shallow = f.scope.model_copy(update={"access_ceiling": 0})
    with pytest.raises(TychoraError) as error:
        f.export(scope_override=shallow)
    assert error.value.code == "FORBIDDEN_SCOPE"
    assert not f.destination.exists()


def test_publish_withdraw_and_purge_wait_for_export_copy(tmp_path, monkeypatch):
    import time

    from tychora import Tychora
    from tychora.adapters.files.archive import ArchiveWriter
    from tychora.contracts.identity import source_id

    class Counter:
        model = "synthetic-backup-counter"
        tokenizer_id = "cl100k_base"

        def count(self, value):
            return len(value)

    def add(kb, key):
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": key,
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "synthetic source text",
                "metadata": {
                    "title": "Synthetic",
                    "publisher": "Example",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        source = source_id(kb.default_collection_id, key)
        revision = kb.sources.revisions(source).revisions[0].revision_id
        return source, revision

    with Tychora.open(tmp_path / "source", token_counter=Counter()) as kb:
        first, _ = add(kb, "first")
        second, revision = add(kb, "second")
        kb.management.run({"operation": "remove", "source_ids": [second]})
        owner = kb._workspace.owner
        entered, release = threading.Event(), threading.Event()
        original_copy = ArchiveWriter.copy_artifact

        def paused_copy(self, source, ref, ctx):
            entered.set()
            assert release.wait(15)
            return original_copy(self, source, ref, ctx)

        monkeypatch.setattr(ArchiveWriter, "copy_artifact", paused_copy)
        outcomes = {}
        errors = []

        def run(name, call):
            try:
                outcomes[name] = call()
            except Exception as error:  # noqa: BLE001 - surface thread failures in the test
                errors.append(error)

        export_thread = threading.Thread(
            target=run,
            args=(
                "backup",
                lambda: BackupService(owner).export(
                    tmp_path / "backup", owner.scope, ctx=ExecutionContext()
                ),
            ),
        )
        export_thread.start()
        operations = []
        try:
            assert entered.wait(15)
            operations = [
                threading.Thread(
                    target=run,
                    args=(
                        "publish",
                        lambda: add(kb, "third"),
                    ),
                ),
                threading.Thread(
                    target=run,
                    args=(
                        "withdraw",
                        lambda: kb.management.run(
                            {
                                "operation": "remove",
                                "source_ids": [first],
                            }
                        ),
                    ),
                ),
                threading.Thread(
                    target=run,
                    args=(
                        "purge",
                        lambda: kb.management.run(
                            {
                                "operation": "purge-originals",
                                "source_id": second,
                                "revision_ids": [revision],
                                "confirm_original_removal": True,
                            }
                        ),
                    ),
                ),
            ]
            for operation in operations:
                operation.start()
            time.sleep(0.15)
            assert "backup" not in outcomes
            assert not any(
                name in outcomes for name in ("publish", "withdraw", "purge")
            )
        finally:
            release.set()
            export_thread.join(20)
            for operation in operations:
                operation.join(20)
        assert not errors, errors
        assert set(outcomes) == {"backup", "publish", "withdraw", "purge"}
        assert (tmp_path / "backup" / "backup-receipt.json").is_file()


@pytest.mark.parametrize(
    ("manifest_type", "reference_field"),
    [
        ("snapshot", "required_artifacts"),
        ("job_capture", "original_artifacts"),
    ],
)
def test_backup_rejects_manifest_payload_size_disagreeing_with_registry(
    backup_case, manifest_type, reference_field
):
    from tychora.contracts.artifacts import ArtifactRef

    f = backup_case

    def seed_bad_reference():
        payload = f.owner.artifacts.put_stream(
            BytesIO(b"registered opaque payload"), "application/octet-stream"
        )
        bad_ref = ArtifactRef(
            sha256=payload.sha256,
            size_bytes=payload.size_bytes + 1,
            media_type=payload.media_type,
            storage_key=payload.storage_key,
        )
        root = f.owner.artifacts.put_stream(
            BytesIO(
                canonical_bytes(
                    {
                        "type": manifest_type,
                        "schema_version": "1",
                        reference_field: [bad_ref.model_dump()],
                    }
                )
            ),
            "application/json",
        )
        for ref in (payload, root):
            f.owner.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        if manifest_type == "snapshot":
            f.owner.db.execute(
                "UPDATE snapshots SET manifest_sha=? WHERE snapshot_id='snapshot-one'",
                (root.sha256,),
            )
        else:
            f.owner.db.execute(
                "INSERT INTO jobs (job_id,operation,principal_id,request_digest,"
                "effective_config_sha,captured_inputs_sha,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "job-bad-capture",
                    "add",
                    "owner",
                    "b" * 64,
                    f.nested_schema_sha,
                    root.sha256,
                    "running",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    f.owner.run_blocking_sync(seed_bad_reference, ExecutionContext())
    with pytest.raises(TychoraError) as error:
        f.export()
    assert error.value.code == "INTEGRITY_ERROR"
    assert not f.destination.exists()
