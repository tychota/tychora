"""Restore a canonical archive into a clean synthetic workspace."""

from io import BytesIO

import pytest

from tychora.adapters.sqlite.migrations import open_verified_import
from tychora.application.backup import BackupService
from tychora.application.owner import WorkspaceOwner
from tychora.application.recovery import RecoveryService
from tychora.application.restore import RestoreService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


def _legacy_empty_archive(tmp_path):
    import json
    from hashlib import sha256

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "custom", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    source, archive = tmp_path / "source", tmp_path / "archive"
    with WorkspaceOwner.open(source, config, scope) as owner:
        BackupService(owner).export(archive, scope, ctx=ExecutionContext())
    (archive / "resolved-config.json").unlink()
    manifest_path = archive / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    del manifest["data"]["resolved_config_sha256"]
    manifest_raw = canonical_bytes(manifest)
    manifest_path.write_bytes(manifest_raw)
    receipt_path = archive / "backup-receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["manifest_sha256"] = sha256(manifest_raw).hexdigest()
    receipt_path.write_bytes(canonical_bytes(receipt))
    settings.unlink()
    return archive, config


def test_legacy_empty_archive_explicit_config_sync_factory_reopens(tmp_path):
    from tychora import Tychora

    archive, config = _legacy_empty_archive(tmp_path)
    destination = tmp_path / "restored"
    with Tychora.restore(archive, destination, config=config) as client:
        assert client._workspace.config.profile == "custom"
    with Tychora.open(destination) as reopened:
        assert reopened._workspace.config.profile == "custom"


@pytest.mark.asyncio
async def test_legacy_empty_archive_explicit_config_async_factory_reopens(tmp_path):
    import asyncio

    from tychora import AsyncTychora, Tychora

    archive, config = await asyncio.to_thread(_legacy_empty_archive, tmp_path)
    destination = tmp_path / "restored"
    async with await AsyncTychora.restore(
        archive, destination, config=config
    ) as client:
        assert client._workspace.config.profile == "custom"
    with Tychora.open(destination) as reopened:
        assert reopened._workspace.config.profile == "custom"


def test_deep_health_verifies_retained_original_payload_bytes(tmp_path):
    from tychora import Tychora

    with Tychora.open(tmp_path / "workspace") as client:
        owner = client._workspace.owner

        def register():
            ref = owner.artifacts.put_stream(BytesIO(b"original"), "text/plain")
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
            owner.db.commit()
            return ref

        ref = owner.run_write_sync(register, ExecutionContext())
        (owner.path / "artifacts" / ref.sha256).write_bytes(b"tampered")
        assert (
            client.management.run(
                {"operation": "health", "verify_payloads": False}
            ).recovery.workspace_id
            == owner.workspace_id
        )
        with pytest.raises(TychoraError, match="payload failed verification"):
            client.management.run({"operation": "health", "verify_payloads": True})
        with pytest.raises(TychoraError, match="payload failed verification"):
            client.management.run(
                {
                    "operation": "health",
                    "verify_payloads": True,
                    "resume_pending_deletions": True,
                }
            )


