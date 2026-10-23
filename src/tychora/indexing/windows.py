"""Lossless token-counted windows over canonical source text."""

import logging
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import pairwise

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import chunk_id, digest
from tychora.contracts.indexing import RepresentationUnit, SearchWindow
from tychora.contracts.sections import ContextLevel, Depth, context_key, section_path_of
from tychora.ports.tokens import OffsetTokenCounter, TokenCounter

_LOG = logging.getLogger(__name__)
TARGET_TOKENS = 512
SOFT_CEILING_TOKENS = 800
_BOUNDARY = re.compile(
    r"(?:\r\n|\r|\n){2,}|(?<=[.!?])(?:[ \t]+|\r\n|\r|\n)|(?:\r\n|\r|\n)"
)


def _count(counter: TokenCounter, text: str) -> int:
    count = counter.count(text)
    if type(count) is not int or count < 0:
        raise ValueError("Token counter returned an invalid count")
    return count


def _atomic_limit() -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        "One atomic source codepoint exceeds the token ceiling",
        reason="tokens.atomic_limit",
    )


def _semantic_boundaries(text: str, start: int, end: int) -> tuple[int, ...]:
    relative = text[start:end]
    return tuple(
        start + match.end()
        for match in _BOUNDARY.finditer(relative)
        if start + match.end() < end
    )


def _fit_prefix(
    text: str, start: int, end: int, counter: TokenCounter, limit: int
) -> int:
    low, high = start + 1, end
    best = start
    while low <= high:
        middle = (low + high) // 2
        if _count(counter, text[start:middle]) <= limit:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == start:
        raise _atomic_limit()
    return best


def _split_interval(
    text: str, start: int, end: int, counter: TokenCounter
) -> tuple[tuple[int, int, int], ...]:
    """Split by counting each candidate range: the path for counters without offsets."""
    output = []
    cursor = start
    while cursor < end:
        remaining = _count(counter, text[cursor:end])
        if remaining <= SOFT_CEILING_TOKENS:
            if remaining <= 0:
                raise ValueError("Nonempty source interval has no provider tokens")
            output.append((cursor, end, remaining))
            break
        candidates = []
        for boundary in _semantic_boundaries(text, cursor, end):
            count = _count(counter, text[cursor:boundary])
            if 0 < count <= SOFT_CEILING_TOKENS:
                candidates.append((boundary, count))
        before = [item for item in candidates if item[1] <= TARGET_TOKENS]
        if before:
            stop, count = max(before, key=lambda item: (item[1], item[0]))
        elif candidates:
            stop, count = min(candidates, key=lambda item: (item[1], item[0]))
        else:
            stop = _fit_prefix(text, cursor, end, counter, TARGET_TOKENS)
            count = _count(counter, text[cursor:stop])
        output.append((cursor, stop, count))
        cursor = stop
    return tuple(output)


@dataclass(frozen=True)
class _Spans:
    """Token spans of one interval in unit coordinates, and the fixed overhead."""

    starts: tuple[int, ...]
    ends: tuple[int, ...]
    overhead: int

    def count(self, start: int, end: int) -> int:
        """Tokens overlapping ``[start, end)`` plus the overhead: ``count(text[start:end])``."""
        return (
            self.overhead
            + bisect_left(self.starts, end)
            - bisect_right(self.ends, start)
        )

    def limit(self, start: int, tokens: int, end: int) -> int:
        """The last position ``p <= end`` with ``count(start, p) <= tokens``."""
        index = bisect_right(self.ends, start) + tokens - self.overhead
        if index < 0:
            return start
        return end if index >= len(self.starts) else min(end, self.starts[index])


def _spans(counter: OffsetTokenCounter, text: str, start: int) -> _Spans | None:
    """The interval's spans, or ``None`` unless they are ordered and inside the text."""
    spans = counter.count_with_offsets(text)
    if not spans and text:
        return None
    starts = tuple(start + left for left, _ in spans)
    ends = tuple(start + right for _, right in spans)
    if any(not 0 <= left < right <= len(text) for left, right in spans) or any(
        left > right for left, right in (*pairwise(starts), *pairwise(ends))
    ):
        return None
    return _Spans(starts, ends, _count(counter, ""))


