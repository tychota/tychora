"""Bounded final evidence excerpts from pinned canonical source ranges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import (
    AnchorRange,
    Citation,
    CodeLocator,
    DocumentLocator,
    Excerpt,
    Omission,
    Segment,
    TextLocator,
)
from tychora.contracts.graph import GraphPath, LegacyEligibilityView
from tychora.contracts.identity import digest
from tychora.indexing.representations import project_code_excerpt_locator
from tychora.ingestion.tables import continuation_context_allowed
from tychora.ports.tokens import TokenCounter
from tychora.retrieval.anchors import permitted_ranges, source_anchors
from tychora.retrieval.rerank import RankedEvidence, RankedEvidenceItem


class OutputTokenCounter:
    """Verified local cl100k counter for final evidence, separate from reranking."""

    model = "cl100k_base"
    tokenizer_id = "cl100k_base"

    def __init__(self, cache_root: Path) -> None:
        from tychora.application.setup import load_output_tokenizer

        self._encoding = load_output_tokenizer(cache_root)

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


@dataclass(frozen=True)
class Selection:
    segments: tuple[Segment, ...]
    reservation_priority: tuple[str, ...]
    omitted_anchors: tuple[AnchorRange, ...]
    lane_coverage: tuple[str, ...]
    text_budget: int
    visual_reserved_tokens: int


def _lane(config: ResolvedConfig, kind: str) -> str:
    registry = config.registry
    lanes = cast(Mapping[str, Mapping[str, object]], registry["lanes"])
    for name, lane in lanes.items():
        if kind in cast(tuple[str, ...], lane["kinds"]):
            return str(name)
    compatibility = cast(Mapping[str, object], registry["compatibility"])
    kind_lanes = cast(Mapping[str, object], compatibility.get("kind_lanes", {}))
    if kind in kind_lanes:
        return str(kind_lanes[kind])
    raise TychoraError(
        "INTEGRITY_ERROR", "Selected kind has no lane.", reason="config.drift"
    )


def _role(
    config: ResolvedConfig, kind: str, historical_kind_roles: Mapping[str, str]
) -> str:
    kinds = cast(Mapping[str, Mapping[str, object]], config.registry["kinds"])
    if kind in kinds:
        return str(kinds[kind]["role"])
    if kind in historical_kind_roles and _lane(config, kind):
        role = historical_kind_roles[kind]
        if isinstance(role, str) and role:
            return role
    raise TychoraError(
        "PROFILE_INCOMPATIBLE", "Selected historical kind role is unavailable."
    )


def _citation(item: RankedEvidenceItem, unit_id: str, start: int, end: int) -> Citation:
    assert item.facts is not None
    rows = item.facts.rows
    unit = item.facts.prepared.representation(unit_id).unit
    locators = []
    for locator in unit.locators:
        if isinstance(locator, CodeLocator):
            try:
                locators.append(project_code_excerpt_locator(rows, unit_id, start, end))
            except ValueError:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Retained code excerpt map is invalid."
                ) from None
        elif isinstance(locator, TextLocator):
            # Native text locators use original artifact codepoint coordinates.
            source_start = locator.char_start + start
            source_end = locator.char_start + end
            if source_end > locator.char_end:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Source locator is shorter than retained text."
                )
            locators.append(
                locator.model_copy(
                    update={"char_start": source_start, "char_end": source_end}
                )
            )
        elif (
            isinstance(locator, DocumentLocator)
            and locator.char_start < end
            and locator.char_end > start
        ):
            locators.append(
                locator.model_copy(
                    update={
                        "char_start": max(start, locator.char_start),
                        "char_end": min(end, locator.char_end),
                    }
                )
            )
    code_locator = next(
        (locator for locator in locators if isinstance(locator, CodeLocator)), None
    )
    if code_locator is not None:
        if not any(
            row.role == "source_file"
            and row.artifact_sha == code_locator.artifact_ref
            and row.source_ref == code_locator.relative_path
            for row in rows.revision_artifacts
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Retained code file citation binding differs."
            )
        source_ref = code_locator.relative_path
    else:
        refs = {row.artifact_sha: row.source_ref for row in rows.revision_artifacts}
        source_ref = next(
            (
                refs[locator.artifact_ref]
                for locator in locators
                if locator.artifact_ref in refs
            ),
            rows.source.source_key,
        )
    return Citation(
        unit_id=unit_id,
        source_id=rows.source.source_id,
        revision_id=rows.revision.revision_id,
        source_ref=source_ref,
        physical_page=next(
            (
                locator.physical_page
                for locator in locators
                if isinstance(locator, DocumentLocator) and locator.physical_page
            ),
            None,
        ),
        printed_page=next(
            (
                locator.printed_page
                for locator in locators
                if isinstance(locator, DocumentLocator) and locator.printed_page
            ),
            None,
        ),
        locators=tuple(locators),
    )


def _fragment_citations(citation: Citation) -> tuple[Citation, ...]:
    """Keep every physical document fragment independently citable."""
    if not any(isinstance(locator, DocumentLocator) for locator in citation.locators):
        return (citation,)
    return tuple(
        citation.model_copy(
            update={
                "locators": (locator,),
                "physical_page": locator.physical_page,
                "printed_page": locator.printed_page,
            }
        )
        for locator in citation.locators
        if isinstance(locator, DocumentLocator)
    )


def _file_key(segment: Segment) -> tuple[str, str]:
    for citation in segment.citations:
        for locator in citation.locators:
            if isinstance(locator, CodeLocator):
                return (segment.revision_id, locator.relative_path)
    return (segment.revision_id, segment.source_id)


def _code_overlap(left: Citation, right: Citation) -> bool:
    return any(
        a.artifact_ref == b.artifact_ref
        and a.relative_path == b.relative_path
        and a.char_start < b.char_end
        and b.char_start < a.char_end
        for a in left.locators
        if isinstance(a, CodeLocator)
        for b in right.locators
        if isinstance(b, CodeLocator)
    )


def _code_context(
    item: RankedEvidenceItem,
    matched_unit_id: str,
    matched_citation: Citation,
    view: EligibilityView,
    counter: TokenCounter,
    remaining: int,
) -> tuple[tuple[Excerpt, ...], tuple[Citation, ...]]:
    assert item.facts is not None
    excerpts: list[Excerpt] = []
    citations: list[Citation] = []
    context_tokens = 0
    for record in item.facts.rows.records:
        if record.unit_id != matched_unit_id or record.record_type != "code_symbol":
            continue
        spans = record.data_json.get("context_spans", ())
        if not isinstance(spans, tuple):
            raise TychoraError("INTEGRITY_ERROR", "Code context is not canonical.")
        for span in spans:
            if not isinstance(span, FrozenJsonMap) or span.get("reason") not in {
                "parent_declaration",
                "referenced_import",
            }:
                continue
            source_id = span.get("source_unit_id")
            start, end = span.get("char_start"), span.get("char_end")
            if (
                not isinstance(source_id, str)
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
            ):
                raise TychoraError("INTEGRITY_ERROR", "Code context is not canonical.")
            try:
                unit = item.facts.prepared.representation(source_id).unit
            except ValueError:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Code context has no retained unit."
                ) from None
            if not _readable(item, source_id, view):
                continue
            for left, right in permitted_ranges(
                item.facts.prepared, source_id, start, end, view
            ):
                citation = _citation(item, source_id, left, right)
                if _code_overlap(matched_citation, citation) or any(
                    _code_overlap(prior, citation) for prior in citations
                ):
                    continue
                exact = unit.text[left:right]
                allowance = min(160 - context_tokens, remaining)
                fitted = _fit(exact, counter, allowance) if allowance > 0 else 0
                if fitted == 0:
                    return tuple(excerpts), tuple(citations)
                excerpt = Excerpt(
                    unit_id=source_id,
                    char_start=left,
                    char_end=left + fitted,
                    text=exact[:fitted],
                )
                excerpts.append(excerpt)
                citations.append(_citation(item, source_id, left, left + fitted))
                consumed = counter.count(excerpt.text)
                context_tokens += consumed
                remaining -= consumed
    return tuple(excerpts), tuple(citations)


def _readable(item: RankedEvidenceItem, unit_id: str, view: EligibilityView) -> bool:
    """Whether a unit of the item's slice is readable: its persisted set is allowed."""
    assert item.facts is not None
    dependencies = item.facts.dependencies.get(unit_id)
    return dependencies is not None and view.allows(dependencies)


