"""Overlapping half-open ranges found by one sort and one sweep."""

import heapq
from collections.abc import Sequence


def overlapping_pairs(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Index pairs ``(i, j)``, ``i != j``, of overlapping nonempty ranges, each once.

    Ranges are visited by start. A heap keyed by end holds the ranges still open
    at the current start, and every one of them overlaps the range being opened.
    The cost is O(n log n) plus the number of pairs.
    """
    order = sorted(range(len(intervals)), key=lambda index: (*intervals[index], index))
    active: list[tuple[int, int]] = []
    pairs: list[tuple[int, int]] = []
    for index in order:
        start, end = intervals[index]
        if start >= end:
            continue
        while active and active[0][0] <= start:
            heapq.heappop(active)
        pairs.extend((other, index) for _, other in active)
        heapq.heappush(active, (end, index))
    return pairs