def test_restore_preserves_identity_and_marks_native_pending(tmp_path):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    destination = tmp_path / "restored"
    with WorkspaceOwner.open(source, config, scope) as owner:

        def seed():
            owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,policy_json) VALUES (?,?,?,?,?)",
                ("collection-one", owner.workspace_id, "one", "One", "{}"),
            )
            original = owner.artifacts.put_stream(
                BytesIO(b"original bytes"), "text/plain"
            )
            index_input = owner.artifacts.put_stream(
                BytesIO(
                    canonical_bytes({"type": "index_input", "schema_version": "1"})
                ),
                "application/json",
            )
            for ref in (original, index_input):
                owner.db.execute(
                    "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,created_at) VALUES (?,?,?,?,?)",
                    (
                        ref.sha256,
                        ref.size_bytes,
                        ref.media_type,
                        ref.storage_key,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            owner.db.execute(
                "INSERT INTO index_families VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "text-family",
                    "text",
                    "chroma",
                    None,
                    None,
                    2,
                    "config",
                    "{}",
                    "namespace",
                    "c" * 64,
                    "00000000-0000-4000-8000-000000000001",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            owner.db.execute(
                "INSERT INTO index_batches (batch_id,family_id,input_manifest_sha,expected_count,actual_count,state,receipt_json) VALUES (?,?,?,?,?,?,?)",
                (
                    "text-batch",
                    "text-family",
                    index_input.sha256,
                    0,
                    0,
                    "ready",
                    '{"sealed":true}',
                ),
            )
            owner.db.commit()
            return original

        original = owner.run_write_sync(seed, ExecutionContext())
        workspace_id = owner.workspace_id
        BackupService(owner).export(backup, scope, ctx=ExecutionContext())
    receipt = RestoreService(config=config).import_backup(
        backup, destination, scope, ExecutionContext()
    )
    assert receipt.workspace_id == workspace_id
    assert (
        destination / "artifacts" / original.sha256
    ).read_bytes() == b"original bytes"
    db = open_verified_import(destination / "corpus.sqlite3")
    try:
        assert db.execute(
            "SELECT state,receipt_json FROM index_batches"
        ).fetchone() == ("pending", '{"sealed":true}')
    finally:
        db.close()
    assert (destination / ".restore-suspended.json").is_file()
    (backup / "artifacts" / original.sha256).write_bytes(b"corrupt")
    with pytest.raises(TychoraError) as corrupted:
        RestoreService(config=config).import_backup(
            backup, tmp_path / "corrupt-restored", scope, ExecutionContext()
        )
    assert corrupted.value.code == "INTEGRITY_ERROR"
    assert not (tmp_path / "corrupt-restored").exists()


def test_restored_pending_deletion_is_suspended_before_owner_reconciliation(tmp_path):
    from tychora import Tychora
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.removal import RemovalService

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    source, backup, destination = (
        tmp_path / name for name in ("source", "backup", "restored")
    )
    with WorkspaceOwner.open(source, config, scope) as owner:

        def seed():
            owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,policy_json) VALUES (?,?,?,?,?)",
                ("collection-one", owner.workspace_id, "one", "One", "{}"),
            )
            ref = owner.artifacts.put_stream(BytesIO(b"pending bytes"), "text/plain")
            owner.db.execute(
                "INSERT INTO artifacts (artifact_sha,size_bytes,media_type,storage_key,payload_state,created_at) VALUES (?,?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "delete_pending",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            owner.db.commit()
            return ref

        ref = owner.run_write_sync(seed, ExecutionContext())
        with owner.artifacts.pin_payloads(frozenset({ref.sha256})):
            BackupService(owner).export(backup, scope, ctx=ExecutionContext())
    RestoreService(config=config).import_backup(
        backup, destination, scope, ExecutionContext()
    )
    stale = destination / "staging" / "parser-interrupted"
    stale.mkdir(parents=True)
    (stale / "retained-worker-output").write_bytes(b"untrusted staged output")
    with WorkspaceOwner.open(destination, config, scope) as owner:

        def reconcile():
            jobs = JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            )
            return RemovalService(owner, jobs).reconcile_pending_deletions()

        owner.run_write_sync(reconcile, ExecutionContext())
        BackupService(owner).export(
            tmp_path / "backup-while-suspended", scope, ctx=ExecutionContext()
        )
    assert (destination / "artifacts" / ref.sha256).read_bytes() == b"pending bytes"
    assert (stale / "retained-worker-output").read_bytes() == b"untrusted staged output"
    with WorkspaceOwner.open(destination, config, scope) as owner:
        report = RecoveryService(owner).reconcile(ExecutionContext())
        assert report.deletion_suspended
        RecoveryService(owner).resume_pending_deletions(scope, ExecutionContext())
    assert not (destination / "artifacts" / ref.sha256).exists()
    assert not (destination / ".restore-suspended.json").exists()
    with Tychora.open(destination, scope=scope) as reopened:
        assert reopened._workspace.config.profile == "test"
        assert (
            not RecoveryService(reopened._workspace.owner)
            .reconcile(ExecutionContext())
            .deletion_suspended
        )


def test_restore_activates_verified_profile_without_original_settings(tmp_path):
    from tychora.api import LocalWorkspace

    settings = tmp_path / "custom.toml"
    settings.write_text('schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "custom", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    with WorkspaceOwner.open(tmp_path / "source", config, scope) as owner:
        BackupService(owner).export(tmp_path / "backup", scope, ctx=ExecutionContext())
    settings.unlink()
    receipt = RestoreService().import_backup(
        tmp_path / "backup", tmp_path / "restored", scope, ExecutionContext()
    )
    assert receipt.workspace_id
    workspace = LocalWorkspace(
        tmp_path / "restored",
        scope=scope,
        config=None,
        providers=None,
        token_counter=None,
    )
    try:
        assert workspace.config.profile == "custom"
        assert workspace.config.registry_digest == config.registry_digest
    finally:
        workspace.close()


def test_empty_custom_profile_reopens_from_bound_backup_configuration(tmp_path):
    from tychora import Tychora

    settings = tmp_path / "custom.toml"
    settings.write_text('schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "custom", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage", "read"}),
    )
    with WorkspaceOwner.open(tmp_path / "source", config, scope) as owner:
        BackupService(owner).export(tmp_path / "backup", scope, ctx=ExecutionContext())
    settings.unlink()
    RestoreService().import_backup(
        tmp_path / "backup", tmp_path / "restored", scope, ExecutionContext()
    )
    with Tychora.open(tmp_path / "restored", scope=scope) as kb:
        assert kb._workspace.config.profile == "custom"
        assert kb._workspace.config.registry_digest == config.registry_digest
        assert kb._workspace.config.values.offline is True


def test_selected_config_export_contains_reference_not_credential_value(
    tmp_path, monkeypatch
):
    import json
    from hashlib import sha256

    from tychora.application.restore import _selected_backup_config

    monkeypatch.setenv("SYNTHETIC_BACKUP_SECRET", "synthetic-value-must-not-export")
    settings = tmp_path / "custom.toml"
    settings.write_text(
        'schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n'
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:SYNTHETIC_BACKUP_SECRET"\n'
    )
    config = resolve_config(settings, "custom", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    with WorkspaceOwner.open(tmp_path / "source", config, scope) as owner:
        BackupService(owner).export(tmp_path / "backup", scope, ctx=ExecutionContext())
    raw = (tmp_path / "backup" / "resolved-config.json").read_bytes()
    assert b"env:SYNTHETIC_BACKUP_SECRET" in raw
    assert b"synthetic-value-must-not-export" not in raw
    manifest = json.loads((tmp_path / "backup" / "backup-manifest.json").read_bytes())
    assert _selected_backup_config(tmp_path / "backup", manifest) == config
    document = json.loads(raw)
    document["data"]["values"]["providers"]["text"]["api_key"] = "unapproved"
    altered = canonical_bytes(document)
    (tmp_path / "backup" / "resolved-config.json").write_bytes(altered)
    manifest["data"]["resolved_config_sha256"] = sha256(altered).hexdigest()
    with pytest.raises(TychoraError) as invalid:
        _selected_backup_config(tmp_path / "backup", manifest)
    assert invalid.value.code == "INTEGRITY_ERROR"


def test_local_owner_restore_factory_needs_no_source_or_control_workspace(tmp_path):
    import shutil

    from tychora import Tychora

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "custom", {})
    collection_id = "d4e96ef0-d41c-4ce1-8368-886d3fcbdbee"
    scope = RequestScope(
        principal_id="original-owner",
        collection_ids=frozenset({collection_id}),
        access_ceiling=3,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"read", "manage"}),
    )
    source = tmp_path / "source"
    with WorkspaceOwner.open(source, config, scope) as owner:
        owner.run_write_sync(
            lambda: owner.db.execute(
                "INSERT INTO collections (collection_id,workspace_id,slug,title,policy_json) VALUES (?,?,?,?,?)",
                (collection_id, owner.workspace_id, "custom", "Custom", "{}"),
            ),
            ExecutionContext(),
        )
        BackupService(owner).export(tmp_path / "backup", scope, ctx=ExecutionContext())
    shutil.rmtree(source)
    settings.unlink()
    destination = tmp_path / "restored"
    assert not destination.exists()
    with Tychora.restore(tmp_path / "backup", destination) as kb:
        assert kb._workspace.config.profile == "custom"
        assert collection_id in kb.scope.collection_ids
        assert kb.restore_receipt.destination == destination
    narrow = scope.model_copy(update={"collection_ids": frozenset({"other"})})
    with pytest.raises(TychoraError) as denied:
        Tychora.restore(tmp_path / "backup", tmp_path / "denied", scope=narrow)
    assert denied.value.code == "FORBIDDEN_SCOPE"
    assert not (tmp_path / "denied").exists()


