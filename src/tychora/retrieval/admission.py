"""Bounded pre-rerank candidate assessment and lane coverage."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from tychora.contracts.candidates import CandidateKey
from tychora.retrieval.fusion import FusedCandidate


@dataclass(frozen=True)
class LanePolicy:
    mode: Literal["none", "one_per_available_lane", "one_per_required_lane"] = "none"
    order: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    known: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in ("none", "one_per_available_lane", "one_per_required_lane"):
            raise ValueError("Unknown lane reservation mode")
        if len(self.order) != len(set(self.order)) or not set(
            self.order + self.required
        ) <= set(self.known):
            raise ValueError("Lane policy includes an unregistered or repeated lane")
        if len(self.required) != len(set(self.required)) or not set(
            self.required
        ) <= set(self.order):
            raise ValueError("Every required lane must occur once in configured order")


@dataclass(frozen=True)
class DiscardedCandidate:
    candidate: FusedCandidate
    reason: str


@dataclass(frozen=True)
class Admission:
    selected: tuple[FusedCandidate, ...]
    discarded: tuple[DiscardedCandidate, ...]
    coverage: Mapping[str, int]
    reservation_coverage: Mapping[str, int]
    missing_lanes: tuple[str, ...]
    missing_lane_reasons: Mapping[str, str]
    unused_slots: int


def admit(
    candidates: Sequence[FusedCandidate],
    exact_ids: Sequence[CandidateKey],
    graph_lists: Mapping[str, Sequence[CandidateKey]],
    lane_policy: LanePolicy,
    limit: int = 64,
    *,
    lanes: Mapping[CandidateKey, str],
) -> Admission:
    """Reserve exact, graph and lane assessment places before filling by fused rank.

    ``lanes`` is canonical detached owner metadata for the same pinned attempt.
    """
    if not 0 <= limit <= 64:
        raise ValueError("Assessment limit must be within 0..64")
    ranked = sorted(candidates, key=lambda item: (-item.score, item.key))
    by_key: dict[CandidateKey, FusedCandidate] = {}
    for item in ranked:
        by_key.setdefault(item.key, item)
    ranked = list(by_key.values())
    for key in by_key:
        lane = lanes.get(key)
        if lane is None:
            raise ValueError(f"Canonical lane missing for candidate {key}")
        if lane_policy.known and lane not in lane_policy.known:
            raise ValueError(f"Unregistered canonical lane: {lane}")
    selected: list[FusedCandidate] = []
    selected_keys: set[CandidateKey] = set()
    exact_set = set(exact_ids)
    graph_keys = {key for values in graph_lists.values() for key in values}
    graph_only = graph_keys - exact_set

    def take(key: CandidateKey) -> bool:
        if key in selected_keys or key not in by_key or len(selected) >= limit:
            return False
        if key in exact_set and sum(item in exact_set for item in selected_keys) >= 32:
            return False
        if key in graph_only and sum(item in graph_only for item in selected_keys) >= 8:
            return False
        selected.append(by_key[key])
        selected_keys.add(key)
        return True

    for item in ranked:
        if (
            item.key in exact_set
            and sum(key in exact_set for key in selected_keys) < 32
        ):
            take(item.key)
    graph_taken = 0
    for strategy in ("structural", "semantic"):
        pool = graph_lists.get(strategy, ())
        cap = 4 if graph_lists.get("structural") and graph_lists.get("semantic") else 8
        local = 0
        for key in pool:
            if key in graph_only and local < cap and graph_taken < 8 and take(key):
                local += 1
                graph_taken += 1
    if graph_taken < 8:
        for item in ranked:
            if item.key in graph_only and graph_taken < 8 and take(item.key):
                graph_taken += 1
    if lane_policy.mode == "one_per_required_lane":
        requested = tuple(
            lane for lane in lane_policy.order if lane in lane_policy.required
        )
    elif lane_policy.mode == "one_per_available_lane":
        requested = lane_policy.order
    else:
        requested = ()
    for lane in requested:
        if any(lanes[item.key] == lane for item in selected):
            continue
        for item in ranked:
            if lanes[item.key] == lane and take(item.key):
                break
    for item in ranked:
        take(item.key)
    coverage: dict[str, int] = {lane: 0 for lane in lane_policy.order}
    for item in selected:
        lane = lanes[item.key]
        coverage[lane] = coverage.get(lane, 0) + 1
    missing_reasons: dict[str, str] = {}
    for lane in requested:
        if coverage.get(lane, 0):
            continue
        matching = next((item.key for item in ranked if lanes[item.key] == lane), None)
        if matching is None:
            missing_reasons[lane] = "no_eligible_candidate"
        elif len(selected) >= limit:
            missing_reasons[lane] = "assessment_limit"
        elif matching in exact_set:
            missing_reasons[lane] = "exact_reservation_cap"
        elif matching in graph_only:
            missing_reasons[lane] = "graph_reservation_cap"
        else:
            missing_reasons[lane] = "assessment_limit"
    reservations = {
        "exact": sum(item.key in exact_set for item in selected),
        "graph": sum(item.key in graph_only for item in selected),
    }

    def discard_reason(key: CandidateKey) -> str:
        if len(selected) >= limit:
            return "assessment_limit"
        if key in exact_set:
            return "exact_reservation_cap"
        if key in graph_only:
            return "graph_reservation_cap"
        return "assessment_limit"

    return Admission(
        selected=tuple(selected),
        discarded=tuple(
            DiscardedCandidate(item, discard_reason(item.key))
            for item in ranked
            if item.key not in selected_keys
        ),
        coverage=coverage,
        reservation_coverage=reservations,
        missing_lanes=tuple(missing_reasons),
        missing_lane_reasons=missing_reasons,
        unused_slots=limit - len(selected),
    )