@dataclass(frozen=True)
class _Continuations:
    """Pinned continuation paths and the unit-level view that admits them."""

    paths: tuple[GraphPath, ...]
    graph_view: LegacyEligibilityView | None

    @property
    def admitting(self) -> LegacyEligibilityView | None:
        """The unit-level view when it binds a graph generation, else ``None``."""
        view = self.graph_view
        return None if view is None or view.graph_generation_id is None else view


def _fit(text: str, counter: TokenCounter, ceiling: int) -> int:
    if counter.count(text) <= ceiling:
        return len(text)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count(text[:middle]) <= ceiling:
            low = middle
        else:
            high = middle - 1
    return low


def _continuation_context(
    item: RankedEvidenceItem,
    unit_id: str,
    matched_citation: Citation,
    view: EligibilityView,
    config: ResolvedConfig,
    counter: TokenCounter,
    remaining: int,
    continuations: _Continuations,
) -> tuple[tuple[Excerpt, ...], tuple[Citation, ...]]:
    """Attach a separate physical fragment only from a pinned eligible path."""
    assert item.facts is not None
    settings = config.values.retrieval
    graph_view = continuations.admitting
    if not continuations.paths or not settings.graph.semantic or graph_view is None:
        return (), ()
    eligible = graph_view.context_unit_ids | graph_view.structural_unit_ids
    excerpts: list[Excerpt] = []
    citations: list[Citation] = []
    used_targets: set[str] = set()
    for path in continuations.paths:
        if path.source_unit_id == unit_id:
            target_id = path.target_unit_id
        elif path.target_unit_id == unit_id:
            target_id = path.source_unit_id
        else:
            continue
        if target_id == unit_id or target_id in used_targets:
            continue
        if not continuation_context_allowed(
            policy=settings.table_continuations,
            status=path.status,
            predicate=path.predicate,
            permitted_predicates=frozenset(settings.graph.predicates),
            permitted_statuses=frozenset(settings.graph.statuses),
            required_support_ids=path.required_support_ids,
            eligible_original_ids=eligible,
            blocked=any(
                assertion_id in graph_view.blocked_assertion_ids
                for assertion_id in path.assertion_ids
            )
            or path.assertion_occurrences != 1
            or not {unit_id, target_id} <= path.required_support_ids,
        ):
            continue
        try:
            source = item.facts.prepared.representation(unit_id).unit
            target = item.facts.prepared.representation(target_id).unit
        except ValueError:
            continue
        if (
            source.unit_type not in {"table", "table_row"}
            or target.unit_type not in {"table", "table_row"}
            or source.extraction_id != target.extraction_id
            or not _separate_fragments(source, target)
            or not target.text
            or not target.citable
            or target.text_origin != "source"
            or target_id not in eligible
            or not any(
                isinstance(locator, DocumentLocator) for locator in target.locators
            )
        ):
            continue
        if permitted_ranges(
            item.facts.prepared, target_id, 0, len(target.text), view
        ) != ((0, len(target.text)),):
            continue
        citation = _citation(item, target_id, 0, len(target.text))
        if (
            _code_overlap(matched_citation, citation)
            or counter.count(target.text) > remaining
        ):
            continue
        excerpts.append(
            Excerpt(
                unit_id=target_id,
                char_start=0,
                char_end=len(target.text),
                text=target.text,
            )
        )
        citations.extend(_fragment_citations(citation))
        remaining -= counter.count(target.text)
        used_targets.add(target_id)
    return tuple(excerpts), tuple(citations)