@pytest.mark.asyncio
async def test_async_restore_cancellation_drains_private_staging(tmp_path, monkeypatch):
    import asyncio
    from threading import Event

    from tychora import AsyncTychora
    from tychora.adapters.files.archive import ArchiveReader

    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    scope = RequestScope(
        principal_id="owner",
        collection_ids=frozenset({"collection-one"}),
        access_ceiling=0,
        uses=frozenset({"compose", "audit", "lookup"}),
        capabilities=frozenset({"manage"}),
    )
    with WorkspaceOwner.open(tmp_path / "source", config, scope) as owner:
        await asyncio.to_thread(
            BackupService(owner).export,
            tmp_path / "backup",
            scope,
            ctx=ExecutionContext(),
        )
    started, release = Event(), Event()
    original = ArchiveReader.copy_to

    def stalled_copy(self, stage, ctx):
        started.set()
        release.wait(5)
        return original(self, stage, ctx)

    monkeypatch.setattr(ArchiveReader, "copy_to", stalled_copy)
    destination = tmp_path / "restored"
    task = asyncio.create_task(AsyncTychora.restore(tmp_path / "backup", destination))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not destination.exists()
    assert not list(tmp_path.glob(".restored.*"))


def test_restore_runtime_tightening_preserves_only_pinned_semantics(tmp_path):
    from tychora.configuration.indexed_representation import pinned_runtime_compatible
    from tychora.configuration.resolve import restored_runtime_config

    settings = tmp_path / "custom.toml"
    settings.write_text(
        'schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n[configuration]\noffline=false\n'
    )
    historical = resolve_config(settings, "custom", {})
    runtime = restored_runtime_config(historical)
    assert pinned_runtime_compatible(historical, runtime)
    assert not pinned_runtime_compatible(runtime, historical)
    settings.write_text(
        settings.read_text() + '[configuration.output]\ntitle="Changed"\n'
    )
    different = resolve_config(settings, "custom", {})
    assert not pinned_runtime_compatible(historical, restored_runtime_config(different))


