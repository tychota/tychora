"""Select source-exact matched representations before owner-bound reranking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tychora.application.reranking import (
    RerankAttestationError,
    attest_ranking_input,
    rank_with_owner,
)
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.corpus import (
    ChunkRow,
    ExtractionBundle,
    RetainedRepresentationRows,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import AnchorRange, EvidenceUnit
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.identity import chunk_id as derive_chunk_id
from tychora.contracts.indexing import PreparedExtraction
from tychora.contracts.requests import PassageInput, QueryRequest, QuestionInput
from tychora.ports.execution import ExecutionContext
from tychora.ports.reranking import (
    RankingInput,
    RankingSpan,
    RankingTruncation,
    render_ranking_input,
)
from tychora.ports.tokens import TokenCounter
from tychora.retrieval.admission import Admission
from tychora.retrieval.fusion import DEFAULT_WEIGHTS, FusedCandidate

if TYPE_CHECKING:
    from tychora.adapters.voyage.rerank import VoyageReranker
    from tychora.application.owner import WorkspaceOwner
    from tychora.retrieval.attempt import EligibilityAttempt


@dataclass(frozen=True)
class RerankMatch:
    """A window hit with its original channel ordering and exact source range."""

    window_id: str | None
    unit_id: str
    char_start: int
    char_end: int
    channel: str
    rank: int
    weight: float
    window_rank: int = 0


@dataclass(frozen=True)
class RerankFacts:
    """Canonical retained ownership and producer matches for one candidate."""

    rows: ExtractionBundle | RetainedRepresentationRows
    prepared: PreparedExtraction
    matches: tuple[RerankMatch, ...]
    graph_support: RerankMatch | None = None


@dataclass(frozen=True)
class RerankContext:
    """One pinned owner operation; no caller-supplied dispatch callback."""

    owner: WorkspaceOwner
    attempt: EligibilityAttempt
    reranker: VoyageReranker | None
    counter: TokenCounter | None
    facts: Mapping[tuple[str, str], RerankFacts]
    execution: ExecutionContext
    allow_degraded: bool
    unavailable: TychoraError | None = None


@dataclass(frozen=True)
class RerankFailure:
    code: str
    message: str


@dataclass(frozen=True)
class RankedEvidenceItem:
    candidate: FusedCandidate
    representation: RankingInput | None
    facts: RerankFacts | None
    status: Literal["ranked", "unavailable", "disabled", "independent"]
    rerank_rank: int | None = None
    rerank_score: float | None = None

    @property
    def window_ids(self) -> tuple[str, ...]:
        if self.facts is None:
            return self.candidate.matching_window_ids
        return tuple(
            dict.fromkeys(
                (
                    *self.candidate.matching_window_ids,
                    *(
                        match.window_id
                        for match in self.facts.matches
                        if match.window_id is not None
                    ),
                )
            )
        )

    @property
    def anchors(self) -> tuple[AnchorRange, ...]:
        if self.representation is not None:
            return self.representation.anchors
        if self.facts is None:
            return ()
        matches = self.facts.matches
        if not matches and self.facts.graph_support is not None:
            matches = (self.facts.graph_support,)
        if not matches and self.candidate.key[0] == "unit":
            unit = self.facts.prepared.representation(self.candidate.key[1]).unit
            if unit.text:
                matches = (
                    RerankMatch(
                        None,
                        unit.unit_id,
                        0,
                        len(unit.text),
                        "graph_unit_fallback"
                        if "graph" in self.candidate.ranks
                        else "exact_unit",
                        1,
                        0.0,
                    ),
                )
        return tuple(
            AnchorRange(
                unit_id=match.unit_id,
                char_start=match.char_start,
                char_end=match.char_end,
                origin=match.channel,
                indexed_window_id=match.window_id,
            )
            for match in matches
        )

    @property
    def omitted_match_count(self) -> int:
        return (
            self.representation.omitted_match_count
            if self.representation is not None
            else 0
        )

    @property
    def source_matches(self) -> tuple[RerankMatch, ...]:
        return self.facts.matches if self.facts is not None else ()

    @property
    def truncations(self) -> tuple[RankingTruncation, ...]:
        return (
            self.representation.truncations if self.representation is not None else ()
        )


@dataclass(frozen=True)
class RankedEvidence:
    items: tuple[RankedEvidenceItem, ...]
    failure: RerankFailure | None = None


async def rerank_candidates(
    query: QueryRequest, admission: Admission, ctx: RerankContext
) -> RankedEvidence:
    """Score one complete textual pool through the existing owner-authorized path."""
    legacy = ctx.attempt.require_legacy()
    ctx.execution.checkpoint()
    if len(admission.selected) > 64:
        raise TychoraError("INVALID_REQUEST", "Rerank candidate count is invalid.")
    text = (
        query.input.text
        if isinstance(query.input, (QuestionInput, PassageInput))
        else None
    )
    representations: list[RankingInput] = []

    def item_for(
        candidate: FusedCandidate,
        *,
        status: Literal["ranked", "unavailable", "disabled", "independent"],
        rank: int | None = None,
        score: float | None = None,
    ) -> RankedEvidenceItem:
        representation = next(
            (item for item in representations if item.key == candidate.key), None
        )
        return RankedEvidenceItem(
            candidate, representation, ctx.facts.get(candidate.key), status, rank, score
        )

    if text is None or not any(
        candidate.key[0] == "unit" for candidate in admission.selected
    ):
        return RankedEvidence(
            tuple(
                item_for(candidate, status="independent")
                for candidate in admission.selected
            )
        )
    for candidate in admission.selected:
        if candidate.key[0] == "unit" and candidate.key not in ctx.facts:
            raise TychoraError(
                "INTEGRITY_ERROR", "Rerank candidate facts are unavailable."
            )
        if candidate.key[0] == "asset":
            continue
        facts = ctx.facts[candidate.key]
        allowed = (
            legacy.revision_ids
            | legacy.context_unit_ids
            | legacy.structural_unit_ids
            | legacy.asset_ids
        )
        source_ids = tuple(
            dict.fromkeys(
                (
                    candidate.key[1],
                    *(match.window_id or match.unit_id for match in facts.matches),
                    *(match.unit_id for match in facts.matches),
                )
            )
        )
        for source_id in source_ids:
            if not ctx.attempt.dependencies_for(source_id) <= allowed:
                raise TychoraError("FORBIDDEN_SCOPE", "Ranking window is not eligible.")
    disabled = ctx.owner.config.values.providers.rerank.provider == "disabled"
    if disabled:
        return RankedEvidence(
            tuple(
                item_for(
                    candidate,
                    status="independent" if candidate.key[0] == "asset" else "disabled",
                )
                for candidate in admission.selected
            )
        )
    unavailable = ctx.unavailable
    if ctx.owner.config.values.offline or ctx.reranker is None:
        unavailable = unavailable or TychoraError(
            "CAPABILITY_MISSING", "Reranker is unavailable."
        )
    if unavailable is not None:
        if not ctx.allow_degraded:
            raise unavailable
        return RankedEvidence(
            tuple(
                item_for(
                    candidate,
                    status="independent"
                    if candidate.key[0] == "asset"
                    else "unavailable",
                )
                for candidate in admission.selected
            ),
            RerankFailure(unavailable.code, unavailable.message),
        )
    if ctx.counter is None:
        raise TychoraError("INTEGRITY_ERROR", "Rerank token counter is unavailable.")
    assert ctx.reranker is not None
    reranker = ctx.reranker
    if ctx.counter is not reranker._counter:
        raise TychoraError("INTEGRITY_ERROR", "Rerank token counter differs.")
    for candidate in admission.selected:
        if candidate.key[0] == "asset":
            continue
        facts = ctx.facts[candidate.key]
        units = {unit.unit_id: unit for unit in facts.rows.units}
        item = ranking_input(candidate, units, legacy, ctx.counter, facts=facts)
        match_ids = tuple(
            dict.fromkeys(
                (
                    *(match.window_id or match.unit_id for match in facts.matches),
                    *(span.unit_id for span in item.spans),
                )
            )
        )
        allowed = (
            legacy.revision_ids
            | legacy.context_unit_ids
            | legacy.structural_unit_ids
            | legacy.asset_ids
        )
        for representation_id in match_ids:
            if not ctx.attempt.dependencies_for(representation_id) <= allowed:
                raise TychoraError("FORBIDDEN_SCOPE", "Ranking window is not eligible.")
        representations.append(item)
    assert text is not None
    try:
        scored = await ctx.attempt.owner_provider(
            lambda: rank_with_owner(
                ctx.owner, reranker, text, tuple(representations), ctx.execution
            )
        )
    except TychoraError as error:
        if (
            isinstance(error, RerankAttestationError)
            or not ctx.allow_degraded
            or error.code
            not in {
                "CAPABILITY_MISSING",
                "PROVIDER_FAILURE",
                "DEADLINE_EXCEEDED",
                "FORBIDDEN_SCOPE",
            }
        ):
            raise
        return RankedEvidence(
            tuple(
                item_for(
                    candidate,
                    status="independent"
                    if candidate.key[0] == "asset"
                    else "unavailable",
                )
                for candidate in admission.selected
            ),
            RerankFailure(error.code, error.message),
        )
    if len(scored) != len(representations) or {item.index for item in scored} != set(
        range(len(representations))
    ):
        raise TychoraError(
            "PROVIDER_FAILURE", "Reranker returned invalid candidate mapping."
        )
    rank_by_key = {
        representations[item.index].key: (rank, item.score)
        for rank, item in enumerate(scored, 1)
    }
    ranked_text = sorted(
        (candidate for candidate in admission.selected if candidate.key[0] == "unit"),
        key=lambda candidate: rank_by_key[candidate.key][0],
    )
    assets = tuple(
        candidate for candidate in admission.selected if candidate.key[0] == "asset"
    )
    return RankedEvidence(
        tuple(
            item_for(
                candidate,
                status="ranked",
                rank=rank_by_key[candidate.key][0],
                score=rank_by_key[candidate.key][1],
            )
            for candidate in ranked_text
        )
        + tuple(item_for(candidate, status="independent") for candidate in assets)
    )


def matches_from_channels(
    candidate: FusedCandidate,
    channels: Mapping[str, tuple[ChannelCandidate, ...]],
    chunks: Mapping[str, ChunkRow],
    prepared: PreparedExtraction,
    view: LegacyEligibilityView,
    *,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> tuple[RerankMatch, ...]:
    """Keep every producer window, including nonwinning text family windows."""
    output: list[RerankMatch] = []
    for channel, hits in channels.items():
        family = "text" if channel.startswith("text:") else channel
        if family not in weights:
            raise TychoraError("INVALID_REQUEST", "Unknown rerank match channel.")
        for hit in hits:
            if hit.key != candidate.key:
                continue
            for window_rank, window_id in enumerate(hit.matching_window_ids):
                if window_id not in view.chunk_ids or window_id not in chunks:
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Ranking window is not eligible."
                    )
                chunk = chunks[window_id]
                try:
                    unit = prepared.representation(chunk.unit_id).unit
                except ValueError:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Ranking range has no canonical unit."
                    ) from None
                if (
                    chunk.chunk_id
                    != derive_chunk_id(
                        chunk.unit_id,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.chunker_fingerprint,
                    )
                    or chunk.extraction_id != prepared.extraction_id
                    or chunk.char_end > len(unit.text)
                    or chunk.text != unit.text[chunk.char_start : chunk.char_end]
                    or (
                        candidate.key[0] == "unit" and chunk.unit_id != candidate.key[1]
                    )
                ):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Ranking range is not source text."
                    )
                output.append(
                    RerankMatch(
                        window_id=window_id,
                        unit_id=chunk.unit_id,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        channel=channel,
                        rank=hit.rank,
                        weight=weights[family],
                        window_rank=window_rank if family == "text" else 0,
                    )
                )
    return tuple(output)


def _dependencies(
    prepared: PreparedExtraction, unit_id: str, start: int, end: int
) -> frozenset[str]:
    unit = prepared.representation(unit_id)
    cursor = start
    dependencies: set[str] = set()
    for interval in unit.ownership:
        if interval.char_end <= cursor or interval.char_start >= end:
            continue
        if interval.char_start > cursor:
            break
        dependencies.update(interval.dependency_ids)
        cursor = min(end, interval.char_end)
        if cursor == end:
            break
    if cursor != end:
        raise TychoraError("INTEGRITY_ERROR", "Ranking range has no exact owner.")
    return frozenset(dependencies)


def ranking_input(
    candidate: FusedCandidate,
    units: Mapping[str, EvidenceUnit],
    view: LegacyEligibilityView,
    counter: TokenCounter,
    budget: int = 1200,
    *,
    facts: RerankFacts,
) -> RankingInput:
    """Render the strongest matched window from canonical source codepoints."""
    if not 0 < budget <= 1200:
        raise TychoraError("INVALID_REQUEST", "Ranking representation is unavailable.")
    if facts.matches:
        matches = facts.matches
    elif "graph" in candidate.ranks:
        unit = facts.prepared.representation(candidate.key[1]).unit
        matches = (
            facts.graph_support
            or RerankMatch(
                None, unit.unit_id, 0, len(unit.text), "graph_unit_fallback", 1, 0.35
            ),
        )
    else:
        unit = facts.prepared.representation(candidate.key[1]).unit
        matches = (
            RerankMatch(None, unit.unit_id, 0, len(unit.text), "exact_unit", 1, 2.0),
        )
    ordered_list: list[RerankMatch] = []
    seen_ranges: set[tuple[str, int, int]] = set()
    for match in sorted(
        matches,
        key=lambda match: (
            match.rank,
            -match.weight,
            match.window_rank,
            match.char_start,
            match.unit_id,
            match.window_id or "",
            match.char_end,
            match.channel,
        ),
    ):
        range_key = (match.unit_id, match.char_start, match.char_end)
        if range_key not in seen_ranges:
            ordered_list.append(match)
            seen_ranges.add(range_key)
    ordered = tuple(ordered_list)
    if candidate.key[0] == "unit" and any(
        match.unit_id != candidate.key[1] for match in ordered
    ):
        raise TychoraError(
            "FORBIDDEN_SCOPE", "Graph support is not on the target unit."
        )
    allowed = (
        view.revision_ids
        | view.context_unit_ids
        | view.structural_unit_ids
        | view.asset_ids
    )

    def span_for(
        match: RerankMatch,
        end: int,
        *,
        role: Literal["match", "context"] = "match",
    ) -> RankingSpan:
        if match.unit_id not in units or (
            match.window_id is not None and match.window_id not in view.chunk_ids
        ):
            raise TychoraError("FORBIDDEN_SCOPE", "Ranking window is not eligible.")
        unit = units[match.unit_id]
        excerpt = unit.text[match.char_start : end]
        if not excerpt or match.char_start < 0 or end > len(unit.text):
            raise TychoraError("INTEGRITY_ERROR", "Ranking range is not source text.")
        dependencies = _dependencies(
            facts.prepared, match.unit_id, match.char_start, end
        )
        if not dependencies <= allowed:
            raise TychoraError("FORBIDDEN_SCOPE", "Ranking window is not eligible.")
        return RankingSpan(
            unit_id=unit.unit_id,
            char_start=match.char_start,
            char_end=end,
            text=excerpt,
            role=role,
            dependency_ids=dependencies,
        )

    def render(spans: tuple[RankingSpan, ...]) -> str:
        return render_ranking_input(
            facts.rows.revision.kind_id,
            facts.rows.revision.title,
            units[spans[0].unit_id].heading_path,
            spans,
        )

    # Validate every supplied match, including ones later omitted by the budget.
    exact = tuple(span_for(match, match.char_end) for match in ordered)
    chosen: list[RankingSpan] = []
    anchors: list[AnchorRange] = []
    truncations: list[RankingTruncation] = []
    for match, span in zip(ordered, exact, strict=True):
        if len(chosen) == 64:
            break
        if any(
            existing.unit_id == span.unit_id
            and span.char_start < existing.char_end
            and existing.char_start < span.char_end
            for existing in chosen
        ):
            continue
        option = (*chosen, span)
        if counter.count(render(option)) > budget:
            if chosen:
                continue
            low, high, fitted = span.char_start + 1, span.char_end, None
            while low <= high:
                middle = (low + high) // 2
                smaller = span_for(match, middle)
                if counter.count(render((smaller,))) <= budget:
                    fitted = smaller
                    low = middle + 1
                else:
                    high = middle - 1
            if fitted is None:
                raise TychoraError(
                    "INVALID_REQUEST", "Ranking match exceeds token budget."
                )
            span = fitted
            if match.window_id is not None:
                truncations.append(
                    RankingTruncation(
                        window_id=match.window_id,
                        original_char_start=match.char_start,
                        original_char_end=match.char_end,
                        selected_char_start=span.char_start,
                        selected_char_end=span.char_end,
                    )
                )
        chosen.append(span)
        anchors.append(
            AnchorRange(
                unit_id=span.unit_id,
                char_start=span.char_start,
                char_end=span.char_end,
                origin=match.channel,
                indexed_window_id=match.window_id,
            )
        )
    if not chosen:
        raise TychoraError("INVALID_REQUEST", "Ranking match exceeds token budget.")
    matched_unit_id = chosen[0].unit_id
    context_tokens = 0
    for record in facts.rows.records:
        if record.unit_id != matched_unit_id or record.record_type != "code_symbol":
            continue
        context_rows = record.data_json.get("context_spans", ())
        if not isinstance(context_rows, tuple):
            raise TychoraError("INTEGRITY_ERROR", "Code context is not canonical.")
        for context in context_rows:
            if not isinstance(context, FrozenJsonMap) or context.get("reason") not in {
                "parent_declaration",
                "referenced_import",
            }:
                continue
            source_id = context.get("source_unit_id")
            start, end = context.get("char_start"), context.get("char_end")
            if (
                not isinstance(source_id, str)
                or type(start) is not int
                or type(end) is not int
                or source_id not in units
                or start < 0
                or end <= start
            ):
                raise TychoraError("INTEGRITY_ERROR", "Code context is not canonical.")
            if source_id not in view.context_unit_ids | view.structural_unit_ids:
                continue
            context_match = RerankMatch(
                None, source_id, start, end, str(context["reason"]), 1, 0.0
            )
            span = span_for(context_match, end, role="context")
            if counter.count(span.text) + context_tokens > 160:
                low, high, fitted = start + 1, end, None
                while low <= high:
                    middle = (low + high) // 2
                    smaller = span_for(context_match, middle, role="context")
                    if counter.count(smaller.text) + context_tokens <= 160:
                        fitted = smaller
                        low = middle + 1
                    else:
                        high = middle - 1
                if fitted is None:
                    break
                span = fitted
            if counter.count(render((*chosen, span))) <= budget:
                chosen.append(span)
                context_tokens += counter.count(span.text)
    spans = tuple(chosen)
    text = render(spans)
    dependencies = frozenset(dep for span in spans for dep in span.dependency_ids)
    if candidate.key[0] == "asset":
        dependencies |= {candidate.key[1]}
    item = RankingInput(
        key=candidate.key,
        text=text,
        source_kind=facts.rows.revision.kind_id,
        title=facts.rows.revision.title,
        heading_path=units[spans[0].unit_id].heading_path,
        spans=spans,
        anchors=tuple(anchors),
        dependency_ids=dependencies,
        token_count=counter.count(text),
        omitted_match_count=len(ordered) - len(anchors),
        truncations=tuple(truncations),
    )
    attest_ranking_input(item, facts.rows, facts.prepared)
    return item
