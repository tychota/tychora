"""Fused candidates checked against persisted dependency sets before admission."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from types import MappingProxyType

from tychora.contracts.candidates import CandidateKey
from tychora.contracts.eligibility import EligibilityView
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_candidate_asset,
    is_eligible_asset,
    is_source_unit,
)
from tychora.retrieval.fusion import FusedCandidate

SURVIVOR_LIMIT = 200
EXACT_RESERVATION = 32
GRAPH_RESERVATION = 8


@dataclass(frozen=True)
class Survivors:
    """The candidates the view admits, their lanes and what did not survive.

    ``dropped`` counts candidates whose owner or every window failed the view,
    the only dependency drops; ``beyond_limit`` counts candidates past
    ``SURVIVOR_LIMIT`` that were never read, which say nothing about eligibility.
    """

    candidates: tuple[FusedCandidate, ...]
    lanes: Mapping[CandidateKey, str]
    dropped: int
    beyond_limit: int = 0


def _graph_reserved(
    ranked: Sequence[FusedCandidate],
    graph_lists: Mapping[str, Sequence[CandidateKey]],
    graph_only: set[CandidateKey],
) -> list[CandidateKey]:
    """The graph-only keys admission would reserve, in admission's order.

    Structural then semantic lists, four each when both ran, then fused rank,
    up to ``GRAPH_RESERVATION`` keys that the fused list holds.
    """
    fused = {item.key for item in ranked}
    both = bool(graph_lists.get("structural")) and bool(graph_lists.get("semantic"))
    cap = GRAPH_RESERVATION // 2 if both else GRAPH_RESERVATION
    picked: list[CandidateKey] = []
    for strategy in ("structural", "semantic"):
        pool = (
            key
            for key in dict.fromkeys(graph_lists.get(strategy, ()))
            if key in graph_only and key in fused and key not in picked
        )
        picked.extend(islice(pool, min(cap, GRAPH_RESERVATION - len(picked))))
    rest = (i.key for i in ranked if i.key in graph_only and i.key not in picked)
    picked.extend(islice(rest, GRAPH_RESERVATION - len(picked)))
    return picked


def survivor_selection(
    candidates: Sequence[FusedCandidate],
    exact_keys: Sequence[CandidateKey] = (),
    graph_lists: Mapping[str, Sequence[CandidateKey]] | None = None,
) -> tuple[FusedCandidate, ...]:
    """The at most ``SURVIVOR_LIMIT`` candidates to check, chosen in admission order.

    Exact keys first (up to ``EXACT_RESERVATION``), then graph-only keys (up to
    ``GRAPH_RESERVATION``), then fused rank, so admission's reserved places
    never go empty because a low fused score put their keys past the bound.
    The selection keeps fused order.
    """
    if len(candidates) <= SURVIVOR_LIMIT:
        return tuple(candidates)
    exact = set(exact_keys)
    graph_only = {
        key for values in (graph_lists or {}).values() for key in values
    } - exact
    chosen = dict.fromkeys(
        islice((i.key for i in candidates if i.key in exact), EXACT_RESERVATION)
    )
    chosen.update(
        dict.fromkeys(_graph_reserved(candidates, graph_lists or {}, graph_only))
    )
    for item in candidates:
        if len(chosen) >= SURVIVOR_LIMIT:
            break
        chosen.setdefault(item.key)
    return tuple(item for item in candidates if item.key in chosen)


def survivor_ids(candidates: Sequence[FusedCandidate]) -> tuple[str, ...]:
    """The owners and windows of the selected candidates."""
    ids: set[str] = set()
    for candidate in candidates:
        ids.add(candidate.key[1])
        ids.update(candidate.matching_window_ids)
    return tuple(sorted(ids))


def _owner_admitted(
    view: EligibilityView,
    fact: OwnerFact,
    facts: Mapping[str, OwnerFact],
    has_windows: bool,
) -> bool:
    """A citable source unit, or an asset reached through windows or its sources.

    The owner's extraction must be granted: a fact from any other extraction is
    refused here rather than failing the lane lookup.
    """
    if fact.extraction_id not in view.revision_of_extraction:
        return False
    if fact.kind == "unit":
        return is_source_unit(view, fact)
    if fact.kind == "asset":
        return (
            is_eligible_asset(view, fact)
            if has_windows
            else is_candidate_asset(view, fact, facts)
        )
    return False


def check_survivors(
    view: EligibilityView,
    candidates: Sequence[FusedCandidate],
    facts: Mapping[str, OwnerFact],
    *,
    total: int | None = None,
) -> Survivors:
    """Keep each candidate whose owner and at least one window the view admits.

    ``candidates`` is the selection to check (see ``survivor_selection``);
    ``total`` is the size of the fused list it came from, so the candidates
    left unread are counted in ``beyond_limit``.

    A candidate without its own fact is dropped: a missing row means the owner
    is unknown to the snapshot (``owner_facts`` already refuses a known owner
    that has no dependency set with ``facts_missing``).
    """
    kept: list[FusedCandidate] = []
    lanes: dict[CandidateKey, str] = {}
    dropped = 0
    for candidate in candidates[:SURVIVOR_LIMIT]:
        fact = facts.get(candidate.key[1])
        windows = tuple(
            window
            for window in candidate.matching_window_ids
            if (chunk := facts.get(window)) is not None
            and is_admitted_chunk(view, chunk, facts)
        )
        if (
            fact is None
            or not _owner_admitted(
                view, fact, facts, bool(candidate.matching_window_ids)
            )
            or (candidate.matching_window_ids and not windows)
        ):
            dropped += 1
            continue
        kept.append(replace(candidate, matching_window_ids=windows))
        revision = view.revision_of_extraction[fact.extraction_id]
        lanes[candidate.key] = view.revisions[revision].lane
    checked = min(len(candidates), SURVIVOR_LIMIT)
    beyond_limit = max(0, (len(candidates) if total is None else total) - checked)
    return Survivors(tuple(kept), MappingProxyType(lanes), dropped, beyond_limit)


def _related(fact: OwnerFact) -> set[str]:
    """What admitting ``fact`` reads besides its own fact.

    A record reads its window and that window's unit, or its asset; a window
    reads its unit; an asset reads its source units for the candidate filter.
    """
    if fact.kind == "record":
        if fact.chunk_id is not None:
            return {fact.chunk_id, *([fact.unit_id] if fact.unit_id else [])}
        return {fact.asset_id} if fact.asset_id else set()
    if fact.kind == "chunk" and fact.unit_id:
        return {fact.unit_id}
    if fact.kind == "asset":
        return set(fact.source_owner_ids)
    return set()


def load_facts(tx: ReadTransaction, owner_ids: Iterable[str]) -> dict[str, OwnerFact]:
    """Facts of the owners and of everything their admission reads.

    Every read stays in the caller's read transaction; each round only adds
    owners not asked for yet, so records take at most three reads (records,
    windows and units or assets, asset sources) and windows or assets two.
    """
    asked = set(owner_ids)
    facts = {f.owner_id: f for f in tx.eligibility.owner_facts(tuple(sorted(asked)))}
    pending = facts.values()
    while missing := {i for f in pending for i in _related(f)} - asked:
        asked |= missing
        read = tx.eligibility.owner_facts(tuple(sorted(missing)))
        facts.update({f.owner_id: f for f in read})
        pending = read
    return facts


def load_survivors(
    tx: ReadTransaction,
    view: EligibilityView,
    candidates: Sequence[FusedCandidate],
    exact_keys: Sequence[CandidateKey] = (),
    graph_lists: Mapping[str, Sequence[CandidateKey]] | None = None,
) -> Survivors:
    """Select the candidates in admission order, read their facts, then check."""
    selected = survivor_selection(candidates, exact_keys, graph_lists)
    return check_survivors(
        view, selected, load_facts(tx, survivor_ids(selected)), total=len(candidates)
    )
