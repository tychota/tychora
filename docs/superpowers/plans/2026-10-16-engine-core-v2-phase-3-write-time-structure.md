# Engine Core v2, Phase 3: Write-Time Structure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every evidence unit a real section boundary and a level-tagged section path, count tokens once per interval, and persist at write time the representation facts that read paths re-derive today.

**Architecture:** Normalizers emit `boundary_key = "<scheme>:<native_ref>"` naming the enclosing section, table, article, file or figure, plus `attributes.section_path`. One pure placement walk (`ingestion/sections.py`) assigns both for native, Docling and record units; one resolver (`contracts/sections.py`) derives context and chunking keys from the path and the kind's `context_level`. Windowing tokenises each ownership interval once with offsets when the counter can report them. `prepare_extraction` finds overlaps with a sort-and-sweep, one pure function derives every dependency set from it, and `commit_extraction` writes those sets to `representation_dependencies` and `ownership_intervals`, which phase 4 reads instead of re-validating.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite (STRICT, `WITHOUT ROWID`, `json_each`), HF `tokenizers` (offsets), markdown-it-py, pytest. Hypothesis is not used: property tests are seeded random loops (no new dependency).

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.8 (write side), §4.10 "Boundary keys", "Section levels and context level per kind", "Linear windowing". Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` (authoritative cross-phase names; Task 9 records the changes this plan makes to it). Flow review: D5, C1, C2 (write side), Q3 (write side).

**Base:** `origin/main` at `980b1ac` (phases 1 and 2 merged). Every `file:line` below was re-read at that commit.

## Global Constraints

All constraints of the index apply. Phase-specific, verbatim from the outline:

- Unit identity formulas are unchanged; extraction IDs change only because normalizer fingerprints bump to `docling-normalizer-v3` and `native-normalizer-v2`.
- `boundary_key` must not be a paragraph's own `native_ref` unless the unit is a section, article, table or file root.
- Windowing output is byte-for-byte identical to the current algorithm (lossless contract kept); only the counting algorithm changes.
- The named-level vocabulary is closed: `part`, `chapter`, `section`, `article`, `annex`, `clause`, `file`. Heading patterns that name levels are kind data owned by sub-project 5; this phase applies patterns given to it and ships none beyond tests.
- DDL changes update `SCHEMA_CHECKSUM` in the same commit.

Also binding here (index and phase 2 rulings):

- Every new `TychoraError` site passes `reason=` from `src/tychora/contracts/error_reasons.py`. This phase registers three reasons, each with its test-table entry in `tests/contracts/test_error_reasons.py`: `kind.section_levels_invalid` (Task 4), `tokens.atomic_limit` (Task 5), `kind.unknown` (Task 6). It also uses `canonical.binding_mismatch` and `canonical.row_invalid` (Task 8).
- Adapters may not import new upper-layer modules (import-linter contract `adapters-implement-ports`, no new `ignore_imports`). The only allowed edge used here is the existing `tychora.adapters.sqlite.eligibility -> tychora.indexing.representations`.
- Dead code is a hard gate (G12, vulture, whitelist may only shrink): every new public name has a caller in `src` or is listed in its module's `__all__`.
- No comment or docstring cites a plan task, plan file or review ID (G15). New functions ≤ 80 lines, cognitive complexity ≤ 15, no new function-body imports.
- Commands: `uv run --locked pytest <path> -v`; full suite `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`; health `uv run --locked python -m tools.health check --test-reports without`.
- macOS (development machines): benchmark runs are informative and their `bench-results/` files are never committed; G16–G18 and test durations are never recorded (`tools.health update` runs with `--test-reports without`). Gate verdicts and committed results come from the reference runner class `github-ubuntu-22.04` (`benchmarks/reference-machine.toml`), through the `Benchmarks` workflow that the owner dispatches.

## Review Focus

1. **A Markdown document with no headings, or text before the first heading.** Expect `section:#root` for those units and a working context group. Pinned by `test_text_before_first_heading_uses_root` in Task 2.
2. **Heading levels that skip (H1 then H4) or go back up (H3 then H2).** Expect the nearest preceding heading at or above the unit's level, matching the existing `heading_path` computation, and nesting depths 1, 2, 2, 2. Pinned by `test_skipped_and_rising_heading_levels` in Task 2.
3. **A table split across pages in a Docling document.** Expect each fragment's table unit and row views to share that fragment's `table:` key, and the two fragments to keep distinct keys: joining them stays a reviewed `table-continuation` assertion (see Spec gaps 4). Pinned by `test_split_table_fragments_keep_their_own_table_keys` in Task 3.
4. **A unit longer than 200 sentence boundaries** (records normalizers produce whole-section units). Expect the counter to see at most 2× the unit's characters. Pinned by `test_long_unit_counts_tokens_in_linear_time` in Task 5.
5. **Overlapping locators from two units on the same source region** (nested provisions, a class and its methods, captions over pictures). Expect the sort-and-sweep result to equal the quadratic result on every fixture. Pinned by `test_sweep_equals_quadratic_overlap_on_fixtures` in Task 7.

## Changes from the outline

- **Records boundaries split by pipeline, not by scheme.** Native records (`numbered_provisions@1`, `rule_records@1` in `ingestion/normalize.py`) move into Task 2, Docling records (`ingestion/document_records.py`) into Task 3, because each shares its pipeline's placement walk. Task 4 keeps code and the kind heading patterns. Commit message of Task 4 becomes `feat: emit file boundary keys for code and apply kind heading levels`.
- **`ingestion/assets.py:196` and `ingestion/vision.py:175` are not modified.** They build asset-proxy and pixel-source units, which the spec's scheme table does not list. Proxies keep `boundary_key = native_ref` (checked by `indexing/representations.py:1012`); pixel-source units have no text, so they are never windowed or grouped. Units without a `section_path` resolve to their `boundary_key` (`section_path_of`), which keeps today's per-proxy context group.
- **Extra fingerprint bumps.** `docling-record-normalizer-v3` and `code-normalizer-v2` bump too: their output changes (boundary keys, `section_path`) and an extraction ID must name one output. Normalizer fingerprints digest the kind without its `context_level` (`normalizer_definition`), so the spec's "a change of `context_level` takes effect without re-normalizing" holds.
- **`TokenCounter` is not widened.** `count_with_offsets` lives on a new runtime-checkable `OffsetTokenCounter(TokenCounter, Protocol)`. `Tychora.open(token_counter=...)` accepts any caller counter and 59 test modules define counters with `count` only; a counter without offsets keeps the counted split. The old split therefore stays in `src` as that path instead of living only in a test; the property test compares the two paths.
- **Windowing gate needs a stage.** The wall-clock stage source has no `windowing` row until traces exist (phase 5). Task 5 makes the coordinator emit `windowing` and `normalize_commit` job stages, which the harness already maps to budget rows once `stages.py` names them.
- **`RepresentationRepository.neighbours` moves to phase 4**, its first caller (G12 refuses an uncalled member). `test_neighbours_stay_within_boundary_key` moves with it. The repository gains `write(facts)`, which `commit_extraction` and the coordinator call.
- **Persisted facts are computed in `indexing`, written by the adapter.** The SQLite adapter may not import `indexing`, so the coordinator computes `RepresentationFacts` and passes them to `CorpusStore.commit_extraction(bundle, facts)`; vector-group dependencies are written with their text batch.
- **`_owners_by_locator` lives in `adapters/sqlite/eligibility.py:61-75`** (not `:56-70`), and the overlap code lives in `indexing/representations.py:768-861` (not `:661-731`). Every other reference was re-verified; see each task's **Files**.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/tychora/contracts/sections.py` (create) | Level vocabulary, `Depth`, `ContextLevel`, `SectionEntry`, `HeadingLevel`, boundary keys, the resolver, section-path (de)serialisation, `normalizer_definition` | 1 |
| `src/tychora/ingestion/sections.py` (create) | `Placement`, `Placeable`, `place_units` (the one reading-order walk), Docling, view and figure placements | 2, 3 |
| `src/tychora/ingestion/normalize.py` (modify `:66-73`, `:84-114`) | Native boundary keys, section paths, native records as articles | 2, 4 |
| `src/tychora/ingestion/native.py` (modify `:186-187`) | `native_normalizer_fingerprint` (`native-normalizer-v2`) | 2 |
| `src/tychora/ingestion/documents.py` (modify `:60-63`, `:76-103`, `:157-182`) | Docling sections, tables, pictures, captions; standalone image `figure:` | 3, 4 |
| `src/tychora/ingestion/document_records.py` (modify `:38-47`, `:337-371`) | `docling-normalizer-v3`, `docling-record-normalizer-v3`; Docling record articles | 3, 4 |
| `src/tychora/ingestion/document_views.py` (modify `:28-32`, `:113-138`), `ingestion/composition.py:469` | Composed views keyed by section or table | 3, 4 |
| `src/tychora/adapters/code/normalize.py` (modify `:163-182`, `:268-376`), `adapters/sqlite/validation.py:400-417` | `file:` scheme, `code-normalizer-v2` | 4 |
| `src/tychora/indexing/representations.py` (modify `:227`, `:304`, `:38-49`, `:528-558`, `:694-861`; add facts) | File-key checks; sweep-backed overlaps; `representation_facts` | 4, 7 |
| `src/tychora/configuration/registry.py` (modify `:394-449`, add accessor) | `heading_levels` and `context_level` validation; `Registry.context_level` | 4, 6 |
| `src/tychora/contracts/error_reasons.py` | Three new reasons | 4, 5, 6 |
| `src/tychora/ports/tokens.py` | `OffsetTokenCounter` | 5 |
| `src/tychora/indexing/windows.py` | Offset-counted split, context-key groups | 5, 6 |
| `src/tychora/indexing/groups.py` (modify `:64-72`, `:92`, `:193-199`; add `group_facts`) | Reuse counts; group by `context_key`; group dependency facts | 5, 6, 8 |
| `src/tychora/application/default_bindings.py:50-79`, `src/tychora/interfaces/bench.py:58-65` | Offsets for the Voyage and bench counters | 5 |
| `src/tychora/indexing/coordinator.py` (`:385-391`, `:415-489`, `:602-635`, `:729-794`, `:1176-1189`) | Stage events, context level, facts at commit, group facts with the text batch | 5, 6, 8 |
| `src/tychora/application/sources.py:850-857`, `src/tychora/evaluation/bench/stages.py:12-19` | Job stages for the windowing gate | 5 |
| `src/tychora/contracts/indexing.py` (modify `SearchWindow` `:89-133`; add facts contracts) | `SearchWindow.context_key`; `OwnerDependencies`, `IntervalRow`, `RepresentationFacts` | 6, 7 |
| `src/tychora/indexing/intervals.py` (create) | `overlapping_pairs` sort-and-sweep | 7 |
| `src/tychora/adapters/sqlite/eligibility.py` (delete `:61-75`, rewrite `:497-629`) | Facts from `representation_facts` | 7 |
| `src/tychora/resources/migrations/0001.sql`, `src/tychora/adapters/sqlite/migrations.py:12-16` | Two tables, checksum | 8 |
| `src/tychora/adapters/sqlite/repositories/representations.py` (create), `repositories/__init__.py`, `repositories/unit_of_work.py` | `SqliteRepresentationRepository`; transactions gain `representations` | 8 |
| `src/tychora/ports/repositories.py`, `src/tychora/ports/corpus.py:13` | `UnitRow`, `ChunkRow`, `IntervalRow`, `RepresentationRepository`; `commit_extraction(bundle, facts=None)` | 8 |
| `src/tychora/adapters/sqlite/corpus.py:255-497` | Write facts in the commit transaction | 8 |
| Tests: `tests/contracts/test_sections.py`, `tests/ingestion/test_boundary_keys.py`, `tests/indexing/test_windows_linear.py`, `tests/indexing/test_interval_sweep.py`, `tests/storage/test_representation_facts.py` (create); `tests/retrieval/test_segments.py`, `tests/indexing/test_windows.py`, `tests/indexing/test_groups.py`, `tests/configuration/test_registry.py`, `tests/code/test_structure.py`, `tests/contracts/test_error_reasons.py`, `tests/evaluation/bench/test_measurement.py` (extend) | Tests | 1–8 |

---

### Task 1: Section vocabulary and resolver

**Files:**
- Create: `src/tychora/contracts/sections.py`
- Test: `tests/contracts/test_sections.py` (new)

**Interfaces:**
- Consumes: `tychora.contracts.base.Contract`, `NonEmptyStr`, `NonNegativeInt`, `PositiveInt`.
- Produces (index names exactly, plus helpers later tasks use):
  - `SectionLevel = Literal["part", "chapter", "section", "article", "annex", "clause", "file"]`, `SECTION_LEVELS: frozenset[str]`
  - `class Depth(Contract): n: PositiveInt`
  - `type ContextLevel = SectionLevel | Depth | Literal["document"]`, `DEFAULT_CONTEXT_LEVEL = "depth:1"`
  - `parse_context_level(text: str) -> ContextLevel` (`ValueError` on anything else)
  - `class SectionEntry(Contract): key: NonEmptyStr; level: SectionLevel | None = None; depth: NonNegativeInt` (depth 0 is the document root)
  - `BoundaryScheme = Literal["file", "section", "table", "article", "figure"]`, `boundary_key(scheme, native_ref) -> str`, `ROOT_SECTION_KEY = "section:#root"`
  - `section_key(section_path, level) -> str`, `context_key(section_path, context_level) -> str`, `chunk_section_key(section_path, section_depth, context_level) -> str`
  - `class HeadingLevel(Contract): pattern: NonEmptyStr; level: SectionLevel`, `parse_heading_levels(value: object) -> tuple[HeadingLevel, ...]`, `kind_heading_levels(kind: Mapping[str, Any]) -> tuple[HeadingLevel, ...]`, `heading_level_for(levels, text) -> SectionLevel | None`
  - `dump_section_path(path) -> list[dict[str, Any]]`, `section_path_of(attributes: Mapping[str, Any], boundary: str) -> tuple[SectionEntry, ...]` (a unit with no stored path resolves to its `boundary_key` as root)
  - `normalizer_definition(kind: Mapping[str, Any]) -> dict[str, Any]` (the kind without `context_level`)
  - A `section_path` always starts with the root entry `SectionEntry(key=<document root key>, level=None, depth=0)`, and an entry's `depth` equals its index in the path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/contracts/test_sections.py
"""Section levels, boundary keys and the context and chunking key resolver."""

import random

import pytest

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.sections import (
    ROOT_SECTION_KEY,
    Depth,
    SectionEntry,
    boundary_key,
    chunk_section_key,
    context_key,
    dump_section_path,
    heading_level_for,
    kind_heading_levels,
    normalizer_definition,
    parse_context_level,
    parse_heading_levels,
    section_key,
    section_path_of,
)


def entry(key, depth, level=None):
    return SectionEntry(key=key, level=level, depth=depth)


ROOT = entry(ROOT_SECTION_KEY, 0)
LAW = (
    ROOT,
    entry("section:part-1", 1, "part"),
    entry("section:chapter-2", 2, "chapter"),
    entry("article:art-7", 3, "article"),
    entry("article:clause-7-1", 4, "clause"),
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("document", "document"),
        ("article", "article"),
        ("file", "file"),
        ("depth:1", Depth(n=1)),
        ("depth:12", Depth(n=12)),
    ],
)
def test_parse_context_level_accepts_named_depth_and_document(text, expected):
    assert parse_context_level(text) == expected


@pytest.mark.parametrize(
    "text", ["depth:0", "depth:x", "depth:", "depth:01", "Article", "paragraph", ""]
)
def test_parse_context_level_rejects_unknown_values(text):
    with pytest.raises(ValueError):
        parse_context_level(text)


def test_boundary_key_joins_scheme_and_native_ref():
    assert boundary_key("section", "block:3") == "section:block:3"
    assert boundary_key("file", "src/a.py") == "file:src/a.py"
    with pytest.raises(ValueError):
        boundary_key("paragraph", "block:3")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        boundary_key("table", "")


def test_section_key_named_level_returns_innermost_match():
    assert section_key(LAW, "article") == "article:art-7"
    assert section_key(LAW, "chapter") == "section:chapter-2"
    nested = (*LAW, entry("article:art-7-bis", 5, "article"))
    assert section_key(nested, "article") == "article:art-7-bis"


def test_section_key_depth_returns_ancestor_at_depth():
    assert section_key(LAW, Depth(n=1)) == "section:part-1"
    assert section_key(LAW, Depth(n=3)) == "article:art-7"


def test_section_key_falls_back_to_depth_one_then_root():
    assert section_key(LAW, "annex") == "section:part-1"
    assert section_key(LAW, Depth(n=9)) == "section:part-1"
    assert section_key((ROOT,), "article") == ROOT_SECTION_KEY
    assert section_key((ROOT,), Depth(n=1)) == ROOT_SECTION_KEY


def test_section_key_document_returns_root_key():
    assert section_key(LAW, "document") == ROOT_SECTION_KEY
    assert section_key((entry("file:notes.txt", 0),), "document") == "file:notes.txt"


def test_section_key_requires_a_root():
    with pytest.raises(ValueError):
        section_key((), "document")


def test_context_key_examples():
    legislation = (
        ROOT,
        entry("section:title-1", 1),
        entry("article:a-12", 2, "article"),
    )
    assert context_key(legislation, parse_context_level("article")) == "article:a-12"
    standard = (
        ROOT,
        entry("section:s-4", 1, "section"),
        entry("section:c-4-2", 2, "clause"),
        entry("section:c-4-2-1", 3),
    )
    assert context_key(standard, parse_context_level("clause")) == "section:c-4-2"
    book = (ROOT, entry("section:ch-3", 1, "chapter"), entry("section:sec-3-2", 2))
    assert context_key(book, parse_context_level("depth:1")) == "section:ch-3"
    code = (ROOT, entry("file:src/a.py", 1, "file"))
    assert context_key(code, parse_context_level("file")) == "file:src/a.py"


def _random_path(rng: random.Random) -> tuple[SectionEntry, ...]:
    levels = [None, "part", "chapter", "section", "article", "annex", "clause", "file"]
    path = [ROOT]
    for depth in range(1, rng.randint(1, 7)):
        path.append(entry(f"section:{depth}-{rng.randrange(1000)}", depth, rng.choice(levels)))
    return tuple(path)


def test_chunk_section_key_never_coarser_than_context_key():
    rng = random.Random(20261016)
    named = ["document", "part", "chapter", "section", "article", "annex", "clause", "file"]
    for _ in range(1000):
        path = _random_path(rng)
        depth_of = {item.key: item.depth for item in path}
        level = rng.choice([*named, Depth(n=rng.randint(1, 8))])
        section_depth = rng.randint(1, 8)
        chunk = chunk_section_key(path, section_depth, level)
        context = context_key(path, level)
        assert depth_of[chunk] >= depth_of[context]
        by_depth = section_key(path, Depth(n=section_depth))
        if depth_of[by_depth] >= depth_of[context]:
            assert chunk == by_depth


def test_heading_levels_name_matching_headings():
    levels = parse_heading_levels(
        [
            {"pattern": r"^Article \d+", "level": "article"},
            {"pattern": r"^Annex\b", "level": "annex"},
        ]
    )
    assert heading_level_for(levels, "Article 12 Scope") == "article"
    assert heading_level_for(levels, "Annex II") == "annex"
    assert heading_level_for(levels, "Introduction") is None
    assert parse_heading_levels(None) == ()
    frozen = FrozenJsonMap({"heading_levels": [{"pattern": "^A", "level": "part"}]})
    assert kind_heading_levels(frozen)[0].level == "part"
    assert kind_heading_levels({}) == ()


@pytest.mark.parametrize(
    "value",
    [
        "^Article",
        [{"pattern": "(", "level": "article"}],
        [{"pattern": "^A", "level": "paragraph"}],
        [{"pattern": "", "level": "article"}],
        [{"pattern": "^A", "level": "article", "extra": 1}],
        [{"pattern": "a" * 257, "level": "article"}],
        [{"pattern": "^A", "level": "article"}] * 33,
    ],
)
def test_heading_levels_reject_invalid_definitions(value):
    with pytest.raises(ValueError):
        parse_heading_levels(value)


