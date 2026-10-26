"""Bounded inventories of retained evidence and canonical stored windows.

Retained membership has no active slots: stored row identity and current range
ownership, rather than active chunk eligibility, authorize each window.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.eligibility import retained_rows
from tychora.contracts.base import CodepointRange
from tychora.contracts.corpus import ChunkRow
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    CodeLocator,
    DocumentLocator,
    ImageLocator,
    TextLocator,
)
from tychora.contracts.identity import chunk_id, digest
from tychora.contracts.operations import (
    ExtractionUnitsPage,
    ExtractionUnitSummary,
    ExtractionWindow,
    ExtractionWindowsPage,
    InspectRequest,
)
from tychora.indexing.representations import (
    prepare_extraction,
    project_code_excerpt_locator,
)
from tychora.retrieval.eligibility import eligible_cache_key

if TYPE_CHECKING:
    from tychora.application.inspection import InspectionService
    from tychora.contracts.operations import ExtractionContentRequest
    from tychora.contracts.requests import RequestScope
    from tychora.ports.execution import ExecutionContext

MAX_PAGE_BYTES = 1024 * 1024
MAX_CANDIDATES = 500


def _locators(rows, unit, start, end):
    projected = []
    for locator in unit.locators:
        if isinstance(locator, ImageLocator):
            # Range ownership has already authorized the asset and its sources.
            # Image provenance has no unit-local text interval to clip.
            projected.append(locator)
        elif isinstance(locator, CodeLocator):
            projected.append(
                project_code_excerpt_locator(rows, unit.unit_id, start, end)
            )
        elif isinstance(locator, TextLocator):
            if locator.char_start + end > locator.char_end:
                raise ValueError("Retained text locator is shorter than its unit")
            projected.append(
                locator.model_copy(
                    update={
                        "char_start": locator.char_start + start,
                        "char_end": locator.char_start + end,
                    }
                )
            )
        elif (
            isinstance(locator, DocumentLocator)
            and locator.char_start < end
            and start < locator.char_end
        ):
            projected.append(
                locator.model_copy(
                    update={
                        "char_start": max(start, locator.char_start),
                        "char_end": min(end, locator.char_end),
                    }
                )
            )
    return tuple(projected)


def content_page(
    service: InspectionService,
    request: ExtractionContentRequest,
    scope: RequestScope,
    ctx: ExecutionContext,
    *,
    windows: bool,
):
    owner = service.owner
    granted = owner.scope.narrow(scope)
    if "read" not in granted.capabilities:
        raise TychoraError("FORBIDDEN_SCOPE", "Item is outside the readable scope.")
    if not windows and request.chunk_id is not None:
        raise TychoraError("INVALID_REQUEST", "Chunk filters require windows.")
    pin_request = InspectRequest(subject="diagnostics", id=request.extraction_id)
    with service._pin_retained(pin_request) as pin:
        ctx.checkpoint()
        member = pin.members[0]
        if (member.source_id, member.revision_id) != (
            request.source_id,
            request.revision_id,
        ):
            raise TychoraError("NOT_FOUND", "Retained lineage is unavailable.")
        attempt = service._attempt(pin.view, granted, pin.members)
        if request.revision_id not in attempt.view.revision_ids:
            raise TychoraError("FORBIDDEN_SCOPE", "Item is outside the readable scope.")
        binding = digest(
            "retained-content-cursor-v1",
            windows,
            request.model_dump(mode="json", exclude={"cursor"}),
            eligible_cache_key(attempt.view),
        )
        offset = 0
        if request.cursor is not None:
            saved = service._content_cursors.get(request.cursor)
            if saved is None or saved[0] != binding:
                raise TychoraError("INVALID_REQUEST", "Invalid content cursor.")
            offset = saved[1]
        params: list = [request.extraction_id]
        where = "u.extraction_id=?"
        if request.unit_id is not None:
            where += " AND u.unit_id=?"
            params.append(request.unit_id)
        if windows and request.chunk_id is not None:
            where += " AND c.chunk_id=?"
            params.append(request.chunk_id)
        if windows:
            sql = (
                "SELECT c.* FROM chunks c JOIN evidence_units u USING(unit_id) WHERE "
                + where
                + " ORDER BY u.ordinal,u.unit_id,c.char_start,c.char_end,c.chunk_id"
            )
        else:
            sql = (
                "SELECT u.unit_id FROM evidence_units u WHERE "
                + where
                + " ORDER BY u.ordinal,u.unit_id"
            )
        cursor = owner.db.execute(
            sql + " LIMIT ? OFFSET ?", (*params, MAX_CANDIDATES + 1, offset)
        )
        columns = [column[0] for column in cursor.description]
        candidates = cursor.fetchall()
        retained = retained_rows(owner.store, member) if windows else None
        prepared = prepare_extraction(retained) if retained is not None else None
        page_type = ExtractionWindowsPage if windows else ExtractionUnitsPage
        field = "windows" if windows else "units"
        lineage = {
            "source_id": request.source_id,
            "revision_id": request.revision_id,
            "extraction_id": request.extraction_id,
        }
        items = []
        limited = False
        consumed = 0
        for raw in candidates[:MAX_CANDIDATES]:
            ctx.checkpoint()
            stored = dict(zip(columns, raw, strict=True))
            uid = stored["unit_id"]
            try:
                if windows:
                    unit = owner.store._hydrate((uid,))[0]
                    start, end = stored["char_start"], stored["char_end"]
                    if (
                        stored["extraction_id"] != request.extraction_id
                        or unit.extraction_id != request.extraction_id
                        or not 0 <= start < end <= len(unit.text)
                        or stored["chunk_id"]
                        != chunk_id(uid, start, end, stored["chunker_fingerprint"])
                    ):
                        raise ValueError("Stored window identity or bounds differ")
                    row = ChunkRow.model_validate(
                        {**stored, "text": unit.text[start:end]}
                    )
                    service._inspect_evidence(
                        InspectRequest(
                            subject="evidence",
                            id=uid,
                            range=CodepointRange(char_start=start, char_end=end),
                        ),
                        pin,
                        attempt,
                        prepared=prepared,
                    )
                    item = ExtractionWindow(
                        id=row.chunk_id,
                        unit_id=uid,
                        extraction_id=row.extraction_id,
                        char_start=start,
                        char_end=end,
                        text=row.text,
                        token_count=row.token_count,
                        chunker_fingerprint=row.chunker_fingerprint,
                        context_group_key=row.context_group_key,
                        locators=_locators(retained, unit, start, end),
                    )
                else:
                    if (
                        uid
                        not in attempt.view.context_unit_ids
                        | attempt.view.structural_unit_ids
                    ):
                        raise TychoraError(
                            "FORBIDDEN_SCOPE", "Unit is outside readable scope."
                        )
                    unit = owner.store._hydrate((uid,))[0]
                    item = ExtractionUnitSummary(
                        id=uid,
                        ordinal=unit.ordinal,
                        unit_type=unit.unit_type,
                        heading_path=unit.heading_path,
                        text_origin=unit.text_origin,
                        review_state=unit.review_state,
                        citable=unit.citable,
                        locators=unit.locators,
                        preview=unit.text[:240],
                    )
            except TychoraError as error:
                if error.code != "FORBIDDEN_SCOPE":
                    raise
                limited = True
                consumed += 1
                continue
            except (ValueError, TypeError, KeyError):
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained window is invalid."
                ) from None
            # Reserve enough room for the opaque continuation and envelope flags.
            proposed = page_type.model_validate(
                {
                    **lineage,
                    field: (*items, item),
                    "next_cursor": "x" * 43,
                    "scope_limited": limited,
                }
            )
            if len(proposed.model_dump_json().encode("utf-8")) > MAX_PAGE_BYTES:
                if not items:
                    raise TychoraError(
                        "BUDGET_TOO_SMALL", "Stored content exceeds the response limit."
                    )
                break
            items.append(item)
            consumed += 1
            if len(items) == request.limit:
                break
        next_cursor = None
        if consumed < len(candidates):
            next_cursor = secrets.token_urlsafe(32)
            if len(service._content_cursors) >= 1024:
                del service._content_cursors[next(iter(service._content_cursors))]
            service._content_cursors[next_cursor] = (binding, offset + consumed)
        result = page_type.model_validate(
            {
                **lineage,
                field: tuple(items),
                "next_cursor": next_cursor,
                "scope_limited": limited,
            }
        )
        service.compiler.revalidate_epoch(attempt.view, "delivery")
        ctx.checkpoint()
        return result
