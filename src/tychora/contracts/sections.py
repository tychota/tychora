"""Section levels, boundary keys and the resolver for context and chunking keys.

A unit's ``boundary_key`` names its innermost enclosing section, table, article,
file or figure as ``"<scheme>:<native_ref>"``. Its ``section_path`` lists the
enclosing sections from the document root (depth 0) to the innermost one; an
entry's depth is its index in the path. Context groups and chunking sections
are derived from the path and a kind's context level, so changing a level never
re-normalizes a document.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast, get_args

from .base import Contract, NonEmptyStr, NonNegativeInt, PositiveInt

__all__ = [
    "BOUNDARY_SCHEMES",
    "DEFAULT_CONTEXT_LEVEL",
    "ROOT_SECTION_KEY",
    "SECTION_LEVELS",
    "BoundaryScheme",
    "ContextLevel",
    "Depth",
    "HeadingLevel",
    "SectionEntry",
    "SectionLevel",
    "boundary_key",
    "chunk_section_key",
    "context_key",
    "dump_section_path",
    "heading_level_for",
    "kind_heading_levels",
    "normalizer_definition",
    "parse_context_level",
    "parse_heading_levels",
    "section_key",
    "section_path_of",
]

SectionLevel = Literal[
    "part", "chapter", "section", "article", "annex", "clause", "file"
]
SECTION_LEVELS: frozenset[str] = frozenset(get_args(SectionLevel))
BoundaryScheme = Literal["file", "section", "table", "article", "figure"]
BOUNDARY_SCHEMES: frozenset[str] = frozenset(get_args(BoundaryScheme))
ROOT_SECTION_KEY = "section:#root"
DEFAULT_CONTEXT_LEVEL = "depth:1"
MAX_HEADING_LEVELS = 32
MAX_HEADING_PATTERN_CHARS = 256
_DEPTH = re.compile(r"depth:([1-9][0-9]{0,2})")


class Depth(Contract):
    """A section depth below the document root; 1 is a top-level chapter."""

    n: PositiveInt


type ContextLevel = SectionLevel | Depth | Literal["document"]


class SectionEntry(Contract):
    """One enclosing section: its boundary key, named level and depth."""

    key: NonEmptyStr
    level: SectionLevel | None = None
    depth: NonNegativeInt


class HeadingLevel(Contract):
    """A kind's pattern naming the level of the headings it matches."""

    pattern: NonEmptyStr
    level: SectionLevel


def parse_context_level(text: str) -> ContextLevel:
    """Parse ``document``, a named level or ``depth:N``; refuse anything else."""
    if text == "document":
        return "document"
    if text in SECTION_LEVELS:
        return cast(SectionLevel, text)
    match = _DEPTH.fullmatch(text)
    if match is None:
        raise ValueError(f"Unknown context level: {text!r}")
    return Depth(n=int(match.group(1)))


def boundary_key(scheme: BoundaryScheme, native_ref: str) -> str:
    """Return ``"<scheme>:<native_ref>"`` for a known scheme and a nonempty reference."""
    if scheme not in BOUNDARY_SCHEMES or not native_ref:
        raise ValueError("A boundary key needs a known scheme and a native reference")
    return f"{scheme}:{native_ref}"


def _index(section_path: Sequence[SectionEntry], level: ContextLevel) -> int:
    """Position of the entry a level selects, with the depth-1 then root fallback."""
    if not section_path:
        raise ValueError("A section path starts with the document root")
    if level == "document":
        return 0
    if isinstance(level, Depth):
        found = next(
            (i for i, item in enumerate(section_path) if item.depth == level.n), None
        )
    else:
        found = next(
            (
                i
                for i in range(len(section_path) - 1, -1, -1)
                if section_path[i].level == level
            ),
            None,
        )
    if found is not None:
        return found
    return next((i for i, item in enumerate(section_path) if item.depth == 1), 0)


def section_key(section_path: Sequence[SectionEntry], level: ContextLevel) -> str:
    """The key of the innermost ancestor at a named level, of the ancestor at a
    depth, or of the root for ``document``; else the depth-1 ancestor, else the root."""
    return section_path[_index(section_path, level)].key


def context_key(
    section_path: Sequence[SectionEntry], context_level: ContextLevel
) -> str:
    """The context group key: the section selected by the kind's context level."""
    return section_key(section_path, context_level)


def chunk_section_key(
    section_path: Sequence[SectionEntry],
    section_depth: int,
    context_level: ContextLevel,
) -> str:
    """The chunking section at ``section_depth``, never coarser than the context key."""
    index = max(
        _index(section_path, Depth(n=section_depth)),
        _index(section_path, context_level),
    )
    return section_path[index].key


def parse_heading_levels(value: object) -> tuple[HeadingLevel, ...]:
    """Validate a kind's ``heading_levels`` list of ``{pattern, level}`` tables."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_HEADING_LEVELS:
        raise ValueError("Heading levels must be a list of at most 32 entries")
    levels = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"pattern", "level"}:
            raise ValueError("A heading level is exactly a pattern and a level")
        pattern, level = item["pattern"], item["level"]
        if (
            not isinstance(pattern, str)
            or not 0 < len(pattern) <= MAX_HEADING_PATTERN_CHARS
        ):
            raise ValueError("A heading pattern is a string of 1 to 256 characters")
        if level not in SECTION_LEVELS:
            raise ValueError(f"Unknown section level: {level!r}")
        try:
            re.compile(pattern)
        except re.error:
            raise ValueError(f"Invalid heading pattern: {pattern!r}") from None
        levels.append(HeadingLevel(pattern=pattern, level=cast(SectionLevel, level)))
    return tuple(levels)


def kind_heading_levels(kind: Mapping[str, Any]) -> tuple[HeadingLevel, ...]:
    """The heading levels a kind definition declares; none when it declares none."""
    return parse_heading_levels(kind.get("heading_levels"))


def heading_level_for(levels: Sequence[HeadingLevel], text: str) -> SectionLevel | None:
    """The level of the first pattern matching a heading's text, if any."""
    return next((item.level for item in levels if re.match(item.pattern, text)), None)


def dump_section_path(path: Sequence[SectionEntry]) -> list[dict[str, Any]]:
    """The JSON form stored in ``attributes.section_path``."""
    return [item.model_dump(mode="json") for item in path]


def section_path_of(
    attributes: Mapping[str, Any], boundary: str
) -> tuple[SectionEntry, ...]:
    """The stored section path, or the boundary key alone as root when none is stored."""
    stored = attributes.get("section_path")
    if not stored:
        return (SectionEntry(key=boundary, depth=0),)
    return tuple(SectionEntry.from_mapping(item) for item in stored)


def normalizer_definition(kind: Mapping[str, Any]) -> dict[str, Any]:
    """The kind fields a normalizer's output depends on; the context level is not one."""
    return {key: value for key, value in kind.items() if key != "context_level"}