def test_restore_loads_verified_active_snapshot_profile_without_settings(tmp_path):
    import shutil
    from datetime import UTC, datetime, timedelta

    from tychora import Tychora

    class Counter:
        model = "synthetic-counter"
        tokenizer_id = "cl100k_base"

        def count(self, text):
            return len(text)

    source = tmp_path / "source"
    source.mkdir()
    settings = source / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.custom]\nbuiltin="documents@1"\n[defaults]\nprofile="custom"\n'
    )
    config = resolve_config(settings, "custom", {})
    source_input = {
        "type": "text",
        "source_key": "manual",
        "collection_id": "default",
        "kind": "manual@1",
        "format": "text",
        "text": "Record verification outcomes.",
        "metadata": {
            "title": "Verification",
            "publisher": "Example",
            "edition": "1",
        },
    }
    with Tychora.open(source, config=config, token_counter=Counter()) as kb:
        job = kb.sources.add(source_input, idempotency_key="retained-key")
        assert kb.jobs.wait(job.id).state == "succeeded"
        original_sha = kb._workspace.owner.run_write_sync(
            lambda: kb._workspace.owner.db.execute(
                "SELECT original_sha FROM revisions"
            ).fetchone()[0],
            ExecutionContext(),
        )
        kb._workspace.owner.run_write_sync(
            lambda: kb._workspace.services.jobs.repository.expire_diagnostics(
                datetime.now(UTC) + timedelta(days=1)
            ),
            ExecutionContext(),
        )
        assert (
            kb._workspace.owner.run_write_sync(
                lambda: kb._workspace.owner.db.execute(
                    "SELECT COUNT(*) FROM job_events WHERE job_id=?", (job.id,)
                ).fetchone()[0],
                ExecutionContext(),
            )
            == 0
        )
        BackupService(kb._workspace.owner).export(
            tmp_path / "backup", kb.scope, ctx=ExecutionContext()
        )
        grant = kb.scope
    shutil.rmtree(source)
    RestoreService().import_backup(
        tmp_path / "backup", tmp_path / "restored", grant, ExecutionContext()
    )
    assert (
        tmp_path / "restored" / "artifacts" / original_sha
    ).read_bytes() == b"Record verification outcomes."
    with Tychora.open(tmp_path / "restored", token_counter=Counter()) as restored:
        assert restored._workspace.config.profile == "custom"
        assert restored._workspace.config.values.offline is True
        assert restored.search(
            {"question": "verification", "modalities": ["lexical"]}
        ).segments
        assert (
            restored.sources.add(source_input, idempotency_key="retained-key").id
            == job.id
        )


