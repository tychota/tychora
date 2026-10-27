"""Fused candidates checked against persisted dependency sets before admission."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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


def survivor_ids(candidates: Sequence[FusedCandidate]) -> tuple[str, ...]:
    """The owners and windows of the first ``SURVIVOR_LIMIT`` candidates."""
    ids: set[str] = set()
    for candidate in candidates[:SURVIVOR_LIMIT]:
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
) -> Survivors:
    """Keep each candidate whose owner and at least one window the view admits.

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
    beyond_limit = max(0, len(candidates) - SURVIVOR_LIMIT)
    return Survivors(tuple(kept), MappingProxyType(lanes), dropped, beyond_limit)


def _missing_units(facts: Iterable[OwnerFact]) -> set[str]:
    """Units a window's or an asset's admission reads besides its own fact."""
    units: set[str] = set()
    for fact in facts:
        if fact.kind == "chunk" and fact.unit_id:
            units.add(fact.unit_id)
        elif fact.kind == "asset":
            units.update(fact.source_owner_ids)
    return units


def load_survivors(
    tx: ReadTransaction, view: EligibilityView, candidates: Sequence[FusedCandidate]
) -> Survivors:
    """Read the candidates', their windows' and those windows' units' facts, then check."""
    # One read task: the second read only completes the first, inside the same
    # read transaction, with the units of proxy windows and of asset sources.
    facts = {
        f.owner_id: f for f in tx.eligibility.owner_facts(survivor_ids(candidates))
    }
    units = tuple(sorted(_missing_units(facts.values()) - set(facts)))
    if units:
        facts.update({f.owner_id: f for f in tx.eligibility.owner_facts(units)})
    return check_survivors(view, candidates, facts)
