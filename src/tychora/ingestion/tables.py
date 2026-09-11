"""Conservative table continuation over retained original fragments."""

from collections.abc import Mapping
from typing import Any, cast

from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.identity import digest
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
)
from tychora.contracts.tables import (
    ContinuationDecision,
    TableFragment,
    TableRowReference,
)


def continuation_score(
    *,
    headers: bool,
    caption: bool,
    heading: bool,
    overlap: bool,
    page_edges: bool,
    marker: bool,
) -> float:
    return round(
        0.35 * headers
        + 0.20 * caption
        + 0.15 * heading
        + 0.10 * overlap
        + 0.10 * page_edges
        + 0.10 * marker,
        2,
    )


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _headers(fragment: TableFragment) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(_normalized(cell) for cell in fragment.grid[row])
        for row in fragment.header_rows
    )


def _box(fragment: TableFragment) -> tuple[float, float, float, float] | None:
    box = fragment.bbox
    if box is None:
        return None
    if box.domain == "normalized":
        return box.x0, box.y0, box.x1, box.y1
    if fragment.page_width is None or fragment.page_height is None:
        return None
    return (
        box.x0 / fragment.page_width,
        box.y0 / fragment.page_height,
        box.x1 / fragment.page_width,
        box.y1 / fragment.page_height,
    )


def continuation(a: TableFragment, b: TableFragment) -> ContinuationDecision:
    rejected = []
    if (a.revision_id, a.extraction_id) != (b.revision_id, b.extraction_id):
        rejected.append("different_extraction")
    if a.page is None or b.page is None:
        rejected.append("unknown_physical_page")
    elif b.page != a.page + 1:
        rejected.append("nonadjacent_pages")
    if a.column_count != b.column_count:
        rejected.append("incompatible_columns")
    if (
        a.table_number
        and b.table_number
        and _normalized(a.table_number) != _normalized(b.table_number)
    ):
        rejected.append("different_table_number")
    if (
        b.intervening_heading_level is not None
        and b.intervening_heading_level <= a.heading_level
    ):
        rejected.append("intervening_heading")
    if rejected:
        return ContinuationDecision(
            score=0.0, status="separate", reasons=tuple(rejected)
        )
    headers = _headers(a)
    matching_headers = bool(
        headers and any(any(row) for row in headers)
    ) and headers == _headers(b)
    caption = bool(
        a.caption and b.caption and _normalized(a.caption) == _normalized(b.caption)
    ) or bool(
        a.table_number
        and b.table_number
        and _normalized(a.table_number) == _normalized(b.table_number)
    )
    left, right = _box(a), _box(b)
    overlap = page_edges = False
    if left is not None and right is not None:
        width = max(left[2] - left[0], right[2] - right[0])
        overlap = (
            width > 0
            and max(0, min(left[2], right[2]) - max(left[0], right[0])) / width >= 0.9
        )
        page_edges = left[3] >= 0.8 and right[1] <= 0.2
    features = {
        "headers": matching_headers,
        "caption": caption,
        "heading": bool(a.heading_path) and a.heading_path == b.heading_path,
        "overlap": overlap,
        "page_edges": page_edges,
        "marker": b.continuation_marker,
    }
    score = continuation_score(**features)
    status = "derived" if score >= 0.85 else "proposed" if score >= 0.60 else "separate"
    reasons = tuple(name for name, present in features.items() if present)
    return ContinuationDecision(
        score=score,
        status=status,
        reasons=reasons or ("missing_continuation_evidence",),
        omit_repeated_header=matching_headers and status != "separate",
    )


def reconstruct_rows(
    a: TableFragment, b: TableFragment, decision: ContinuationDecision
) -> tuple[TableRowReference, ...]:
    """A derived view only; policy permission is required before context delivery."""
    if decision != continuation(a, b) or decision.status == "separate":
        raise ValueError("Reconstruction requires this pair's continuation decision")
    return tuple(
        TableRowReference(fragment_id=fragment.fragment_id, row_index=row, cells=cells)
        for fragment in (a, b)
        for row, cells in enumerate(fragment.grid)
        if not (
            fragment is b and decision.omit_repeated_header and row in b.header_rows
        )
    )


