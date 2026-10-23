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
from importlib import import_module
from typing import Any, Literal, cast, get_args

from .base import Contract, NonEmptyStr, NonNegativeInt, PositiveInt

# The stdlib regular-expression parser, used to reject backtracking-prone patterns.
_sre = import_module("re._constants")
_sre_parser = import_module("re._parser")

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


def _children(op: object, arg: Any) -> list[Any]:
    """The sub-patterns of one parsed regular-expression node."""
    parts = {
        _sre.BRANCH: lambda: list(arg[1]),
        _sre.SUBPATTERN: lambda: [arg[3]],
        _sre.MAX_REPEAT: lambda: [arg[2]],
        _sre.MIN_REPEAT: lambda: [arg[2]],
        _sre.POSSESSIVE_REPEAT: lambda: [arg[2]],
        _sre.ASSERT: lambda: [arg[1]],
        _sre.ASSERT_NOT: lambda: [arg[1]],
        _sre.ATOMIC_GROUP: lambda: [arg],
        _sre.GROUPREF_EXISTS: lambda: [x for x in arg[1:] if x is not None],
    }
    return parts[op]() if op in parts else []


def _is_unbounded_repeat(op: object, arg: Any) -> bool:
    return op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT) and arg[1] == _sre.MAXREPEAT


def _starts_with_literal(body: Any) -> bool:
    """Whether every pass of a repeated body must first consume a fixed character."""
    items = list(body)
    while items and items[0][0] is _sre.SUBPATTERN:
        items = list(items[0][1][3])
    return bool(items) and items[0][0] is _sre.LITERAL


def _backtracks(body: Any) -> bool:
    """Whether a repeated body holds an alternation or an unanchored unbounded repeat."""
    stack = [body]
    while stack:
        for op, arg in stack.pop():
            if op is _sre.BRANCH or (
                _is_unbounded_repeat(op, arg) and not _starts_with_literal(body)
            ):
                return True
            stack.extend(_children(op, arg))
    return False


def _catastrophic(tree: Any) -> bool:
    """Whether a repeat nests another repeat or an alternation in a way that explodes."""
    stack = [tree]
    while stack:
        for op, arg in stack.pop():
            repeated = op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT) and arg[1] > 1
            if repeated and _backtracks(arg[2]):
                return True
            stack.extend(_children(op, arg))
    return False


def _check_pattern(pattern: str) -> None:
    """Refuse a pattern that does not compile or can backtrack catastrophically."""
    try:
        tree = _sre_parser.parse(pattern)
        re.compile(pattern)
    except (re.error, RecursionError, OverflowError):
        raise ValueError(f"Invalid heading pattern: {pattern!r}") from None
    if _catastrophic(tree):
        raise ValueError(
            f"Heading pattern may backtrack catastrophically: {pattern!r}; "
            "do not nest repeats or repeat an alternation"
        )


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
        _check_pattern(pattern)
        levels.append(HeadingLevel(pattern=pattern, level=cast(SectionLevel, level)))
    return tuple(levels)


def kind_heading_levels(kind: Mapping[str, Any]) -> tuple[HeadingLevel, ...]:
    """The heading levels a kind definition declares; none when it declares none."""
    return parse_heading_levels(kind.get("heading_levels"))


def heading_level_for(levels: Sequence[HeadingLevel], text: str) -> SectionLevel | None:
    """The level of the first pattern matching a heading's text, if any.

    Only the first ``MAX_HEADING_PATTERN_CHARS`` characters of a longer heading are
    matched, which bounds the work any accepted pattern can do.
    """
    head = text[:MAX_HEADING_PATTERN_CHARS]
    return next((item.level for item in levels if re.match(item.pattern, head)), None)


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
