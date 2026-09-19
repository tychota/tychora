"""Lossless token-counted windows over canonical source text."""

import re

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import chunk_id, digest
from tychora.contracts.indexing import RepresentationUnit, SearchWindow
from tychora.ports.tokens import TokenCounter

TARGET_TOKENS = 512
SOFT_CEILING_TOKENS = 800


def _count(counter: TokenCounter, text: str) -> int:
    count = counter.count(text)
    if type(count) is not int or count < 0:
        raise ValueError("Token counter returned an invalid count")
    return count


def _semantic_boundaries(text: str, start: int, end: int) -> tuple[int, ...]:
    relative = text[start:end]
    return tuple(
        start + match.end()
        for match in re.finditer(
            r"(?:\r\n|\r|\n){2,}|(?<=[.!?])(?:[ \t]+|\r\n|\r|\n)|(?:\r\n|\r|\n)",
            relative,
        )
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
        raise TychoraError(
            "INVALID_REQUEST", "One atomic source codepoint exceeds the token ceiling"
        )
    return best


def _split_interval(
    text: str, start: int, end: int, counter: TokenCounter
) -> tuple[tuple[int, int, int], ...]:
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


def window_unit(
    prepared: RepresentationUnit, counter: TokenCounter
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
    group_key = digest("context-group-v1", unit.extraction_id, unit.boundary_key)
    output = []
    for interval in prepared.ownership:
        for start, end, count in _split_interval(
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
                    dependency_ids=interval.dependency_ids,
                    title=prepared.title,
                    kind_id=prepared.kind_id,
                    heading_path=unit.heading_path,
                    symbol_path=prepared.symbol_path,
                )
            )
    return tuple(output)