def test_section_path_round_trips_through_attributes():
    stored = FrozenJsonMap({"section_path": dump_section_path(LAW)})
    assert section_path_of(stored, "ignored") == LAW


def test_missing_section_path_is_the_boundary_key_alone():
    assert section_path_of({}, "asset-proxy:abc") == (entry("asset-proxy:abc", 0),)


def test_normalizer_definition_ignores_only_the_context_level():
    kind = {
        "id": "law@1",
        "normalizer": "numbered_provisions@1",
        "context_level": "article",
        "heading_levels": [],
    }
    assert normalizer_definition(kind) == {
        "id": "law@1",
        "normalizer": "numbered_provisions@1",
        "heading_levels": [],
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_sections.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.contracts.sections'`.

- [ ] **Step 3: Implement the module**

```python
# src/tychora/contracts/sections.py
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

SectionLevel = Literal["part", "chapter", "section", "article", "annex", "clause", "file"]
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


def context_key(section_path: Sequence[SectionEntry], context_level: ContextLevel) -> str:
    """The context group key: the section selected by the kind's context level."""
    return section_key(section_path, context_level)


def chunk_section_key(
    section_path: Sequence[SectionEntry], section_depth: int, context_level: ContextLevel
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
        if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_HEADING_PATTERN_CHARS:
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


def section_path_of(attributes: Mapping[str, Any], boundary: str) -> tuple[SectionEntry, ...]:
    """The stored section path, or the boundary key alone as root when none is stored."""
    stored = attributes.get("section_path")
    if not stored:
        return (SectionEntry(key=boundary, depth=0),)
    return tuple(SectionEntry.from_mapping(item) for item in stored)


def normalizer_definition(kind: Mapping[str, Any]) -> dict[str, Any]:
    """The kind fields a normalizer's output depends on; the context level is not one."""
    return {key: value for key, value in kind.items() if key != "context_level"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/contracts/test_sections.py -v`
Expected: PASS (31 test cases).

- [ ] **Step 5: Commit**

```bash
git add src/tychora/contracts/sections.py tests/contracts/test_sections.py
git commit -m "feat: add section levels and boundary key resolver"
```

---

### Task 2: Native Markdown, text and record boundaries

**Files:**
- Create: `src/tychora/ingestion/sections.py`
- Modify: `src/tychora/ingestion/native.py:186-187` (fingerprint), `src/tychora/ingestion/normalize.py:1-31` (imports), `:65-72` (fingerprint check), `:84-114` (unit construction)
- Test: `tests/ingestion/test_boundary_keys.py` (new), `tests/retrieval/test_segments.py` (add one test)

The heading stack of `parse_markdown` (`ingestion/native.py:106-110`) already computes `heading_path` from the nearest preceding heading at or above each level; the placement walk repeats that rule on the normalized units, which also covers the record units `prepare_records` merges or keeps (`ingestion/normalize.py:346-560`).

**Interfaces:**
- Consumes (Task 1): `ROOT_SECTION_KEY`, `SectionEntry`, `HeadingLevel`, `boundary_key`, `dump_section_path`, `heading_level_for`, `normalizer_definition`, `section_path_of`.
- Produces:
  - `ingestion/sections.py`: `@dataclass(frozen=True) class Placement: boundary_key: str; section_path: tuple[SectionEntry, ...]; def attribute(self) -> list[dict[str, Any]]`; `@dataclass(frozen=True) class Placeable: native_ref: str; heading_level: int = 0; text: str = ""; unit_type: str = ""; parent_ref: str | None = None; record: bool = False`; `place_units(items: Iterable[Placeable], root_key: str, headings: Sequence[HeadingLevel] = ()) -> dict[str, Placement]`; `ARTICLE_NORMALIZERS = frozenset({"numbered_provisions@1", "rule_records@1"})`.
  - Rule of `place_units`, in reading order: a heading closes open sections at or below its level and opens `section:<its ref>` at depth `len(open) + 1`; a record opens `article:<its ref>` (level `article`, or `clause` inside its parent record); a table is keyed `table:<its ref>` inside the current path; anything else takes the innermost open key.
  - `ingestion/native.py`: `native_normalizer_fingerprint(kind: Mapping[str, Any], registry: Registry) -> str` = `digest("native-normalizer-v2", normalizer_definition(kind), registry.schema_digests)`.
  - `ingestion/normalize.py`: `native_placements(units: Sequence[ParsedUnit], registration: NativeRegistration, kind: Mapping[str, Any]) -> dict[str, Placement]`. Root key: `section:#root` for Markdown, `file:<source_key>` for text and JSON. Every native unit carries `attributes.section_path`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingestion/test_boundary_keys.py
"""Boundary keys and section paths emitted by the normalizers."""

from pathlib import Path

import pytest

from tychora.contracts.identity import digest
from tychora.contracts.sections import (
    BOUNDARY_SCHEMES,
    ROOT_SECTION_KEY,
    normalizer_definition,
    section_path_of,
)
from tychora.ingestion.native import native_normalizer_fingerprint
from tychora.ingestion.normalize import normalize

FIXTURES = Path(__file__).parents[1] / "fixtures/documents"


def _bundle(native_case, text, **options):
    parsed, _, registry = native_case(text, **options)
    return normalize(parsed, parsed.registration.revision.revision_id, registry)


def _units(native_case, text, **options):
    return {unit.text.strip(): unit for unit in _bundle(native_case, text, **options).units}


def _own(unit, scheme="section"):
    return f"{scheme}:{unit.native_ref}"


def _path(unit):
    return [
        (item.key, item.level, item.depth)
        for item in section_path_of(unit.attributes, unit.boundary_key)
    ]


def _starting(bundle, prefix):
    return next(unit for unit in bundle.units if unit.text.lstrip().startswith(prefix))


def assert_boundary_contract(bundle):
    """A unit keyed by its own native_ref is the root of what the key names."""
    for unit in bundle.units:
        scheme, _, ref = unit.boundary_key.partition(":")
        assert scheme in BOUNDARY_SCHEMES, unit.boundary_key
        path = section_path_of(unit.attributes, unit.boundary_key)
        assert [item.depth for item in path] == list(range(len(path)))
        if ref == unit.native_ref:
            assert scheme in {"table", "figure"} or path[-1].key == unit.boundary_key, (
                unit.unit_type,
                unit.boundary_key,
            )


def test_markdown_paragraphs_share_enclosing_heading_key(native_case):
    units = _units(
        native_case,
        "# Pumps\n\nCheck the housing.\n\nInspect seals.\n\n# Valves\n\nReplace valves.\n",
    )
    pumps, valves = units["# Pumps"], units["# Valves"]
    assert pumps.boundary_key == _own(pumps)
    assert units["Check the housing."].boundary_key == _own(pumps)
    assert units["Inspect seals."].boundary_key == _own(pumps)
    assert units["Replace valves."].boundary_key == _own(valves)
    assert _path(units["Inspect seals."]) == [
        (ROOT_SECTION_KEY, None, 0),
        (_own(pumps), None, 1),
    ]


def test_text_before_first_heading_uses_root(native_case):
    units = _units(native_case, "Preamble text.\n\n# First\n\nBody.\n")
    assert units["Preamble text."].boundary_key == ROOT_SECTION_KEY
    assert _path(units["Preamble text."]) == [(ROOT_SECTION_KEY, None, 0)]
    assert units["Body."].boundary_key == _own(units["# First"])
    plain = _units(native_case, "One paragraph.\n\nAnother paragraph.\n")
    assert {unit.boundary_key for unit in plain.values()} == {ROOT_SECTION_KEY}


def test_skipped_and_rising_heading_levels(native_case):
    text = (
        "# Guide\n\n#### Deep\n\nDeep body.\n\n### Middle\n\nMiddle body.\n\n"
        "## Upper\n\nUpper body.\n\n# Next\n\nNext body.\n"
    )
    units = _units(native_case, text)
    guide = units["# Guide"]
    root = (ROOT_SECTION_KEY, None, 0)
    for heading, body in (
        ("#### Deep", "Deep body."),
        ("### Middle", "Middle body."),
        ("## Upper", "Upper body."),
    ):
        assert units[body].boundary_key == _own(units[heading])
        assert _path(units[body]) == [root, (_own(guide), None, 1), (_own(units[heading]), None, 2)]
    assert _path(units["Next body."]) == [root, (_own(units["# Next"]), None, 1)]
    assert units["Deep body."].heading_path == ("Guide", "Deep")


def test_markdown_table_uses_table_scheme(native_case):
    units = _units(
        native_case,
        "# Results\n\n| Pump | Hours |\n| --- | --- |\n| A | 3 |\n\nAfter the table.\n",
    )
    table = next(unit for unit in units.values() if unit.unit_type == "table")
    results = units["# Results"]
    assert table.boundary_key == _own(table, "table")
    assert _path(table)[-1][0] == _own(results)
    assert units["After the table."].boundary_key == _own(results)


def test_text_without_headings_uses_file_scheme(native_case):
    units = _units(native_case, "# not a heading\nline two\n\nnext\n", format="text")
    assert {unit.boundary_key for unit in units.values()} == {"file:synthetic.text"}
    assert all(_path(unit) == [("file:synthetic.text", None, 0)] for unit in units.values())


def test_native_normalizer_fingerprint_is_v2(native_case):
    parsed, _, registry = native_case("# Manual\n\nBody.\n")
    kind = registry.kinds["manual@1"]
    assert parsed.normalizer_fingerprint == native_normalizer_fingerprint(kind, registry)
    assert parsed.normalizer_fingerprint == digest(
        "native-normalizer-v2", normalizer_definition(kind), registry.schema_digests
    )
    assert parsed.normalizer_fingerprint != digest(
        "native-normalizer-v1", kind, registry.schema_digests
    )


def test_numbered_provisions_are_articles(native_case):
    text = (
        "# Policy\n\n## Article 4\n\nIntro.\n\n1. First numbered paragraph.\n"
        "   1. Nested paragraph.\n\n## Article 5\n\nNext article.\n\n## Annex\n\nAnnex text.\n"
    )
    bundle = _bundle(native_case, text, kind="law@1")
    policy = _starting(bundle, "# Policy")
    article = _starting(bundle, "## Article 4")
    first = _starting(bundle, "1. First")
    nested = next(unit for unit in bundle.units if unit.text.startswith("   1. Nested"))
    annex_text = _starting(bundle, "Annex text.")
    root = (ROOT_SECTION_KEY, None, 0)
    section = (_own(policy), None, 1)
    assert article.boundary_key == _own(article, "article")
    assert _path(article) == [root, section, (_own(article, "article"), "article", 2)]
    assert first.boundary_key == _own(first, "article")
    assert _path(first)[-1] == (_own(first, "article"), "clause", 3)
    assert _path(nested)[-2:] == [
        (_own(first, "article"), "clause", 3),
        (_own(nested, "article"), "clause", 4),
    ]
    assert annex_text.boundary_key == _own(_starting(bundle, "## Annex"))
    assert_boundary_contract(bundle)


def test_rule_records_are_articles(native_case):
    bundle = _bundle(native_case, (FIXTURES / "standards.md").read_text(), kind="standard@1")
    rules = [unit for unit in bundle.units if unit.unit_type == "rule"]
    assert len(rules) == 2
    for rule in rules:
        assert rule.boundary_key == _own(rule, "article")
        assert _path(rule)[-1] == (rule.boundary_key, "article", 2)


@pytest.mark.parametrize(
    "text,kind,source_format",
    [
        ((FIXTURES / "standards.md").read_text(), "standard@1", "markdown"),
        ((FIXTURES / "template.md").read_text(), "template@1", "markdown"),
        (
            "# Manual\n\nIntro.\n\n## Part\n\n- item one\n- item two\n\n| A | B |\n| - | - |\n| 1 | 2 |\n",
            "manual@1",
            "markdown",
        ),
        (
            "## Article 4\n\nIntroductory text.\n\n1. First.\n   1. Nested.\n\n## Article 5\n\nNext.\n",
            "law@1",
            "markdown",
        ),
        ("Plain line.\n\nSecond paragraph.\n", "manual@1", "text"),
    ],
)
def test_boundary_key_is_never_a_paragraph_ref(native_case, text, kind, source_format):
    assert_boundary_contract(_bundle(native_case, text, kind=kind, format=source_format))
```

Append to `tests/retrieval/test_segments.py` (the module already imports `prepare_extraction`, `eligibility_view`, `assemble_segments`, `config`, `Counter` and defines `_ranked`):

```python
from tests.ingestion.conftest import native_case  # noqa: E402,F401  (fixture)


def test_neighbour_context_attaches_within_native_section(native_case, tmp_path):
    from tychora.ingestion.normalize import normalize

    text = (
        "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
        "Record the outcome.\n\n# Valves\n\nReplace worn valves.\n"
    )
    parsed, _, registry = native_case(text)
    rows = normalize(parsed, parsed.registration.revision.revision_id, registry)
    prepared = prepare_extraction(rows)
    units = rows.units
    view = eligibility_view(
        revision_ids=frozenset({rows.revision.revision_id}),
        context_unit_ids=frozenset(unit.unit_id for unit in units),
        chunk_ids=frozenset(),
    )
    segment = assemble_segments(
        _ranked(rows, prepared, (units[2],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == (units[1].unit_id, units[3].unit_id)
    edge = assemble_segments(
        _ranked(rows, prepared, (units[3],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert edge.context_only_unit_ids == (units[2].unit_id,)
```

Put the `native_case` import with the other imports at the top of the module (drop the `noqa` then); it registers the fixture for this module.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/ingestion/test_boundary_keys.py tests/retrieval/test_segments.py::test_neighbour_context_attaches_within_native_section -v`
Expected: FAIL. `test_boundary_keys.py` fails at collection with `ImportError: cannot import name 'native_normalizer_fingerprint'`; the segment test fails with `assert () == (...)` (each native paragraph is its own boundary today, so no neighbour shares it).

- [ ] **Step 3: Add the placement walk**

```python
# src/tychora/ingestion/sections.py
"""Boundary keys and section paths of units visited in reading order."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tychora.contracts.sections import (
    HeadingLevel,
    SectionEntry,
    boundary_key,
    dump_section_path,
    heading_level_for,
)

ARTICLE_NORMALIZERS = frozenset({"numbered_provisions@1", "rule_records@1"})


@dataclass(frozen=True)
class Placement:
    """Where one unit sits: its innermost boundary key and its enclosing sections."""

    boundary_key: str
    section_path: tuple[SectionEntry, ...]

    def attribute(self) -> list[dict[str, Any]]:
        """The ``section_path`` attribute value stored on the unit."""
        return dump_section_path(self.section_path)


@dataclass(frozen=True)
class Placeable:
    """What placement reads from one unit."""

    native_ref: str
    heading_level: int = 0
    text: str = ""
    unit_type: str = ""
    parent_ref: str | None = None
    record: bool = False


def _record(
    item: Placeable, path: tuple[SectionEntry, ...], parent: Placement | None
) -> Placement:
    """A record is an article, or a clause nested in its parent record."""
    base = parent.section_path if parent is not None else path
    entry = SectionEntry(
        key=boundary_key("article", item.native_ref),
        level="clause" if parent is not None else "article",
        depth=len(base),
    )
    return Placement(entry.key, (*base, entry))


def place_units(
    items: Iterable[Placeable],
    root_key: str,
    headings: Sequence[HeadingLevel] = (),
) -> dict[str, Placement]:
    """Place units read in order.

    A heading closes the open sections at or below its level and opens its own
    section; a record opens an article; a table is its own boundary inside the
    current section; any other unit takes the innermost open section.
    """
    root = SectionEntry(key=root_key, depth=0)
    open_sections: list[tuple[int, SectionEntry]] = []
    records: dict[str, Placement] = {}
    placements: dict[str, Placement] = {}
    for item in items:
        if item.heading_level:
            while open_sections and open_sections[-1][0] >= item.heading_level:
                open_sections.pop()
        path = (root, *(entry for _, entry in open_sections))
        if item.record:
            placement = _record(item, path, records.get(item.parent_ref or ""))
            records[item.native_ref] = placement
        elif item.heading_level:
            entry = SectionEntry(
                key=boundary_key("section", item.native_ref),
                level=heading_level_for(headings, item.text),
                depth=len(path),
            )
            open_sections.append((item.heading_level, entry))
            placement = Placement(entry.key, (*path, entry))
        elif item.unit_type == "table":
            placement = Placement(boundary_key("table", item.native_ref), path)
        else:
            placement = Placement(path[-1].key, path)
        placements[item.native_ref] = placement
    return placements
```

- [ ] **Step 4: Bump the native fingerprint**

In `src/tychora/ingestion/native.py`, add `from tychora.contracts.sections import normalizer_definition` to the imports and this function after `native_parser_fingerprint`:

```python
def native_normalizer_fingerprint(kind: Mapping[str, Any], registry: Registry) -> str:
    """Identity of the native normalizer's output for one kind definition."""
    return digest(
        "native-normalizer-v2", normalizer_definition(kind), registry.schema_digests
    )
```

Replace line 187:

```python
        normalizer = digest("native-normalizer-v1", kind, registry.schema_digests)
```

with:

```python
        normalizer = native_normalizer_fingerprint(kind, registry)
```

- [ ] **Step 5: Place native units**

In `src/tychora/ingestion/normalize.py`, add to the imports:

```python
from tychora.contracts.sections import ROOT_SECTION_KEY, boundary_key

from .native import native_normalizer_fingerprint
from .sections import ARTICLE_NORMALIZERS, Placeable, Placement, place_units
```

Add this function before `normalize`:

```python
def native_placements(
    units: Sequence[ParsedUnit],
    registration: NativeRegistration,
    kind: Mapping[str, Any],
) -> dict[str, Placement]:
    """Boundary keys and section paths of native units, in reading order."""
    articles = kind["normalizer"] in ARTICLE_NORMALIZERS
    root = (
        ROOT_SECTION_KEY
        if registration.format == "markdown"
        else boundary_key("file", registration.source.source_key)
    )
    return place_units(
        (
            Placeable(
                native_ref=unit.native_ref,
                heading_level=unit.heading_level,
                text=unit.text,
                unit_type=unit.unit_type,
                parent_ref=unit.parent_ref,
                record=articles
                and (unit.identifier is not None or unit.token_kind == "json_record"),
            )
            for unit in units
        ),
        root,
    )
```

(`Sequence` joins the existing `collections.abc` import.) Replace the fingerprint check (lines 65-72):

```python
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    if parsed.normalizer_fingerprint != identity.digest(
        "native-normalizer-v1", kind, registry.schema_digests
    ):
```

with:

```python
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    if parsed.normalizer_fingerprint != native_normalizer_fingerprint(kind, registry):
```

Before `for ordinal, unit in enumerate(native_units):` add `placements = native_placements(native_units, registration, kind)`, and in the `EvidenceUnit(...)` call replace

```python
                boundary_key=unit.native_ref,
```

with `boundary_key=placements[unit.native_ref].boundary_key,`, and add `"section_path": placements[unit.native_ref].attribute(),` to the attributes mapping (after `"cells": unit.cells,`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/ingestion/test_boundary_keys.py tests/retrieval/test_segments.py -v`
Expected: PASS. `test_neighbour_context_attaches_within_native_section` passes because `_same_section` (`retrieval/segments.py:392-406`) compares boundary keys for non-document units.

- [ ] **Step 7: Run the affected suites**

Run: `uv run --locked pytest tests/ingestion tests/retrieval tests/indexing tests/storage -q`
Expected: PASS. `tests/ingestion/test_native.py::test_article_preserves_numbered_descendants_and_subordinate_heading` still sees 7 distinct boundary keys (every provision is its own article). If a test pins a native unit's `boundary_key` to its `native_ref`, change the expectation to the section key, not the code.

- [ ] **Step 8: Commit**

```bash
git add src/tychora/ingestion/sections.py src/tychora/ingestion/native.py \
  src/tychora/ingestion/normalize.py tests/ingestion/test_boundary_keys.py \
  tests/retrieval/test_segments.py
git commit -m "feat: emit section boundary keys for native documents"
```

---

### Task 3: Docling section, table, picture, caption, figure and record boundaries

**Files:**
- Modify: `src/tychora/ingestion/sections.py` (add Docling, view and figure placements)
- Modify: `src/tychora/ingestion/document_records.py:38-47` (`document_normalizer_fingerprint`), `:337-371` (record units)
- Modify: `src/tychora/ingestion/documents.py:60-63` (fingerprint check), `:66-103` (units), `:157-182` (standalone image)
- Modify: `src/tychora/ingestion/document_views.py:28-32`, `:43-141` (views)
- Test: `tests/ingestion/test_boundary_keys.py` (extend), `tests/retrieval/test_segments.py` (add one test)

Docling headers come from `adapters/docling/mapping.py:135-139` (`level` of `section_header` and `title` items, default 1); `DocumentUnit.heading_level` carries it. Tables are one `table` item; row views (`docling-view:table_row:<ref>:<row>`) and table views are built in `document_views.source_views`. Multi-page tables stay separate fragments joined only by a `table-continuation` assertion (`ingestion/tables.py`).

**Interfaces:**
- Consumes: Task 2 `Placement`, `Placeable`, `place_units`, `ARTICLE_NORMALIZERS`; Task 1 `ROOT_SECTION_KEY`, `normalizer_definition`.
- Produces:
  - `ingestion/sections.py`: `document_placements(items: Iterable[DocumentUnit], headings: Sequence[HeadingLevel] = ()) -> dict[str, Placement]`; `view_placement(item: Placement, kind: str, native_ref: str) -> Placement` (`table` and `table_row` views are keyed `table:<native_ref>`); `figure_placement(native_ref: str) -> Placement` (`figure:<ref>`, alone at depth 0); `TABLE_VIEW_TYPES = frozenset({"table", "table_row"})`.
  - `document_normalizer_fingerprint(kind, registry, *, legacy_v1=False)` returns `docling-normalizer-v3` / `docling-record-normalizer-v3` over `normalizer_definition(kind)`; the `legacy_v1` values are unchanged (`-v1` over the whole kind).
  - `source_views(parsed, context, base, headings: Sequence[HeadingLevel] = ())` (Task 4 passes the kind's levels).

- [ ] **Step 1: Write the failing tests**

Append to `tests/ingestion/test_boundary_keys.py`, merging the new imports into the module's import block:

```python
import base64
import io
import json

from tests.ingestion.test_docling_mapping import mapping_case
from tychora.adapters.docling.mapping import map_document
from tychora.ingestion.document_records import document_normalizer_fingerprint
from tychora.ingestion.sections import (
    document_placements,
    figure_placement,
    view_placement,
)


def _text(ref, label, text, level=None):
    item = {
        "self_ref": ref,
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": label,
        "text": text,
        "orig": text,
        "prov": [],
    }
    if level is not None:
        item["level"] = level
    return item


def _table(ref, rows):
    return {
        "self_ref": ref,
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "table",
        "prov": [],
        "data": {
            "num_rows": len(rows),
            "num_cols": len(rows[0]),
            "table_cells": [
                {
                    "text": value,
                    "start_row_offset_idx": row,
                    "end_row_offset_idx": row + 1,
                    "start_col_offset_idx": column,
                    "end_col_offset_idx": column + 1,
                }
                for row, values in enumerate(rows)
                for column, value in enumerate(values)
            ],
        },
    }


def _export(texts, tables=(), order=None):
    """A synthetic Docling export whose body lists the items in reading order."""
    export, original = mapping_case()
    refs = order or [item["self_ref"] for item in (*texts, *tables)]
    export["texts"] = list(texts)
    export["tables"] = list(tables)
    export["body"]["children"] = [{"$ref": ref} for ref in refs]
    return export, original


SECTIONED = (
    _text("#/texts/0", "title", "Pump manual"),
    _text("#/texts/1", "text", "Read this first."),
    _text("#/texts/2", "section_header", "Seals", level=1),
    _text("#/texts/3", "text", "Inspect seals quarterly."),
    _text("#/texts/4", "section_header", "Seal rings", level=2),
    _text("#/texts/5", "text", "Replace worn rings."),
    _text("#/texts/6", "section_header", "Valves", level=1),
    _text("#/texts/7", "caption", "Table 1 Valve hours"),
)


def test_docling_units_use_enclosing_section_header():
    export, original = _export(SECTIONED)
    placements = document_placements(map_document(export, original).units)
    assert placements["#/texts/1"].boundary_key == "section:#/texts/0"
    assert placements["#/texts/3"].boundary_key == "section:#/texts/2"
    assert placements["#/texts/5"].boundary_key == "section:#/texts/4"
    assert [entry.key for entry in placements["#/texts/5"].section_path] == [
        "section:#root",
        "section:#/texts/2",
        "section:#/texts/4",
    ]
    assert placements["#/texts/6"].boundary_key == "section:#/texts/6"
    assert [entry.depth for entry in placements["#/texts/6"].section_path] == [0, 1]
    assert placements["#/texts/7"].boundary_key == "section:#/texts/6"


def test_split_table_fragments_keep_their_own_table_keys():
    texts = (_text("#/texts/0", "section_header", "Hours", level=1),)
    tables = (
        _table("#/tables/0", [["Pump", "Hours"], ["A", "3"]]),
        _table("#/tables/1", [["Pump", "Hours"], ["B", "5"]]),
    )
    export, original = _export(texts, tables)
    placements = document_placements(map_document(export, original).units)
    first, second = placements["#/tables/0"], placements["#/tables/1"]
    assert first.boundary_key == "table:#/tables/0"
    assert second.boundary_key == "table:#/tables/1"
    assert first.section_path[-1].key == "section:#/texts/0"
    for kind in ("table", "table_row"):
        assert view_placement(first, kind, "#/tables/0").boundary_key == "table:#/tables/0"
        assert view_placement(second, kind, "#/tables/1").boundary_key == "table:#/tables/1"
    caption = placements["#/texts/0"]
    assert view_placement(caption, "caption", "#/texts/0") == caption


def test_standalone_image_uses_figure_scheme():
    placement = figure_placement("#/original-image")
    assert placement.boundary_key == "figure:#/original-image"
    assert [(e.key, e.depth) for e in placement.section_path] == [
        ("figure:#/original-image", 0)
    ]


def test_docling_normalizer_fingerprints_are_v3(native_case):
    _, _, registry = native_case("# Manual\n")
    structured, records = registry.kinds["manual@1"], registry.kinds["standard@1"]
    for kind, family in (
        (structured, "docling-normalizer"),
        (records, "docling-record-normalizer"),
    ):
        assert document_normalizer_fingerprint(kind, registry) == digest(
            f"{family}-v3", normalizer_definition(kind), registry.schema_digests
        )
        assert document_normalizer_fingerprint(kind, registry, legacy_v1=True) == digest(
            f"{family}-v1", kind, registry.schema_digests
        )


@pytest.fixture
def docling_bundle(execution, rich_capture):
    """The registry and bundle of a retained synthetic export with sections, a picture and a table."""
    from tychora.adapters.docling.parser import INVENTORY, DoclingParser
    from tychora.application.owner import StagedOutput
    from tychora.configuration.registry import Registry
    from tychora.contracts.identity import canonical_bytes

    pixels = (FIXTURES / "rich-figure.png").read_bytes()
    picture = {
        "self_ref": "#/pictures/0",
        "parent": {"$ref": "#/body"},
        "children": [],
        "label": "picture",
        "prov": [],
        "captions": [{"$ref": "#/texts/7"}],
        "image": {
            "uri": "data:image/png;base64," + base64.b64encode(pixels).decode(),
            "size": {"width": 640.0, "height": 320.0},
            "dpi": 72,
            "mimetype": "image/png",
        },
    }
    texts = (*SECTIONED[:7], _text("#/texts/7", "caption", "Figure 1 Seal ring"))
    order = [item["self_ref"] for item in texts[:4]] + ["#/pictures/0", "#/texts/7"]
    order += [item["self_ref"] for item in texts[4:7]] + ["#/tables/0"]
    export, _ = _export(texts, (_table("#/tables/0", [["Valve", "Hours"], ["V1", "7"]]),), order)
    export["pictures"] = [picture]
    registry = Registry.from_config(execution.config)
    parser = DoclingParser(
        execution.owner, execution.registration, Path("/nonexistent-explicit-model-bundle")
    )
    payload = canonical_bytes(export)
    artifact = execution.owner.run_blocking_sync(
        lambda: parser._retain(
            (
                (
                    StagedOutput("document.json", "application/json", len(payload)),
                    io.BytesIO(payload),
                ),
            ),
            rich_capture,
            execution.config,
            registry,
            registry.kinds["manual@1"],
            json.loads(INVENTORY.read_text()),
        ),
        execution.ctx,
    )
    return registry, normalize(
        artifact, artifact.registration.revision.revision_id, registry
    )


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("document_kind", ["manual@1"], indirect=True)
def test_picture_and_caption_use_enclosing_section(docling_bundle):
    registry, bundle = docling_bundle
    units = {unit.native_ref: unit for unit in bundle.units}
    assert units["#/texts/3"].boundary_key == "section:#/texts/2"
    assert units["#/pictures/0"].boundary_key == "section:#/texts/2"
    assert units["#/texts/7"].boundary_key == "section:#/texts/2"
    assert units["#/tables/0"].boundary_key == "table:#/tables/0"
    assert section_path_of(units["#/tables/0"].attributes, units["#/tables/0"].boundary_key)[-1].key == "section:#/texts/6"
    assert bundle.extraction.normalizer_fingerprint == document_normalizer_fingerprint(
        registry.kinds["manual@1"], registry
    )


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
@pytest.mark.parametrize("document_kind", ["manual@1"], indirect=True)
def test_docling_boundary_key_is_never_a_paragraph_ref(docling_bundle):
    assert_boundary_contract(docling_bundle[1])
```

The synthetic shapes follow `retained_case` and `test_document_table_grid_cannot_be_silently_omitted` in `tests/ingestion/test_docling_mapping.py`; if `map_document` refuses an item, copy that test's item shape rather than relaxing the mapping.

Append to `tests/retrieval/test_segments.py`:

```python
def test_neighbour_context_attaches_within_docling_section(tmp_path):
    section = ("Pumps", "Seals")
    units, rows, prepared, view = _document_case(
        (
            ("before", "Check the housing.", section),
            ("match", "Inspect seals quarterly.", section),
            ("after", "Record the outcome.", section),
        )
    )
    keyed = tuple(
        unit.model_copy(update={"boundary_key": "section:#/texts/9"}) for unit in units
    )
    prepared = prepared.model_copy(
        update={
            "units": tuple(
                rep.model_copy(update={"unit": unit})
                for rep, unit in zip(prepared.units, keyed)
            )
        }
    )
    rows = rows.model_copy(update={"units": keyed})
    segment = assemble_segments(
        _ranked(rows, prepared, (keyed[1],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == (keyed[0].unit_id, keyed[2].unit_id)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/ingestion/test_boundary_keys.py -v -k "docling or table or figure or picture"`
Expected: FAIL at collection with `ImportError: cannot import name 'document_placements' from 'tychora.ingestion.sections'`.

- [ ] **Step 3: Add the Docling placements**

Append to `src/tychora/ingestion/sections.py` (add `from tychora.contracts.documents import DocumentUnit` and `ROOT_SECTION_KEY` to the imports):

```python
TABLE_VIEW_TYPES = frozenset({"table", "table_row"})


def document_placements(
    items: Iterable[DocumentUnit], headings: Sequence[HeadingLevel] = ()
) -> dict[str, Placement]:
    """Place Docling items: section headers open sections, tables are their own boundary."""
    return place_units(
        (
            Placeable(
                native_ref=item.native_ref,
                heading_level=item.heading_level,
                text=item.text,
                unit_type=item.unit_type,
            )
            for item in items
        ),
        ROOT_SECTION_KEY,
        headings,
    )


def view_placement(item: Placement, kind: str, native_ref: str) -> Placement:
    """A table or row view is keyed by its table; any other view keeps its item's place."""
    if kind in TABLE_VIEW_TYPES:
        return Placement(boundary_key("table", native_ref), item.section_path)
    return item


def figure_placement(native_ref: str) -> Placement:
    """A standalone image with no section is its own document root."""
    key = boundary_key("figure", native_ref)
    return Placement(key, (SectionEntry(key=key, depth=0),))
```

- [ ] **Step 4: Bump the Docling fingerprints**

In `src/tychora/ingestion/document_records.py` add `from tychora.contracts.sections import normalizer_definition` and replace lines 38-47:

```python
def document_normalizer_fingerprint(
    kind: Mapping[str, Any], registry: Registry, *, legacy_v1: bool = False
) -> str:
    version = "v1" if legacy_v1 else "v2"
    version = (
        f"docling-normalizer-{version}"
        if kind["normalizer"] == "structured_sections@1"
        else f"docling-record-normalizer-{version}"
    )
    return digest(version, kind, registry.schema_digests)
```

with:

```python
def document_normalizer_fingerprint(
    kind: Mapping[str, Any], registry: Registry, *, legacy_v1: bool = False
) -> str:
    """Identity of the rich-document normalizer's output for one kind definition."""
    family = (
        "docling-normalizer"
        if kind["normalizer"] == "structured_sections@1"
        else "docling-record-normalizer"
    )
    if legacy_v1:
        return digest(f"{family}-v1", kind, registry.schema_digests)
    return digest(f"{family}-v3", normalizer_definition(kind), registry.schema_digests)
```

In `src/tychora/ingestion/documents.py` replace lines 60-63:

```python
    if parsed.normalizer_fingerprint != digest(
        "docling-normalizer-v2", kind, registry.schema_digests
    ):
        raise TychoraError("INTEGRITY_ERROR", "Document normalizer fingerprint differs")
```

with nothing: the check at lines 49-53 already compares the same value through `document_normalizer_fingerprint`.

- [ ] **Step 5: Place Docling units, the standalone image and composed views**

In `src/tychora/ingestion/documents.py` add `from .sections import document_placements, figure_placement`. In `normalize_document`, after `document_units = normalize_native_document_units(parsed)` add `placements = document_placements(document_units)`; in the unit loop replace `boundary_key=unit.native_ref,` with `boundary_key=placements[unit.native_ref].boundary_key,` and add `"section_path": placements[unit.native_ref].attribute(),` to the `attributes` mapping. In `normalize_standalone_image`, compute `placement = figure_placement(image.native_ref)` before the unit, replace `boundary_key=image.native_ref,` with `boundary_key=placement.boundary_key,` and add `"section_path": placement.attribute(),` to its attributes.

In `src/tychora/ingestion/document_records.py` add `from .sections import ARTICLE_NORMALIZERS, Placeable, Placement, place_units` and `ROOT_SECTION_KEY` to the `contracts.sections` import, and this helper before `normalize_document_records`:

```python
def _group_placements(
    groups: Sequence[_Group], strategy: str, headings: Sequence[HeadingLevel] = ()
) -> list[Placement]:
    """Place record groups: numbered provisions and rules are articles."""
    articles = strategy in ARTICLE_NORMALIZERS
    placed = place_units(
        (
            Placeable(
                native_ref=group.native_ref,
                heading_level=group.originals[0].heading_level,
                text=group.originals[0].text,
                unit_type=group.unit_type,
                parent_ref=group.parent_ref,
                record=articles and group.identifier is not None,
            )
            for group in groups
        ),
        ROOT_SECTION_KEY,
        headings,
    )
    return [placed[group.native_ref] for group in groups]
```

(`Sequence` and `HeadingLevel` join the imports.) After `groups = _groups(...)` add `placements = _group_placements(groups, strategy)`; in the loop `for ordinal, group in enumerate(groups):` replace `boundary_key=group.native_ref,` with `boundary_key=placements[ordinal].boundary_key,` and add `"section_path": placements[ordinal].attribute(),` to the attributes.

In `src/tychora/ingestion/document_views.py` change the signature to

```python
def source_views(
    parsed: ExtractionArtifact,
    context: DocumentNormalizationContext,
    base: ExtractionBundle | None,
    headings: Sequence[HeadingLevel] = (),
) -> tuple[ExtractionBundle, tuple[TableFragment, ...]]:
```

add `placements = document_placements(items, headings)` after `items = normalize_native_document_units(parsed)`, and inside `view` compute, before the `EvidenceUnit(...)`:

```python
        placement = view_placement(placements[item.native_ref], kind, item.native_ref)
```

then replace `boundary_key=ref,` with `boundary_key=placement.boundary_key,` and add `"section_path": placement.attribute(),` to the attributes. Imports: `Sequence` from `collections.abc`, `HeadingLevel` from `tychora.contracts.sections`, `document_placements, view_placement` from `.sections`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/ingestion/test_boundary_keys.py tests/retrieval/test_segments.py -v`
Expected: PASS.

- [ ] **Step 7: Run the affected suites**

Run: `uv run --locked pytest tests/ingestion tests/retrieval tests/indexing tests/application tests/operations -q`
Expected: PASS; Docling-model cases skip without `TYCHORA_TEST_DOCLING_MODELS`. `tests/ingestion/test_tables.py::test_legacy_document_replay_keeps_identity_but_new_normalizer_separates_it` still passes: the composition fingerprint digests `plan.semantic_data()`, which holds the base normalizer fingerprint, so composed IDs follow the v3 bump without a new composition version.

- [ ] **Step 8: Commit**

```bash
git add src/tychora/ingestion/sections.py src/tychora/ingestion/documents.py \
  src/tychora/ingestion/document_records.py src/tychora/ingestion/document_views.py \
  tests/ingestion/test_boundary_keys.py tests/retrieval/test_segments.py
git commit -m "feat: emit section, table and figure boundary keys for Docling documents"
```

---

### Task 4: Code `file:` boundaries and kind heading levels

**Files:**
- Modify: `src/tychora/adapters/code/normalize.py:163-182` (fingerprints), `:268-376` (the three `boundary_key=path` sites at `:283`, `:332`, `:374`)
- Modify: `src/tychora/adapters/sqlite/validation.py:400-417` (fingerprint recomputation)
- Modify: `src/tychora/indexing/representations.py:227`, `:304` (code container and code locator checks)
- Modify: `tests/indexing/test_windows.py:223`, `:476`, `:558`, `:597`, `:701` (synthetic code units)
- Modify: `src/tychora/configuration/registry.py:394-449` (kind validation), `src/tychora/contracts/error_reasons.py` (`kind.section_levels_invalid`)
- Modify: `src/tychora/ingestion/normalize.py` (`native_placements`), `ingestion/documents.py`, `ingestion/document_records.py`, `ingestion/composition.py:469` (pass the kind's heading levels)
- Test: `tests/code/test_structure.py`, `tests/ingestion/test_boundary_keys.py`, `tests/configuration/test_registry.py`, `tests/contracts/test_error_reasons.py` (extend)

`indexing/representations.py:918` named by the outline is `_representation`, which never reads `boundary_key`; the code checks are at `:227` (`_retained_code_files`) and `:304` (`_validate_locator`). The asset-proxy check at `:1012` stays.

**Interfaces:**
- Consumes: Task 1 `boundary_key`, `dump_section_path`, `kind_heading_levels`, `parse_heading_levels`, `parse_context_level` (validation only in Task 6), `normalizer_definition`; Tasks 2–3 placements.
- Produces: code units keyed `file:<relative path>` with `section_path = [section:#root (depth 0), file:<path> (level "file", depth 1)]`; `code-normalizer-v2` over `normalizer_definition(kind)` (both the current and the assertion-less variant). Kind TOML key `heading_levels = [{ pattern = "...", level = "..." }]`, validated at registry load with `INVALID_REQUEST` reason `kind.section_levels_invalid`, applied by the native and Docling normalizers; the kind is digested into the normalizer fingerprint, so a pattern change re-normalizes. `registry._validate_section_levels(kind)` is the one validation entry point (Task 6 adds `context_level` to it).

- [ ] **Step 1: Write the failing tests**

Add to the imports of `tests/code/test_structure.py`:

```python
from tychora.contracts.code import CODE_ASSERTION_PROJECTION, CODE_RESOLVER_FINGERPRINT
from tychora.contracts.sections import (
    ROOT_SECTION_KEY,
    normalizer_definition,
    section_path_of,
)
```

and append:

```python
def test_code_units_use_file_scheme(tmp_path):
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    bundle = normalize_code(
        replay_code_extraction(retained.raw_document_ref, registration, artifacts),
        registry(),
    )
    for unit in bundle.units:
        path = unit.attributes["relative_path"]
        assert unit.boundary_key == f"file:{path}"
        assert [
            (entry.key, entry.level, entry.depth)
            for entry in section_path_of(unit.attributes, unit.boundary_key)
        ] == [(ROOT_SECTION_KEY, None, 0), (f"file:{path}", "file", 1)]
    kind = registry().kinds["code_file@1"]
    assert bundle.extraction.normalizer_fingerprint == digest(
        "code-normalizer-v2",
        normalizer_definition(kind),
        registry().schema_digests,
        CODE_RESOLVER_FINGERPRINT,
        CODE_ASSERTION_PROJECTION,
    )
```

Append to `tests/ingestion/test_boundary_keys.py` (merge imports into the module block):

```python
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.sections import HeadingLevel


def _documents_config(tmp_path, **kind_fields):
    """The documents@1 profile with fields added to manual@1."""
    path = tmp_path / "custom.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    data = config.model_dump(mode="json")["registry"]
    data["kinds"]["manual@1"].update(kind_fields)
    return config.model_copy(
        update={
            "registry": FrozenJsonMap(data),
            "registry_digest": digest("registry-v1", data),
        }
    )


ARTICLE_HEADINGS = [{"pattern": r"^Article \d+", "level": "article"}]


def test_kind_heading_pattern_names_level(native_case, tmp_path):
    text = "# Article 1 Scope\n\nBody.\n\n# Definitions\n\nTerms.\n"
    config = _documents_config(tmp_path, heading_levels=ARTICLE_HEADINGS)
    units = _units(native_case, text, config_override=config)
    assert _path(units["Body."])[-1] == (_own(units["# Article 1 Scope"]), "article", 1)
    assert _path(units["Terms."])[-1] == (_own(units["# Definitions"]), None, 1)


def test_heading_pattern_change_changes_normalizer_fingerprint(native_case, tmp_path):
    plain, _, _ = native_case("# Article 1\n\nBody.\n")
    patterned, _, _ = native_case(
        "# Article 1\n\nBody.\n",
        config_override=_documents_config(tmp_path, heading_levels=ARTICLE_HEADINGS),
    )
    assert plain.normalizer_fingerprint != patterned.normalizer_fingerprint


def test_docling_heading_pattern_names_level():
    export, original = _export(
        (
            _text("#/texts/0", "section_header", "Article 3 Duties", level=1),
            _text("#/texts/1", "text", "Keep records."),
        )
    )
    placements = document_placements(
        map_document(export, original).units,
        (HeadingLevel(pattern=r"^Article \d+", level="article"),),
    )
    assert placements["#/texts/1"].section_path[-1].level == "article"
```

Append to `tests/configuration/test_registry.py`:

```python
@pytest.mark.parametrize(
    "value",
    [
        [{"pattern": "(", "level": "article"}],
        [{"pattern": "^A", "level": "paragraph"}],
        "^Article",
    ],
)
def test_invalid_heading_levels_are_refused_at_load(builtin_config, value):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["standard@1"].update({"heading_levels": value}),
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.section_levels_invalid"


def test_heading_levels_load_when_valid(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["standard@1"].update(
            {"heading_levels": [{"pattern": r"^\d+(\.\d+)*", "level": "clause"}]}
        ),
    )
    assert Registry.from_config(config).kinds["standard@1"]["heading_levels"]
```

In `tests/contracts/test_error_reasons.py` change the `CLASSIFIED_REASONS` entry

```python
    "INVALID_REQUEST": {"bench.incomparable", "review.time_naive"},
```

to

```python
    "INVALID_REQUEST": {
        "bench.incomparable",
        "review.time_naive",
        "kind.section_levels_invalid",
    },
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/code/test_structure.py::test_code_units_use_file_scheme tests/ingestion/test_boundary_keys.py tests/configuration/test_registry.py tests/contracts/test_error_reasons.py -v`
Expected: FAIL: the code test with `assert 'a.py' == 'file:a.py'`; the heading tests with `assert (..., None, 1) == (..., 'article', 1)` and an equal fingerprint; the registry tests with `DID NOT RAISE` (or a reasonless error); `test_registry_is_exactly_the_closed_set` with a set difference naming `kind.section_levels_invalid`.

- [ ] **Step 3: Register the reason**

In `src/tychora/contracts/error_reasons.py`, after the `filter.unknown_field` entry add:

```python
    Reason(
        "INVALID_REQUEST",
        "kind.section_levels_invalid",
        False,
        "Fix the kind's `heading_levels` or `context_level`: levels are part, chapter, section, article, annex, clause or file.",
    ),
```

- [ ] **Step 4: Validate heading levels at registry load**

In `src/tychora/configuration/registry.py` import `parse_heading_levels` from `tychora.contracts.sections`, add this function after `_require`:

```python
def _validate_section_levels(kind: dict[str, Any]) -> None:
    """Refuse a kind whose section-level declarations do not parse."""
    try:
        parse_heading_levels(kind.get("heading_levels"))
    except ValueError:
        raise TychoraError(
            "INVALID_REQUEST",
            "Invalid kind section levels: " + str(kind.get("id")),
            reason="kind.section_levels_invalid",
        ) from None
```

and call `_validate_section_levels(kind)` in `_validate_definitions`'s kind loop, right after the `allowed_uses` check (line 418).

- [ ] **Step 5: Apply the kind's heading levels**

- `ingestion/normalize.py` `native_placements`: import `kind_heading_levels` and pass it as the third argument of `place_units`: `root,` becomes `root, kind_heading_levels(kind),`.
- `ingestion/documents.py` `normalize_document`: `placements = document_placements(document_units, kind_heading_levels(kind))`.
- `ingestion/document_records.py`: `placements = _group_placements(groups, strategy, kind_heading_levels(kind))`.
- `ingestion/composition.py:469`: `bundle, fragments = source_views(parsed, context, base, kind_heading_levels(kind))` (`kind` is bound at `:449`).

- [ ] **Step 6: Key code units by file**

In `src/tychora/adapters/code/normalize.py` add to the imports:

```python
from tychora.contracts.sections import (
    ROOT_SECTION_KEY,
    SectionEntry,
    boundary_key,
    dump_section_path,
    normalizer_definition,
)
```

Add after `_native_ref`:

```python
def _file_placement(path: str) -> tuple[str, list[dict[str, Any]]]:
    """The boundary key and section path of every unit of one file."""
    key = boundary_key("file", path)
    sections = (
        SectionEntry(key=ROOT_SECTION_KEY, depth=0),
        SectionEntry(key=key, level="file", depth=1),
    )
    return key, dump_section_path(sections)
```

In `_build`, right after `path, syntax = item.captured.relative_path, item.syntax` add `file_key, file_sections = _file_placement(path)`. At the three unit sites replace `boundary_key=path,` with `boundary_key=file_key,`, and add `"section_path": file_sections,` to each attributes mapping (the container's mapping after `"adapter_fingerprint"`, the symbol and code mappings after `"language"`).

Replace both fingerprint digests (lines 163-169 and 177-182): the tag `"code-normalizer-v1"` becomes `"code-normalizer-v2"` and `kind` becomes `normalizer_definition(kind)`. In `src/tychora/adapters/sqlite/validation.py:400-411` make the same two replacements (import `normalizer_definition` from `tychora.contracts.sections`).

In `src/tychora/indexing/representations.py` import `from tychora.contracts.sections import boundary_key`, then replace `or unit.boundary_key != path` (line 227) with `or unit.boundary_key != boundary_key("file", path)` and `or unit.boundary_key != retained.relative_path` (line 304) with `or unit.boundary_key != boundary_key("file", retained.relative_path)`.

In `tests/indexing/test_windows.py` replace each `boundary_key=path,` (lines 223, 558, 597, 701) with `boundary_key=f"file:{path}",` and, at line 476, `unit.boundary_key != binding.source_ref` with `unit.boundary_key != "file:" + binding.source_ref`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/code tests/ingestion/test_boundary_keys.py tests/configuration/test_registry.py tests/contracts/test_error_reasons.py tests/indexing -q`
Expected: PASS.

- [ ] **Step 8: Run the affected suites**

Run: `uv run --locked pytest tests/code tests/ingestion tests/indexing tests/retrieval tests/storage tests/evaluation -q`
Expected: PASS. The code gold-set metrics do not move: windows and lexical rows are unchanged, only context group keys and boundary keys of code units change.

- [ ] **Step 9: Commit**

```bash
git add src/tychora/adapters/code/normalize.py src/tychora/adapters/sqlite/validation.py \
  src/tychora/indexing/representations.py src/tychora/configuration/registry.py \
  src/tychora/contracts/error_reasons.py src/tychora/ingestion/normalize.py \
  src/tychora/ingestion/documents.py src/tychora/ingestion/document_records.py \
  src/tychora/ingestion/composition.py tests/code/test_structure.py \
  tests/ingestion/test_boundary_keys.py tests/configuration/test_registry.py \
  tests/contracts/test_error_reasons.py tests/indexing/test_windows.py
git commit -m "feat: emit file boundary keys for code and apply kind heading levels"
```

---

### Task 5: Linear windowing with token offsets (C1)

**Files:**
- Modify: `src/tychora/ports/tokens.py` (add `OffsetTokenCounter`)
- Modify: `src/tychora/indexing/windows.py:14-79` (offset-counted split beside the counted one), `src/tychora/indexing/groups.py:64-72`, `:92` (reuse window counts)
- Modify: `src/tychora/application/default_bindings.py:50-79` (`_VerifiedVoyageCounter.count_with_offsets`), `src/tychora/interfaces/bench.py:58-65` (`BenchCounter.count_with_offsets`)
- Modify: `src/tychora/contracts/error_reasons.py` (`tokens.atomic_limit`), `tests/contracts/test_error_reasons.py`
- Modify: `src/tychora/indexing/coordinator.py:1176-1189` (`on_stage`), `src/tychora/application/sources.py:850-857`, `src/tychora/evaluation/bench/stages.py:12-19` (stage events for the gate)
- Test: `tests/indexing/test_windows_linear.py` (new), `tests/evaluation/bench/test_measurement.py` (extend)

**Why offsets give the same windows.** The counted split (`_split_interval`, kept as `_split_by_counts`) asks `count(text[cursor:b])` for every semantic boundary `b` up to the interval end, again for every window: quadratic in boundaries and characters. With one tokenisation of the interval, the count of `[a, b)` is `count("")` plus the tokens whose span overlaps `[a, b)`, found by two binary searches. For a counter where that equality holds (the `OffsetTokenCounter` contract), every count the counted split reads is the same number, the counts grow with `b`, and boundaries past the 800-token horizon can never be chosen; so the same boundaries win. The window's stored `token_count` is still one exact `count(window.text)`, so `_validate_windows` and `_make_group` may reuse it. A counter without offsets, or whose spans are not ordered and inside the text, keeps the counted split.

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `ports/tokens.py`: `@runtime_checkable class OffsetTokenCounter(TokenCounter, Protocol): def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]` (one `(char_start, char_end)` per token, special tokens excluded).
  - `window_unit(prepared, counter)` signature and output unchanged for counters that satisfy the contract.
  - `ProjectionCoordinator.prepare(bundle, base_snapshot_id, job, ctx, *, on_stage: Callable[[str], None] | None = None)`; job stages `windowing` and `normalize_commit`, mapped by `ingest_bucket` to the budget rows of the same names.
  - Reason `tokens.atomic_limit` (`INVALID_REQUEST`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/indexing/test_windows_linear.py
"""Windows counted from one tokenisation per interval equal the counted split."""

import random
import re
from hashlib import sha256

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.indexing import OwnedInterval, RepresentationUnit
from tychora.indexing.windows import window_unit
from tychora.interfaces.bench import BenchCounter
from tychora.ports.tokens import OffsetTokenCounter

REVISION = "revision-linear"
UNIT = "unit-linear"
WORDS = ("alpha", "beta", "gamma", "pump", "seal", "valve", "record", "quarter")


class Characters:
    model = "synthetic-codepoint-counter"
    tokenizer_id = "synthetic-codepoint-counter@1"

    def count(self, text: str) -> int:
        return len(text)


class OffsetCharacters(Characters):
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        return tuple((index, index + 1) for index in range(len(text)))


class Words:
    model = "synthetic-word-counter"
    tokenizer_id = "synthetic-word-counter@1"

    def count(self, text: str) -> int:
        return len(re.findall(r"\S+", text))


class OffsetWords(Words):
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        return tuple(match.span() for match in re.finditer(r"\S+", text))


class Unordered(Characters):
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        return tuple((index, index + 1) for index in reversed(range(len(text))))


class Spy(OffsetCharacters):
    """Counts every character handed to the tokenizer."""

    def __init__(self) -> None:
        self.characters = 0

    def count(self, text: str) -> int:
        self.characters += len(text)
        return len(text)

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        self.characters += len(text)
        return super().count_with_offsets(text)


def _representation(text: str, cuts: tuple[int, ...] = ()) -> RepresentationUnit:
    unit = EvidenceUnit(
        unit_id=UNIT,
        extraction_id="extraction-linear",
        parent_id=None,
        ordinal=0,
        unit_type="paragraph",
        native_ref="paragraph:0",
        boundary_key="section:#root",
        text=text,
        text_sha=sha256(text.encode()).hexdigest(),
        locators=(),
    )
    edges = (0, *sorted({cut for cut in cuts if 0 < cut < len(text)}), len(text))
    return RepresentationUnit(
        unit=unit,
        revision_id=REVISION,
        title="Linear",
        kind_id="manual@1",
        ownership=tuple(
            OwnedInterval(
                char_start=start,
                char_end=end,
                dependency_ids=frozenset({REVISION, UNIT}),
            )
            for start, end in zip(edges, edges[1:])
        ),
    )


def _sentence(rng: random.Random) -> str:
    words = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 40)))
    end = rng.choice((" ", "  ", "\t", "\n", "\r\n", "\n\n", "\n\n\n", "\r"))
    return words.capitalize() + rng.choice(".!?") + end


def _random_text(rng: random.Random) -> str:
    """Sentences, headings, CJK and combining runs, unbroken runs and long clauses."""
    pieces = []
    for _ in range(rng.randint(1, 14)):
        roll = rng.random()
        if roll < 0.5:
            pieces.append(_sentence(rng))
        elif roll < 0.6:
            pieces.append("# " + " ".join(rng.choice(WORDS) for _ in range(3)) + "\n\n")
        elif roll < 0.7:
            pieces.append("".join(rng.choice("漢字仮名交じり文書") for _ in range(rng.randint(5, 300))) + "。")
        elif roll < 0.8:
            pieces.append("é" * rng.randint(1, 200) + " ")
        elif roll < 0.9:
            pieces.append("x" * rng.randint(100, 1800))
        else:
            pieces.append(" ".join(rng.choice(WORDS) for _ in range(rng.randint(50, 900))) + " ")
    return "".join(pieces)


def _outcome(prepared: RepresentationUnit, counter) -> object:
    try:
        return window_unit(prepared, counter)
    except (ValueError, TychoraError) as error:
        return type(error).__name__, str(error)


@pytest.mark.parametrize(
    "fast,counted", [(OffsetCharacters, Characters), (OffsetWords, Words)]
)
def test_linear_windows_equal_reference_on_1000_random_texts(fast, counted):
    rng = random.Random(f"windows:{fast.__name__}")
    for _ in range(1000):
        text = _random_text(rng)
        cuts = tuple(rng.randrange(len(text)) for _ in range(rng.randint(0, 3)))
        prepared = _representation(text, cuts)
        assert _outcome(prepared, fast()) == _outcome(prepared, counted()), text[:80]


def test_long_unit_counts_tokens_in_linear_time():
    text = "The pump seal is inspected every quarter and recorded. " * 400
    spy = Spy()
    windows = window_unit(_representation(text), spy)
    assert len(windows) > 20
    assert "".join(window.text for window in windows) == text
    assert spy.characters <= 2 * len(text)


def test_counter_without_offsets_keeps_counted_windows():
    assert isinstance(OffsetCharacters(), OffsetTokenCounter)
    assert not isinstance(Characters(), OffsetTokenCounter)
    prepared = _representation("Seal. " * 300)
    assert window_unit(prepared, Unordered()) == window_unit(prepared, Characters())


def test_bench_counter_offsets_cover_text_in_order():
    counter = BenchCounter()
    assert counter.count_with_offsets("abcdefghij") == ((0, 4), (4, 10))
    assert counter.count_with_offsets("ab") == ((0, 2),)
    assert counter.count_with_offsets("") == ()
    assert len(counter.count_with_offsets("x" * 4000)) == counter.count("x" * 4000)


def test_add_emits_windowing_then_normalize_commit(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "manual.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# Pumps\n\nInspect seals.\n",
                "metadata": {"title": "Pumps"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        stages = [event.stage for event in kb.jobs.events_after(job.id)]
    assert stages.index("windowing") < stages.index("normalize_commit") < stages.index("project")
```

Append to `tests/evaluation/bench/test_measurement.py`:

```python
def test_windowing_and_commit_stages_have_budget_rows():
    assert ingest_bucket("windowing") == "windowing"
    assert ingest_bucket("normalize_commit") == "normalize_commit"
    assert {"windowing", "normalize_commit"} <= set(INGEST_STAGE_BUDGETS_S)
```

In `tests/contracts/test_error_reasons.py` add `"tokens.atomic_limit"` to the `CLASSIFIED_REASONS["INVALID_REQUEST"]` set.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_windows_linear.py tests/evaluation/bench/test_measurement.py tests/contracts/test_error_reasons.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'OffsetTokenCounter' from 'tychora.ports.tokens'`; after Step 3 alone, `test_long_unit_counts_tokens_in_linear_time` fails with a character count far above `2 * len(text)` (the counted split reads about 200× the text).

- [ ] **Step 3: Declare the port**

```python
# src/tychora/ports/tokens.py
"""Qualified provider token counting boundary."""

from typing import Protocol, runtime_checkable


class TokenCounter(Protocol):
    model: str
    tokenizer_id: str

    def count(self, text: str) -> int: ...


@runtime_checkable
class OffsetTokenCounter(TokenCounter, Protocol):
    """A token counter that also reports where each token sits in the text.

    ``count_with_offsets(text)`` returns one ``(char_start, char_end)`` span per
    counted token, in text order, without the fixed tokens ``count("")`` adds.
    For every ``a < b`` the spans overlapping ``[a, b)`` plus ``count("")`` must
    equal ``count(text[a:b])``; windows are then identical to counting each
    candidate range separately.
    """

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]: ...
```

- [ ] **Step 4: Register the reason**

In `src/tychora/contracts/error_reasons.py`, after the `kind.section_levels_invalid` entry add:

```python
    Reason(
        "INVALID_REQUEST",
        "tokens.atomic_limit",
        False,
        "Check the configured tokenizer: it counts one source code point above the window ceiling.",
    ),
```

- [ ] **Step 5: Split intervals from offsets**

Replace `src/tychora/indexing/windows.py` lines 1-79 (imports through `_split_interval`) with:

```python
"""Lossless token-counted windows over canonical source text."""

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import pairwise

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import chunk_id, digest
from tychora.contracts.indexing import RepresentationUnit, SearchWindow
from tychora.ports.tokens import OffsetTokenCounter, TokenCounter

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
        raise TychoraError(
            "INVALID_REQUEST", "One atomic source codepoint exceeds the token ceiling"
        )
    return best


def _split_by_counts(
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
    """Token spans of one interval, relative to its start, and the fixed overhead."""

    starts: tuple[int, ...]
    ends: tuple[int, ...]
    overhead: int

    def count(self, start: int, end: int) -> int:
        """Tokens overlapping ``[start, end)`` plus the overhead."""
        return self.overhead + bisect_left(self.starts, end) - bisect_right(self.ends, start)

    def limit(self, start: int, tokens: int, end: int) -> int:
        """The last position ``p <= end`` with ``count(start, p) <= tokens``."""
        index = bisect_right(self.ends, start) + tokens - self.overhead
        if index < 0:
            return start
        return end if index >= len(self.starts) else min(end, self.starts[index])


def _spans(counter: OffsetTokenCounter, text: str) -> _Spans | None:
    """The interval's spans, or ``None`` unless they are ordered and inside the text."""
    spans = counter.count_with_offsets(text)
    starts = tuple(start for start, _ in spans)
    ends = tuple(end for _, end in spans)
    if any(not 0 <= start < end <= len(text) for start, end in spans) or any(
        left > right for left, right in (*pairwise(starts), *pairwise(ends))
    ):
        return None
    return _Spans(starts, ends, _count(counter, ""))


def _stop(text: str, cursor: int, start: int, end: int, spans: _Spans) -> int:
    """Choose a window end as the counted split does, scanning only to the ceiling."""
    relative = cursor - start
    horizon = start + spans.limit(relative, SOFT_CEILING_TOKENS, end - start)
    before: tuple[int, int] | None = None
    after: tuple[int, int] | None = None
    for boundary in _semantic_boundaries(text, cursor, min(end, horizon + 1)):
        count = spans.count(relative, boundary - start)
        if not 0 < count <= SOFT_CEILING_TOKENS:
            continue
        if count <= TARGET_TOKENS:
            before = max(before or (count, boundary), (count, boundary))
        else:
            after = min(after or (count, boundary), (count, boundary))
    chosen = before if before is not None else after
    if chosen is not None:
        return chosen[1]
    stop = start + spans.limit(relative, TARGET_TOKENS, end - start)
    if stop <= cursor:
        raise TychoraError(
            "INVALID_REQUEST",
            "One atomic source codepoint exceeds the token ceiling",
            reason="tokens.atomic_limit",
        )
    return stop


def _split_by_spans(
    text: str, start: int, end: int, counter: TokenCounter, spans: _Spans
) -> tuple[tuple[int, int, int], ...]:
    """Split one interval from its spans; each window is counted once exactly."""
    output = []
    cursor = start
    while cursor < end:
        if spans.count(cursor - start, end - start) <= SOFT_CEILING_TOKENS:
            stop = end
        else:
            stop = _stop(text, cursor, start, end, spans)
        count = _count(counter, text[cursor:stop])
        if count <= 0:
            raise ValueError("Nonempty source interval has no provider tokens")
        output.append((cursor, stop, count))
        cursor = stop
    return tuple(output)


def _split_interval(
    text: str, start: int, end: int, counter: TokenCounter
) -> tuple[tuple[int, int, int], ...]:
    """Split by offsets when the counter reports usable spans, else by counting."""
    if isinstance(counter, OffsetTokenCounter):
        spans = _spans(counter, text[start:end])
        if spans is not None:
            return _split_by_spans(text, start, end, counter, spans)
    return _split_by_counts(text, start, end, counter)
```

`window_unit` (lines 82-128) is unchanged. Two notes on `_stop`: the slice ends one character past the horizon, so a run of whitespace that the full scan would extend ends there too and is excluded by its count; and `before`/`after` keep the counted split's tie rules (`max` and `min` over `(count, boundary)`).

- [ ] **Step 6: Reuse window counts in groups**

In `src/tychora/indexing/groups.py` replace `_validate_windows` (lines 64-72) with:

```python
def _validate_windows(windows: tuple[SearchWindow, ...], counter: TokenCounter) -> None:
    """Windows carry their exact count from ``window_unit``; check identity only."""
    if len({window.chunk_id for window in windows}) != len(windows):
        raise ValueError("Context groups require unique windows")
    if any(window.tokenizer_id != counter.tokenizer_id for window in windows):
        raise ValueError("Window count differs from the qualified tokenizer")
```

and line 92

```python
    tokens = sum(counter.count(text) for text in inputs)
```

with

```python
    tokens = counter.count(capsule) + sum(window.token_count for window in windows)
```

- [ ] **Step 7: Offsets for the Voyage and bench counters**

In `src/tychora/application/default_bindings.py` replace `_VerifiedVoyageCounter.count` (lines 68-79) with:

```python
    def _loaded(self) -> Tokenizer:
        if self._tokenizer is None:
            try:
                self._tokenizer = load_voyage_tokenizer(self.cache_root, self.model)
            except ImportError:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Voyage tokenizer package is unavailable; install the provider extra.",
                ) from None
        if self.model == "voyage-multimodal-3.5":
            load_voyage_multimodal_accounting(self.cache_root)
        return self._tokenizer

    def count(self, text: str) -> int:
        return len(self._loaded().encode(text).ids)

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """Character spans of the counted tokens; special tokens are left out."""
        encoding = self._loaded().encode(text)
        return tuple(
            (start, end)
            for (start, end), special in zip(
                encoding.offsets, encoding.special_tokens_mask, strict=True
            )
            if not special
        )
```

In `src/tychora/interfaces/bench.py` add to `BenchCounter`:

```python
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """Four-character spans; the last span takes the remainder."""
        spans = len(text) // 4
        if spans == 0:
            return ((0, len(text)),) if text else ()
        return tuple(
            (4 * index, len(text) if index == spans - 1 else 4 * index + 4)
            for index in range(spans)
        )
```

`BenchCounter` is not additive (`count("")` is 1), so its windows differ slightly from the counted split; it only drives the offline benchmark.

- [ ] **Step 8: Emit the gate's job stages**

In `src/tychora/evaluation/bench/stages.py` add two rows to `_INGEST_BUCKETS`:

```python
    "windowing": "windowing",
    "normalize_commit": "normalize_commit",
```

In `src/tychora/indexing/coordinator.py` change `prepare`'s signature to

```python
    def prepare(
        self,
        bundle: ExtractionBundle,
        base_snapshot_id: str | None,
        job: JobHandle,
        ctx: ExecutionContext,
        *,
        on_stage: Callable[[str], None] | None = None,
    ) -> PreparedProjection:
```

and lines 1186-1189 to:

```python
        config_ref = self._config_ref(job)
        if on_stage is not None:
            on_stage("windowing")
        windows = self._windows(bundle)
        canonical = self._canonical_bundle(bundle, windows)
        if on_stage is not None:
            on_stage("normalize_commit")
        self._commit_or_reuse(canonical)
```

In `src/tychora/application/sources.py` (inside `_project.prepare`, line 854) pass the stage callback:

```python
            prepared = self.coordinator.prepare(
                attempt.bundle,
                base,
                attempt.handle,
                ctx,
                on_stage=lambda stage: attempt.jobs.stage(attempt.handle.id, stage),
            )
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/indexing/test_windows_linear.py tests/evaluation/bench/test_measurement.py tests/contracts/test_error_reasons.py -v`
Expected: PASS. The property test runs 2 × 1,000 texts in a few seconds.

- [ ] **Step 10: Qualify the Voyage tokenizer (asset-dependent)**

Add `import os`, `from pathlib import Path` and `from tychora.application.default_bindings import _VerifiedVoyageCounter` to the imports of `tests/indexing/test_windows_linear.py`, and append:

```python
@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
def test_voyage_offsets_give_the_counted_windows():
    counter = _VerifiedVoyageCounter(Path(os.environ["TYCHORA_TEST_MODEL_CACHE"]), "voyage-context-4")

    class CountOnly:
        model, tokenizer_id = counter.model, counter.tokenizer_id

        def count(self, text: str) -> int:
            return counter.count(text)

    rng = random.Random("voyage-windows")
    for _ in range(100):
        prepared = _representation(_random_text(rng))
        assert _outcome(prepared, counter) == _outcome(prepared, CountOnly())
```

Run, where the verified model cache is provisioned: `TYCHORA_TEST_MODEL_CACHE=<cache> uv run --locked pytest tests/indexing/test_windows_linear.py -k voyage -v`. Expected: PASS. If it fails, the Voyage tokenizer does not meet the `OffsetTokenCounter` contract (a byte-level BPE can attach whitespace to the next token): remove `count_with_offsets` from `_VerifiedVoyageCounter` so production keeps the counted split, keep the test as an expected failure with the reason, and report it to the owner (Open question 1). Without the cache the test is skipped.

- [ ] **Step 11: Run the affected suites**

Run: `uv run --locked pytest tests/indexing tests/application tests/api tests/evaluation -q`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add src/tychora/ports/tokens.py src/tychora/indexing/windows.py src/tychora/indexing/groups.py \
  src/tychora/application/default_bindings.py src/tychora/interfaces/bench.py \
  src/tychora/contracts/error_reasons.py src/tychora/indexing/coordinator.py \
  src/tychora/application/sources.py src/tychora/evaluation/bench/stages.py \
  tests/indexing/test_windows_linear.py tests/evaluation/bench/test_measurement.py \
  tests/contracts/test_error_reasons.py
git commit -m "perf: count window tokens once per interval"
```

---

### Task 6: Context groups keyed by `context_key`

**Files:**
- Modify: `src/tychora/configuration/registry.py` (`_validate_section_levels`, new `Registry.context_level`), `src/tychora/contracts/error_reasons.py` (`kind.unknown`)
- Modify: `src/tychora/contracts/indexing.py:89-133` (`SearchWindow.context_key`), `src/tychora/indexing/windows.py:82-128` (`window_unit`), `src/tychora/indexing/groups.py:193-199` (group check), `src/tychora/indexing/coordinator.py:385-391` (`_windows`)
- Test: `tests/indexing/test_groups.py` (helper and new tests), `tests/configuration/test_registry.py`, `tests/ingestion/test_boundary_keys.py`, `tests/contracts/test_error_reasons.py`

Today `window_unit` keys a window's context group on the unit's boundary key (`windows.py:99`), `SearchWindow` re-derives that key in its validator (`contracts/indexing.py:122-125`), and `build_groups` refuses a group whose windows differ in boundary key (`groups.py:193-199`). With section keys, units of one chapter have different boundary keys but one context key.

**Interfaces:**
- Consumes: Task 1 `ContextLevel`, `Depth`, `DEFAULT_CONTEXT_LEVEL`, `context_key`, `parse_context_level`, `section_path_of`.
- Produces:
  - `Registry.context_level(kind_id: str) -> ContextLevel` (kind TOML key `context_level`, default `depth:1`; unknown kind: `INVALID_REQUEST` reason `kind.unknown`); an invalid value is refused at registry load with `INVALID_REQUEST` reason `kind.section_levels_invalid`. This is the only definition of the field (sub-project 5 sets values per kind).
  - `SearchWindow.context_key: NonEmptyStr`; validator: `context_group_key == digest("context-group-v1", extraction_id, context_key)`.
  - `window_unit(prepared, counter, *, context_level: ContextLevel = Depth(n=1)) -> tuple[SearchWindow, ...]`.
  - Normalizer fingerprints already exclude `context_level` (Tasks 2–4), so changing it keeps the extraction ID and changes only group keys at the next index rebuild.

- [ ] **Step 1: Write the failing tests**

In `tests/indexing/test_groups.py` add `context_key=boundary,` to the `SearchWindow(...)` call of `_window` (after `boundary_key=boundary,`), add `from hashlib import sha256`, `from tychora.contracts.evidence import EvidenceUnit`, `from tychora.contracts.indexing import OwnedInterval, RepresentationUnit` and `from tychora.contracts.sections import Depth, SectionEntry, dump_section_path` to the imports, and append:

```python
def _sectioned(name: str, ordinal: int, path: tuple[SectionEntry, ...]) -> RepresentationUnit:
    text = f"Paragraph {name} of the manual."
    unit = EvidenceUnit(
        unit_id=f"unit-{name}",
        extraction_id="extraction-a",
        parent_id=None,
        ordinal=ordinal,
        unit_type="paragraph",
        native_ref=f"block:{ordinal}",
        boundary_key=path[-1].key,
        text=text,
        text_sha=sha256(text.encode()).hexdigest(),
        locators=(),
        attributes=FrozenJsonMap({"section_path": dump_section_path(path)}),
    )
    return RepresentationUnit(
        unit=unit,
        revision_id="revision-a",
        title="Synthetic Manual",
        kind_id="manual@1",
        ownership=(
            OwnedInterval(
                char_start=0,
                char_end=len(text),
                dependency_ids=frozenset({"revision-a", unit.unit_id}),
            ),
        ),
    )


ROOT = SectionEntry(key="section:#root", depth=0)
CHAPTER = SectionEntry(key="section:block:0", level="chapter", depth=1)


def _two_sections(level_a=None, level_b=None):
    return (
        _sectioned("a", 1, (ROOT, CHAPTER, SectionEntry(key="section:block:1", level=level_a, depth=2))),
        _sectioned("b", 3, (ROOT, CHAPTER, SectionEntry(key="section:block:2", level=level_b, depth=2))),
    )


def test_context_group_spans_top_level_chapter_by_default(config_factory):
    counter = ExactCounter("voyage-context-4")
    windows = tuple(w for rep in _two_sections() for w in window_unit(rep, counter))
    assert len({w.boundary_key for w in windows}) == 2
    assert {w.context_key for w in windows} == {"section:block:0"}
    (group,) = build_groups(windows, config_factory(), counter)
    assert group.window_ids == tuple(w.chunk_id for w in windows)


def test_context_level_article_groups_per_article(config_factory):
    counter = ExactCounter("voyage-context-4")
    reps = _two_sections("article", "article")
    windows = tuple(w for rep in reps for w in window_unit(rep, counter, context_level="article"))
    assert {w.context_key for w in windows} == {"section:block:1", "section:block:2"}
    assert len(build_groups(windows, config_factory(), counter)) == 2
    chapter = tuple(w for rep in reps for w in window_unit(rep, counter, context_level=Depth(n=1)))
    assert len({w.context_group_key for w in chapter}) == 1
```

Append to `tests/configuration/test_registry.py`:

```python
from tychora.contracts.sections import Depth


def test_context_level_defaults_to_top_level_chapter(builtin_config):
    assert Registry.from_config(builtin_config()).context_level("manual@1") == Depth(n=1)


def test_context_level_reads_the_kind_value(builtin_config):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["law@1"].update({"context_level": "article"}),
    )
    assert Registry.from_config(config).context_level("law@1") == "article"


@pytest.mark.parametrize("value", ["depth:0", "paragraph", 3])
def test_invalid_context_level_is_refused_at_load(builtin_config, value):
    config = changed_config(
        builtin_config(),
        lambda data: data["kinds"]["law@1"].update({"context_level": value}),
    )
    with pytest.raises(TychoraError) as err:
        Registry.from_config(config)
    assert err.value.reason == "kind.section_levels_invalid"


def test_unknown_kind_has_no_context_level(builtin_config):
    with pytest.raises(TychoraError) as err:
        Registry.from_config(builtin_config()).context_level("missing@1")
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.unknown"
```

(The `Depth` import joins the module's import block.)

Append to `tests/ingestion/test_boundary_keys.py`:

```python
from tychora.configuration.registry import Registry
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit


class _Characters:
    model = "synthetic-codepoint-counter"
    tokenizer_id = "synthetic-codepoint-counter@1"

    def count(self, text):
        return len(text)


def test_context_level_change_takes_effect_on_rebuild_without_renormalizing(
    native_case, tmp_path
):
    text = "# Pumps\n\n## Seals\n\nInspect seals.\n\n## Rings\n\nReplace rings.\n"
    chapter = _documents_config(tmp_path, context_level="depth:1")
    document = _documents_config(tmp_path, context_level="document")
    first, _, _ = native_case(text, config_override=chapter)
    second, _, _ = native_case(text, config_override=document)
    one = normalize(first, first.registration.revision.revision_id, Registry.from_config(chapter))
    two = normalize(second, second.registration.revision.revision_id, Registry.from_config(document))
    assert one.extraction.extraction_id == two.extraction.extraction_id
    keys = []
    for bundle, config in ((one, chapter), (two, document)):
        level = Registry.from_config(config).context_level("manual@1")
        windows = [
            window
            for item in prepare_extraction(bundle).units
            for window in window_unit(item, _Characters(), context_level=level)
        ]
        keys.append({window.context_key for window in windows})
    assert keys[0] == {"section:" + one.units[0].native_ref}
    assert keys[1] == {"section:#root"}
```

In `tests/contracts/test_error_reasons.py` add `"kind.unknown"` to `CLASSIFIED_REASONS["INVALID_REQUEST"]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_groups.py tests/configuration/test_registry.py tests/ingestion/test_boundary_keys.py tests/contracts/test_error_reasons.py -v`
Expected: FAIL: `SearchWindow` rejects the unknown field `context_key` (`ValidationError ... Extra inputs are not permitted`), `AttributeError: 'Registry' object has no attribute 'context_level'`, and `window_unit() got an unexpected keyword argument 'context_level'`.

- [ ] **Step 3: Register `kind.unknown` and read the context level**

In `src/tychora/contracts/error_reasons.py`, after the `kind.section_levels_invalid` entry add:

```python
    Reason(
        "INVALID_REQUEST",
        "kind.unknown",
        False,
        "Use a kind the workspace registry defines; list them with `tychora inspect kinds`.",
    ),
```

In `src/tychora/configuration/registry.py` extend the `contracts.sections` import with `DEFAULT_CONTEXT_LEVEL, ContextLevel, parse_context_level`, and replace `_validate_section_levels` (Task 4) with:

```python
def _validate_section_levels(kind: dict[str, Any]) -> None:
    """Refuse a kind whose section-level declarations do not parse."""
    try:
        parse_heading_levels(kind.get("heading_levels"))
        level = kind.get("context_level", DEFAULT_CONTEXT_LEVEL)
        if not isinstance(level, str):
            raise ValueError("A context level is a string")
        parse_context_level(level)
    except ValueError:
        raise TychoraError(
            "INVALID_REQUEST",
            "Invalid kind section levels: " + str(kind.get("id")),
            reason="kind.section_levels_invalid",
        ) from None
```

Add to `class Registry`, after `validate_record`:

```python
    def context_level(self, kind_id: str) -> ContextLevel:
        """The section level a kind's context groups span (``depth:1`` by default)."""
        kind = self.kinds.get(kind_id)
        if not isinstance(kind, Mapping):
            raise TychoraError(
                "INVALID_REQUEST", "Unknown kind: " + kind_id, reason="kind.unknown"
            )
        return parse_context_level(str(kind.get("context_level", DEFAULT_CONTEXT_LEVEL)))
```

- [ ] **Step 4: Carry the context key on windows**

In `src/tychora/contracts/indexing.py`, add `context_key: NonEmptyStr` to `SearchWindow` right after `context_group_key`, and change the validator's group check (lines 122-125) from

```python
        ) or self.context_group_key != digest(
            "context-group-v1", self.extraction_id, self.boundary_key
        ):
```

to

```python
        ) or self.context_group_key != digest(
            "context-group-v1", self.extraction_id, self.context_key
        ):
```

In `src/tychora/indexing/windows.py` add `from tychora.contracts.sections import ContextLevel, Depth, context_key, section_path_of`, change the signature of `window_unit` to

```python
def window_unit(
    prepared: RepresentationUnit,
    counter: TokenCounter,
    *,
    context_level: ContextLevel = Depth(n=1),
) -> tuple[SearchWindow, ...]:
```

replace line 99

```python
    group_key = digest("context-group-v1", unit.extraction_id, unit.boundary_key)
```

with

```python
    group = context_key(section_path_of(unit.attributes, unit.boundary_key), context_level)
    group_key = digest("context-group-v1", unit.extraction_id, group)
```

and add `context_key=group,` after `context_group_key=group_key,` in the `SearchWindow(...)` call.

In `src/tychora/indexing/groups.py` lines 193-199 replace `or window.boundary_key != first.boundary_key` with `or window.context_key != first.context_key`.

In `src/tychora/indexing/coordinator.py` `_windows` (lines 385-391), pass the kind's level:

```python
    def _windows(self, bundle: ExtractionBundle) -> tuple[SearchWindow, ...]:
        prepared = prepare_extraction(bundle)
        level = self.store.registry.context_level(bundle.revision.kind_id)
        return tuple(
            window
            for item in prepared.units
            for window in window_unit(item, self.counter, context_level=level)
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/indexing tests/configuration tests/ingestion/test_boundary_keys.py tests/contracts/test_error_reasons.py -q`
Expected: PASS. Windows of a unit without `section_path` (asset proxies, synthetic test units) keep the group key of their boundary key, so existing group tests are unchanged.

- [ ] **Step 6: Run the affected suites**

Run: `uv run --locked pytest tests/indexing tests/retrieval tests/storage tests/application -q`
Expected: PASS. `_group_vector_facts` (`adapters/sqlite/eligibility.py:296-326`) recomputes group IDs from the stored `context_group_key`, which is the same column.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/configuration/registry.py src/tychora/contracts/error_reasons.py \
  src/tychora/contracts/indexing.py src/tychora/indexing/windows.py \
  src/tychora/indexing/groups.py src/tychora/indexing/coordinator.py \
  tests/indexing/test_groups.py tests/configuration/test_registry.py \
  tests/ingestion/test_boundary_keys.py tests/contracts/test_error_reasons.py
git commit -m "feat: key context groups on the kind context level"
```

---

### Task 7: Sort-and-sweep ownership and dependency facts (C2 write side)

**Files:**
- Create: `src/tychora/indexing/intervals.py`
- Modify: `src/tychora/indexing/representations.py:38-49` (`_RetainedIndex`), `:528-558` (`_build_bundle_index`), `:703-718` (`_source_locators` becomes `_locator_overlaps`), `:768-825` (`_locator_intervals`), `:828-861` (`_document_intervals`); add `ChunkSpan` and `representation_facts`
- Modify: `src/tychora/contracts/indexing.py` (add `dependency_digest`, `OwnerDependencies`, `IntervalRow`, `RepresentationFacts`)
- Modify: `src/tychora/adapters/sqlite/eligibility.py`: delete `_owners_by_locator` (`:61-75`), rewrite the per-member dependency block (`:497-635`)
- Test: `tests/indexing/test_interval_sweep.py` (new)

Today `_locator_intervals` compares each unit's locator with every locator of its domain, and `_document_intervals` compares each part with every part of every document unit: O(n²) per extraction. `read_eligibility_facts` then rebuilds the same overlaps a third time with `_owners_by_locator` for units without text.

**Interfaces:**
- Consumes: `PreparedExtraction`, `RepresentationUnit.ownership`, `RetainedRepresentationRows`, `ExtractionBundle`.
- Produces:
  - `indexing/intervals.py`: `overlapping_pairs(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]` (index pairs of overlapping nonempty half-open ranges, each pair once).
  - `contracts/indexing.py`: `dependency_digest(dependency_ids: Iterable[str]) -> str` (`digest("ownership-dependencies-v1", sorted(ids))`); `class OwnerDependencies(Contract): owner_id: Identifier; dependency_ids: frozenset[Identifier]`; `class IntervalRow(Contract): unit_id: Identifier; char_start: NonNegativeInt; char_end: PositiveInt; dependency_digest: Sha256`; `class RepresentationFacts(Contract): extraction_id; dependencies: tuple[OwnerDependencies, ...]; intervals: tuple[IntervalRow, ...]; def dependency_map(self) -> dict[str, frozenset[str]]` (an interval's dependency set is stored as an owner keyed by its digest).
  - `indexing/representations.py`: `class ChunkSpan(NamedTuple): chunk_id: str; unit_id: str; char_start: int; char_end: int`; `representation_facts(rows: RepresentationRows, prepared: PreparedExtraction, chunks: Iterable[ChunkSpan]) -> RepresentationFacts`, equal to what `read_eligibility_facts` derives per member today.
  - `prepare_extraction(bundle) -> PreparedExtraction` unchanged in signature and output.

- [ ] **Step 1: Write the failing tests**

```python
# tests/indexing/test_interval_sweep.py
"""The sort-and-sweep finds exactly the overlaps the pairwise scan found."""

import random
import time
from hashlib import sha256
from pathlib import Path

from tests.code.test_structure import _registration
from tests.helpers.corpus import registry, seed_bundle
from tests.ingestion.conftest import native_case  # noqa: F401  (fixture)
from tychora.adapters.code.normalize import (
    normalize_code,
    replay_code_extraction,
    retain_code_extraction,
)
from tychora.contracts.corpus import LocatorRow
from tychora.contracts.identity import digest
from tychora.contracts.indexing import dependency_digest
from tychora.indexing import representations
from tychora.indexing.intervals import overlapping_pairs
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ingestion.normalize import normalize

FIXTURES = Path(__file__).parents[1] / "fixtures/documents"
LAW = (
    "## Article 4\n\nIntro.\n\n1. First.\n   1. Nested.\n   2. Second nested.\n"
    "2. Second.\n\n## Article 5\n\nNext.\n"
)


def _quadratic(intervals):
    return sorted(
        (i, j)
        for i, (a, b) in enumerate(intervals)
        for j, (c, d) in enumerate(intervals)
        if i < j and a < b and c < d and a < d and c < b
    )


def test_overlapping_pairs_equal_quadratic_on_random_intervals():
    rng = random.Random(20261016)
    for _ in range(1000):
        intervals = [
            (start, start + rng.randint(0, 30))
            for start in (rng.randint(0, 100) for _ in range(rng.randint(0, 40)))
        ]
        pairs = overlapping_pairs(intervals)
        assert sorted(tuple(sorted(pair)) for pair in pairs) == _quadratic(intervals)


def test_sweep_scales_n_log_n():
    intervals = [(10 * i, 10 * i + 5) for i in range(10_000)] + [(0, 100_000)]
    started = time.perf_counter()
    pairs = overlapping_pairs(intervals)
    assert time.perf_counter() - started < 1.0
    assert len(pairs) == 10_000


def _fixture_bundles(native_case, tmp_path):
    for text, kind in (
        (LAW, "law@1"),
        ((FIXTURES / "standards.md").read_text(), "standard@1"),
        ((FIXTURES / "template.md").read_text(), "template@1"),
    ):
        parsed, _, rules = native_case(text, kind=kind)
        yield normalize(parsed, parsed.registration.revision.revision_id, rules)
    artifacts, registration, parsed = _registration(tmp_path)
    retained = retain_code_extraction(registration, parsed, registry(), artifacts)
    yield normalize_code(
        replay_code_extraction(retained.raw_document_ref, registration, artifacts),
        registry(),
    )


def test_sweep_equals_quadratic_overlap_on_fixtures(native_case, tmp_path, monkeypatch):
    bundles = list(_fixture_bundles(native_case, tmp_path))
    swept = [prepare_extraction(bundle.model_copy()) for bundle in bundles]
    monkeypatch.setattr(representations, "overlapping_pairs", _quadratic)
    assert [prepare_extraction(bundle.model_copy()) for bundle in bundles] == swept
    for prepared in (swept[0], swept[-1]):
        assert any(
            len(interval.dependency_ids) > 2
            for item in prepared.units
            for interval in item.ownership
        )


def test_empty_unit_depends_on_units_that_strictly_contain_its_position():
    bundle = seed_bundle("Seal checks are quarterly.")
    host = bundle.units[0]
    empty_locator = host.locators[0].model_copy(update={"char_start": 5, "char_end": 5})
    empty = host.model_copy(
        update={
            "unit_id": "empty-point",
            "ordinal": 1,
            "native_ref": "paragraph:2",
            "text": "",
            "text_sha": sha256(b"").hexdigest(),
            "locators": (empty_locator,),
        }
    )
    rows = bundle.model_copy(
        update={
            "units": (host, empty),
            "locators": (
                *bundle.locators,
                LocatorRow(
                    locator_id=digest("locator-v1", "empty-point", 0),
                    unit_id="empty-point",
                    fragment_ordinal=0,
                    locator=empty_locator,
                ),
            ),
        }
    )
    chunk = rows.chunks[0]
    facts = representation_facts(
        rows,
        prepare_extraction(rows),
        (ChunkSpan(chunk.chunk_id, chunk.unit_id, chunk.char_start, chunk.char_end),),
    )
    revision = rows.revision.revision_id
    deps = facts.dependency_map()
    assert deps[host.unit_id] == frozenset({revision, host.unit_id})
    assert deps["empty-point"] == frozenset({revision, "empty-point", host.unit_id})
    assert deps[chunk.chunk_id] == deps[host.unit_id]
    (interval,) = facts.intervals
    assert (interval.unit_id, interval.char_start, interval.char_end) == (
        host.unit_id,
        0,
        len(host.text),
    )
    assert interval.dependency_digest == dependency_digest(deps[host.unit_id])
    assert deps[interval.dependency_digest] == deps[host.unit_id]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/indexing/test_interval_sweep.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'tychora.indexing.intervals'`.

- [ ] **Step 3: Add the sweep**

```python
# src/tychora/indexing/intervals.py
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
```

- [ ] **Step 4: Add the fact contracts**

Append to `src/tychora/contracts/indexing.py` (add `Iterable` from `collections.abc`):

```python
def dependency_digest(dependency_ids: Iterable[str]) -> str:
    """The identity of one dependency set; its owner key when persisted."""
    return digest("ownership-dependencies-v1", sorted(dependency_ids))


class OwnerDependencies(Contract):
    """Every identity one owner's text needs: a unit, chunk, asset, group or interval set."""

    owner_id: Identifier
    dependency_ids: frozenset[Identifier] = Field(min_length=1)


class IntervalRow(Contract):
    """One owned interval of a unit and the digest of its dependency set."""

    unit_id: Identifier
    char_start: NonNegativeInt
    char_end: PositiveInt
    dependency_digest: Sha256


class RepresentationFacts(Contract):
    """The dependency sets and ownership intervals one extraction persists."""

    extraction_id: Identifier
    dependencies: tuple[OwnerDependencies, ...] = ()
    intervals: tuple[IntervalRow, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        owners = [item.owner_id for item in self.dependencies]
        keys = [(row.unit_id, row.char_start) for row in self.intervals]
        if len(set(owners)) != len(owners) or len(set(keys)) != len(keys):
            raise ValueError("Representation facts repeat an owner or an interval")
        if any(row.char_end <= row.char_start for row in self.intervals):
            raise ValueError("Representation intervals must be nonempty")
        if not {row.dependency_digest for row in self.intervals} <= set(owners):
            raise ValueError("An interval names a dependency set the facts omit")
        return self

    def dependency_map(self) -> dict[str, frozenset[str]]:
        """Owner ID to dependency IDs."""
        return {item.owner_id: item.dependency_ids for item in self.dependencies}
```

- [ ] **Step 5: Sweep in `prepare_extraction`**

In `src/tychora/indexing/representations.py`:

1. Imports: add `from bisect import bisect_left`, `from typing import NamedTuple`, `from collections.abc import Iterable`, `from tychora.contracts.indexing import IntervalRow, OwnerDependencies, RepresentationFacts, dependency_digest` (joining the existing `contracts.indexing` import) and `from .intervals import overlapping_pairs`.

2. In `_RetainedIndex` replace the field

```python
    source_locators: Mapping[
        tuple[str, str, str], tuple[tuple[EvidenceUnit, TextLocator | CodeLocator], ...]
    ]
```

with

```python
    locator_overlaps: Mapping[
        str, tuple[tuple[EvidenceUnit, TextLocator | CodeLocator], ...]
    ]
    part_overlaps: Mapping[tuple[str, int], tuple[tuple[str, DocumentSourcePart], ...]]
```

3. In `_build_bundle_index` replace

```python
        document_parts=_validated_documents(bundle),
        source_locators=_source_locators(units),
    )
```

with

```python
        document_parts=document_parts,
        locator_overlaps=_locator_overlaps(units),
        part_overlaps=_part_overlaps(document_parts),
    )
```

and add `document_parts = _validated_documents(bundle)` before the `return`.

4. Replace `_source_locators` (lines 703-718) with:

```python
type _LocatorPair = tuple[EvidenceUnit, TextLocator | CodeLocator]


def _locator_overlaps(units: Mapping[str, EvidenceUnit]) -> dict[str, tuple[_LocatorPair, ...]]:
    """For each unit, every same-domain source locator overlapping one of its own
    (its own included), found by one sort-and-sweep per source domain."""
    grouped: dict[tuple[str, str, str], list[_LocatorPair]] = defaultdict(list)
    for owner in units.values():
        for candidate in owner.locators:
            domain = _source_domain(candidate)
            if domain is not None:
                assert isinstance(candidate, (TextLocator, CodeLocator))
                grouped[domain].append((owner, candidate))
    found: dict[str, list[_LocatorPair]] = defaultdict(list)
    for items in grouped.values():
        for owner, candidate in items:
            if candidate.char_start < candidate.char_end:
                found[owner.unit_id].append((owner, candidate))
        ranges = [(candidate.char_start, candidate.char_end) for _, candidate in items]
        for left, right in overlapping_pairs(ranges):
            found[items[left][0].unit_id].append(items[right])
            found[items[right][0].unit_id].append(items[left])
    return {unit_id: tuple(pairs) for unit_id, pairs in found.items()}


def _part_overlaps(
    document_parts: Mapping[str, tuple[DocumentSourcePart, ...]],
) -> dict[tuple[str, int], tuple[tuple[str, DocumentSourcePart], ...]]:
    """For each unit part, every same-domain part overlapping it, itself included."""
    grouped: dict[str, list[tuple[str, int, DocumentSourcePart]]] = defaultdict(list)
    for unit_id, parts in document_parts.items():
        for position, part in enumerate(parts):
            grouped[part.source_domain].append((unit_id, position, part))
    found: dict[tuple[str, int], list[tuple[str, DocumentSourcePart]]] = defaultdict(list)
    for items in grouped.values():
        for unit_id, position, part in items:
            if part.source_start < part.source_end:
                found[(unit_id, position)].append((unit_id, part))
        ranges = [(part.source_start, part.source_end) for _, _, part in items]
        for left, right in overlapping_pairs(ranges):
            left_unit, left_position, left_part = items[left]
            right_unit, right_position, right_part = items[right]
            found[(left_unit, left_position)].append((right_unit, right_part))
            found[(right_unit, right_position)].append((left_unit, left_part))
    return {key: tuple(pairs) for key, pairs in found.items()}
```

5. In `_locator_intervals` replace

```python
    domain = _source_domain(locator)
    assert domain is not None
    result: list[tuple[int, int, str]] = []
    for owner, candidate in index.source_locators.get(domain, ()):
```

with

```python
    result: list[tuple[int, int, str]] = []
    for owner, candidate in index.locator_overlaps.get(target.unit_id, ()):
```

The pair checks below it (slice equality, code coordinate mapping) are unchanged.

6. Replace the body of `_document_intervals` after `result: list[tuple[int, int, str]] = []` with:

```python
    for position, part in enumerate(target_parts):
        for owner_id, candidate in index.part_overlaps.get((target.unit_id, position), ()):
            owner = index.units[owner_id]
            left = max(part.source_start, candidate.source_start)
            right = min(part.source_end, candidate.source_end)
            if left < right:
                target_start = part.unit_start + left - part.source_start
                owner_start = candidate.unit_start + left - candidate.source_start
                length = right - left
                if (
                    target.text[target_start : target_start + length]
                    != owner.text[owner_start : owner_start + length]
                ):
                    raise ValueError("Retained document source slices differ across owners")
                result.append((target_start, target_start + length, owner.unit_id))
    return result
```

7. Append the facts derivation:

```python
class ChunkSpan(NamedTuple):
    """The range one chunk covers in its unit."""

    chunk_id: str
    unit_id: str
    char_start: int
    char_end: int


def _point_owners(units: Iterable[EvidenceUnit]) -> dict[str, set[str]]:
    """Units whose source range strictly contains the position of an empty unit."""
    spans: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    points: list[tuple[tuple[str, str], int, str]] = []
    for unit in units:
        for locator in unit.locators:
            if not isinstance(locator, (TextLocator, CodeLocator)):
                continue
            key = (locator.artifact_ref, locator.native_ref)
            if locator.char_start < locator.char_end:
                spans[key].append((locator.char_start, locator.char_end, unit.unit_id))
            else:
                points.append((key, locator.char_start, unit.unit_id))
    ordered = {key: sorted(items) for key, items in spans.items()}
    owners: dict[str, set[str]] = defaultdict(set)
    for key, point, unit_id in points:
        items = ordered.get(key, [])
        before = bisect_left([start for start, _, _ in items], point)
        owners[unit_id].update(owner for _, end, owner in items[:before] if end > point)
    return owners


def _unit_dependencies(
    unit: EvidenceUnit,
    ownership: tuple[OwnedInterval, ...],
    revision_id: str,
    proxy: AssetProxyRow | None,
    point_owners: Mapping[str, set[str]],
) -> frozenset[str]:
    """A unit depends on its intervals' owners, else on its declared or containing owners."""
    covered = frozenset(dep for interval in ownership for dep in interval.dependency_ids)
    if covered:
        return covered
    declared = unit.attributes.get(
        "overlapping_owner_ids", unit.attributes.get("range_dependency_ids")
    )
    owners = (
        set(cast(tuple[str, ...], declared))
        if declared is not None
        else set(point_owners.get(unit.unit_id) or {unit.unit_id})
    )
    owners.add(unit.unit_id)
    if proxy is not None:
        owners.add(proxy.asset_id)
        owners.update(proxy.required_unit_ids)
    return frozenset({revision_id, *owners})


def representation_facts(
    rows: RepresentationRows,
    prepared: PreparedExtraction,
    chunks: Iterable[ChunkSpan],
) -> RepresentationFacts:
    """Every dependency set the units, chunks, assets and intervals of one extraction need.

    A chunk depends on the owners of the intervals it covers; an asset on itself
    and every unit linked to it; an interval's set is stored under its digest.
    """
    revision = rows.revision.revision_id
    ownership = {item.unit.unit_id: item.ownership for item in prepared.units}
    proxies = {proxy.proxy_unit_id: proxy for proxy in rows.proxies}
    points = _point_owners(rows.units)
    units = {
        unit.unit_id: _unit_dependencies(
            unit, ownership[unit.unit_id], revision, proxies.get(unit.unit_id), points
        )
        for unit in rows.units
    }
    owners: dict[str, frozenset[str]] = dict(units)
    for chunk in chunks:
        owners[chunk.chunk_id] = frozenset(
            dep
            for interval in ownership[chunk.unit_id]
            if interval.char_start < chunk.char_end and chunk.char_start < interval.char_end
            for dep in interval.dependency_ids
        ) or units[chunk.unit_id]
    linked: dict[str, set[str]] = defaultdict(set)
    for link in rows.unit_assets:
        linked[link.asset_id].add(link.unit_id)
    for asset in rows.assets:
        owners[asset.asset_id] = frozenset(
            {revision, asset.asset_id}.union(*(units[uid] for uid in linked[asset.asset_id]))
        )
    intervals = []
    for unit_id, owned in ownership.items():
        for interval in owned:
            key = dependency_digest(interval.dependency_ids)
            owners.setdefault(key, interval.dependency_ids)
            intervals.append(
                IntervalRow(
                    unit_id=unit_id,
                    char_start=interval.char_start,
                    char_end=interval.char_end,
                    dependency_digest=key,
                )
            )
    return RepresentationFacts(
        extraction_id=rows.extraction.extraction_id,
        dependencies=tuple(
            OwnerDependencies(owner_id=owner, dependency_ids=deps)
            for owner, deps in sorted(owners.items())
        ),
        intervals=tuple(intervals),
    )
```

(`OwnedInterval` and `AssetProxyRow` are already imported by the module.)

- [ ] **Step 6: Run the sweep tests**

Run: `uv run --locked pytest tests/indexing/test_interval_sweep.py -v`
Expected: PASS (4 tests; the random oracle checks 1,000 interval sets).

- [ ] **Step 7: Derive eligibility dependencies from the facts and delete `_owners_by_locator`**

In `src/tychora/adapters/sqlite/eligibility.py`:

1. Delete `_owners_by_locator` (lines 61-75).
2. Change the import `from tychora.indexing.representations import prepare_extraction` to `from tychora.indexing.representations import ChunkSpan, prepare_extraction, representation_facts` (same module, so no new import edge).
3. Replace the block from `unit_rows = db.execute(` (line 497) to the end of the chunk loop (line 635, just before `chunks_by_id = ...`) with:

```python
            unit_rows = db.execute(
                "SELECT unit_id,unit_type,citable FROM evidence_units WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT chunk_id,unit_id,context_group_key,char_start,char_end FROM chunks WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            retained = retained_rows(store, member)
            dependencies = representation_facts(
                retained,
                prepare_extraction(retained),
                (ChunkSpan(chunk, uid, start, end) for chunk, uid, _, start, end in chunk_rows),
            ).dependency_map()
            proxy_assets = {p.proxy_unit_id: p.asset_id for p in retained.proxies}
            for uid, unit_type, citable in unit_rows:
                facets = []
                for key, text, number, boolean in db.execute(
                    "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values WHERE unit_id=?",
                    (uid,),
                ):
                    facets.append(
                        (
                            key,
                            text
                            if text is not None
                            else boolean
                            if boolean is not None
                            else number,
                        )
                    )
                unit_facts.append(
                    UnitFact(
                        uid,
                        member.revision_id,
                        member.extraction_id,
                        unit_type,
                        store.review_state(ReviewTarget(unit_id=uid)),
                        bool(citable),
                        dependencies[uid],
                        proxy_asset_id=proxy_assets.get(uid),
                        facets=tuple(facets),
                    )
                )
            source_asset_owners: dict[str, set[str]] = defaultdict(set)
            for uid, aid in db.execute(
                "SELECT ua.unit_id,ua.asset_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=? AND ua.role='source'",
                (member.extraction_id,),
            ):
                source_asset_owners[aid].add(uid)
            for (aid,) in db.execute(
                "SELECT asset_id FROM assets WHERE extraction_id=?",
                (member.extraction_id,),
            ):
                asset_facts.append(
                    AssetFact(
                        aid,
                        member.revision_id,
                        store.review_state(ReviewTarget(asset_id=aid)),
                        dependencies[aid],
                        source_owner_ids=frozenset(source_asset_owners.get(aid, ())),
                    )
                )
            unit_ordinals = {
                uid: ordinal
                for uid, ordinal in db.execute(
                    "SELECT unit_id,ordinal FROM evidence_units WHERE extraction_id=?",
                    (member.extraction_id,),
                )
            }
            for chunk_id, uid, group, start, _end in chunk_rows:
                chunk_facts.append(ChunkFact(chunk_id, uid, dependencies[chunk_id], group))
                chunk_order[chunk_id] = (
                    unit_ordinals[uid],
                    start,
                    chunk_id,
                    member.extraction_id,
                )
```

`json` is still used by the module (other readers); `defaultdict` too.

- [ ] **Step 8: Run the affected suites**

Run: `uv run --locked pytest tests/indexing tests/retrieval tests/application tests/storage -q`
Expected: PASS. `tests/retrieval/test_eligibility.py` pins the facts that `read_eligibility_facts` returns; it must pass unchanged.

- [ ] **Step 9: Confirm the deletion**

Run: `rg -n "_owners_by_locator|_source_locators|index\.source_locators" src/tychora`
Expected: no match.

- [ ] **Step 10: Commit**

```bash
git add src/tychora/indexing/intervals.py src/tychora/indexing/representations.py \
  src/tychora/contracts/indexing.py src/tychora/adapters/sqlite/eligibility.py \
  tests/indexing/test_interval_sweep.py
git commit -m "perf: compute ownership overlaps with a sort-and-sweep"
```

---

### Task 8: Persisted representation facts

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (two tables after `chunks`, line 194), `src/tychora/adapters/sqlite/migrations.py:12-16` (`SCHEMA_CHECKSUM`, `EARLIER_FORMAT_TWO_CHECKSUMS`)
- Create: `src/tychora/adapters/sqlite/repositories/representations.py`
- Modify: `src/tychora/adapters/sqlite/repositories/unit_of_work.py` (`SqliteTransaction.representations`), `repositories/__init__.py` (export)
- Modify: `src/tychora/ports/repositories.py` (`UnitRow`, `ChunkRow`, `IntervalRow` re-export, `RepresentationRepository`, `ReadTransaction.representations`), `src/tychora/ports/corpus.py:13`
- Modify: `src/tychora/adapters/sqlite/corpus.py:255-497` (`commit_extraction` writes the facts)
- Modify: `src/tychora/indexing/groups.py` (`group_facts`), `src/tychora/indexing/coordinator.py:385-391`, `:415-489`, `:602-635`, `:729-794`, `:1176-1189`
- Test: `tests/storage/test_representation_facts.py` (new), `tests/storage/test_schema.py` (extend)

**Interfaces:**
- Consumes: Task 7 `RepresentationFacts`, `OwnerDependencies`, `IntervalRow`, `ChunkSpan`, `representation_facts`; phase 1 `require_write`, `json_each` binding (`repositories/batches.py:256-262`).
- Produces:
  - DDL `representation_dependencies(owner_id, dependency_id, PRIMARY KEY(owner_id, dependency_id)) STRICT, WITHOUT ROWID` and `ownership_intervals(unit_id REFERENCES evidence_units, char_start, char_end, dependency_digest, PRIMARY KEY(unit_id, char_start)) STRICT`.
  - `ports/repositories.py`: `class UnitRow(Contract): unit_id; extraction_id; boundary_key; ordinal: int; text_sha; char_count: int`; `class ChunkRow(Contract): chunk_id; unit_id; extraction_id; char_start: int; char_end: int; context_group_key: str`; `IntervalRow` (re-exported from `contracts/indexing.py`); `class RepresentationRepository(Protocol)` with `units(unit_ids) -> tuple[UnitRow, ...]`, `chunks(chunk_ids) -> tuple[ChunkRow, ...]`, `dependencies(owner_ids) -> Mapping[str, frozenset[str]]`, `intervals(unit_ids) -> tuple[IntervalRow, ...]`, `write(facts: RepresentationFacts) -> None` (idempotent; a different set for a stored owner is `INTEGRITY_ERROR` reason `canonical.binding_mismatch`). Reads take ID tuples bound through one `json_each` parameter and use explicit column lists. `neighbours(unit_id, radius)` is added by phase 4, its first caller.
  - `ReadTransaction.representations: RepresentationRepository`; `SqliteTransaction.representations`.
  - `CorpusStore.commit_extraction(bundle: ExtractionBundle, facts: RepresentationFacts | None = None) -> str` (port and adapter); the coordinator always passes facts.
  - `group_facts(extraction_id: str, groups: Iterable[ContextGroup]) -> RepresentationFacts` in `indexing/groups.py`; text batches write their groups' dependency sets in the batch transaction.

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_representation_facts.py
"""Representation facts written at commit and read back by ID tuples."""

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.errors import TychoraError
from tychora.contracts.indexing import OwnerDependencies, RepresentationFacts
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ports.execution import ExecutionContext


def _facts(bundle):
    return representation_facts(
        bundle,
        prepare_extraction(bundle),
        (ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end) for c in bundle.chunks),
    )


@pytest.fixture
def store(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    yield CorpusStore(db, registry=registry(), artifacts=reader())
    db.close()


def test_commit_writes_dependencies_and_intervals(store):
    bundle = seed_bundle("Seal checks are quarterly.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    with store.uow.read() as tx:
        stored = tx.representations.dependencies(tuple(facts.dependency_map()))
        intervals = tx.representations.intervals(tuple(u.unit_id for u in bundle.units))
    assert stored == facts.dependency_map()
    assert intervals == facts.intervals


def test_recommit_is_idempotent(store):
    bundle = seed_bundle("Record the result.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    store.commit_extraction(bundle, facts)
    with store.uow.write() as tx:
        tx.representations.write(facts)
    count = store.db.execute("SELECT COUNT(*) FROM representation_dependencies").fetchone()[0]
    assert count == sum(len(item.dependency_ids) for item in facts.dependencies)
    intervals = store.db.execute("SELECT COUNT(*) FROM ownership_intervals").fetchone()[0]
    assert intervals == len(facts.intervals)


def test_conflicting_facts_are_refused(store):
    bundle = seed_bundle("Record the result.")
    facts = _facts(bundle)
    store.commit_extraction(bundle, facts)
    changed = RepresentationFacts(
        extraction_id=facts.extraction_id,
        dependencies=(
            OwnerDependencies(
                owner_id=bundle.units[0].unit_id, dependency_ids=frozenset({"other"})
            ),
        ),
    )
    with pytest.raises(TychoraError) as error:
        with store.uow.write() as tx:
            tx.representations.write(changed)
    assert error.value.reason == "canonical.binding_mismatch"


def test_facts_of_another_extraction_are_refused(store):
    with pytest.raises(TychoraError) as error:
        store.commit_extraction(
            seed_bundle("Record the result."), _facts(seed_bundle("Different text."))
        )
    assert error.value.reason == "canonical.binding_mismatch"
    assert store.db.execute("SELECT COUNT(*) FROM evidence_units").fetchone()[0] == 0


def test_write_requires_a_write_transaction(store):
    bundle = seed_bundle("Record the result.")
    with pytest.raises(TychoraError) as error:
        with store.uow.read() as tx:
            tx.representations.write(_facts(bundle))
    assert error.value.reason == "invariant.unexpected"


def test_repository_reads_bind_id_tuples(store):
    bundle = seed_bundle("Seal checks are quarterly.")
    store.commit_extraction(bundle, _facts(bundle))
    unit, chunk = bundle.units[0], bundle.chunks[0]
    padding = tuple(f"missing-{index}" for index in range(999))
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    try:
        with store.uow.read() as tx:
            (unit_row,) = tx.representations.units((unit.unit_id, *padding))
            (chunk_row,) = tx.representations.chunks((chunk.chunk_id, *padding))
            dependencies = tx.representations.dependencies((unit.unit_id, *padding))
    finally:
        store.db.set_trace_callback(None)
    assert (unit_row.boundary_key, unit_row.ordinal, unit_row.text_sha, unit_row.char_count) == (
        unit.boundary_key,
        unit.ordinal,
        unit.text_sha,
        len(unit.text),
    )
    assert (chunk_row.unit_id, chunk_row.char_start, chunk_row.char_end) == (
        chunk.unit_id,
        chunk.char_start,
        chunk.char_end,
    )
    assert chunk_row.context_group_key == chunk.context_group_key
    assert set(dependencies) == {unit.unit_id}
    assert sum("json_each" in statement for statement in statements) == 3


def test_published_add_persists_what_eligibility_derives(tmp_path):
    text = (
        "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
        "# Valves\n\nReplace worn valves.\n"
    )
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "manual.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": text,
                "metadata": {"title": "Pumps"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner

        def read():
            store = owner.store
            facts = store.read_eligibility_facts(store.resolve_snapshot(None))
            owners = (
                *(fact.unit_id for fact in facts.units),
                *(fact.chunk_id for fact in facts.chunks),
                *(fact.asset_id for fact in facts.assets),
            )
            with store.uow.read() as tx:
                return facts, tx.representations.dependencies(owners)

        facts, stored = owner.run_blocking_sync(read, ExecutionContext())
    assert facts.units and facts.chunks
    for fact in facts.units:
        assert stored[fact.unit_id] == fact.dependency_ids
    for fact in facts.chunks:
        assert stored[fact.chunk_id] == fact.dependency_ids
    for fact in facts.assets:
        assert stored[fact.asset_id] == fact.dependency_ids
```

Append to `tests/storage/test_schema.py`:

```python
PHASE_TWO_CHECKSUM = "2415f138aa1778f80ea655acaf59eeaf7d7c7dc28fa3d983d78222875127c4f1"


def test_phase_two_checksum_reports_format_old(tmp_path):
    assert PHASE_TWO_CHECKSUM in EARLIER_FORMAT_TWO_CHECKSUMS
    path = tmp_path / "corpus.sqlite3"
    db = open_corpus(path, initialize=True)
    db.execute("UPDATE schema_migrations SET checksum=?", (PHASE_TWO_CHECKSUM,))
    db.close()
    with pytest.raises(TychoraError) as error:
        open_corpus(path)
    assert error.value.reason == "workspace.format_old"


def test_representation_fact_tables_exist(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()
    assert {"representation_dependencies", "ownership_intervals"} <= names
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_representation_facts.py tests/storage/test_schema.py -v`
Expected: FAIL: `commit_extraction() takes 2 positional arguments but 3 were given`, `AttributeError: 'SqliteTransaction' object has no attribute 'representations'`, and `assert '2415f138…' in (...)`.

- [ ] **Step 3: Add the tables and the checksum**

In `src/tychora/resources/migrations/0001.sql`, after the `chunks` table (line 194) add:

```sql
CREATE TABLE representation_dependencies (
  owner_id TEXT NOT NULL,
  dependency_id TEXT NOT NULL,
  PRIMARY KEY(owner_id, dependency_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE ownership_intervals (
  unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  char_start INTEGER NOT NULL CHECK(char_start>=0),
  char_end INTEGER NOT NULL CHECK(char_end>char_start),
  dependency_digest TEXT NOT NULL,
  PRIMARY KEY(unit_id, char_start)
) STRICT;
```

Compute the new checksum:

Run: `uv run --locked python -c "from hashlib import sha256; from tychora.adapters.sqlite.migrations import migration_sql; print(sha256(migration_sql().encode()).hexdigest())"`
Expected: one 64-character hex digest.

In `src/tychora/adapters/sqlite/migrations.py` set `SCHEMA_CHECKSUM` to that digest and append the previous value to the earlier receipts:

```python
EARLIER_FORMAT_TWO_CHECKSUMS = (
    "5899bf5b00ec52d686602369801491bdc6e50d6e9dd803e790bb1cd32d15cabe",
    "2415f138aa1778f80ea655acaf59eeaf7d7c7dc28fa3d983d78222875127c4f1",
)
```

- [ ] **Step 4: Declare the port**

In `src/tychora/ports/repositories.py` add `from tychora.contracts.indexing import IntervalRow, RepresentationFacts` (this also re-exports `IntervalRow` under the index's path), then before `class ReadTransaction`:

```python
class UnitRow(Contract):
    """One evidence unit's identity, place and text digest, without its text."""

    unit_id: Identifier
    extraction_id: Identifier
    boundary_key: NonEmptyStr
    ordinal: NonNegativeInt
    text_sha: Sha256
    char_count: NonNegativeInt


class ChunkRow(Contract):
    """One chunk's range in its unit and its context group."""

    chunk_id: Identifier
    unit_id: Identifier
    extraction_id: Identifier
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    context_group_key: NonEmptyStr


class RepresentationRepository(Protocol):
    """Write-time representation facts; each read binds its ID tuple once."""

    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]:
        """Return the stored units among these IDs; unknown IDs are skipped."""
        ...

    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]:
        """Return the stored chunks among these IDs; unknown IDs are skipped."""
        ...

    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]:
        """Return the dependency set of each stored owner among these IDs."""
        ...

    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]:
        """Return the ownership intervals of these units in unit and start order."""
        ...

    def write(self, facts: RepresentationFacts) -> None:
        """Store facts once; a repeat is a no-op and a different set is refused."""
        ...
```

and add `representations: RepresentationRepository` to `ReadTransaction`. In `src/tychora/ports/corpus.py` change `commit_extraction` to

```python
    def commit_extraction(
        self, bundle: ExtractionBundle, facts: RepresentationFacts | None = None
    ) -> str: ...
```

with `from tychora.contracts.indexing import RepresentationFacts`.

- [ ] **Step 5: Implement the repository**

```python
# src/tychora/adapters/sqlite/repositories/representations.py
"""Persisted dependency sets and ownership intervals of committed extractions."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping

from pydantic import ValidationError

from tychora.contracts.errors import TychoraError
from tychora.contracts.indexing import IntervalRow, RepresentationFacts
from tychora.ports.repositories import ChunkRow, UnitRow

from .workspace import require_write

_IN_IDS = "IN (SELECT value FROM json_each(?))"
_UNITS = (
    "SELECT unit_id,extraction_id,boundary_key,ordinal,text_sha,length(text) "
    f"FROM evidence_units WHERE unit_id {_IN_IDS} ORDER BY extraction_id,ordinal"
)
_CHUNKS = (
    "SELECT chunk_id,unit_id,extraction_id,char_start,char_end,context_group_key "
    f"FROM chunks WHERE chunk_id {_IN_IDS} ORDER BY extraction_id,unit_id,char_start"
)
_DEPENDENCIES = (
    "SELECT owner_id,dependency_id FROM representation_dependencies "
    f"WHERE owner_id {_IN_IDS}"
)
_INTERVALS = (
    "SELECT unit_id,char_start,char_end,dependency_digest FROM ownership_intervals "
    f"WHERE unit_id {_IN_IDS} ORDER BY unit_id,char_start"
)


def _ids(values: tuple[str, ...]) -> str:
    return json.dumps(sorted(set(values)))


def _row_invalid() -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "Representation row is invalid.",
        reason="canonical.row_invalid",
    )


def _differs(owner: str) -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "Stored representation facts differ for " + owner,
        reason="canonical.binding_mismatch",
    )


class SqliteRepresentationRepository:
    """Reads by ID tuples and idempotent writes of representation facts."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db
        self._writable = writable

    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]:
        """Return the stored units among these IDs; unknown IDs are skipped."""
        rows = self._db.execute(_UNITS, (_ids(unit_ids),)).fetchall()
        try:
            return tuple(
                UnitRow(
                    unit_id=row[0],
                    extraction_id=row[1],
                    boundary_key=row[2],
                    ordinal=row[3],
                    text_sha=row[4],
                    char_count=row[5],
                )
                for row in rows
            )
        except ValidationError:
            raise _row_invalid() from None

    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]:
        """Return the stored chunks among these IDs; unknown IDs are skipped."""
        rows = self._db.execute(_CHUNKS, (_ids(chunk_ids),)).fetchall()
        try:
            return tuple(
                ChunkRow(
                    chunk_id=row[0],
                    unit_id=row[1],
                    extraction_id=row[2],
                    char_start=row[3],
                    char_end=row[4],
                    context_group_key=row[5],
                )
                for row in rows
            )
        except ValidationError:
            raise _row_invalid() from None

    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]:
        """Return the dependency set of each stored owner among these IDs."""
        found: dict[str, set[str]] = defaultdict(set)
        for owner, dependency in self._db.execute(_DEPENDENCIES, (_ids(owner_ids),)):
            found[owner].add(dependency)
        return {owner: frozenset(dependencies) for owner, dependencies in found.items()}

    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]:
        """Return the ownership intervals of these units in unit and start order."""
        rows = self._db.execute(_INTERVALS, (_ids(unit_ids),)).fetchall()
        try:
            return tuple(
                IntervalRow(
                    unit_id=row[0],
                    char_start=row[1],
                    char_end=row[2],
                    dependency_digest=row[3],
                )
                for row in rows
            )
        except ValidationError:
            raise _row_invalid() from None

    def write(self, facts: RepresentationFacts) -> None:
        """Store facts once; a repeat is a no-op and a different set is refused."""
        require_write(self._db, self._writable)
        expected = facts.dependency_map()
        stored = self.dependencies(tuple(expected))
        for owner, dependencies in stored.items():
            if dependencies != expected[owner]:
                raise _differs(owner)
        self._db.executemany(
            "INSERT INTO representation_dependencies(owner_id,dependency_id) VALUES (?,?)",
            [
                (owner, dependency)
                for owner, dependencies in sorted(expected.items())
                if owner not in stored
                for dependency in sorted(dependencies)
            ],
        )
        existing = {
            (row.unit_id, row.char_start): row
            for row in self.intervals(tuple({row.unit_id for row in facts.intervals}))
        }
        for row in facts.intervals:
            current = existing.get((row.unit_id, row.char_start))
            if current is not None and current != row:
                raise _differs(row.unit_id)
        self._db.executemany(
            "INSERT INTO ownership_intervals(unit_id,char_start,char_end,dependency_digest) "
            "VALUES (?,?,?,?)",
            [
                (row.unit_id, row.char_start, row.char_end, row.dependency_digest)
                for row in facts.intervals
                if (row.unit_id, row.char_start) not in existing
            ],
        )
```

In `repositories/unit_of_work.py` import `RepresentationRepository` from the port and `SqliteRepresentationRepository` from `.representations`, declare `representations: RepresentationRepository` on `SqliteTransaction`, and add `self.representations = SqliteRepresentationRepository(db, writable=writable)` in its `__init__`. In `repositories/__init__.py` import and list `SqliteRepresentationRepository` in `__all__`.

- [ ] **Step 6: Write the facts in the commit transaction**

In `src/tychora/adapters/sqlite/corpus.py` add `SqliteRepresentationRepository` to the `tychora.adapters.sqlite.repositories` import and `from tychora.contracts.indexing import RepresentationFacts`; change the signature to

```python
    def commit_extraction(
        self, bundle: ExtractionBundle, facts: RepresentationFacts | None = None
    ) -> str:
```

and after the `identifiers` loop (line 494, still inside `self._write()`) add:

```python
                if facts is not None:
                    if facts.extraction_id != bundle.extraction.extraction_id:
                        raise TychoraError(
                            "INTEGRITY_ERROR",
                            "Representation facts belong to another extraction",
                            reason="canonical.binding_mismatch",
                        )
                    SqliteRepresentationRepository(self.db, writable=True).write(facts)
```

- [ ] **Step 7: Compute facts in the coordinator**

Append to `src/tychora/indexing/groups.py` (imports: `Iterable`, `OwnerDependencies`, `RepresentationFacts` from `contracts.indexing`):

```python
def group_facts(extraction_id: str, groups: Iterable[ContextGroup]) -> RepresentationFacts:
    """The dependency set of each context group, keyed by group ID."""
    found = {group.group_id: group.dependency_ids for group in groups}
    return RepresentationFacts(
        extraction_id=extraction_id,
        dependencies=tuple(
            OwnerDependencies(owner_id=group_id, dependency_ids=dependencies)
            for group_id, dependencies in sorted(found.items())
        ),
    )
```

In `src/tychora/indexing/coordinator.py`:

1. Imports: `from tychora.contracts.indexing import ContextGroup, FamilySpec, PreparedExtraction, RepresentationFacts, SearchWindow`; `from tychora.indexing.groups import build_groups, group_facts`; `from tychora.indexing.representations import ChunkSpan, prepare_extraction, representation_facts`.

2. `_windows` takes the prepared extraction:

```python
    def _windows(
        self, prepared: PreparedExtraction, kind_id: str
    ) -> tuple[SearchWindow, ...]:
        level = self.store.registry.context_level(kind_id)
        return tuple(
            window
            for item in prepared.units
            for window in window_unit(item, self.counter, context_level=level)
        )
```

3. `_commit_or_reuse(self, bundle: ExtractionBundle, facts: RepresentationFacts) -> None`: in the reuse branch, after `self.store._check_membership(bundle)` add

```python
            with self.store.uow.write() as tx:
                tx.representations.write(facts)
```

and change the final call to `self.store.commit_extraction(bundle, facts)`.

4. In `prepare` replace the windowing and commit lines with:

```python
        if on_stage is not None:
            on_stage("windowing")
        extraction = prepare_extraction(bundle)
        windows = self._windows(extraction, bundle.revision.kind_id)
        canonical = self._canonical_bundle(bundle, windows)
        if on_stage is not None:
            on_stage("normalize_commit")
        facts = representation_facts(
            canonical,
            extraction,
            (
                ChunkSpan(row.chunk_id, row.unit_id, row.char_start, row.char_end)
                for row in canonical.chunks
            ),
        )
        self._commit_or_reuse(canonical, facts)
```

5. `_vector_batch` gains `facts: RepresentationFacts | None = None` and writes them with the rows:

```python
        def write(tx: WriteTransaction) -> None:
            tx.batches.write_vectors(batch.batch_id, rows)
            if facts is not None:
                tx.representations.write(facts)

        self._store_batch(batch, write)
        return batch
```

and `_text_batch` passes `group_facts(bundle.extraction.extraction_id, groups)` as that argument.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/storage/test_representation_facts.py tests/storage/test_schema.py tests/storage/test_format.py tests/architecture -v`
Expected: PASS; `test_no_sqlite_type_escapes` finds no store type or query parameter in `RepresentationRepository`.

- [ ] **Step 9: Run the affected suites**

Run: `uv run --locked pytest tests/storage tests/indexing tests/retrieval tests/application tests/operations tests/architecture -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/tychora/resources/migrations/0001.sql src/tychora/adapters/sqlite/migrations.py \
  src/tychora/adapters/sqlite/repositories/ src/tychora/ports/repositories.py \
  src/tychora/ports/corpus.py src/tychora/adapters/sqlite/corpus.py \
  src/tychora/indexing/groups.py src/tychora/indexing/coordinator.py \
  tests/storage/test_representation_facts.py tests/storage/test_schema.py
git commit -m "feat: persist representation dependencies and ownership intervals at commit"
```

---

### Task 9: Fixtures, gate, ratchet and index

**Files:**
- Modify: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` (phase 3 row, "Produced by phase 3", spec gaps)
- Modify: `quality/baseline.json` (through the checker, only where values improved)
- Not committed from macOS: `bench-results/` (the reference runner publishes results)

**Interfaces:** none new.

- [ ] **Step 1: Confirm no committed fixture embeds a changed identity**

Run: `rg -l '"(extraction_id|unit_id|chunk_id)"' tests/fixtures`
Expected: only `tests/fixtures/results/canonical.json` and `tests/fixtures/results/hierarchical.json`. Then run `rg -n "fixtures/results" tests src tools`; expected: no match (these rendered examples are read by no test). The evaluation fixtures (`tests/fixtures/evaluation/corpus.json`, `code-gold/*`) hold texts, labels by native reference, and metrics, not identities. If the full suite in Step 2 fails on a fixture comparison, regenerate that fixture with the script that produced it (`tests/fixtures/documents/create_rich_fixtures.py`, `create_record_fixtures.py`) and list it in the commit message.

- [ ] **Step 2: Full suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 3: Deletions owned by this phase**

Run: `rg -n "_owners_by_locator|_source_locators|index\.source_locators|native-normalizer-v1|docling-normalizer-v2|code-normalizer-v1" src/tychora`
Expected: no match.

- [ ] **Step 4: Code health**

Run: `uv run --locked python -m tools.health check --test-reports without`
Expected: PASS, or "stale baseline" naming only improved metrics (complexity of `read_eligibility_facts`, `_locator_intervals`, `_document_intervals`; the deleted `_owners_by_locator`). G12 must report no dead code: every new public name has a caller or sits in `contracts/sections.py`'s `__all__`. If stale:

Run: `uv run --locked python -m tools.health update --test-reports without`
Expected: `quality/baseline.json` rewritten with only improved values. G16–G18 (coverage and test runtime) are never updated from macOS; the CI run of the pull request records them with `tools.health update --gates G16 --test-reports only` from its merged reports, as phase 2 did.

- [ ] **Step 5: Benchmark, informative on macOS**

Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline --out "$TMPDIR/bench-phase3"`
Expected: exit 0 and a result whose `stages.windowing` row exists. Read it:

Run: `uv run --locked python -c "import json, sys; s = json.load(open(sys.argv[1]))['stages']; print({k: s[k]['p90'] for k in ('windowing', 'normalize_commit', 'total') if k in s})" "$TMPDIR"/bench-phase3/*/ingest-offline.json`
Expected: `windowing` p90 well under 1.0 s. Do not copy this result into the repository.

bench-small holds short synthetic documents; the 100-page bound is probed on one whole-section unit of about 300,000 characters (100 pages of 3,000 characters), the worst case for windowing:

```bash
uv run --locked python - <<'EOF'
import time

from tests.indexing.test_windows_linear import _representation
from tychora.indexing.windows import window_unit
from tychora.interfaces.bench import BenchCounter

page = ("The pump seal is inspected every quarter and recorded. " * 6 + "\n\n") * 9
unit = _representation(page * 100)
runs = []
for _ in range(10):
    started = time.perf_counter()
    window_unit(unit, BenchCounter())
    runs.append(time.perf_counter() - started)
print(f"chars={len(unit.unit.text)} p90={sorted(runs)[8]:.3f}s")
EOF
```

Expected: `p90` below 1.0 s (informative on macOS).

- [ ] **Step 6: The phase gate on the reference runner**

Gate (index, "Benchmark gates per phase", phase 3): windowing property test green; stage `windowing` p90 ≤ 1.0 s per 100-page native document; no regression > 20% on any other ingest stage.

1. The property test is `tests/indexing/test_windows_linear.py::test_linear_windows_equal_reference_on_1000_random_texts` (Step 2).
2. Ask the owner to dispatch the `Benchmarks` workflow (`.github/workflows/bench.yml`) on the phase-3 branch head; a development session never dispatches it. It runs `bench run --corpus bench-small --suite ingest --profile offline` on `github-ubuntu-22.04` and publishes to the `bench-results` branch.
3. With the new result `NEW` and the latest phase-2 reference result `OLD` (same runner class) fetched from `bench-results`:

Run: `uv run --locked python -c "import sys; from pathlib import Path; from tychora.evaluation.bench.results import load_result, regressions; new, old = (load_result(Path(p)) for p in sys.argv[1:]); print(new.stages['windowing'].p90, regressions(new, old))" NEW OLD`
Expected: a number ≤ 1.0 and `()`. A regression or a missed budget blocks the merge; report it rather than relaxing the gate.

- [ ] **Step 7: Record the phase in the index**

In `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`:

1. Phase table row 3, column "Detail": `Fully detailed`.
2. Replace the `### Produced by phase 3` code block with:

```python
# src/tychora/contracts/sections.py
SectionLevel = Literal["part", "chapter", "section", "article", "annex", "clause", "file"]
class Depth(Contract): n: PositiveInt
ContextLevel = SectionLevel | Depth | Literal["document"]
def parse_context_level(text: str) -> ContextLevel        # "article", "depth:2", "document"
class SectionEntry(Contract): key: NonEmptyStr; level: SectionLevel | None; depth: NonNegativeInt  # depth 0 = document root, depth == index in the path
BoundaryScheme = Literal["file", "section", "table", "article", "figure"]
def boundary_key(scheme: BoundaryScheme, native_ref: str) -> str   # "<scheme>:<native_ref>"
ROOT_SECTION_KEY = "section:#root"
def section_key(section_path: Sequence[SectionEntry], level: ContextLevel) -> str
def context_key(section_path: Sequence[SectionEntry], context_level: ContextLevel) -> str
def chunk_section_key(section_path: Sequence[SectionEntry], section_depth: int, context_level: ContextLevel) -> str
class HeadingLevel(Contract): pattern: NonEmptyStr; level: SectionLevel   # kind TOML key heading_levels
def section_path_of(attributes: Mapping, boundary: str) -> tuple[SectionEntry, ...]   # no stored path: the boundary key alone
def normalizer_definition(kind: Mapping) -> dict   # the kind without context_level; digested by every normalizer fingerprint

# src/tychora/configuration/registry.py (changed)
class Registry: def context_level(self, kind_id: str) -> ContextLevel: ...   # kind TOML key context_level, default "depth:1"

# src/tychora/ports/tokens.py (changed; TokenCounter itself is unchanged)
@runtime_checkable
class OffsetTokenCounter(TokenCounter, Protocol):
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]: ...  # char span per counted token

# src/tychora/indexing/windows.py (changed)
def window_unit(prepared, counter, *, context_level: ContextLevel = Depth(n=1)) -> tuple[SearchWindow, ...]
# src/tychora/contracts/indexing.py (changed): SearchWindow.context_key; context_group_key = digest("context-group-v1", extraction_id, context_key)
def dependency_digest(dependency_ids: Iterable[str]) -> str
class OwnerDependencies(Contract): owner_id; dependency_ids: frozenset[str]
class IntervalRow(Contract): unit_id; char_start: int; char_end: int; dependency_digest: Sha256
class RepresentationFacts(Contract): extraction_id; dependencies: tuple[OwnerDependencies, ...]; intervals: tuple[IntervalRow, ...]
# src/tychora/indexing/representations.py (added)
class ChunkSpan(NamedTuple): chunk_id; unit_id; char_start; char_end
def representation_facts(rows, prepared: PreparedExtraction, chunks: Iterable[ChunkSpan]) -> RepresentationFacts

# src/tychora/ports/repositories.py (added; IntervalRow re-exported from contracts/indexing.py)
class UnitRow(Contract): unit_id; extraction_id; boundary_key; ordinal: int; text_sha; char_count: int
class ChunkRow(Contract): chunk_id; unit_id; extraction_id; char_start: int; char_end: int; context_group_key: str
class RepresentationRepository(Protocol):
    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]: ...
    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]: ...
    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]: ...
    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]: ...
    def write(self, facts: RepresentationFacts) -> None: ...
    # neighbours(unit_id, radius) -> tuple[UnitRow, ...] (same boundary_key): added by phase 4, its first caller
# ReadTransaction.representations; CorpusStore.commit_extraction(bundle, facts: RepresentationFacts | None = None) -> str
```

and the sentence after it with:

> Schema added: `representation_dependencies(owner_id, dependency_id)` (owners: units, chunks, assets, vector groups, and interval dependency sets keyed by their `dependency_digest`), `ownership_intervals(unit_id, char_start, char_end, dependency_digest)`. Normalizer fingerprints become `native-normalizer-v2`, `docling-normalizer-v3`, `docling-record-normalizer-v3` and `code-normalizer-v2`. Reasons added: `kind.section_levels_invalid`, `kind.unknown`, `tokens.atomic_limit`. Job stages added: `windowing`, `normalize_commit`.

3. Append this plan's "Spec gaps" 1–13 to the index's "Spec gaps found while planning", numbered from 16.

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/plans/2026-10-16-engine-core-v2-index.md quality/baseline.json
git commit -m "chore: record phase 3 benchmark and health baseline"
```

(`quality/baseline.json` only if Step 4 rewrote it.)

---

## Spec gaps

Each gap names what the spec leaves undecided and the answer this plan applies.

1. **What `depth` counts.** §4.10 says "its heading depth (1 for a top-level chapter)". A document whose top headings are `##` would get depth 2 if the Markdown level were used. Answer: depth is the nesting depth (an entry's index in the path), so the outermost heading present is depth 1 whatever its markup level; the parser's level only decides nesting.
2. **Records below records.** "The record itself is the article" does not say what a numbered paragraph inside an article is. Answer: every provision or rule is its own boundary (`article:<its ref>`); a top-level one has level `article`, a nested one level `clause` with the enclosing records in its path. `annex` is assigned only through kind heading patterns: no records normalizer recognises annexes structurally today (`ingestion/normalize.py:533-540`).
3. **Units outside the scheme table.** Asset proxies and pixel-source units have no row. Answer: they keep `boundary_key = native_ref` and carry no `section_path`; `section_path_of` resolves such a unit to its boundary key, so its context group is unchanged.
4. **Tables split across pages.** Answer: each Docling table item is one `table:` boundary; its table and row views share it. Fragments are joined only by the reviewed `table-continuation` assertion (`ingestion/tables.py`), never by sharing a key, because a continuation can be rejected after ingestion.
5. **Identical windows need an additive tokenizer.** Counting a range from one tokenisation equals counting the range alone only when the tokenizer is additive at split positions; the spec's property test cannot hold for every tokenizer. Answer: `OffsetTokenCounter` states the contract, the property test checks it on additive counters, a counter without offsets or with unusable spans keeps the counted split, and Task 5 Step 10 qualifies the Voyage tokenizer (Open question 1 covers a failure).
6. **`context_level` versus pinned kinds.** Kind envelopes are digest-pinned (`Registry.validate_activation`, `configuration/registry.py:209-236`) and `extraction_fingerprint` digests every kind (`configuration/resolve.py:540-551`), so changing a retained kind's `context_level` is refused today however the normalizer fingerprints are computed. Answer: this phase excludes `context_level` from normalizer fingerprints, so the extraction ID is stable; exempting the field from the envelope digest belongs to the kind ontology (sub-project 5), which owns per-kind values.
7. **What `dependency_digest` names.** Answer: the digest of an interval's dependency set, which is stored in `representation_dependencies` with that digest as `owner_id`; phase 4 reads intervals and resolves their sets in the same query as the candidates' own sets.
8. **Vector-group dependencies are not known at commit.** Context groups depend on the text provider configuration and are built after `commit_extraction`. Answer: their sets are written with the text batch, in the batch's transaction, keyed by group ID.
9. **Fingerprints the spec does not name.** Answer: `docling-record-normalizer-v3` and `code-normalizer-v2` bump too, since their output changes; composed documents follow automatically, because the composition fingerprint digests the base normalizer fingerprint.
10. **Root keys.** Answer: Markdown and Docling documents use `section:#root`; plain text and JSON records use `file:<source_key>` (text with no headings); a standalone image uses `figure:<ref>`; a code extraction uses `section:#root` above its `file:` entries, so `document` groups a whole repository.
11. **Measuring the windowing gate before traces.** Answer: the coordinator emits `windowing` and `normalize_commit` job stages; bench-small has no 100-page document, so a 300,000-character single-unit probe (Task 9 Step 5) stands in, informative off the reference runner.
12. **`depth:N` deeper than the unit.** §4.10's fallback reads "the depth-1 ancestor". Answer: applied literally, then raised to the context key by `chunk_section_key`'s "never coarser" rule.
13. **Fact reads before facts exist.** Extractions committed by tests without facts, and any committed by an earlier commit of this branch, have no rows. Answer: the coordinator's reuse path writes the facts of an extraction it finds already committed, so every published member has them; how a read reports a missing set is phase 4's decision (suggested default: `INTEGRITY_ERROR` reason `canonical.row_invalid`).

## Open questions for the owner

1. **If the Voyage tokenizer fails the additivity qualification (Task 5 Step 10):** keep the counted split for it (windows identical, C1 stays quadratic in production; the plan's default), or accept offset windows that may move by a token at split points, with a chunker fingerprint bump (`exact-windows-v2`)?

## Self-Review

**Spec coverage.** §4.10 boundary keys: `file:` code and text (Tasks 2, 4), `section:` native (2), `section:` Docling (3), picture and caption (3), `table:` (2, 3), `article:` (2, 3), `figure:` (3); `section_path` (2–4); closed vocabulary and `context_level` (1, 6); heading patterns applied, fingerprint bumps on change (4); `section_key`, `context_key`, `chunk_section_key` (1); computed at chunking time from the stored path (6); fingerprint bumps (2–4); paragraph-ref contract test over native fixtures (2) and Docling (3). Linear windowing, port method, reuse of counts, 1,000-text property test (5). §4.8 write side: sort-and-sweep and `_owners_by_locator` deletion (7); `representation_dependencies` for units, chunks, assets (7, 8) and vector groups (8); `ownership_intervals` (8). The read-side removal of `prepare_extraction` calls and `neighbours` are phase 4.

**Placeholder scan.** No TBD; every code step carries its code; the two runtime-dependent values (the new `SCHEMA_CHECKSUM`, the reference benchmark result) come with the command that produces them.

**Type consistency.** `Placement`/`Placeable`/`place_units` (Tasks 2–4), `ContextLevel`/`Depth` (1, 6), `SearchWindow.context_key` (6), `RepresentationFacts`/`OwnerDependencies`/`IntervalRow`/`ChunkSpan` (7, 8), `commit_extraction(bundle, facts)` (8), `_windows(prepared, kind_id)` and `prepare(..., on_stage=)` (5, 6, 8) are used with the same names and fields in every task.

**Review Focus.** Each item has its pinned test in the owning task: 1 and 2 in Task 2, 3 in Task 3, 4 in Task 5, 5 in Task 7.

**Index changes needed** (Task 9 Step 7 applies them): `TokenCounter` stays as is and `OffsetTokenCounter` carries `count_with_offsets`; `RepresentationRepository` gains `write` and defers `neighbours` to phase 4; `IntervalRow` is defined in `contracts/indexing.py` and re-exported by the port; `CorpusStore.commit_extraction` takes optional facts; `window_unit` takes `context_level`; two more fingerprint bumps; three reasons; two job stages.
