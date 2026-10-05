"""Real SIGKILL/reopen probes at named durable boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from tests.acceptance.graph_crash_probe import fixture as graph_fixture
from tests.acceptance.source_crash_probe import service, source
from tests.acceptance.vector_crash_probe import family
from tests.operations.test_removal import _add, _Counter, _original_sha
from tests.performance.engine_probe import _config, _scope, setup_corpus
from tychora import Tychora
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.application.backup import BackupService
from tychora.application.owner import WorkspaceOwner
from tychora.application.removal import RemovalService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.graph import GraphManifest
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


def _kill_test_child(root, point, module):
    victim = subprocess.Popen(
        [sys.executable, "-m", module, str(root), point],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        marker = root / point
        deadline = time.monotonic() + 20
        while (
            not marker.exists()
            and victim.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not marker.exists():
            _, error = victim.communicate(timeout=5)
            raise AssertionError(f"Child did not reach {point}: {error}")
        assert int(marker.read_text()) == victim.pid
        victim.kill()
        victim.wait(timeout=5)
        assert victim.returncode != 0
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)
        if victim.stdout is not None:
            victim.stdout.close()
        if victim.stderr is not None:
            victim.stderr.close()


@pytest.mark.parametrize(
    "point,expected_before",
    [("half_embeddings", 1), ("backend_before_receipt", 1)],
)
def test_native_vector_write_death_reopens_and_retries(
    tmp_path, point, expected_before
):
    root = tmp_path / "victim"
    root.mkdir()
    _kill_test_child(root, point, "tests.acceptance.vector_crash_probe")
    index = ChromaVectorIndex(root / "vectors")
    spec = family()
    index.create_family(spec.spec)
    ids = index._collection.get(ids=["r1", "r2"])["ids"]
    assert len(ids) == expected_before
    rows = [("r1", "u1", (1.0, 0.0))]
    if point == "half_embeddings":
        rows.append(("r2", "u2", (0.0, 1.0)))
    batch = spec.batch(rows)
    receipt = index.upsert_batch(batch, ExecutionContext())
    assert index.verify_batch(receipt).ready
    assert index.upsert_batch(batch, ExecutionContext()) == receipt


@pytest.mark.parametrize(
    "point", ["mid_extraction", "artifact_before_sql", "publish_before_event"]
)
def test_source_process_death_reconciles_exact_job_and_snapshot(tmp_path, point):
    root = tmp_path / "victim"
    setup_corpus(root)
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        before = owner.run_blocking_sync(
            owner.store.active_snapshot_id, ExecutionContext()
        )
    _kill_test_child(root, point, "tests.acceptance.source_crash_probe")
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        after = owner.run_blocking_sync(
            owner.store.active_snapshot_id, ExecutionContext()
        )
        job_id = owner.run_blocking_sync(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='crash-retry-key'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        jobs = owner.run_blocking_sync(
            lambda: JobRepository(
                owner.db,
                artifacts=owner.artifacts,
                workspace_id=owner.workspace_id,
                receipts=PublicationReceipts(owner.store),
            ),
            ExecutionContext(),
        )
        if point in {"mid_extraction", "artifact_before_sql"}:
            assert after == before
            assert owner.run_blocking_sync(
                lambda: jobs.recover(job_id).state, ExecutionContext()
            ) in {"failed", "interrupted"}
            owner.run_blocking_sync(lambda: jobs.retry(job_id), ExecutionContext())
            completed = asyncio.run(
                service(owner, root).add(source(), _scope(), "crash-retry-key")
            )
            assert completed.state == "succeeded" and completed.id == job_id
            assert (
                owner.run_blocking_sync(
                    owner.store.active_snapshot_id, ExecutionContext()
                )
                != before
            )
        else:
            assert after != before
            recovered = owner.run_blocking_sync(
                lambda: jobs.recover(job_id), ExecutionContext()
            )
            assert recovered.state == "succeeded"
            assert recovered.result_snapshot_id == after
            assert (
                owner.run_blocking_sync(
                    lambda: jobs.retry(job_id).result_snapshot_id,
                    ExecutionContext(),
                )
                == after
            )


@pytest.mark.parametrize(
    "point", ["tombstone_before_unlink", "unlink_before_final_sql"]
)
def test_original_removal_death_reconciles_tombstone_and_payload(tmp_path, point):
    root = tmp_path / "victim"
    root.mkdir()
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        source_id, revision = _add(
            kb, "unique-crash-original", text="unique crash bytes"
        )
        sha = _original_sha(kb, revision)
        kb.management.run({"operation": "remove", "source_ids": [source_id]})
    (root / "victim.json").write_text(json.dumps([source_id, revision, sha]))
    _kill_test_child(root, point, "tests.acceptance.removal_crash_probe")
    with sqlite3.connect(root / "knowledge" / "corpus.sqlite3") as db:
        assert db.execute(
            "SELECT payload_state FROM artifacts WHERE artifact_sha=?", (sha,)
        ).fetchone() == ("delete_pending",)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM original_payload_tombstones WHERE revision_id=?",
                (revision,),
            ).fetchone()[0]
            >= 1
        )
    assert (root / "knowledge" / "artifacts" / sha).exists() == (
        point == "tombstone_before_unlink"
    )
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        owner = kb._workspace.owner
        tombstones = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: owner.db.execute(
                    "SELECT COUNT(*) FROM original_payload_tombstones WHERE revision_id=?",
                    (revision,),
                ).fetchone()[0],
                ExecutionContext(),
            )
        )
        assert tombstones >= 1
        counts = kb._workspace.bridge.call(
            owner.run_blocking(
                lambda: RemovalService(
                    owner, kb._workspace.services.jobs.repository
                ).reconcile_pending_deletions(),
                ExecutionContext(),
            )
        )
        assert counts["pending_payloads"] == 0
        assert (
            kb._workspace.bridge.call(
                owner.run_blocking(
                    lambda: owner.db.execute(
                        "SELECT payload_state FROM artifacts WHERE artifact_sha=?",
                        (sha,),
                    ).fetchone()[0],
                    ExecutionContext(),
                )
            )
            == "deleted"
        )
        assert not (owner.path / "artifacts" / sha).exists()


def test_owner_death_with_live_parser_child_retains_unknown_staging(tmp_path):
    root = tmp_path / "victim"
    root.mkdir()
    (root / "workspace.toml").write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    owner = subprocess.Popen(
        [sys.executable, "-m", "tests.acceptance.parser_crash_probe", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    parser_pid = None
    try:
        marker = root / "parser-child-live"
        deadline = time.monotonic() + 15
        while (
            not marker.exists() and owner.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not marker.exists():
            _, error = owner.communicate(timeout=5)
            raise AssertionError(f"Parser child did not start: {error}")
        parser_pid = int(marker.read_text())
        assert parser_pid != owner.pid
        assert subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(parser_pid)], text=True
        ).strip() == str(owner.pid)
        staging = tuple((root / "data" / "staging").glob("parser-*/0"))
        assert len(staging) == 1
        assert staging[0].read_text() == str(root)
        owner.kill()
        owner.wait(timeout=5)
        assert owner.returncode != 0
        try:
            os.kill(parser_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parser_pid = None
        with WorkspaceOwner.open(
            root / "data",
            resolve_config(root / "workspace.toml", "test", {}),
            RequestScope(principal_id="synthetic-parser-owner"),
        ):
            assert staging[0].read_text() == str(root)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if parser_pid is not None:
            try:
                os.kill(parser_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if owner.stdout is not None:
            owner.stdout.close()
        if owner.stderr is not None:
            owner.stderr.close()


def test_backup_death_before_receipt_preserves_unpublished_staging(tmp_path):
    root = tmp_path / "victim"
    root.mkdir()
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        _add(kb, "backup-crash", text="synthetic backup bytes")
    _kill_test_child(
        root, "backup_before_receipt", "tests.acceptance.backup_crash_probe"
    )
    assert not (root / "backup").exists()
    stages = tuple(root.glob(".backup-staging-*"))
    assert len(stages) == 1
    assert (stages[0] / "corpus.sqlite3").is_file()
    assert any((stages[0] / "artifacts").iterdir())
    with Tychora.open(root / "knowledge", token_counter=_Counter()) as kb:
        receipt = BackupService(kb._workspace.owner).export(
            root / "retry-backup", kb._workspace.owner.scope, ctx=ExecutionContext()
        )
        assert receipt.destination == root / "retry-backup"
        assert (root / "retry-backup" / "backup-receipt.json").is_file()
    shutil.rmtree(stages[0])


def test_graph_seal_death_before_publication_reopens_native_receipt(tmp_path):
    root = tmp_path / "victim"
    root.mkdir()
    _kill_test_child(
        root, "graph_seal_before_publish", "tests.acceptance.graph_crash_probe"
    )
    spec, build, _units, _assertion = graph_fixture(root, True)
    graph = LadybugGraphStore(root / "graph", build=build)
    try:
        generation = graph.begin_generation(spec)
        receipt = graph.seal_generation(
            generation, GraphManifest.from_build(build), ExecutionContext()
        )
        assert receipt.assertion_count == 1
        assert receipt.evidence_count == 2
        assert graph._connection.execute(
            "MATCH (a:RelationAssertion) RETURN count(a)"
        ).get_all() == [[1]]
    finally:
        graph.close()


def test_canonical_graph_seal_death_preserves_prior_snapshot_then_retries(tmp_path):
    root = tmp_path / "victim"
    setup_corpus(root, graph=True)
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        before = owner.run_blocking_sync(
            owner.store.active_snapshot_id, ExecutionContext()
        )
    prior_generations = {
        item.name
        for item in (root / "graphs").iterdir()
        if not item.name.endswith((".generations", ".wal"))
    }
    _kill_test_child(
        root, "graph_seal_before_publish", "tests.acceptance.source_crash_probe"
    )
    sealed_generations = {
        item.name
        for item in (root / "graphs").iterdir()
        if not item.name.endswith((".generations", ".wal"))
    }
    staged = sealed_generations - prior_generations
    assert len(staged) == 1
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        assert (
            owner.run_blocking_sync(owner.store.active_snapshot_id, ExecutionContext())
            == before
        )
        assert (
            owner.run_blocking_sync(
                lambda: owner.db.execute(
                    "SELECT COUNT(*) FROM index_batches WHERE receipt_json LIKE ?",
                    (f"%{next(iter(staged))}%",),
                ).fetchone()[0],
                ExecutionContext(),
            )
            == 0
        )
        job_id = owner.run_blocking_sync(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='crash-retry-key'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        jobs = owner.run_blocking_sync(
            lambda: JobRepository(
                owner.db,
                artifacts=owner.artifacts,
                workspace_id=owner.workspace_id,
                receipts=PublicationReceipts(owner.store),
            ),
            ExecutionContext(),
        )
        owner.run_blocking_sync(lambda: jobs.recover(job_id), ExecutionContext())
        owner.run_blocking_sync(lambda: jobs.retry(job_id), ExecutionContext())
        completed = asyncio.run(
            service(owner, root).add(source(), _scope(), "crash-retry-key")
        )
        assert completed.state == "succeeded" and completed.id == job_id
        snapshot = owner.run_blocking_sync(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        assert snapshot.snapshot_id != before
        graph_slot = next(slot for slot in snapshot.slots if slot.slot == "graph")
        assert graph_slot.batches[0].receipt.generation_id in staged
        assert (
            graph_slot.batches[0].graph_manifest.spec.projection_version == "stable-v2"
        )


def test_graph_v2_reopens_across_hash_seeds_and_v1_preserves_legacy_proof(tmp_path):
    def probe(root, mode, seed):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.acceptance.graph_crash_probe",
                str(root),
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": seed},
        )

    legacy = tmp_path / "legacy"
    stable = tmp_path / "stable"
    legacy.mkdir()
    stable.mkdir()
    assert probe(legacy, "legacy-create", "1").returncode == 0
    matching = probe(legacy, "legacy-verify", "1")
    assert matching.returncode == 0, matching.stderr
    mismatched = probe(legacy, "legacy-verify", "2")
    assert mismatched.returncode != 0
    assert "legacy-v1 proof cannot be reordered" in mismatched.stderr
    assert "new stable-v2 publication" in mismatched.stderr
    assert probe(legacy, "legacy-verify", "1").stdout == matching.stdout
    assert probe(stable, "stable-create", "1").returncode == 0
    reopened = probe(stable, "stable-verify", "2")
    assert reopened.returncode == 0, reopened.stderr
    assert reopened.stdout != matching.stdout


def test_combined_vector_graph_crash_reopens_prior_vector_and_retries(tmp_path):
    from tests.acceptance.combined_store_probe import run

    result = run(tmp_path / "victim")
    assert result["after_snapshot"] == result["prior_snapshot"]
    assert result["prior_vector_ready"] is True
    assert result["retry_state"] == "succeeded"