_TABLE_UNIT_TYPES = frozenset({"table", "table_row"})


def _is_document_unit(unit) -> bool:
    return any(isinstance(locator, DocumentLocator) for locator in unit.locators)


def _separate_fragments(first, second) -> bool:
    """Whether two units are distinct table fragments of one boundary.

    A table and its row views share one boundary key, so the key alone cannot tell
    a continuation on another fragment from a view of the fragment already matched.
    """
    refs = first.attributes.get("source_refs")
    return first.boundary_key == second.boundary_key and (
        refs is None or refs != second.attributes.get("source_refs")
    )


def _same_section(first, second) -> bool:
    """Whether two units share the section that bounds neighbour context and joins.

    The boundary key is the section identity: two articles under one heading, or
    two sibling sections with identical headings, are different sections. Document
    tables are excluded: a table continued on another page joins only through a
    verified graph path.
    """
    if first.boundary_key != second.boundary_key:
        return False
    if _is_document_unit(first) or _is_document_unit(second):
        return (
            _is_document_unit(first)
            and _is_document_unit(second)
            and first.unit_type not in _TABLE_UNIT_TYPES
            and second.unit_type not in _TABLE_UNIT_TYPES
        )
    return True


def _neighbor_context(
    item: RankedEvidenceItem,
    unit_id: str,
    view: EligibilityView,
    counter: TokenCounter,
    remaining: int,
) -> tuple[tuple[Excerpt, ...], tuple[Citation, ...]]:
    """Add at most one eligible same-boundary unit on either side."""
    assert item.facts is not None
    unit = item.facts.prepared.representation(unit_id).unit
    if any(isinstance(locator, CodeLocator) for locator in unit.locators):
        return (), ()
    excerpts: list[Excerpt] = []
    citations: list[Citation] = []
    for ordinal in (unit.ordinal - 1, unit.ordinal + 1):
        neighbor = next(
            (
                rep.unit
                for rep in item.facts.prepared.units
                if rep.unit.ordinal == ordinal and _same_section(rep.unit, unit)
            ),
            None,
        )
        if (
            neighbor is None
            or not _readable(item, neighbor.unit_id, view)
            or not neighbor.citable
            or neighbor.text_origin != "source"
            or not neighbor.text
            or counter.count(neighbor.text) > remaining
        ):
            continue
        if permitted_ranges(
            item.facts.prepared, neighbor.unit_id, 0, len(neighbor.text), view
        ) != ((0, len(neighbor.text)),):
            continue
        excerpts.append(
            Excerpt(
                unit_id=neighbor.unit_id,
                char_start=0,
                char_end=len(neighbor.text),
                text=neighbor.text,
            )
        )
        citations.append(_citation(item, neighbor.unit_id, 0, len(neighbor.text)))
        remaining -= counter.count(neighbor.text)
    return tuple(excerpts), tuple(citations)


