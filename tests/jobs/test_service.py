import asyncio
import time
from io import BytesIO

import pytest

from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ports.execution import ExecutionContext


@pytest.mark.asyncio
async def test_service_wait_cancel_events_use_canonical_thread(tmp_path):
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.jobs import JobService

    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    settings = resolve_config(config_path, "test", {})
    scope = RequestScope(
        principal_id="writer", capabilities=frozenset({"read", "write"})
    )
    with WorkspaceOwner.open(tmp_path / "workspace", settings, scope) as owner:
        repo = await owner.run_blocking(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ExecutionContext(),
        )
        config = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(
                BytesIO(
                    b'{"type":"registry","schema_version":"1","data":{"offline":true}}'
                ),
                "application/json",
            ),
            ExecutionContext(),
        )
        service = JobService(owner, repo, scope)
        job = await service.accept("add", "key", {}, config, ExecutionContext())
        waiter = asyncio.create_task(
            service.wait(job.id, ExecutionContext(), timeout=2)
        )
        await asyncio.sleep(0.03)
        await service.cancel(job.id, ExecutionContext())
        assert (await waiter).state == "cancelled"
        assert (await service.events_after(job.id, 0, 100, ExecutionContext()))[
            -1
        ].state == "cancelled"
        assert (await service.get(job.id, ExecutionContext())).id == job.id


@pytest.mark.asyncio
async def test_wait_timeout_and_delivery_cancel_do_not_cancel_job(tmp_path):
    from tychora.adapters.sqlite.jobs import JobRepository
    from tychora.application.jobs import JobService

    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    settings = resolve_config(config_path, "test", {})
    scope = RequestScope(
        principal_id="writer", capabilities=frozenset({"read", "write"})
    )
    with WorkspaceOwner.open(tmp_path / "workspace", settings, scope) as owner:
        repo = await owner.run_blocking(
            lambda: JobRepository(
                owner.db, artifacts=owner.artifacts, workspace_id=owner.workspace_id
            ),
            ExecutionContext(),
        )
        config = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(
                BytesIO(b'{"type":"registry","schema_version":"1"}'), "application/json"
            ),
            ExecutionContext(),
        )
        service = JobService(owner, repo, scope)
        job = await service.accept("add", "key", {}, config, ExecutionContext())
        assert (
            await service.wait(job.id, ExecutionContext(), timeout=0.02)
        ).state == "queued"
        pending = asyncio.create_task(
            service.wait(job.id, ExecutionContext(), timeout=2)
        )
        await asyncio.sleep(0.03)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await service.get(job.id, ExecutionContext())).state == "queued"
        with pytest.raises(TychoraError) as error:
            await service.wait(job.id, ExecutionContext(deadline=time.monotonic() - 1))
        assert error.value.code == "DEADLINE_EXCEEDED"
        restricted = JobService(
            owner, repo, scope.model_copy(update={"capabilities": frozenset()})
        )
        with pytest.raises(TychoraError) as error:
            await restricted.get(job.id, ExecutionContext())
        assert error.value.code == "FORBIDDEN_SCOPE"


def test_source_intent_hashes_supplied_bytes_and_preserves_omission():
    from tychora.application.jobs import source_intent

    source = SourceInput.from_mapping(
        {
            "type": "text",
            "source_key": "./manual.md",
            "collection_id": "00000000-0000-0000-0000-000000000001",
            "kind": "manual@1",
            "text": "synthetic",
        }
    )
    intent = source_intent(source)
    assert intent["source_key"] == "manual.md"
    assert "text" not in intent and "format" not in intent
    assert (
        intent["content_sha256"]
        == "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2"
    )


def test_job_contracts_roundtrip_and_schema(jobs):
    from tychora.contracts.jobs import JobStatus, PublicationReceipt

    job = jobs.accept("add", jobs.scope, "key", {}, jobs.config)
    for model in (
        jobs.repo.get(job.id),
        PublicationReceipt(job_id=job.id, result_snapshot_id="snapshot"),
    ):
        assert type(model).model_validate_json(model.model_dump_json()) == model
        assert type(model).model_json_schema()["additionalProperties"] is False
    assert (
        JobStatus.model_json_schema()["properties"]["latest_sequence"]["minimum"] == 0
    )
