"""Retained content exposes exact stored ranges under current permissions."""

import pytest

from tests.application.test_inspection import Counter
from tests.application.test_inspection import published as published_fixture
from tychora.application.inspection import InspectionService
from tychora.application.sources import SourceService
from tychora.contracts import SourceInput, operations
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import TextLocator
from tychora.indexing.coordinator import ProjectionCoordinator
from tychora.ports.execution import ExecutionContext

published = published_fixture


async def content_request(owner, **kwargs):
    lineage = await owner.run_blocking(
        lambda: owner.db.execute(
            "SELECT r.source_id,e.revision_id,e.extraction_id FROM extractions e "
            "JOIN revisions r USING(revision_id) ORDER BY e.created_at DESC LIMIT 1"
        ).fetchone(),
        ExecutionContext(),
    )
    return operations.ExtractionContentRequest(
        source_id=lineage[0], revision_id=lineage[1], extraction_id=lineage[2], **kwargs
    )


@pytest.fixture
async def long_content(published):
    owner, scope, _ = published
    text = ("Unicode 😀 e\u0301 <|literal|> & <script>\r\n" + "word " * 90) * 30
    service = SourceService(
        owner,
        ProjectionCoordinator(
            store=owner.store,
            artifacts=owner.artifacts,
            config=owner.config,
            counter=Counter(),
        ),
    )
    await service.add(
        SourceInput.from_mapping(
            {
                "type": "text",
                "text": text,
                "format": "text",
                "kind": "manual@1",
                "source_key": "long.txt",
                "collection_id": next(iter(scope.collection_ids)),
                "metadata": {
                    "title": "Long synthetic",
                    "publisher": "Fixture",
                    "edition": "1",
                },
            }
        ),
        scope,
    )
    yield owner, scope


@pytest.mark.asyncio
async def test_pages_preserve_stored_identity_unicode_and_source_order(long_content):
    owner, scope = long_content
    request = await content_request(owner, limit=1)
    rows, units = await owner.run_blocking(
        lambda: (
            owner.db.execute(
                "SELECT c.chunk_id,c.unit_id,c.char_start,c.char_end,c.token_count,c.chunker_fingerprint "
                "FROM chunks c JOIN evidence_units u USING(unit_id) WHERE c.extraction_id=? "
                "ORDER BY u.ordinal,u.unit_id,c.char_start,c.char_end,c.chunk_id",
                (request.extraction_id,),
            ).fetchall(),
            owner.store._hydrate(
                tuple(
                    r[0]
                    for r in owner.db.execute(
                        "SELECT unit_id FROM evidence_units WHERE extraction_id=? ORDER BY ordinal,unit_id",
                        (request.extraction_id,),
                    )
                )
            ),
        ),
        ExecutionContext(),
    )
    assert len(rows) > 2
    canonical = {u.unit_id: u for u in units}
    service = InspectionService(owner)
    windows = []
    while True:
        page = await service.extraction_windows(request, scope, ExecutionContext())
        windows.extend(page.windows)
        assert page.scope_limited is False
        assert len(page.model_dump_json().encode()) <= 1048576
        if page.next_cursor is None:
            break
        request = request.model_copy(update={"cursor": page.next_cursor})
    assert [
        (
            w.id,
            w.unit_id,
            w.char_start,
            w.char_end,
            w.token_count,
            w.chunker_fingerprint,
        )
        for w in windows
    ] == rows
    for window in windows:
        assert (
            window.text
            == canonical[window.unit_id].text[window.char_start : window.char_end]
        )
        assert "tokenizer_id" not in window.model_dump()
    unit_page = await service.extraction_units(
        request.model_copy(update={"cursor": None, "limit": 50}),
        scope,
        ExecutionContext(),
    )
    assert [u.id for u in unit_page.units] == [u.unit_id for u in units]
    assert all(len(u.preview) <= 240 for u in unit_page.units)
    assert "parent_id" not in unit_page.model_dump_json()


