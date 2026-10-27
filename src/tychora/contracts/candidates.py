"""Shared typed ranking owner identity."""

from typing import Literal

from .base import Contract, FiniteFloat, Identifier, NonNegativeInt, PositiveInt

type CandidateKey = tuple[Literal["unit", "asset"], Identifier]


class ChannelCandidate(Contract):
    key: CandidateKey
    rank: PositiveInt
    matching_window_ids: tuple[Identifier, ...] = ()
    score: FiniteFloat | None = None


class ChannelCandidates(Contract):
    channel: Literal["exact", "lexical"]
    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    source_candidates: tuple[ChannelCandidate, ...] = ()
    asset_candidates: tuple[ChannelCandidate, ...] = ()
    matching_window_count: NonNegativeInt = 0
    distinct_candidate_count: NonNegativeInt = 0
    lexical_statistics_epoch: NonNegativeInt | None = None
    complete: bool = True
    truncated: bool = False
