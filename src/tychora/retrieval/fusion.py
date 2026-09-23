"""Reciprocal rank fusion over typed evidence owners."""

from collections.abc import Mapping
from dataclasses import dataclass

from tychora.contracts.candidates import CandidateKey, ChannelCandidate

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "exact": 2.0,
    "lexical": 1.0,
    "text": 1.0,
    "visual": 0.7,
    "graph": 0.35,
}


class RankedCandidate(ChannelCandidate):
    """A dense, one-based owner rank within a channel."""


@dataclass(frozen=True)
class FusedCandidate:
    key: CandidateKey
    rank: int
    score: float
    ranks: Mapping[str, int]
    matching_window_ids: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    hint: float = 0.0


def rrf_score(ranks: Mapping[str, int], weights: Mapping[str, float]) -> float:
    """Compute reciprocal rank fusion with one vote per supplied channel."""
    return sum(weights[channel] / (60 + rank) for channel, rank in ranks.items())


def fuse(
    channels: Mapping[str, tuple[RankedCandidate | ChannelCandidate, ...]],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    *,
    hints: Mapping[CandidateKey, float] | None = None,
    hints_enabled: bool = False,
) -> tuple[FusedCandidate, ...]:
    """Collapse owner windows and text families before assigning channel votes."""
    votes: dict[CandidateKey, dict[str, tuple[int, tuple[str, ...]]]] = {}
    text_best: dict[CandidateKey, tuple[int, str, tuple[str, ...]]] = {}
    for channel, candidates in sorted(channels.items()):
        if (
            channel != "text"
            and not channel.startswith("text:")
            and channel not in weights
        ):
            raise ValueError(f"Unknown fusion channel: {channel}")
        best: dict[CandidateKey, ChannelCandidate] = {}
        for item in candidates:
            prior = best.get(item.key)
            if prior is None or (item.rank, item.matching_window_ids) < (
                prior.rank,
                prior.matching_window_ids,
            ):
                best[item.key] = item
        if channel == "text" or channel.startswith("text:"):
            for key, item in best.items():
                option = (int(item.rank), channel, item.matching_window_ids)
                if key not in text_best or option < text_best[key]:
                    text_best[key] = option
        else:
            for key, item in best.items():
                votes.setdefault(key, {})[channel] = (
                    int(item.rank),
                    item.matching_window_ids,
                )
    for dense_rank, (key, (_, _, windows)) in enumerate(
        sorted(text_best.items(), key=lambda pair: (pair[1][0], pair[0])), start=1
    ):
        votes.setdefault(key, {})["text"] = (dense_rank, windows)
    output = []
    for key, channel_votes in votes.items():
        ranks = {channel: rank for channel, (rank, _) in channel_votes.items()}
        raw_hint = (hints or {}).get(key, 0.0) if hints_enabled else 0.0
        if not 0 <= raw_hint < float("inf"):
            raise ValueError("Fusion hint must be nonnegative and finite")
        hint = min(raw_hint, 0.01)
        windows = tuple(
            dict.fromkeys(
                window
                for _, channel_windows in channel_votes.values()
                for window in channel_windows
            )
        )
        output.append(
            FusedCandidate(
                key=key,
                rank=0,
                score=rrf_score(ranks, weights) + hint,
                ranks=ranks,
                matching_window_ids=windows,
                signals=tuple(sorted(ranks)),
                reasons=tuple(sorted(ranks)),
                hint=hint,
            )
        )
    output.sort(key=lambda item: (-item.score, item.key))
    return tuple(
        FusedCandidate(
            key=item.key,
            rank=rank,
            score=item.score,
            ranks=item.ranks,
            matching_window_ids=item.matching_window_ids,
            signals=item.signals,
            reasons=item.reasons,
            hint=item.hint,
        )
        for rank, item in enumerate(output, start=1)
    )
