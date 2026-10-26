"""Durable publication receipt recovery."""

import pytest

from tychora.adapters.sqlite.publication import PublicationReceipts
from tychora.application.owner import WorkspaceOwner
from tychora.application.sources import SourceService
from tychora.configuration.resolve import resolve_config
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext

pytest_plugins = ("tests.indexing.test_publication",)


def test_publication_before_completion_event_reconciles(projected_corpus) -> None:
    f = projected_corpus
    f.jobs.receipts = PublicationReceipts(f.store)
    _, job, plan = f.prepare_revision(
        "crash between publication and complete", source_key="crash.md"
    )
    published = f.store.publish_snapshot(plan)
    assert f.jobs.get(job.id).state == "running"
    assert f.store.resolve_snapshot(None).snapshot_id == published
    recovered = f.jobs.recover(job.id)
    assert recovered.state == "succeeded"
    assert recovered.result_snapshot_id == published
    assert f.jobs.retry(job.id).result_snapshot_id == published


@pytest.mark.asyncio
async def test_retry_uses_exact_committed_capture_and_verified_plan(
    tmp_path, monkeypatch
):
    settings = tmp_path / "workspace.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(settings, "test", {})
    collection = "55825936-6a6a-4d29-945a-327d771a76b7"
    scope = RequestScope(
        principal_id="writer",
        collection_ids=frozenset({collection}),
        capabilities=frozenset({"write", "manage", "read"}),
        uses=frozenset({"lookup"}),
    )

    class Counter:
        model = "local"
        tokenizer_id = "local-codepoints@1"

        def count(self, text):
            return len(text)

    source = SourceInput.from_mapping(
        {
            "type": "text",
            "text": "Resume without recapture.",
            "format": "text",
            "kind": "manual@1",
            "metadata": {
                "title": "Resume",
                "publisher": "Example Team",
                "edition": "1",
            },
            "source_key": "resume.txt",
            "collection_id": collection,
        }
    )

    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        await owner.run_write(
            lambda: owner.db.execute(
                'INSERT INTO collections (collection_id,workspace_id,slug,title,access_floor,policy_json) VALUES (?,?,?,?,0,\'{"schema_version":1,"upload_providers":[]}\')',
                (collection, owner.workspace_id, "manuals", "Manuals"),
            ),
            ExecutionContext(),
        )
        service = SourceService(
            owner,
            ProjectionCoordinator(
                store=owner.store,
                artifacts=owner.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        original_publish = owner.store.publish_snapshot

        def fail_once(plan):
            monkeypatch.setattr(owner.store, "publish_snapshot", original_publish)
            raise RuntimeError("sensitive activation interruption")

        monkeypatch.setattr(owner.store, "publish_snapshot", fail_once)
        with pytest.raises(RuntimeError, match="sensitive activation interruption"):
            await service.add(source, scope, "same-request")
        jobs = service._jobs
        assert jobs is not None
        status = await owner.run_write(
            lambda: jobs.get(owner.db.execute("SELECT job_id FROM jobs").fetchone()[0]),
            ExecutionContext(),
        )
        assert status.state == "interrupted" and status.captured_inputs is not None
        assert status.error_code == "JOB_INTERRUPTED"

    with WorkspaceOwner.open(tmp_path / "data", config, scope) as reopened:
        resumed = SourceService(
            reopened,
            ProjectionCoordinator(
                store=reopened.store,
                artifacts=reopened.artifacts,
                config=config,
                counter=Counter(),
            ),
        )
        jobs, acquirer = await resumed._ports(ExecutionContext())
        await reopened.run_write(lambda: jobs.retry(status.id), ExecutionContext())
        original_capture = acquirer.capture

        def forbidden_capture(*args):
            raise AssertionError("Retry must use committed capture")

        monkeypatch.setattr(acquirer, "capture", forbidden_capture)
        completed = await resumed.add(source, scope, "same-request")
        assert completed.state == "succeeded" and completed.id == status.id
        assert (
            await reopened.run_write(
                lambda: reopened.db.execute(
                    "SELECT COUNT(*) FROM extractions"
                ).fetchone()[0],
                ExecutionContext(),
            )
            == 1
        )

        monkeypatch.setattr(acquirer, "capture", original_capture)

        parser_source = SourceInput.from_mapping(
            {
                "type": "text",
                "text": "Parser interruption candidate.",
                "format": "text",
                "kind": "manual@1",
                "metadata": {
                    "title": "Parser interruption",
                    "publisher": "Example Team",
                    "edition": "1",
                },
                "source_key": "parser.txt",
                "collection_id": collection,
            }
        )
        original_extract = resumed._extract

        async def fail_parse(*args):
            monkeypatch.setattr(resumed, "_extract", original_extract)
            raise RuntimeError("sensitive parser interruption")

        monkeypatch.setattr(resumed, "_extract", fail_parse)
        with pytest.raises(RuntimeError, match="sensitive parser interruption"):
            await resumed.add(parser_source, scope, "parser-before-plan")
        parser_job = await reopened.run_write(
            lambda: reopened.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='parser-before-plan'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        assert (
            await reopened.run_write(
                lambda: jobs.get(parser_job).error_code, ExecutionContext()
            )
            == "JOB_INTERRUPTED"
        )
        await reopened.run_write(lambda: jobs.retry(parser_job), ExecutionContext())
        assert (
            await resumed.add(parser_source, scope, "parser-before-plan")
        ).state == "succeeded"

        original_complete = jobs.complete

        def fail_after_publication(*args, **kwargs):
            monkeypatch.setattr(jobs, "complete", original_complete)
            raise RuntimeError("sensitive completion interruption")

        monkeypatch.setattr(jobs, "complete", fail_after_publication)
        with pytest.raises(RuntimeError, match="sensitive completion interruption"):
            await resumed.add(source, scope, "published-before-event")
        published_job = await reopened.run_write(
            lambda: reopened.db.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key='published-before-event'"
            ).fetchone()[0],
            ExecutionContext(),
        )
        published_status = await reopened.run_write(
            lambda: jobs.get(published_job), ExecutionContext()
        )
        assert published_status.state == "succeeded"
        assert published_status.result_snapshot_id == await reopened.run_write(
            reopened.store.active_snapshot_id, ExecutionContext()
        )