def _stop(text: str, cursor: int, end: int, spans: _Spans) -> int:
    """Choose a window end as the counted split does, scanning only to the ceiling.

    Counts grow with the end position, so no boundary past ``horizon`` fits the
    ceiling; the scan stops one character later so a boundary run cut at the
    slice end is dropped like the counted split drops it by its count.
    """
    horizon = spans.limit(cursor, SOFT_CEILING_TOKENS, end)
    before: tuple[int, int] | None = None
    after: tuple[int, int] | None = None
    for boundary in _semantic_boundaries(text, cursor, min(end, horizon + 1)):
        count = spans.count(cursor, boundary)
        if not 0 < count <= SOFT_CEILING_TOKENS:
            continue
        if count <= TARGET_TOKENS:
            before = max(before or (count, boundary), (count, boundary))
        else:
            after = min(after or (count, boundary), (count, boundary))
    chosen = before if before is not None else after
    if chosen is not None:
        return chosen[1]
    stop = spans.limit(cursor, TARGET_TOKENS, end)
    if stop <= cursor:
        raise _atomic_limit()
    return stop


def _split_by_spans(
    text: str, start: int, end: int, counter: TokenCounter, spans: _Spans
) -> tuple[tuple[int, int, int], ...] | None:
    """Split one interval from its spans, or ``None`` if the spans miscount.

    The first window is counted exactly and must match its span count, so a
    counter that breaks the offsets contract keeps the counted split; every
    other count comes from the one tokenisation.
    """
    output = []
    cursor = start
    while cursor < end:
        remaining = spans.count(cursor, end)
        stop = (
            end if remaining <= SOFT_CEILING_TOKENS else _stop(text, cursor, end, spans)
        )
        count = spans.count(cursor, stop)
        if not output and _count(counter, text[cursor:stop]) != count:
            return None
        if stop == end and count <= 0:
            raise ValueError("Nonempty source interval has no provider tokens")
        output.append((cursor, stop, count))
        cursor = stop
    return tuple(output)


def _split(
    text: str, start: int, end: int, counter: TokenCounter
) -> tuple[tuple[int, int, int], ...]:
    """Split by offsets when the counter reports usable spans, else by counting."""
    if isinstance(counter, OffsetTokenCounter):
        spans = _spans(counter, text[start:end], start)
        windows = (
            None if spans is None else _split_by_spans(text, start, end, counter, spans)
        )
        if windows is not None:
            return windows
        _LOG.debug("Token offsets disagree with counts; counting each range")
    return _split_interval(text, start, end, counter)


_TOP_LEVEL = Depth(n=1)


def window_unit(
    prepared: RepresentationUnit,
    counter: TokenCounter,
    *,
    context_level: ContextLevel = _TOP_LEVEL,
) -> tuple[SearchWindow, ...]:
    """Create adjacent exact source windows without loading any tokenizer or storage."""
    unit = prepared.unit
    if not unit.text:
        return ()
    if not counter.model or not counter.tokenizer_id:
        raise ValueError("Token counter must declare its model and tokenizer identity")
    fingerprint = digest(
        "chunker-v1",
        "exact-windows-v1",
        counter.model,
        counter.tokenizer_id,
        TARGET_TOKENS,
        SOFT_CEILING_TOKENS,
    )
    group = context_key(
        section_path_of(unit.attributes, unit.boundary_key), context_level
    )
    group_key = digest("context-group-v1", unit.extraction_id, group)
    output = []
    for interval in prepared.ownership:
        for start, end, count in _split(
            unit.text, interval.char_start, interval.char_end, counter
        ):
            output.append(
                SearchWindow(
                    chunk_id=chunk_id(unit.unit_id, start, end, fingerprint),
                    unit_id=unit.unit_id,
                    extraction_id=unit.extraction_id,
                    revision_id=prepared.revision_id,
                    unit_ordinal=unit.ordinal,
                    unit_type=unit.unit_type,
                    char_start=start,
                    char_end=end,
                    text=unit.text[start:end],
                    token_count=count,
                    tokenizer_id=counter.tokenizer_id,
                    chunker_fingerprint=fingerprint,
                    boundary_key=unit.boundary_key,
                    context_group_key=group_key,
                    context_key=group,
                    dependency_ids=interval.dependency_ids,
                    title=prepared.title,
                    kind_id=prepared.kind_id,
                    heading_path=unit.heading_path,
                    symbol_path=prepared.symbol_path,
                )
            )
    return tuple(output)
