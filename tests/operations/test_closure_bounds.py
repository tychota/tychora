"""Manifest closures scale with the workspace, not with a fixed per-root cap.

A snapshot root reaches every retained index input, so a fixed cap on one
closure is met by an ordinary corpus. The fixture lowers the closure floors
below its own snapshot closure and checks every caller still works.
"""

import json
import os
import shutil
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from io import BytesIO

import pytest

from tests.api.test_public_sync import CharacterCounter
from tests.operations.export_support import add_texts, owner_rows
from tychora import Tychora
from tychora.adapters.files import references
from tychora.adapters.files.cas import LocalArtifacts
from tychora.application import restore as restore_module
from tychora.application.removal import RemovalService
from tychora.application.restore import RestoreService
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

FORMER_CAP = 20
DOCUMENTS = 24


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """A closed workspace whose snapshot closure exceeds ``FORMER_CAP``."""
    root = tmp_path_factory.mktemp("closure") / "kb"
    with Tychora.open(root, token_counter=CharacterCounter()) as kb:
        add_texts(kb, DOCUMENTS, "Bound")
        inputs = owner_rows(kb, "SELECT COUNT(*) FROM index_batches")[0][0]
        assert inputs > FORMER_CAP
    return root


@pytest.fixture
def copied(corpus, tmp_path):
    copy = tmp_path / "kb"
    shutil.copytree(corpus, copy)
    return copy


@pytest.fixture
def workspace(copied, monkeypatch):
    """The copy, with the closure floors lowered below its snapshot closure.

    Only the bound derived from the inventory being walked can then admit it.
    """
    monkeypatch.setattr(references, "MIN_CLOSURE_MANIFESTS", FORMER_CAP)
    monkeypatch.setattr(references, "MIN_CLOSURE_REFERENCES", FORMER_CAP)
    return copied


def _on_owner(kb, call):
    owner = kb._workspace.owner
    return owner.run_write_sync(call, ExecutionContext())


def _mark_rooted_pending(kb) -> str:
    """Leave a rooted manifest delete_pending, as a crash mid-collection does."""
    owner = kb._workspace.owner
    (sha,) = owner_rows(kb, "SELECT input_manifest_sha FROM index_batches LIMIT 1")[0]

    def mark():
        owner.db.execute(
            "UPDATE artifacts SET payload_state='delete_pending' WHERE artifact_sha=?",
            (sha,),
        )
        owner.db.commit()

    _on_owner(kb, mark)
    return sha


def _state(kb, sha: str) -> str:
    return owner_rows(
        kb, "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
    )[0][0]


def test_open_reconciles_a_corpus_above_the_former_cap(workspace):
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        sha = _mark_rooted_pending(kb)
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        assert _state(kb, sha) == "present"
        assert kb.search({"question": "outcome", "modalities": ["lexical"]}).segments


def test_gc_reconciles_a_corpus_above_the_former_cap(workspace):
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        sha = _mark_rooted_pending(kb)
        assert kb.management.run({"operation": "gc"}).gc is not None
        assert _state(kb, sha) == "present"


def test_export_and_import_a_corpus_above_the_former_cap(workspace, tmp_path):
    archive = tmp_path / "archive"
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        kb.management.run({"operation": "export", "destination": str(archive)})
        pages = owner_rows(kb, "SELECT COUNT(*) FROM snapshot_pages")
    receipt = RestoreService().import_backup(
        archive, tmp_path / "restored", None, ExecutionContext()
    )
    with Tychora.open(
        tmp_path / "restored",
        scope=receipt.opening_scope,
        token_counter=CharacterCounter(),
    ) as restored:
        assert owner_rows(restored, "SELECT COUNT(*) FROM snapshot_pages") == pages


