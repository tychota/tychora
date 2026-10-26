"""One-shot synthetic Chroma/Ladybug process-death diagnostic; not a pass gate."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.acceptance.source_crash_probe import service, source
from tests.acceptance.test_crash_matrix import _kill_test_child
from tests.helpers.publication import slots_of
from tests.performance.engine_probe import _config, _scope, setup_corpus
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.application.owner import WorkspaceOwner
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import BatchReceipt
from tychora.ports.execution import ExecutionContext


def run(root: Path, *, isolated_setup: bool = True) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if isolated_setup:
        setup = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.performance.engine_probe",
                "setup-graph-vector",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "1"},
        )
        if setup.returncode:
            return {"setup_error": setup.stderr[-2000:]}
    else:
        setup_corpus(root, graph=True, graph_vector=True)
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        before = owner.run_write_sync(
            owner.store.active_snapshot_id, ExecutionContext()
        )
    _kill_test_child(
        root, "graph_seal_before_publish", "tests.acceptance.source_crash_probe"
    )
    findings: dict = {"prior_snapshot": before}
    with WorkspaceOwner.open(root / "data", _config(root), _scope()) as owner:
        snapshot = owner.run_write_sync(
            lambda: owner.store.resolve_snapshot(None), ExecutionContext()
        )
        findings["after_snapshot"] = snapshot.snapshot_id
        slot = next(
            slot
            for slot in owner.run_write_sync(
                lambda: slots_of(owner.store, snapshot), ExecutionContext()
            )
            if slot.slot == "text"
        )
        index = ChromaVectorIndex(root / "vectors")
        index.create_family(
            FamilySpec.model_validate_json(
                canonical_bytes(slot.family.config_json["spec"])
            )
        )
        try:
            receipt = slot.batches[0].receipt
            assert isinstance(receipt, BatchReceipt)
            findings["prior_vector_ready"] = index.verify_batch(receipt).ready
        except Exception as error:  # noqa: BLE001 - diagnostic records native failure
            findings["prior_vector_error"] = type(error).__name__ + ": " + str(error)
        job_id = owner.run_write_sync(
            lambda: owner.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='crash-retry-key'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        jobs = owner.run_write_sync(
            lambda: JobRepository(
                owner.db,
                artifacts=owner.artifacts,
                workspace_id=owner.workspace_id,
                receipts=PublicationReceipts(owner.store),
            ),
            ExecutionContext(),
        )
        owner.run_write_sync(lambda: jobs.recover(job_id), ExecutionContext())
        owner.run_write_sync(lambda: jobs.retry(job_id), ExecutionContext())
        try:
            completed = asyncio.run(
                service(owner, root).add(source(), _scope(), "crash-retry-key")
            )
            findings["retry_state"] = completed.state
        except Exception as error:  # noqa: BLE001 - diagnostic records retry failure
            findings["retry_error"] = type(error).__name__ + ": " + str(error)
    return findings


if __name__ == "__main__":
    print(json.dumps(run(Path(sys.argv[1])), sort_keys=True))