def _physical_ranges(
    item: RankedEvidenceItem, unit_id: str, start: int, end: int
) -> tuple[tuple[int, int], ...]:
    """Do not infer a table continuation from page or boundary adjacency."""
    assert item.facts is not None
    unit = item.facts.prepared.representation(unit_id).unit
    fragments = [
        locator for locator in unit.locators if isinstance(locator, DocumentLocator)
    ]
    if not fragments:
        return ((start, end),)
    ranges = tuple(
        (max(start, locator.char_start), min(end, locator.char_end))
        for locator in fragments
        if locator.char_start < end and locator.char_end > start
    )
    if not ranges and (start >= end or unit.text[start:end].isspace()):
        # Textless units (pictures) and separators between fragments (table
        # cell tabs) have no physical extent; there is nothing to excerpt.
        return ()
    if not ranges:
        raise TychoraError(
            "INTEGRITY_ERROR", "Document excerpt has no physical fragment."
        )
    return ranges


def _expand_to_line(
    item: RankedEvidenceItem,
    unit_id: str,
    start: int,
    end: int,
    view: EligibilityView,
    counter: TokenCounter,
    ceiling: int,
) -> tuple[int, int]:
    assert item.facts is not None
    unit = item.facts.prepared.representation(unit_id).unit
    if any(
        isinstance(locator, (CodeLocator, DocumentLocator)) for locator in unit.locators
    ):
        return start, end
    left = unit.text.rfind("\n", 0, start) + 1
    newline = unit.text.find("\n", end)
    right = len(unit.text) if newline < 0 else newline + 1
    if (
        start - left > 256
        or right - end > 256
        or counter.count(unit.text[left:right]) > min(1200, ceiling)
        or permitted_ranges(item.facts.prepared, unit_id, left, right, view)
        != ((left, right),)
    ):
        return start, end
    return left, right


