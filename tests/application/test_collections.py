"""Strict collection lifecycle policy and administrative DTO boundaries."""

import pytest
from pydantic import ValidationError

from tychora.application.collection_policy import (
    collection_is_active,
    parse_collection_policy,
)
from tychora.contracts.collections import CollectionCreate, CollectionEdit
from tychora.contracts.errors import TychoraError


def test_v1_collection_policy_remains_active():
    policy = parse_collection_policy('{"schema_version":1,"upload_providers":[]}')
    assert policy.archived_at is None
    assert policy.upload_providers == policy.owner_upload_providers == ()
    assert policy.default_kind_id is None
    assert collection_is_active(policy.model_dump_json())


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":2,"surprise":true}',
        '{"schema_version":2,"archived_at":"yesterday"}',
        '{"schema_version":2,"archived_at":"2026-01-01T00:00:00"}',
        '{"schema_version":2,"upload_providers":["unknown"]}',
        '{"schema_version":2,"upload_providers":["voyage","voyage"]}',
        '{"schema_version":2,"schema_version":1,"upload_providers":[]}',
        '{"schema_version":3}',
        '{"schema_version":true}',
    ],
)
def test_malformed_policy_fails_closed(raw):
    with pytest.raises(TychoraError) as error:
        parse_collection_policy(raw)
    assert error.value.code == "INTEGRITY_ERROR"


def test_injected_provider_mode_is_explicit():
    policy = parse_collection_policy(
        '{"schema_version":1,"upload_providers":["fake"]}',
        allow_injected_provider_ids=True,
    )
    assert policy.upload_providers == ("fake",)


@pytest.mark.parametrize(
    "title,slug", [("  ", "valid"), ("ok", ""), ("ok", "Not Valid")]
)
def test_invalid_names(title, slug):
    with pytest.raises(ValidationError):
        CollectionCreate(title=title, slug=slug)


def test_title_trim_and_omission_semantics():
    request = CollectionEdit(title="  Library  ", slug="library")
    assert request.title == "Library"
    assert "default_kind_id" not in request.model_fields_set
    assert (
        "default_kind_id"
        in CollectionEdit(
            title="Library", slug="library", default_kind_id=None
        ).model_fields_set
    )


