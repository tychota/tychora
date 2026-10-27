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
    """Every record of the page with its own score, nearest first.

    An owner without per-window scores lends its score to each window; the
    sort is stable, so the adapter's nearest-first order breaks ties.
    """
    scores: dict[str, float] = {}
    for owner in ranked.owners:
        own = owner.window_scores or (owner.score,) * len(owner.window_ids)
        for record, score in zip(owner.window_ids, own, strict=True):
            scores[record] = max(scores.get(record, score), score)
    return sorted(scores.items(), key=lambda item: -item[1])


def _canonical_key(fact: OwnerFact) -> CandidateKey:
    """A proxy window's or an image's record belongs to its asset, others to their unit."""
    if fact.asset_id is not None:
        return ("asset", fact.asset_id)
    return ("unit", fact.unit_id or "")


def _admitted(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """A record of a selected extraction that the view admits."""
    return fact.extraction_id in view.revision_of_extraction and is_admitted_record(
        view, fact, facts
    )


def _group(
    view: EligibilityView,
    facts: Mapping[str, OwnerFact],
    records: Iterable[tuple[str, float]],
) -> _Best:
    """Admitted records grouped by canonical owner, scored by the nearest one.

    A record the store does not know, or of a batch the snapshot does not
    serve (unpublished or collected, still in the family's collection), is
    dropped rather than refused.
    """
    best: _Best = {}
    for record, score in records:
        fact = facts.get(record)
        if fact is None or not _admitted(view, fact, facts):
            continue
        windows = best.setdefault(_canonical_key(fact), (score, []))[1]
        if fact.chunk_id is not None and fact.chunk_id not in windows:
            windows.append(fact.chunk_id)
    return best


def _ranked(best: _Best, limit: int) -> dict[str, tuple[ChannelCandidate, ...]]:
    """Per canonical kind, the best ``limit`` owners with dense ranks."""
    output: dict[str, tuple[ChannelCandidate, ...]] = {}
    for kind in ("unit", "asset"):
        pool = sorted(
            (
                (score, key, windows)
                for key, (score, windows) in best.items()
                if key[0] == kind
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if pool:
            output[kind] = tuple(
                ChannelCandidate(
                    key=key, rank=rank, score=score, matching_window_ids=tuple(windows)
                )
                for rank, (score, key, windows) in enumerate(pool[:limit], 1)
            )
    return output


def vector_candidates(
    tx: ReadTransaction,
    view: EligibilityView,
    ranked: RankedOwners,
    limit: int = CHANNEL_LIMIT,
) -> tuple[tuple[ChannelCandidate, ...], bool]:
    """Canonical owners of the admitted records, ``limit`` per kind, and ``truncated``.

    Admission can leave a kind short while nearer admitted owners lie past
    the page, so unless the page held every stored record the channel is
    ``truncated`` when no kind survived or a surviving kind is short.
    """
    records = _record_scores(ranked)
    facts = load_facts(tx, (record for record, _ in records))
    kinds = _ranked(_group(view, facts, records), limit)
    exhausted = bool(ranked.detail.get("exhausted", 0))
    short = not kinds or any(len(pool) < limit for pool in kinds.values())
    candidates = tuple(c for pool in kinds.values() for c in pool)
    return candidates, not exhausted and short


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


def visual_candidates(
    tx: ReadTransaction,
    view: EligibilityView,
    ranked: RankedOwners,
    limit: int = CHANNEL_LIMIT,
) -> tuple[tuple[ChannelCandidate, ...], bool]:
    """Matched assets and the unit votes of their linked sources, in one read."""
    assets, truncated = vector_candidates(tx, view, ranked, limit)
    return (*assets, *linked_visual_context(tx, view, assets)), truncated