def _segment(
    item: RankedEvidenceItem,
    anchor: AnchorRange,
    start: int,
    end: int,
    config: ResolvedConfig,
    counter: TokenCounter,
    view: EligibilityView,
    continuations: _Continuations,
    historical_kind_roles: Mapping[str, str],
    segment_ceiling: int,
) -> Segment | None:
    assert item.facts is not None
    rows = item.facts.rows
    unit = item.facts.prepared.representation(anchor.unit_id).unit
    if not unit.citable or unit.text_origin != "source":
        return None
    exact = unit.text[start:end]
    ceiling = min(1200, segment_ceiling)
    fitted = _fit(exact, counter, ceiling)
    if fitted == 0:
        return None
    excerpt = Excerpt(
        unit_id=unit.unit_id,
        char_start=start,
        char_end=start + fitted,
        text=exact[:fitted],
    )
    complete = fitted == len(exact) and start == 0 and end == len(unit.text)
    truncated = fitted < len(exact)
    citation = _citation(item, unit.unit_id, start, start + fitted)
    context_excerpts, context_citations = (
        _code_context(
            item,
            unit.unit_id,
            citation,
            view,
            counter,
            ceiling - counter.count(excerpt.text),
        )
        if any(
            record.unit_id == unit.unit_id and record.record_type == "code_symbol"
            for record in rows.records
        )
        else ((), ())
    )
    continuation_excerpts, continuation_citations = _continuation_context(
        item,
        unit.unit_id,
        citation,
        view,
        config,
        counter,
        ceiling
        - counter.count(excerpt.text)
        - sum(counter.count(part.text) for part in context_excerpts),
        continuations,
    )
    neighbor_excerpts, neighbor_citations = _neighbor_context(
        item,
        unit.unit_id,
        view,
        counter,
        ceiling
        - counter.count(excerpt.text)
        - sum(
            counter.count(part.text)
            for part in (*context_excerpts, *continuation_excerpts)
        ),
    )
    all_context_excerpts = (
        *context_excerpts,
        *continuation_excerpts,
        *neighbor_excerpts,
    )
    return Segment(
        segment_id=digest(
            "segment-v1", rows.revision.revision_id, unit.unit_id, start, start + fitted
        ),
        source_id=rows.source.source_id,
        revision_id=rows.revision.revision_id,
        extraction_id=rows.extraction.extraction_id,
        kind=rows.revision.kind_id,
        role=_role(config, rows.revision.kind_id, historical_kind_roles),
        lane=_lane(config, rows.revision.kind_id),
        excerpts=(excerpt,),
        text=excerpt.text,
        matched_unit_ids=(unit.unit_id,),
        context_only_unit_ids=tuple(
            dict.fromkeys(ex.unit_id for ex in all_context_excerpts)
        ),
        context_excerpts=all_context_excerpts,
        anchors=(
            anchor.model_copy(
                update={
                    "char_start": max(anchor.char_start, start),
                    "char_end": min(anchor.char_end, start + fitted),
                }
            ),
        ),
        signals=(
            *item.candidate.signals,
            *(("table_continuation",) if continuation_excerpts else ()),
        ),
        citations=(
            *_fragment_citations(citation),
            *context_citations,
            *continuation_citations,
            *neighbor_citations,
        ),
        complete=complete,
        truncated=truncated,
        omissions=(
            Omission(reason="full_fetch_available", unit_ids=(unit.unit_id,), count=1),
        )
        if not complete
        else (),
    )