@pytest.mark.asyncio
async def test_content_scope_lineage_and_cursor_bindings(long_content):
    owner, scope = long_content
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    first = await service.extraction_windows(request, scope, ExecutionContext())
    assert first.next_cursor
    request = request.model_copy(update={"cursor": first.next_cursor})
    for update in (
        {"limit": 2},
        {"unit_id": first.windows[0].unit_id},
        {"chunk_id": first.windows[0].id},
        {"cursor": "broken"},
    ):
        with pytest.raises(TychoraError) as error:
            await service.extraction_windows(
                request.model_copy(update=update), scope, ExecutionContext()
            )
        assert error.value.code == "INVALID_REQUEST"
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(
            request,
            scope.model_copy(update={"uses": frozenset({"lookup"})}),
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"
    for field in ("source_id", "revision_id", "extraction_id"):
        with pytest.raises(TychoraError) as error:
            await service.extraction_windows(
                request.model_copy(update={field: "missing", "cursor": None}),
                scope,
                ExecutionContext(),
            )
        assert error.value.code == "NOT_FOUND"
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(
            request.model_copy(update={"cursor": None}),
            scope.model_copy(update={"collection_ids": frozenset()}),
            ExecutionContext(),
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


async def restricted_windows(owner, uid):
    from hashlib import sha256

    from tychora.contracts.identity import chunk_id

    def install():
        owner.db.execute(
            "INSERT INTO evidence_units SELECT 'rejected-child',extraction_id,unit_id,"
            "ordinal+100,'paragraph','child',boundary_key,heading_path_json,"
            "'source',?,text_origin,citable,'unreviewed','{}' FROM evidence_units WHERE unit_id=?",
            (sha256(b"source").hexdigest(), uid),
        )
        owner.db.execute(
            "INSERT INTO locators SELECT 'child-locator','rejected-child',0,locator_type,"
            "artifact_sha,source_ref,physical_page,page_label,line_start,line_end,byte_start,"
            "byte_end,geometry_json,json_set(details_json,'$.char_start',6,'$.char_end',12) "
            "FROM locators WHERE unit_id=?",
            (uid,),
        )
        owner.db.execute(
            "INSERT INTO review_events(unit_id,state,actor_id,created_at) "
            "VALUES ('rejected-child','rejected','reviewer','2026-01-01T00:00:00Z')"
        )
        extraction = owner.db.execute(
            "SELECT extraction_id FROM evidence_units WHERE unit_id=?", (uid,)
        ).fetchone()[0]
        for ordinal, (start, end) in enumerate(((0, 6), (0, 22), (6, 12), (12, 22))):
            owner.db.execute(
                "INSERT INTO chunks(chunk_id,unit_id,extraction_id,ordinal_in_unit,char_start,char_end,context_group_key,chunker_fingerprint,token_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    chunk_id(uid, start, end, "retained-fixture"),
                    uid,
                    extraction,
                    ordinal,
                    start,
                    end,
                    "fixture",
                    "retained-fixture",
                    1,
                ),
            )

    await owner.run_blocking(install, ExecutionContext())


@pytest.mark.asyncio
async def test_excluded_overlap_cannot_hide_later_disjoint_windows(published):
    owner, scope, uid = published
    await restricted_windows(owner, uid)
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    units = await service.extraction_units(request, scope, ExecutionContext())
    assert units.units == ()
    assert units.scope_limited
    assert "rejected-child" not in units.model_dump_json()
    first = await service.extraction_windows(request, scope, ExecutionContext())
    second = await service.extraction_windows(
        request.model_copy(update={"cursor": first.next_cursor}),
        scope,
        ExecutionContext(),
    )
    assert [w.text for w in (*first.windows, *second.windows)] == [
        "Exact ",
        " evidence.",
    ]
    assert second.scope_limited
    assert second.next_cursor is None
    assert "rejected-child" not in second.model_dump_json()
    assert [
        (loc.char_start, loc.char_end)
        for loc in second.windows[0].locators
        if isinstance(loc, TextLocator)
    ] == [(12, 22)]
    # A rejected parent cannot be revived by requesting a locally disjoint range.
    await owner.run_blocking(
        lambda: owner.db.execute(
            "INSERT INTO review_events(unit_id,state,actor_id,created_at) VALUES (?,'rejected','reviewer','2026-01-01T00:00:00Z')",
            (uid,),
        ),
        ExecutionContext(),
    )
    denied = await service.extraction_windows(request, scope, ExecutionContext())
    assert denied.windows == ()
    assert denied.scope_limited


@pytest.mark.asyncio
async def test_cursor_rejects_policy_epoch_change_and_delivery_race(
    long_content, monkeypatch
):
    owner, scope = long_content
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    first = await service.extraction_windows(request, scope, ExecutionContext())
    await owner.run_blocking(
        lambda: owner.store.record_review(
            ReviewTarget(unit_id=first.windows[0].unit_id), "accepted", "reviewer"
        ),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(
            request.model_copy(update={"cursor": first.next_cursor}),
            scope,
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"
    original = service.compiler.revalidate_epoch

    def mutate_then_check(view, boundary):
        owner.store.record_review(
            ReviewTarget(unit_id=first.windows[0].unit_id), "rejected", "reviewer"
        )
        original(view, boundary)

    monkeypatch.setattr(service.compiler, "revalidate_epoch", mutate_then_check)
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(request, scope, ExecutionContext())
    assert error.value.code == "SCOPE_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "assignment", ["char_end=999999", "chunk_id='forged'", "extraction_id='wrong'"]
)
async def test_corrupt_window_is_never_sliced_or_served(published, assignment):
    owner, scope, _ = published
    request = await content_request(owner)

    # Foreign-key checks may reject corruption at storage instead; simulate a damaged row.
    def corrupt():
        owner.db.execute("PRAGMA foreign_keys=OFF")
        owner.db.execute("UPDATE chunks SET " + assignment)

    await owner.run_blocking(corrupt, ExecutionContext())
    with pytest.raises(TychoraError) as error:
        await InspectionService(owner).extraction_windows(
            request, scope, ExecutionContext()
        )
    assert error.value.code == "INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_removed_original_and_empty_unit_remain_inspectable(published):
    from hashlib import sha256

    owner, scope, uid = published
    request = await content_request(owner)

    def mutate():
        owner.db.execute(
            "INSERT INTO original_payload_tombstones SELECT revision_id,original_sha,"
            "(SELECT job_id FROM jobs LIMIT 1),'2026-01-01T00:00:00Z' FROM revisions"
        )
        owner.db.execute(
            "INSERT INTO evidence_units SELECT 'empty',extraction_id,NULL,ordinal+1,"
            "'section','empty',boundary_key,'[]','',?,text_origin,0,'unreviewed','{}' "
            "FROM evidence_units WHERE unit_id=?",
            (sha256(b"").hexdigest(), uid),
        )

    await owner.run_blocking(mutate, ExecutionContext())
    service = InspectionService(owner)
    page = await service.extraction_units(request, scope, ExecutionContext())
    assert [(u.id, u.preview) for u in page.units] == [
        (uid, "Exact source evidence."),
        ("empty", ""),
    ]
    windows = await service.extraction_windows(request, scope, ExecutionContext())
    assert [w.text for w in windows.windows] == ["Exact source evidence."]
    missing = await service.extraction_windows(
        request.model_copy(update={"chunk_id": "unknown"}), scope, ExecutionContext()
    )
    assert missing.windows == ()


@pytest.mark.asyncio
async def test_candidate_bound_continues_across_an_entire_denied_batch(
    published, monkeypatch
):
    import tychora.application.retained_content as content

    owner, scope, uid = published
    await restricted_windows(owner, uid)
    # The candidate budget is independent of the number of readable results.
    monkeypatch.setattr(content, "MAX_CANDIDATES", 2)
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    first = await service.extraction_windows(request, scope, ExecutionContext())
    assert [w.text for w in first.windows] == ["Exact "]
    skipped = await service.extraction_windows(
        request.model_copy(update={"cursor": first.next_cursor}),
        scope,
        ExecutionContext(),
    )
    assert skipped.windows == ()
    assert skipped.scope_limited and skipped.next_cursor
    assert uid not in skipped.next_cursor
    last = await service.extraction_windows(
        request.model_copy(update={"cursor": skipped.next_cursor}),
        scope,
        ExecutionContext(),
    )
    assert [w.text for w in last.windows] == [" evidence."]
    assert last.next_cursor is None


@pytest.mark.asyncio
async def test_byte_budget_continues_without_truncating_windows(
    long_content, monkeypatch
):
    import tychora.application.retained_content as content

    owner, scope = long_content
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    first = await service.extraction_windows(request, scope, ExecutionContext())
    budget = len(first.model_dump_json().encode("utf-8"))
    monkeypatch.setattr(content, "MAX_PAGE_BYTES", budget)
    page = await service.extraction_windows(
        request.model_copy(update={"limit": 50}), scope, ExecutionContext()
    )
    assert page.windows == first.windows
    assert page.next_cursor
    assert len(page.model_dump_json().encode("utf-8")) <= budget
    monkeypatch.setattr(content, "MAX_PAGE_BYTES", budget - 1)
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(request, scope, ExecutionContext())
    assert error.value.code == "BUDGET_TOO_SMALL"


@pytest.mark.asyncio
async def test_cursor_cannot_move_to_other_retained_lineage_or_resource(long_content):
    owner, scope = long_content
    service = InspectionService(owner)
    request = await content_request(owner, limit=1)
    first = await service.extraction_windows(request, scope, ExecutionContext())
    with pytest.raises(TychoraError) as error:
        await service.extraction_units(
            request.model_copy(update={"cursor": first.next_cursor}),
            scope,
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"
    other = await owner.run_blocking(
        lambda: owner.db.execute(
            "SELECT r.source_id,e.revision_id,e.extraction_id FROM extractions e JOIN revisions r USING(revision_id) WHERE e.extraction_id<>?",
            (request.extraction_id,),
        ).fetchone(),
        ExecutionContext(),
    )
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(
            request.model_copy(
                update={
                    "source_id": other[0],
                    "revision_id": other[1],
                    "extraction_id": other[2],
                    "cursor": first.next_cursor,
                }
            ),
            scope,
            ExecutionContext(),
        )
    assert error.value.code == "INVALID_REQUEST"
    with pytest.raises(TychoraError) as error:
        await service.extraction_windows(
            request.model_copy(update={"source_id": other[0]}),
            scope,
            ExecutionContext(),
        )
    assert error.value.code == "NOT_FOUND"