@pytest.mark.asyncio
async def test_bounded_inventory_hidden_revision_and_cursor_epoch(
    tmp_path, monkeypatch
):
    from tests.transports.test_http import Counter
    from tychora import Tychora
    from tychora.contracts.collections import CollectionCreate
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        service = kb._workspace.services.collections
        source = {
            "type": "text",
            "source_key": "public-key",
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": "# Synthetic\n\nCobalt calibration.",
            "metadata": {"title": "Hidden revision title"},
        }
        job = kb.sources.add(source)
        assert kb.jobs.wait(job.id, timeout=5).state == "succeeded"
        page = await service.documents(kb.scope, ExecutionContext())
        assert len(page.documents) == 1
        item = page.documents[0]
        assert item.title == "Hidden revision title"
        assert item.original_size_bytes > 0
        assert item.channel_readiness == ("lexical",)
        assert item.channel_observations_complete
        assert (
            item.processing_state == "unknown"
            and item.published_content_state == "published"
        )
        assert item.unit_count is None and not item.counts_complete
        statements = []

        def hide_and_overflow():
            owner.db.execute("UPDATE revisions SET access_rank=3")
            owner.db.execute(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5001) INSERT INTO evidence_units(unit_id,extraction_id,parent_id,ordinal,unit_type,native_ref,boundary_key,heading_path_json,text,text_sha,text_origin,citable,declared_review_state,attributes_json) SELECT 'synthetic-'||x,extraction_id,NULL,10000+x,unit_type,'synthetic-'||x,boundary_key,heading_path_json,text,text_sha,text_origin,citable,declared_review_state,attributes_json FROM n JOIN (SELECT * FROM evidence_units LIMIT 1)"
            )
            owner.db.commit()
            owner.db.set_trace_callback(statements.append)

        await owner.run_blocking(hide_and_overflow, ExecutionContext())

        def forbidden_artifact(*args, **kwargs):
            raise AssertionError("Inventory must not decode corpus/job artifacts")

        monkeypatch.setattr(owner.artifacts, "open_verified", forbidden_artifact)
        hidden = await service.documents(kb.scope, ExecutionContext())
        await owner.run_blocking(
            lambda: owner.db.set_trace_callback(None), ExecutionContext()
        )
        item = hidden.documents[0]
        assert item.title == "public-key"
        assert (
            item.revision_id
            is item.extraction_id
            is item.original_size_bytes
            is item.original_media_type
            is None
        )
        assert item.unit_count is item.window_count is item.latest_job is None
        assert not item.counts_complete and not item.job_observations_complete
        assert hidden.candidate_record_limit == 5000
        queries = [
            sql for sql in statements if sql not in {"BEGIN", "ROLLBACK", "COMMIT"}
        ]
        assert len(queries) == 4
        assert not any("evidence_units" in sql or "jobs" in sql for sql in queries)
        for cursor in ("bad", "a" * 4097):
            with pytest.raises(TychoraError):
                await service.documents(kb.scope, ExecutionContext(), cursor=cursor)
        await service.create(
            CollectionCreate(title="Another", slug="another"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )
        first = await service.list(kb.scope, ExecutionContext(), limit=1)
        assert first.next_cursor
        await service.edit(
            str(kb.default_collection_id),
            CollectionEdit(title="Changed", slug="default"),
            kb.scope,
            ExecutionContext(),
        )
        with pytest.raises(TychoraError):
            await service.list(
                kb.scope, ExecutionContext(), limit=1, cursor=first.next_cursor
            )


@pytest.mark.asyncio
async def test_edit_kind_omission_null_and_reserved_default(tmp_path):
    from tests.transports.test_http import Counter
    from tychora import Tychora
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        service = kb._workspace.services.collections
        cid = str(kb.default_collection_id)

        async def edit(**values):
            return await service.edit(
                cid,
                CollectionEdit(title="Library", slug="default", **values),
                kb.scope,
                ExecutionContext(),
            )

        chosen = await edit(default_kind_id="manual@1")
        assert chosen.default_kind_id == "manual@1"
        assert (await edit()).default_kind_id == "manual@1"
        reset = await edit(default_kind_id=None)
        assert (
            reset.default_kind_id is None
            and reset.effective_default_kind_id == "document@1"
        )
        with pytest.raises(TychoraError):
            await edit(default_kind_id="unknown@1")
        with pytest.raises(TychoraError):
            await service.edit(
                cid,
                CollectionEdit(title="Library", slug="renamed-default"),
                kb.scope,
                ExecutionContext(),
            )


def test_actual_http_fixture_contracts():
    from pathlib import Path

    from tychora.contracts.collections import (
        CollectionConsentView,
        CollectionPage,
        CollectionSummary,
        DocumentPage,
    )

    root = Path(__file__).resolve().parents[2] / "dashboard" / "contracts"
    for filename, model in (
        ("collections-detail.json", CollectionSummary),
        ("collections-archived.json", CollectionSummary),
        ("collections-page.json", CollectionPage),
        ("collections-consent.json", CollectionConsentView),
        ("collections-documents.json", DocumentPage),
    ):
        assert model.model_validate_json((root / filename).read_bytes())


@pytest.mark.asyncio
async def test_source_page_filters_hidden_rows_before_sql_limit(tmp_path):
    from tests.transports.test_http import Counter
    from tychora import Tychora
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "workspace", token_counter=Counter()) as kb:
        owner = kb._workspace.owner
        service = kb._workspace.services.collections
        hidden = await service.create(
            CollectionCreate(title="Hidden", slug="hidden"),
            kb.scope,
            ExecutionContext(),
            managed_owner_authorized=True,
        )

        def populate():
            for n in range(101):
                owner.db.execute(
                    "INSERT INTO sources(source_id,collection_id,source_key,source_type,registration_json,access_floor,created_at) VALUES (?,?,?,'document','{}',0,'2026-01-01T00:00:00Z')",
                    (f"000-{n:03}", hidden.collection_id, f"hidden-{n}"),
                )
            owner.db.execute(
                "INSERT INTO sources(source_id,collection_id,source_key,source_type,registration_json,access_floor,created_at) VALUES ('zzz',?,'visible','document','{}',0,'2026-01-01T00:00:00Z')",
                (str(kb.default_collection_id),),
            )
            owner.db.commit()

        await owner.run_blocking(populate, ExecutionContext())
        restricted = kb.scope.model_copy(
            update={"collection_ids": frozenset({str(kb.default_collection_id)})}
        )
        page = await service.documents(restricted, ExecutionContext(), limit=1)
        assert len(page.documents) == 1 and page.documents[0].title == "visible"
        assert page.next_cursor is None
        assert page.documents[0].extraction_id is None
        assert page.documents[0].unit_count is None