def _coalesce_boundary_matches(
    options: list[tuple[int, int, Segment]],
    ranked: RankedEvidence,
    view: EligibilityView,
    counter: TokenCounter,
    segment_ceiling: int,
) -> list[tuple[int, int, Segment]]:
    """Join two matched text units across at most one fully eligible gap."""
    consumed: set[int] = set()
    output: list[tuple[int, int, Segment]] = []
    for index, row in enumerate(options):
        if index in consumed:
            continue
        order, anchor_order, primary = row
        unit_id = primary.excerpts[0].unit_id
        owner = next(
            (
                item
                for item in ranked.items
                if item.facts is not None
                and item.facts.prepared.extraction_id == primary.extraction_id
                and any(
                    rep.unit.unit_id == unit_id for rep in item.facts.prepared.units
                )
            ),
            None,
        )
        if (
            owner is None
            or owner.facts is None
            or any(
                isinstance(locator, CodeLocator)
                for citation in primary.citations
                for locator in citation.locators
            )
        ):
            output.append(row)
            continue
        first_unit = owner.facts.prepared.representation(unit_id).unit
        merged = None
        for later in range(index + 1, len(options)):
            if later in consumed:
                continue
            _, _, candidate = options[later]
            if (
                candidate.extraction_id != primary.extraction_id
                or candidate.source_id != primary.source_id
                or candidate.revision_id != primary.revision_id
                or candidate.lane != primary.lane
                or any(
                    isinstance(locator, CodeLocator)
                    for citation in candidate.citations
                    for locator in citation.locators
                )
            ):
                continue
            try:
                second_unit = owner.facts.prepared.representation(
                    candidate.excerpts[0].unit_id
                ).unit
            except ValueError:
                continue
            distance = abs(first_unit.ordinal - second_unit.ordinal)
            if distance not in (1, 2) or not _same_section(first_unit, second_unit):
                continue
            if distance == 2:
                gap = next(
                    (
                        rep.unit
                        for rep in owner.facts.prepared.units
                        if rep.unit.ordinal
                        == min(first_unit.ordinal, second_unit.ordinal) + 1
                        and _same_section(rep.unit, first_unit)
                    ),
                    None,
                )
                if (
                    gap is None
                    or not _readable(owner, gap.unit_id, view)
                    or not gap.citable
                    or gap.text_origin != "source"
                    or not gap.text
                    or permitted_ranges(
                        owner.facts.prepared, gap.unit_id, 0, len(gap.text), view
                    )
                    != ((0, len(gap.text)),)
                ):
                    continue
            matched = tuple(
                dict.fromkeys((*primary.matched_unit_ids, *candidate.matched_unit_ids))
            )
            contexts = tuple(
                dict.fromkeys(
                    (
                        excerpt.unit_id,
                        excerpt.char_start,
                        excerpt.char_end,
                        excerpt.text,
                    )
                    for excerpt in (
                        *primary.context_excerpts,
                        *candidate.context_excerpts,
                    )
                    if excerpt.unit_id not in matched
                )
            )
            context_excerpts = tuple(
                Excerpt(unit_id=uid, char_start=start, char_end=end, text=text)
                for uid, start, end, text in contexts
            )
            prepared = owner.facts.prepared
            ordered = sorted(
                (primary, candidate),
                key=lambda segment: (
                    prepared.representation(segment.excerpts[0].unit_id).unit.ordinal
                ),
            )
            joined_text = "\n[separate source unit]\n".join(
                segment.text for segment in ordered
            )
            cost = counter.count(joined_text) + sum(
                counter.count(excerpt.text) for excerpt in context_excerpts
            )
            if cost > segment_ceiling:
                continue
            citations = []
            for segment in ordered:
                for citation in segment.citations:
                    if citation.unit_id in matched and citation not in citations:
                        citations.append(citation)
            for segment in ordered:
                for citation in segment.citations:
                    if citation.unit_id not in matched and citation not in citations:
                        citations.append(citation)
            merged = primary.model_copy(
                update={
                    "segment_id": digest(
                        "joined-segment-v1", primary.segment_id, candidate.segment_id
                    ),
                    "excerpts": tuple(
                        excerpt for segment in ordered for excerpt in segment.excerpts
                    ),
                    "text": joined_text,
                    "matched_unit_ids": matched,
                    "context_only_unit_ids": tuple(
                        dict.fromkeys(excerpt.unit_id for excerpt in context_excerpts)
                    ),
                    "context_excerpts": context_excerpts,
                    "anchors": (*primary.anchors, *candidate.anchors),
                    "signals": tuple(
                        dict.fromkeys((*primary.signals, *candidate.signals))
                    ),
                    "citations": tuple(citations),
                    "complete": False,
                    "omissions": (*primary.omissions, *candidate.omissions),
                }
            )
            consumed.add(later)
            break
        output.append((order, anchor_order, merged or primary))
    return output


