"""Retained content exposes exact stored ranges under current permissions."""

import pytest

from tests.application.test_inspection import Counter
from tests.application.test_inspection import published as published_fixture
from tests.helpers.corpus import raw_review, refresh_facts
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
    lineage = await owner.run_write(
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
    rows, units = await owner.run_write(
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
        raw_review(owner.db, "unit", "rejected-child")
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
        refresh_facts(owner.store, extraction)

    await owner.run_write(install, ExecutionContext())


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
    await owner.run_write(
        lambda: raw_review(owner.db, "unit", uid),
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
    await owner.run_write(
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

    await owner.run_write(corrupt, ExecutionContext())
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
        refresh_facts(owner.store, request.extraction_id)

    await owner.run_write(mutate, ExecutionContext())
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
    other = await owner.run_write(
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


@pytest.mark.asyncio
@pytest.mark.parametrize("review_state", ["unreviewed", "needs_review"])
@pytest.mark.parametrize("denied_dependency", ["source", "asset"])
async def test_proxy_window_preserves_image_locator_only_with_readable_dependencies(
    published, denied_dependency, review_state
):
    import json
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image

    from tychora.contracts.evidence import ImageLocator
    from tychora.contracts.identity import chunk_id, digest, unit_id

    owner, scope, source_uid = published
    request = await content_request(owner)

    def install_proxy():
        image_stream = BytesIO()
        Image.new("RGB", (1, 1), (40, 80, 120)).save(image_stream, format="PNG")
        image = owner.artifacts.put_stream(
            BytesIO(image_stream.getvalue()), "image/png"
        )
        derivation = owner.artifacts.put_stream(
            BytesIO(b'{"task":"synthetic-caption-copy"}'), "application/json"
        )
        for ref in (image, derivation):
            owner.db.execute(
                "INSERT INTO artifacts(artifact_sha,size_bytes,media_type,storage_key,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.storage_key,
                    "2026-01-01T00:00:00Z",
                ),
            )
        locator = ImageLocator.from_mapping(
            {
                "type": "image",
                "artifact_ref": image.sha256,
                "native_ref": "synthetic-figure",
                "physical_page": 2,
                "printed_page": "iv",
                "geometry": {
                    "domain": "normalized",
                    "x0": 0.1,
                    "y0": 0.2,
                    "x1": 0.8,
                    "y1": 0.9,
                },
            }
        )
        text = owner.store._hydrate((source_uid,))[0].text
        asset_id = "synthetic-caption-asset"
        owner.db.execute(
            "INSERT INTO assets(asset_id,extraction_id,artifact_sha,asset_type,native_ref,"
            "locator_json,caption,declared_review_state) VALUES (?,?,?,?,?,?,?,'unreviewed')",
            (
                asset_id,
                request.extraction_id,
                image.sha256,
                "image",
                locator.native_ref,
                locator.model_dump_json(),
                text,
            ),
        )
        owner.db.execute(
            "INSERT INTO unit_assets(unit_id,asset_id,role) VALUES (?,?,'caption')",
            (source_uid, asset_id),
        )
        native_ref = "asset-proxy:" + digest(
            "asset-proxy-input-v1",
            asset_id,
            "caption_copy",
            text,
            [source_uid],
            derivation.sha256,
        )
        text_sha = sha256(text.encode()).hexdigest()
        proxy_uid = unit_id(request.extraction_id, native_ref, 100, text_sha)
        owner.db.execute(
            "INSERT INTO evidence_units(unit_id,extraction_id,parent_id,ordinal,unit_type,"
            "native_ref,boundary_key,heading_path_json,text,text_sha,text_origin,citable,"
            "declared_review_state,attributes_json) VALUES (?,?,NULL,100,'asset_proxy',?,?,'[]',"
            "?,?,'source',0,'unreviewed',?)",
            (
                proxy_uid,
                request.extraction_id,
                native_ref,
                native_ref,
                text,
                text_sha,
                json.dumps({"asset_id": asset_id, "proxy_kind": "caption_copy"}),
            ),
        )
        owner.db.execute(
            "INSERT INTO locators(locator_id,unit_id,fragment_ordinal,locator_type,artifact_sha,"
            "source_ref,details_json) VALUES (?,?,0,'image',?,?,?)",
            (
                "synthetic-proxy-locator",
                proxy_uid,
                image.sha256,
                locator.native_ref,
                locator.model_dump_json(),
            ),
        )
        owner.db.execute(
            "INSERT INTO asset_proxies(proxy_unit_id,asset_id,origin_type,derivation_sha) "
            "VALUES (?,?,'caption_copy',?)",
            (proxy_uid, asset_id, derivation.sha256),
        )
        owner.db.execute(
            "INSERT INTO asset_proxy_dependencies(proxy_unit_id,required_unit_id) VALUES (?,?)",
            (proxy_uid, source_uid),
        )
        window_id = chunk_id(proxy_uid, 0, len(text), "retained-proxy-fixture")
        owner.db.execute(
            "INSERT INTO chunks(chunk_id,unit_id,extraction_id,ordinal_in_unit,char_start,char_end,"
            "context_group_key,chunker_fingerprint,token_count) VALUES (?,?,?,0,0,?,'fixture','retained-proxy-fixture',3)",
            (window_id, proxy_uid, request.extraction_id, len(text)),
        )
        refresh_facts(owner.store, request.extraction_id)
        return window_id, locator, asset_id

    window_id, locator, asset_id = await owner.run_write(
        install_proxy, ExecutionContext()
    )
    if review_state == "needs_review":
        await owner.run_write(
            lambda: owner.store.record_review(
                ReviewTarget(
                    unit_id=owner.db.execute(
                        "SELECT unit_id FROM chunks WHERE chunk_id=?", (window_id,)
                    ).fetchone()[0]
                ),
                "needs_review",
                "synthetic-reviewer",
            ),
            ExecutionContext(),
        )
    # Force the supported fallback from configured compose to caller-authorized lookup.
    scope = scope.model_copy(update={"uses": frozenset({"lookup"})})
    service = InspectionService(owner)
    request = request.model_copy(update={"chunk_id": window_id})
    allowed = await service.extraction_windows(request, scope, ExecutionContext())
    assert len(allowed.windows) == 1
    assert allowed.windows[0].text == "Exact source evidence."
    assert allowed.windows[0].locators == (locator,)
    target = (
        ReviewTarget(unit_id=source_uid)
        if denied_dependency == "source"
        else ReviewTarget(asset_id=asset_id)
    )
    await owner.run_write(
        lambda: owner.store.record_review(target, "rejected", "reviewer"),
        ExecutionContext(),
    )
    denied = await service.extraction_windows(request, scope, ExecutionContext())
    assert denied.windows == ()
    assert denied.scope_limited
    assert window_id not in denied.model_dump_json()
    assert locator.artifact_ref not in denied.model_dump_json()
    assert locator.native_ref not in denied.model_dump_json()