@pytest.mark.asyncio
async def test_injected_provider_collection_mutations_preserve_consent(tmp_path):
    from tests.runtime.test_upload_policy import install, prepared
    from tychora.application.collections import CollectionService
    from tychora.ports.execution import ExecutionContext

    with prepared(tmp_path) as (owner, original_scope):
        scope = original_scope.model_copy(
            update={"capabilities": frozenset({"read", "manage"})}
        )
        owner.scope = scope
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        service = CollectionService(owner)
        cid = bundle.collection.collection_id
        page = await service.list(scope, ExecutionContext())
        assert len(page.collections) == 1
        request = CollectionEdit(title="Renamed", slug=bundle.collection.slug)
        edited = await service.edit(cid, request, scope, ExecutionContext())
        assert edited.title == "Renamed"
        epoch = edited.policy_epoch
        unchanged = await service.edit(cid, request, scope, ExecutionContext())
        assert unchanged.policy_epoch == epoch
        archived = await service.set_archived(cid, True, scope, ExecutionContext())
        assert archived.archived_at is not None
        active = await service.set_archived(cid, False, scope, ExecutionContext())
        assert active.archived_at is None
        consent = await service.consent(cid, scope, ExecutionContext())
        assert consent.collection_upload_providers == ("synthetic",)
        raw = await owner.run_blocking(
            lambda: owner.db.execute(
                "SELECT policy_json FROM collections WHERE collection_id=?", (cid,)
            ).fetchone()[0],
            ExecutionContext(),
        )
        policy = parse_collection_policy(raw, allow_injected_provider_ids=True)
        assert policy.schema_version == 2
        assert policy.upload_providers == ("synthetic",)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "archive"])
async def test_managed_collection_mutations_reject_injected_provider(
    tmp_path, operation
):
    from tests.runtime.test_upload_policy import install, prepared
    from tychora.application.collections import CollectionService
    from tychora.ports.execution import ExecutionContext

    with prepared(tmp_path) as (owner, original_scope):
        scope = original_scope.model_copy(
            update={"capabilities": frozenset({"read", "manage"})}
        )
        owner.scope = scope
        bundle = await owner.run_blocking(lambda: install(owner), ExecutionContext())
        owner.managed_default_owner = True
        service = CollectionService(owner)
        with pytest.raises(TychoraError) as error:
            if operation == "edit":
                await service.edit(
                    bundle.collection.collection_id,
                    CollectionEdit(title="Renamed", slug=bundle.collection.slug),
                    scope,
                    ExecutionContext(),
                )
            else:
                await service.set_archived(
                    bundle.collection.collection_id, True, scope, ExecutionContext()
                )
        assert error.value.code == "INTEGRITY_ERROR"