def test_vector_rebuild_rejects_missing_or_altered_float32_cache(tmp_path):
    import struct
    from uuid import uuid4

    import pytest

    from tychora.adapters.files.cas import LocalArtifacts
    from tychora.application.recovery import (
        vector_batch_records,
        verify_native_vector,
    )
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.embeddings import EmbeddingRecord
    from tychora.contracts.identity import family_id, space_id
    from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
    from tychora.contracts.publication import RetainedBatch, VectorRecordBinding
    from tychora.contracts.snapshots import IndexFamily
    from tychora.contracts.vector_batches import (
        VECTOR_BATCH_MEDIA_TYPE,
        VectorBatchRow,
        encode_vector_batch,
    )
    from tychora.contracts.vectors import BatchReceipt
    from tychora.indexing.receipts import receipt_digest

    store = LocalArtifacts(tmp_path / "artifacts")
    raw = struct.pack("<ff", 1.0, 0.0)
    creation = str(uuid4())
    config_sha = "a" * 64
    spec = FamilySpec(
        family_id=family_id("workspace", config_sha, creation),
        workspace_id="workspace",
        config_sha=config_sha,
        creation_uuid=creation,
        modality="visual",
        provider="synthetic",
        model="synthetic",
        dimensions=2,
        metric="cosine",
        normalization="unit-l2",
        representation_fingerprint="b" * 64,
        backend_namespace="synthetic",
        backend_space="ip",
    )
    family_id_value = spec.family_id
    embedding = EmbeddingRecord.from_normalized_bytes(
        record_id="record",
        input_sha="c" * 64,
        space_id=space_id("visual", "synthetic", "synthetic", 2, "cosine", "b" * 64),
        stored=raw,
    )
    cache = store.put_stream(
        BytesIO(
            encode_vector_batch(
                family_id_value,
                2,
                (
                    VectorBatchRow(
                        record_id="record",
                        input_sha=embedding.input_sha,
                        values=(1.0, 0.0),
                    ),
                ),
            )
        ),
        VECTOR_BATCH_MEDIA_TYPE,
    )
    binding = VectorRecordBinding(
        record_id="record",
        input_sha=embedding.input_sha,
        value_sha=embedding.value_sha,
        asset_id="asset",
        row_index=0,
    )
    entry = IndexRecordDigest(
        record_id="record", input_sha=embedding.input_sha, value_sha=embedding.value_sha
    )
    receipt = BatchReceipt(
        family_id=spec.family_id,
        namespace_hash="d" * 64,
        config_sha=config_sha,
        records=(entry,),
        digest=receipt_digest((entry,)),
        count=1,
    )
    family = IndexFamily(
        family_id=spec.family_id,
        channel="image",
        backend="chroma",
        provider=spec.provider,
        model=spec.model,
        dimension=2,
        config_sha=config_sha,
        config_json=FrozenJsonMap(
            {
                "spec": spec.model_dump(mode="json"),
                "namespace_hash": receipt.namespace_hash,
                "effective_config_sha": "e" * 64,
            }
        ),
        backend_namespace="synthetic",
        created_at="2026-01-01T00:00:00+00:00",
    )
    batch = RetainedBatch(
        batch_id="batch",
        family_id=spec.family_id,
        input_manifest=cache,
        expected_count=1,
        receipt=receipt,
        vector_records=(binding,),
        vectors=cache,
    )
    assert vector_batch_records(family, batch, store).records == (embedding,)
    (tmp_path / "artifacts" / cache.sha256).write_bytes(b"altered")
    with pytest.raises(TychoraError) as corrupt:
        vector_batch_records(family, batch, store)
    assert corrupt.value.code == "INTEGRITY_ERROR"
    (tmp_path / "artifacts" / cache.sha256).unlink()
    with pytest.raises(TychoraError) as absent:
        vector_batch_records(family, batch, store)
    assert absent.value.code == "CAPABILITY_MISSING"
    store.put_stream(
        BytesIO(
            encode_vector_batch(
                family_id_value,
                2,
                (
                    VectorBatchRow(
                        record_id="record",
                        input_sha=embedding.input_sha,
                        values=(1.0, 0.0),
                    ),
                ),
            )
        ),
        VECTOR_BATCH_MEDIA_TYPE,
    )

    class AlteredNative:
        def create_family(self, _spec):
            from tychora.contracts.vectors import FamilyHandle

            return FamilyHandle(
                spec=spec, namespace_hash=receipt.namespace_hash, config_sha=config_sha
            )

        def upsert_batch(self, _batch, _ctx):
            return receipt

        def verify_batch(self, _receipt):
            from tychora.contracts.vectors import Verification

            return Verification(ready=False, mismatched_record_ids=("record",))

    with pytest.raises(TychoraError) as altered_native:
        verify_native_vector(
            family,
            batch,
            lambda: vector_batch_records(family, batch, store),
            AlteredNative(),
            ExecutionContext(),
        )
    assert altered_native.value.code == "INTEGRITY_ERROR"


