"""An interrupted add whose checkpointed batches were collected re-prepares."""

import pytest

from tests.indexing.test_sources import _jobs, _text_source, _text_workspace
from tychora.application.snapshot_gc import SnapshotGc
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


async def _interrupted_add(owner, service, scope, monkeypatch) -> str:
    """Add a source that dies after checkpointing its plan; return the job ID."""
    original = owner.store.publish_snapshot

    def killed(plan):
        monkeypatch.setattr(owner.store, "publish_snapshot", original)
        raise TychoraError("PROVIDER_FAILURE", "Killed", retryable=True)

    monkeypatch.setattr(owner.store, "publish_snapshot", killed)
    with pytest.raises(TychoraError):
        await service.add(_text_source("second"), scope, "resume-second")
    return await owner.run_write(
        lambda: owner.db.execute(
            "SELECT job_id FROM jobs WHERE idempotency_key='resume-second'"
        ).fetchone()[0],
        ExecutionContext(),
    )


async def _collect_everything(owner) -> None:
    def collect():
        gc = SnapshotGc(owner)
        gc.collect(ExecutionContext(), limit=1024, include_unpublished=True)
        gc.reclaim()

    await owner.run_write(collect, ExecutionContext())


async def _retry_and_resume(owner, service, scope, job_id) -> None:
    await owner.run_write(lambda: _jobs(service).retry(job_id), ExecutionContext())
    resumed = await service.add(_text_source("second"), scope, "resume-second")
    assert resumed.state == "succeeded"
    state = await owner.run_write(
        lambda: owner.db.execute(
            "SELECT state FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0],
        ExecutionContext(),
    )
    assert state == "complete"


@pytest.mark.asyncio
async def test_resume_after_gc_collected_the_checkpointed_batches(
    tmp_path, monkeypatch
):
    async with _text_workspace(tmp_path) as (owner, service, _coordinator, scope):
        await service.add(_text_source("first"), scope)
        job_id = await _interrupted_add(owner, service, scope, monkeypatch)
        await _collect_everything(owner)
        await _retry_and_resume(owner, service, scope, job_id)


@pytest.mark.asyncio
async def test_resume_after_page_bytes_were_lost(tmp_path, monkeypatch):
    async with _text_workspace(tmp_path) as (owner, service, _coordinator, scope):
        await service.add(_text_source("first"), scope)
        job_id = await _interrupted_add(owner, service, scope, monkeypatch)
        # The checkpointed plan's page bytes are gone; a fresh plan has its own.
        real = owner.store.publish_snapshot
        calls = []

        def lost(plan):
            if not calls:
                calls.append(plan)
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Membership page bytes are missing",
                    reason="payload.missing",
                )
            return real(plan)

        monkeypatch.setattr(owner.store, "publish_snapshot", lost)
        await _retry_and_resume(owner, service, scope, job_id)
        assert len(calls) == 1