def assemble_segments(
    ranked: RankedEvidence,
    view: EligibilityView,
    config: ResolvedConfig,
    counter: TokenCounter,
    *,
    graph_view: LegacyEligibilityView | None = None,
    visual_limit: int = 3,
    continuation_paths: tuple[GraphPath, ...] = (),
    historical_kind_roles: Mapping[str, str] | None = None,
    total_tokens: int | None = None,
    result_limit: int | None = None,
) -> Selection:
    """Apply final lane and source diversity limits to exact original ranges."""
    if counter.tokenizer_id != "cl100k_base" or not 0 <= visual_limit <= 3:
        raise TychoraError(
            "INVALID_REQUEST", "Output selection counter or visual limit differs."
        )
    if total_tokens is not None and total_tokens <= 0:
        raise TychoraError("INVALID_REQUEST", "Output token budget must be positive.")
    if result_limit is not None and result_limit <= 0:
        raise TychoraError("INVALID_REQUEST", "Result limit must be positive.")
    total = min(
        config.values.limits.max_tokens, total_tokens or config.values.limits.max_tokens
    )
    visual_reserve = min(200 * visual_limit, total // 10)
    text_budget = total - visual_reserve
    cap = min(8, config.values.limits.limit, result_limit or config.values.limits.limit)
    options: list[tuple[int, int, Segment]] = []
    omitted: list[AnchorRange] = []
    for order, item in enumerate(ranked.items):
        if item.candidate.key[0] != "unit":
            continue
        if item.facts is None:
            raise TychoraError(
                "INTEGRITY_ERROR", "Selected text has no retained facts."
            )
        if item.facts.prepared.revision_id not in view.revisions:
            continue
        for index, anchor in enumerate(source_anchors(item)):
            window = anchor.indexed_window_id
            if window is not None and window not in item.facts.dependencies:
                continue
            for start, end in permitted_ranges(
                item.facts.prepared,
                anchor.unit_id,
                anchor.char_start,
                anchor.char_end,
                view,
            ):
                for fragment_start, fragment_end in _physical_ranges(
                    item, anchor.unit_id, start, end
                ):
                    excerpt_start, excerpt_end = _expand_to_line(
                        item,
                        anchor.unit_id,
                        fragment_start,
                        fragment_end,
                        view,
                        counter,
                        text_budget,
                    )
                    segment = _segment(
                        item,
                        anchor,
                        excerpt_start,
                        excerpt_end,
                        config,
                        counter,
                        view,
                        _Continuations(continuation_paths, graph_view),
                        historical_kind_roles or {},
                        text_budget,
                    )
                    if segment is not None:
                        options.append((order, index, segment))
    options.sort(key=lambda row: (row[0], row[1], row[2].excerpts[0].char_start))
    options = _coalesce_boundary_matches(
        options, ranked, view, counter, min(1200, text_budget)
    )
    policy = config.values.selection.lane_reservation
    required = set()
    if policy == "one_per_required_lane":
        mode = config.values.mode or config.values.default_mode
        if mode is not None:
            required = set(
                config.model_dump(mode="json")["registry"]["modes"][mode].get(
                    "required_lanes", ()
                )
            )
    if policy == "one_per_available_lane":
        required = {segment.lane for _, _, segment in options}
    priority: list[tuple[int, int, Segment]] = []
    if required:
        for lane in config.values.output.lane_order or tuple(sorted(required)):
            first = next(
                (row for row in options if row[2].lane == lane and lane in required),
                None,
            )
            if first is not None and first not in priority:
                priority.append(first)
    priority.extend(row for row in options if row not in priority)
    selected: list[Segment] = []
    used = 0
    counts: dict[tuple[str, str], int] = {}
    ranges: set[tuple[str, int, int]] = set()
    for _, _, segment in priority:
        excerpt = segment.excerpts[0]
        key = (excerpt.unit_id, excerpt.char_start, excerpt.char_end)
        file_key = _file_key(segment)
        cost = counter.count(segment.text) + sum(
            counter.count(excerpt.text) for excerpt in segment.context_excerpts
        )
        if (
            len(selected) == cap
            or counts.get(file_key, 0) >= 2
            or cost > text_budget - used
            or key in ranges
            or any(
                _code_overlap(segment.citations[0], prior.citations[0])
                for prior in selected
            )
        ):
            omitted.extend(segment.anchors)
            continue
        selected.append(segment)
        ranges.add(key)
        counts[file_key] = counts.get(file_key, 0) + 1
        used += cost
    return Selection(
        segments=tuple(selected),
        reservation_priority=tuple(
            row[2].lane for row in priority if row[2].lane in required
        ),
        omitted_anchors=tuple(omitted),
        lane_coverage=tuple(dict.fromkeys(segment.lane for segment in selected)),
        text_budget=text_budget,
        visual_reserved_tokens=visual_reserve,
    )
