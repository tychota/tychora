"""Vector hits mapped to canonical owners and admitted against the view."""

from collections.abc import Iterable, Mapping

from tychora.contracts.candidates import CandidateKey, ChannelCandidate
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.ports.search import RankedOwners
from tychora.retrieval.attempt import is_admitted_record, is_source_unit
from tychora.retrieval.survivors import load_facts

CHANNEL_LIMIT = 80
MAX_SOURCE_LINKS = 2048

type _Best = dict[CandidateKey, tuple[float, list[str]]]


def _record_scores(ranked: RankedOwners) -> list[tuple[str, float]]:
    """Every record the adapter returned with its owner's score, best owner first.

    Within an owner the adapter's nearest-first window order is kept, so a
    canonical owner's first matching window stays its nearest one.
    """
    scores: dict[str, float] = {}
    for owner in sorted(ranked.owners, key=lambda owner: -owner.score):
        for record in owner.window_ids:
            scores.setdefault(record, owner.score)
    return list(scores.items())


def _canonical_key(fact: OwnerFact) -> CandidateKey:
    """A proxy window's or an image's record belongs to its asset, others to their unit."""
    if fact.asset_id is not None:
        return ("asset", fact.asset_id)
    return ("unit", fact.unit_id or "")


def _group(
    view: EligibilityView,
    facts: Mapping[str, OwnerFact],
    records: Iterable[tuple[str, float]],
) -> tuple[_Best, int]:
    """Admitted records grouped by canonical owner, and how many were dropped.

    A record the store does not know, or of a batch the snapshot does not
    serve (unpublished or collected, still in the family's collection), is
    dropped rather than refused.
    """
    best: _Best = {}
    dropped = 0
    for record, score in records:
        fact = facts.get(record)
        if fact is None or not is_admitted_record(view, fact, facts):
            dropped += 1
            continue
        windows = best.setdefault(_canonical_key(fact), (score, []))[1]
        if fact.chunk_id is not None and fact.chunk_id not in windows:
            windows.append(fact.chunk_id)
    return best, dropped


def _ranked(best: _Best) -> tuple[ChannelCandidate, ...]:
    """Dense ranks per kind, units first, best score then key."""
    candidates: list[ChannelCandidate] = []
    for kind in ("unit", "asset"):
        pool = sorted(
            (
                (score, key, windows)
                for key, (score, windows) in best.items()
                if key[0] == kind
            ),
            key=lambda item: (-item[0], item[1]),
        )
        candidates.extend(
            ChannelCandidate(
                key=key, rank=rank, score=score, matching_window_ids=tuple(windows)
            )
            for rank, (score, key, windows) in enumerate(pool, 1)
        )
    return tuple(candidates)


def vector_candidates(
    tx: ReadTransaction,
    view: EligibilityView,
    ranked: RankedOwners,
    limit: int = CHANNEL_LIMIT,
) -> tuple[tuple[ChannelCandidate, ...], bool]:
    """Canonical owners of the admitted records and the channel's ``truncated``.

    Records dropped after the adapter can leave fewer than ``limit`` owners
    while nearer admitted ones lie past the page; the channel is then
    ``truncated`` as well as when the adapter's over-fetch ran out.
    """
    records = _record_scores(ranked)
    facts = load_facts(tx, (record for record, _ in records))
    best, dropped = _group(view, facts, records)
    candidates = _ranked(best)
    truncated = ranked.truncated or (dropped > 0 and len(candidates) < limit)
    return candidates, truncated


def _linked_sources(
    assets: tuple[ChannelCandidate, ...], asset_facts: Mapping[str, OwnerFact]
) -> list[str]:
    """The source units of the matched assets in candidate order, bounded per asset."""
    linked: dict[str, None] = {}
    for candidate in assets:
        fact = asset_facts.get(candidate.key[1])
        if fact is None:
            continue
        if len(fact.source_owner_ids) > MAX_SOURCE_LINKS:
            raise TychoraError(
                "INDEX_INCOMPLETE",
                "Visual source links exceed the bound.",
                reason="channel.unavailable",
            )
        linked.update(dict.fromkeys(sorted(fact.source_owner_ids)))
    return list(linked)


def linked_visual_context(
    tx: ReadTransaction, view: EligibilityView, assets: tuple[ChannelCandidate, ...]
) -> tuple[ChannelCandidate, ...]:
    """The eligible source units linked to matched original assets, as unit votes."""
    asset_ids = tuple(c.key[1] for c in assets if c.key[0] == "asset")
    if not asset_ids:
        return ()
    asset_facts = {
        f.owner_id: f
        for f in tx.eligibility.owner_facts(asset_ids)
        if f.kind == "asset"
    }
    linked = _linked_sources(assets, asset_facts)
    if not linked:
        return ()
    units = {f.owner_id: f for f in tx.eligibility.owner_facts(tuple(linked))}
    return tuple(
        ChannelCandidate(key=("unit", unit), rank=rank)
        for rank, unit in enumerate(
            (u for u in linked if u in units and is_source_unit(view, units[u])), 1
        )
    )