def test_an_archive_above_the_closure_ceiling_fails_with_a_registered_reason(
    workspace, tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        kb.management.run({"operation": "export", "destination": str(archive)})
    monkeypatch.setattr(references, "CLOSURE_MANIFEST_CEILING", FORMER_CAP)
    with pytest.raises(TychoraError) as error:
        RestoreService().import_backup(
            archive, tmp_path / "restored", None, ExecutionContext()
        )
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason == "closure.limit_exceeded"
    assert not (tmp_path / "restored").exists()


def test_one_reconcile_pass_reads_each_manifest_once(copied, monkeypatch):
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        _mark_rooted_pending(kb)
        owner = kb._workspace.owner
        reads: Counter[str] = Counter()
        artifacts = type(owner.artifacts)
        open_verified = artifacts.open_verified

        def counting(self, ref, *args, **kwargs):
            reads[ref.sha256] += 1
            return open_verified(self, ref, *args, **kwargs)

        monkeypatch.setattr(artifacts, "open_verified", counting)
        service = RemovalService(owner, kb._workspace.services.jobs.repository)
        _on_owner(kb, service.reconcile_pending_deletions)
        captures = owner_rows(
            kb,
            "SELECT COUNT(*) FROM jobs WHERE captured_inputs_sha IS NOT NULL "
            "AND state='complete' AND result_snapshot_id IS NOT NULL",
        )[0][0]
        assert captures
        assert reads
        assert max(reads.values()) == 1, reads.most_common(3)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _export(workspace, archive) -> None:
    with Tychora.open(workspace, token_counter=CharacterCounter()) as kb:
        kb.management.run({"operation": "export", "destination": str(archive)})


def _archive_entries(archive) -> int:
    return len(os.listdir(archive / "artifacts"))


def test_import_budgets_the_walk_by_the_archive_entries(copied, tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _export(copied, archive)
    calls = []
    original = restore_module.from_backup

    def spying(db, **kwargs):
        calls.append(kwargs.get("archive_entries"))
        return original(db, **kwargs)

    monkeypatch.setattr(restore_module, "from_backup", spying)
    RestoreService().import_backup(
        archive, tmp_path / "restored", None, ExecutionContext()
    )
    assert calls == [_archive_entries(archive)]


def _store_manifest(store: LocalArtifacts, db, value) -> dict:
    ref = store.put_stream(BytesIO(json.dumps(value).encode()), "application/json")
    db.execute(
        "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,'present',NULL,?)",
        (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key, _now()),
    )
    return ref.model_dump()


def test_padded_rows_do_not_raise_an_archive_budget(copied, tmp_path, monkeypatch):
    """Rows are archive-controlled; repeated references must fail fast."""
    archive = tmp_path / "archive"
    _export(copied, archive)
    store = LocalArtifacts(archive / "artifacts")
    db = sqlite3.connect(archive / "corpus.sqlite3")
    child = _store_manifest(store, db, {"type": "registry", "schema_version": "1"})
    link = {"artifact": child, "type": "registry", "schema_version": "1"}
    parent = _store_manifest(
        store,
        db,
        {
            "type": "registry",
            "schema_version": "1",
            "required_manifests": [link] * 9000,
        },
    )
    db.execute("UPDATE jobs SET effective_config_sha=?", (parent["sha256"],))
    padding = [(f"{index:064x}",) for index in range(1, 50_001)]
    db.executemany(
        "INSERT INTO artifacts VALUES (?,1,'application/octet-stream',?1,"
        "'deleted','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        padding,
    )
    db.commit()
    entries = _archive_entries(archive)
    assert 16 * entries < 9000 < 16 * len(padding)
    monkeypatch.setattr(references, "MIN_CLOSURE_REFERENCES", 1)
    try:
        with pytest.raises(TychoraError) as error:
            references.from_backup(db, artifacts=store, archive_entries=entries)
    finally:
        db.close()
    assert error.value.reason == "closure.limit_exceeded"


def test_rows_above_the_ceiling_fail_before_registration_is_read(
    copied, tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    _export(copied, archive)
    db = sqlite3.connect(archive / "corpus.sqlite3")
    reads = []
    db.set_trace_callback(reads.append)
    monkeypatch.setattr(references, "CLOSURE_MANIFEST_CEILING", 10)
    try:
        with pytest.raises(TychoraError) as error:
            references.from_backup(db, artifacts=LocalArtifacts(archive / "artifacts"))
    finally:
        db.close()
    assert error.value.reason == "closure.limit_exceeded"
    assert not any("payload_state FROM artifacts" in sql for sql in reads)


def _job_capture(store, db, *, required=(), originals=()) -> str:
    value = {"type": "job_capture", "schema_version": "1"}
    if required:
        value["required_artifacts"] = list(required)
    if originals:
        value["original_artifacts"] = list(originals)
    return _store_manifest(store, db, value)["sha256"]


def _payload(store, db, text: str) -> dict:
    ref = store.put_stream(BytesIO(text.encode()), "text/plain")
    db.execute(
        "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,'delete_pending',NULL,?)",
        (ref.sha256, ref.size_bytes, ref.media_type, ref.storage_key, _now()),
    )
    return ref.model_dump()


def _reconcile_with_captures(kb, build) -> dict[str, str]:
    """Rebind job captures with ``build``, reconcile, return payload states."""
    owner = kb._workspace.owner

    def run():
        db = owner.db
        jobs = [
            row[0]
            for row in db.execute(
                "SELECT job_id FROM jobs WHERE state='complete' "
                "AND result_snapshot_id IS NOT NULL ORDER BY job_id LIMIT 2"
            )
        ]
        payloads = build(owner.artifacts, db, jobs)
        db.commit()
        service = RemovalService(owner, kb._workspace.services.jobs.repository)
        service.reconcile_pending_deletions(frozenset(p["sha256"] for p in payloads))
        return {
            p["sha256"]: db.execute(
                "SELECT payload_state FROM artifacts WHERE artifact_sha=?",
                (p["sha256"],),
            ).fetchone()[0]
            for p in payloads
        }

    return _on_owner(kb, run)


def test_a_payload_kept_only_by_a_published_capture_survives(copied):
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        kept, original = {}, {}

        def build(store, db, jobs):
            kept.update(_payload(store, db, "kept only by the published capture"))
            original.update(_payload(store, db, "original of a published capture"))
            capture = _job_capture(store, db, required=[kept], originals=[original])
            db.execute(
                "UPDATE jobs SET captured_inputs_sha=? WHERE job_id=?",
                (capture, jobs[0]),
            )
            return [kept, original]

        states = _reconcile_with_captures(kb, build)
    assert states == {kept["sha256"]: "present", original["sha256"]: "deleted"}


def test_an_original_reached_by_an_unpublished_capture_survives(copied):
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        shared = {}

        def build(store, db, jobs):
            shared.update(_payload(store, db, "original of both captures"))
            published = _job_capture(store, db, originals=[shared])
            unpublished = _job_capture(store, db, required=[], originals=[shared])
            db.execute(
                "UPDATE jobs SET captured_inputs_sha=? WHERE job_id=?",
                (published, jobs[0]),
            )
            db.execute(
                "UPDATE jobs SET captured_inputs_sha=?,state='failed',"
                "error_code='INTEGRITY_ERROR',result_snapshot_id=NULL WHERE job_id=?",
                (unpublished, jobs[1]),
            )
            return [shared]

        states = _reconcile_with_captures(kb, build)
    assert states == {shared["sha256"]: "present"}


_BINDING_TABLES = (
    "revisions",
    "revision_artifacts",
    "original_payload_tombstones",
    "locators",
    "extractions",
    "assets",
    "asset_proxies",
    "index_batches",
    "membership_pages",
    "query_runs",
    "evaluation_cases",
    "job_steps",
    "review_events",
    "assertion_decisions",
)


def test_one_reconcile_pass_reads_the_bindings_independently_of_pending(copied):
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        shas = [
            row[0]
            for row in owner_rows(
                kb,
                "SELECT artifact_sha FROM artifacts WHERE payload_state='present' "
                "ORDER BY artifact_sha",
            )
        ]
        service = RemovalService(owner, kb._workspace.services.jobs.repository)

        def binding_reads(candidates) -> int:
            statements: list[str] = []

            def run():
                owner.db.set_trace_callback(statements.append)
                try:
                    return service.reconcile_pending_deletions(candidates)
                finally:
                    owner.db.set_trace_callback(None)

            with owner.store.leases.pin(frozenset({"hold"})):
                counts = _on_owner(kb, run)
            assert counts["deleted_payloads"] == 0
            return sum(
                any(f"FROM {table}" in sql for table in _BINDING_TABLES)
                for sql in statements
            )

        one = binding_reads(frozenset(shas[:1]))
        assert one > 0
        assert binding_reads(frozenset(shas[:200])) == one


def test_the_bound_set_decides_as_the_row_checks_on_a_real_workspace(copied):
    from tychora.adapters.sqlite.bindings import BoundSet

    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        owner = kb._workspace.owner
        service = RemovalService(owner, kb._workspace.services.jobs.repository)

        def compare():
            db = owner.db
            shas = frozenset(
                row[0] for row in db.execute("SELECT artifact_sha FROM artifacts")
            )
            rooted = service._rooted()
            bound = BoundSet.load(db, shas)
            return [
                sha
                for sha in shas
                if (bound.binds(sha) or sha in rooted)
                != service._live_root(sha, rooted=rooted)
            ], len(shas)

        differing, total = _on_owner(kb, compare)
    assert total > 100
    assert differing == []


def test_a_reconcile_pass_writes_only_payload_states(copied):
    """The per-pass bound set stays valid: the pass changes no binding row."""
    with Tychora.open(copied, token_counter=CharacterCounter()) as kb:
        _mark_rooted_pending(kb)
        owner = kb._workspace.owner
        service = RemovalService(owner, kb._workspace.services.jobs.repository)
        statements: list[str] = []

        def run():
            _payload(owner.artifacts, owner.db, "unbound pending payload")
            owner.db.commit()
            owner.db.set_trace_callback(statements.append)
            try:
                return service.reconcile_pending_deletions()
            finally:
                owner.db.set_trace_callback(None)

        counts = _on_owner(kb, run)
    assert counts["deleted_payloads"] >= 1
    assert counts["shared_payloads"] >= 1
    writes = [
        sql.split()
        for sql in statements
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    assert writes
    targets = {
        words[words.index("INTO") + 1]
        if words[0].upper() == "INSERT"
        else words[2]
        if words[0].upper() == "DELETE"
        else words[1]
        for words in writes
    }
    assert targets == {"artifacts"}, targets