def test_native_recovery_on_empty_synthetic_workspace_is_an_explicit_manage_action(
    tmp_path,
):
    from tychora import Tychora

    with Tychora.open(tmp_path / "workspace") as kb:
        report = RecoveryService(kb._workspace.owner).rebuild_native(
            kb.scope, ExecutionContext()
        )
        assert report.native_pending_batches == 0


def test_archived_v2_collection_and_active_v1_survive_backup_restore(tmp_path):
    from tests.transports.test_http import Counter
    from tychora import Tychora
    from tychora.contracts.collections import CollectionCreate

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        ws = kb._workspace
        service = ws.services.collections
        created = ws.bridge.call(
            service.create(
                CollectionCreate(
                    title="Archive", slug="archive", default_kind_id="manual@1"
                ),
                kb.scope,
                ExecutionContext(),
                managed_owner_authorized=True,
            )
        )
        archived = ws.bridge.call(
            service.set_archived(
                created.collection_id, True, kb.scope, ExecutionContext()
            )
        )

        def grant_retained_policy():
            from tychora.configuration.collection_policy import parse_collection_policy

            raw = ws.owner.db.execute(
                "SELECT policy_json FROM collections WHERE collection_id=?",
                (created.collection_id,),
            ).fetchone()[0]
            policy = parse_collection_policy(raw).model_copy(
                update={
                    "upload_providers": ("voyage",),
                    "owner_upload_providers": ("voyage",),
                }
            )
            ws.owner.db.execute(
                "UPDATE collections SET policy_json=? WHERE collection_id=?",
                (policy.model_dump_json(), created.collection_id),
            )
            ws.owner.db.commit()

        ws.bridge.call(ws.owner.run_write(grant_retained_policy, ExecutionContext()))
        BackupService(ws.owner).export(
            tmp_path / "backup", kb.scope, ctx=ExecutionContext()
        )
    with Tychora.restore(tmp_path / "backup", tmp_path / "restored") as restored:
        ws = restored._workspace
        retained = ws.bridge.call(
            ws.services.collections.get(
                created.collection_id, restored.scope, ExecutionContext()
            )
        )
        assert retained.collection_id == created.collection_id
        assert retained.archived_at == archived.archived_at
        assert retained.default_kind_id == "manual@1"
        consent = ws.bridge.call(
            ws.services.collections.consent(
                created.collection_id, restored.scope, ExecutionContext()
            )
        )
        assert (
            consent.collection_upload_providers
            == consent.owner_upload_providers
            == ("voyage",)
        )
        default = ws.bridge.call(
            ws.services.collections.get(
                str(restored.default_collection_id), restored.scope, ExecutionContext()
            )
        )
        assert default.archived_at is None
        result = restored.management.run(
            {"operation": "migrate", "target_version": "2"}
        )
        assert result