def continuation_assertion(
    a: TableFragment,
    b: TableFragment,
    decision: ContinuationDecision,
    derivation: ArtifactRef,
) -> RelationshipAssertion | None:
    if decision != continuation(a, b):
        raise ValueError("Continuation decision differs from original fragments")
    if decision.status == "separate":
        return None
    if not a.locators:
        raise ValueError("Continuation requires an exact original origin locator")
    observation = AssertionObservation(
        observation_key=f"table-continuation:{a.native_ref}:{b.native_ref}",
        revision_id=a.revision_id,
        extraction_id=a.extraction_id,
        predicate="continues_table@1",
        subject=RelationshipEndpoint(
            entity_id=a.fragment_id, unit_id=a.unit_id, kind="table"
        ),
        object=RelationshipEndpoint(
            entity_id=b.fragment_id, unit_id=b.unit_id, kind="table"
        ),
        origin_unit_id=a.unit_id,
        origin_locator=a.locators[0],
        required_support_ids=a.required_unit_ids | b.required_unit_ids,
        method="parser_inferred",
        status="recorded" if decision.status == "derived" else "proposed",
        confidence=decision.score,
        derivation_fingerprint=digest("table-continuation-v1", derivation.sha256),
        required_artifacts=(derivation,),
    )
    return RelationshipAssertion.from_observation(observation)


def validate_assertion(
    assertion: RelationshipAssertion,
    units: Mapping[str, EvidenceUnit],
    registry: Registry,
    *,
    proxy_ids: frozenset[str],
    kind_id: str,
    revision_id: str,
) -> None:
    assertion = RelationshipAssertion.model_validate(assertion.model_dump())
    relation = cast(
        Mapping[str, Any] | None, registry.relations.get(assertion.predicate)
    )
    if (
        relation is None
        or assertion.method not in relation["methods"]
        or assertion.revision_id != revision_id
    ):
        raise ValueError("Assertion registry, method or revision binding differs")
    for uid in assertion.required_support_ids:
        unit = units.get(uid)
        if (
            unit is None
            or uid in proxy_ids
            or unit.text_origin != "source"
            or unit.extraction_id != assertion.extraction_id
        ):
            raise ValueError(
                "Assertion requires complete original same-extraction supports"
            )
    source_ids = {
        assertion.origin_unit_id,
        assertion.subject.unit_id,
        assertion.object.unit_id,
    }
    source_ids.update(
        span.unit_id for span in (*assertion.support_spans, *assertion.target_spans)
    )
    for uid in source_ids:
        owners = cast(
            tuple[str, ...], units[uid].attributes.get("overlapping_owner_ids", (uid,))
        )
        if not set(owners) <= assertion.required_support_ids:
            raise ValueError("Assertion omits overlapping original source owners")
    for name, endpoint in (
        ("subject", assertion.subject),
        ("object", assertion.object),
    ):
        unit = units[endpoint.unit_id]
        if (
            endpoint.kind != unit.unit_type
            or relation[name + "_unit_types"]
            and unit.unit_type not in relation[name + "_unit_types"]
            or relation[name + "_kinds"]
            and kind_id not in relation[name + "_kinds"]
        ):
            raise ValueError("Assertion endpoint does not satisfy its registered type")
    if (
        not relation["self_links"]
        and assertion.subject.entity_id == assertion.object.entity_id
    ):
        raise ValueError("Assertion predicate does not permit self links")
    if assertion.origin_locator not in units[assertion.origin_unit_id].locators:
        raise ValueError("Assertion origin locator differs from canonical source")
    for span in (*assertion.support_spans, *assertion.target_spans):
        if not 0 <= span.char_start < span.char_end <= len(units[span.unit_id].text):
            raise ValueError("Assertion span exceeds exact canonical source text")


def continuation_context_allowed(
    *,
    policy: str,
    status: str,
    predicate: str,
    permitted_predicates: frozenset[str],
    permitted_statuses: frozenset[str],
    required_support_ids: frozenset[str],
    eligible_original_ids: frozenset[str],
    blocked: bool,
) -> bool:
    """Narrows the caller's pinned relation/source/current-decision eligibility."""
    if (
        blocked
        or policy not in ("derived", "confirmed_only")
        or predicate != "continues_table@1"
        or predicate not in permitted_predicates
        or status not in permitted_statuses
        or status not in ("recorded", "confirmed")
        or not required_support_ids
        or not required_support_ids <= eligible_original_ids
    ):
        return False
    return policy == "derived" or status == "confirmed"
