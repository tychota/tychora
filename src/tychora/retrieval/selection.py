"""Edition selection over the retained revisions of each source."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal, Self

from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest


@dataclass(frozen=True)
class RevisionFact:
    """Detached selected edition and current canonical authorization facts."""

    source_id: str
    revision_id: str
    extraction_id: str
    collection_id: str
    kind_id: str
    access_rank: int
    source_floor: int
    collection_floor: int
    allowed_uses: frozenset[str]
    review_state: str
    withdrawn: bool
    published_on: str | None
    effective_from: str | None
    effective_until: str | None
    is_default: bool
    kind_allowed_uses: frozenset[str] | None = None
    edition_key: str = ""


@dataclass(frozen=True)
class TemporalSelector:
    """A request's edition selection: explicit revisions, selector and date bounds."""

    explicit_ids: tuple[str, ...] = ()
    selector: Literal["current", "all", "applicable"] = "current"
    as_of: date | None = None
    cutoff: date | None = None
    unknown_dates: Literal["strict", "permissive"] = "strict"

    @classmethod
    def of(cls, request: QueryRequest) -> Self:
        """The request's edition selection."""
        return cls(
            explicit_ids=request.revision_ids,
            selector=request.revision_selector,
            as_of=request.as_of,
            cutoff=request.cutoff,
            unknown_dates=request.unknown_dates,
        )


_CURRENT = TemporalSelector()

DecisionReason = Literal["tie", "overlap", "undated", "applicable"]
_MESSAGES: dict[str, str] = {
    "tie": "Retained editions tie for source",
    "overlap": "Overlapping retained editions for source",
    "undated": "Undated retained editions may apply for source",
    "applicable": "Several retained editions apply for source",
}


@dataclass(frozen=True)
class EditionDecision:
    """An edition choice the caller must make: a tie, a declared overlap, undated or applicable editions."""

    reason: DecisionReason
    source_id: str
    revision_ids: tuple[str, ...]

    def message(self) -> str:
        """The decision as the warning text a result carries."""
        return f"{_MESSAGES[self.reason]} {self.source_id}"


@dataclass(frozen=True)
class EditionSelection:
    """The selected revisions and the decisions their selection leaves open."""

    selected: frozenset[str]
    decisions: tuple[EditionDecision, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        """The warning text of each decision, in decision order."""
        return tuple(decision.message() for decision in self.decisions)


def _parsed_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _in_force(
    edition: RevisionFact, as_of: date | None, cutoff: date | None
) -> bool | None:
    """Whether a dated edition passes both constraints; ``None`` when a needed date is unknown.

    Every exclusion the known dates prove is decided first, so an edition that
    is provably expired, future or too late is never reported as undated.
    """
    start, end = (
        _parsed_date(edition.effective_from),
        _parsed_date(edition.effective_until),
    )
    published = _parsed_date(edition.published_on)
    if as_of is not None and end is not None and as_of >= end:
        return False
    if as_of is not None and start is not None and as_of < start:
        return False
    if cutoff is not None and published is not None and published > cutoff:
        return False
    unknown = (as_of is not None and start is None) or (
        cutoff is not None and published is None
    )
    return None if unknown else True


def _ids(editions: list[RevisionFact]) -> tuple[str, ...]:
    return tuple(sorted(edition.revision_id for edition in editions))


def _key(edition: RevisionFact, as_of: date | None) -> tuple[str, str, str]:
    """The ordering key: effective start with ``as_of``, publication otherwise.

    Under ``as_of`` a missing ``published_on`` sorts lowest among equal starts.
    ``edition_key`` order is lexicographic (``"10"`` sorts before ``"9"``).
    """
    published = edition.published_on or ""
    if as_of is not None:
        return (edition.effective_from or "", published, edition.edition_key)
    return (published, edition.edition_key, "")


def _overlapping(
    winners: list[RevisionFact], known: list[RevisionFact]
) -> list[RevisionFact]:
    """Other editions in force that declare an end later than the winners' start."""
    start = winners[0].effective_from or ""
    chosen = {edition.revision_id for edition in winners}
    return [
        edition
        for edition in known
        if edition.revision_id not in chosen
        and edition.effective_until is not None
        and edition.effective_until > start
    ]


def _winners(
    source: str, known: list[RevisionFact], as_of: date | None
) -> tuple[list[RevisionFact], list[EditionDecision]]:
    """The editions with the greatest key, and the tie and overlap they leave open.

    A tie and an overlap together report both decisions.
    """
    best = max(_key(edition, as_of) for edition in known)
    winners = [edition for edition in known if _key(edition, as_of) == best]
    decisions: list[EditionDecision] = []
    if len(winners) > 1:
        decisions.append(EditionDecision("tie", source, _ids(winners)))
    overlapping = _overlapping(winners, known) if as_of is not None else []
    if overlapping:
        decisions.append(
            EditionDecision("overlap", source, _ids(winners + overlapping))
        )
    return winners, decisions


def _pick(
    source: str, known: list[RevisionFact], temporal: TemporalSelector
) -> tuple[list[RevisionFact], list[EditionDecision]]:
    """The dated editions one source contributes, and the decisions they raise."""
    if temporal.explicit_ids or temporal.selector == "all":
        return known, []
    if temporal.selector == "applicable":
        if len(known) > 1:
            return known, [EditionDecision("applicable", source, _ids(known))]
        return known, []
    if temporal.as_of is None and temporal.cutoff is None:
        return [edition for edition in known if edition.is_default], []
    if not known:
        return [], []
    return _winners(source, known, temporal.as_of)


def _grouped(
    revisions: tuple[RevisionFact, ...], explicit_ids: tuple[str, ...]
) -> dict[str, list[RevisionFact]]:
    explicit = set(explicit_ids)
    grouped: dict[str, list[RevisionFact]] = defaultdict(list)
    for revision in revisions:
        if not explicit or revision.revision_id in explicit:
            grouped[revision.source_id].append(revision)
    return grouped


def _split(
    editions: list[RevisionFact], temporal: TemporalSelector
) -> tuple[list[RevisionFact], list[RevisionFact]]:
    """The editions passing both constraints, and the undated ones kept when permissive."""
    states = [(e, _in_force(e, temporal.as_of, temporal.cutoff)) for e in editions]
    known = [edition for edition, state in states if state]
    if temporal.unknown_dates != "permissive":
        return known, []
    return known, [edition for edition, state in states if state is None]


def select_temporal_revisions(
    revisions: tuple[RevisionFact, ...],
    temporal: TemporalSelector = _CURRENT,
) -> EditionSelection:
    """Select editions per source after applying both date constraints (spec 4.9).

    Permissive unknown-date editions are selected beside the winners and
    reported apart, by an ``undated`` decision.
    """
    if temporal.selector == "applicable" and temporal.as_of is None:
        raise TychoraError(
            "INVALID_REQUEST",
            "The applicable selector needs an as_of date",
            reason="edition.as_of_required",
        )
    selected: set[str] = set()
    undated: set[str] = set()
    decisions: list[EditionDecision] = []
    for source, editions in sorted(_grouped(revisions, temporal.explicit_ids).items()):
        known, unknown = _split(editions, temporal)
        if unknown:
            undated.update(_ids(unknown))
            decisions.append(EditionDecision("undated", source, _ids(unknown)))
        picked, found = _pick(source, known, temporal)
        selected.update(edition.revision_id for edition in picked)
        decisions.extend(found)
    return EditionSelection(frozenset(selected | undated), tuple(decisions))