def _export(kb, archive):
    kb.management.run({"operation": "export", "destination": str(archive)})


def _restore_open(tmp_path, **kwargs):
    from tychora import Tychora

    receipt = RestoreService().import_backup(
        tmp_path / "archive", tmp_path / "restored", None, ExecutionContext()
    )
    return Tychora.open(tmp_path / "restored", scope=receipt.opening_scope, **kwargs)


def _recover(restored):
    restored.management.run({"operation": "rebuild", "channels": ["text"]})
    return restored.management.run(
        {
            "operation": "health",
            "verify_payloads": True,
            "verify_native": True,
            "resume_pending_deletions": True,
        }
    ).recovery


def test_export_import_round_trip_keeps_pages_and_search(tmp_path):
    from tests.api.test_public_sync import CharacterCounter
    from tests.operations.export_support import add_texts, owner_rows
    from tychora import Tychora

    count = "SELECT COUNT(*) FROM snapshot_pages"
    with Tychora.open(tmp_path / "source", token_counter=CharacterCounter()) as kb:
        add_texts(kb, 1, "Restored")
        _export(kb, tmp_path / "archive")
        pages = owner_rows(kb, count)
        assert pages[0][0]
    with _restore_open(tmp_path, token_counter=CharacterCounter()) as restored:
        assert owner_rows(restored, count) == pages
        assert not _recover(restored).deletion_suspended
        assert restored.search(
            {"question": "restored", "modalities": ["lexical"]}
        ).segments


def test_export_import_round_trip_verifies_real_vector_batches(tmp_path, monkeypatch):
    from tests.api.test_public_sync import CharacterCounter
    from tests.operations.export_support import (
        add_texts,
        grant_upload,
        owner_rows,
        upload_scope,
        vector_config,
    )
    from tychora import Tychora

    config = vector_config(tmp_path, monkeypatch)
    ready = "SELECT COUNT(*) FROM index_batches WHERE vectors_sha IS NOT NULL"
    with Tychora.open(
        tmp_path / "source",
        config=config,
        scope=upload_scope(),
        token_counter=CharacterCounter(),
    ) as kb:
        grant_upload(kb)
        add_texts(kb, 3, "Vector")
        batches = owner_rows(kb, ready)[0][0]
        assert batches
    with Tychora.open(
        tmp_path / "source", config=config, token_counter=CharacterCounter()
    ) as kb:
        _export(kb, tmp_path / "archive")
    with _restore_open(
        tmp_path, config=config, token_counter=CharacterCounter()
    ) as restored:
        assert owner_rows(restored, ready)[0][0] == batches
        pending = restored.management.run(
            {"operation": "health", "verify_native": True}
        ).recovery
        assert pending.native_pending_batches == batches
        recovered = _recover(restored)
        assert recovered.native_verified_records >= 1
        assert not recovered.deletion_suspended
