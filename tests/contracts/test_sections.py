"""Section levels, boundary keys and the context and chunking key resolver."""

import random
import re
import time

import pytest
import regex

from tychora.contracts import sections as sections_module
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.sections import (
    HEADING_MATCH_SECONDS,
    ROOT_SECTION_KEY,
    ContextLevel,
    Depth,
    HeadingBudget,
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
        boundary_key("paragraph", "block:3")  # ty: ignore[invalid-argument-type]
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
        path.append(
            entry(f"section:{depth}-{rng.randrange(1000)}", depth, rng.choice(levels))
        )
    return tuple(path)


def test_chunk_section_key_never_coarser_than_context_key():
    rng = random.Random(20261016)
    named: list[ContextLevel] = [
        "document",
        "part",
        "chapter",
        "section",
        "article",
        "annex",
        "clause",
        "file",
    ]
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


def _levels(pattern):
    return parse_heading_levels([{"pattern": pattern, "level": "article"}])


@pytest.mark.parametrize(
    "pattern", [r"^(a+)+$", r"(a*)*", r"(a|aa)+$", r"(\w+\s?)+$", r"^((a+)b)*$"]
)
def test_backtracking_prone_patterns_are_refused(pattern):
    with pytest.raises(ValueError, match="backtrack"):
        _levels(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^Article \d+",
        r"^Annex [A-Z]+",
        r"^(Part|Chapter) [IVXLC]+",
        r"^\d+(\.\d+)*\s",
        r"^(a+)?b",
    ],
)
def test_ordinary_heading_patterns_are_accepted(pattern):
    assert _levels(pattern)


def test_heading_match_is_bounded_on_adversarial_text():
    levels = _levels(r"^\d+(\.\d+)*\s")
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        assert heading_level_for(levels, "1." * 5000 + "!") is None
        best = min(best, time.perf_counter() - start)
    # Matching the whole 10k-character heading would take far longer than this.
    assert best < 0.5


def test_only_the_heading_prefix_is_matched():
    levels = _levels(r"^x+$")
    assert heading_level_for(levels, "x" * 256) == "article"
    assert heading_level_for(levels, "x" * 256 + "y") == "article"


def _timed(pattern):
    """Validate and match a pattern against adversarial headings; return seconds."""
    start = time.perf_counter()
    try:
        levels = _levels(pattern)
        for text in ("a" * 256, "a" * 255 + "!", "1" * 255 + "!", "a," * 128):
            heading_level_for(levels, text)
    except (ValueError, TychoraError):
        pass
    return time.perf_counter() - start


@pytest.mark.parametrize(
    "pattern",
    [r"^\w*\w*\w*\w*\w*!", r"^(\d+)\d*\d*\d*$", r".*.*.*.*x", r"(?:a.*){8}!"],
)
def test_polynomial_patterns_are_refused_or_time_out(pattern):
    assert _timed(pattern) < 1.5


def test_timeout_is_an_error_not_a_miss(monkeypatch):
    def slow(pattern, text, *, timeout):
        assert timeout == HEADING_MATCH_SECONDS
        raise TimeoutError

    monkeypatch.setattr(regex, "match", slow)
    with pytest.raises(TychoraError) as err:
        heading_level_for(_levels(r"^Article"), "Article 1")
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.section_levels_invalid"


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        (r"^Article \d+", "Article 12 Scope", True),
        (r"^Article \d+", "Article ٣ Scope", True),
        (r"^Annex [A-Z]+", "Annex II", True),
        (r"^(Part|Chapter) [IVXLC]+", "Chapter IV", True),
        (r"^\d+(\.\d+)*\s", "1.2.3 Terms", True),
        (r"^\d+(\.\d+)*\s", "Terms", False),
    ],
)
def test_regex_engine_matches_the_re_semantics(pattern, text, expected):
    assert bool(re.match(pattern, text)) == expected
    assert (heading_level_for(_levels(pattern), text) is not None) == expected


def test_heading_match_time_is_budgeted_per_document(monkeypatch):
    clock = iter(range(1000))
    monkeypatch.setattr(sections_module, "_monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(regex, "match", lambda pattern, text, *, timeout: None)
    levels = _levels(r"^Article")
    budget = HeadingBudget(seconds=2.5)
    heading_level_for(levels, "a", budget)
    with pytest.raises(TychoraError) as err:
        for _ in range(10):
            heading_level_for(levels, "a", budget)
    assert err.value.code == "INVALID_REQUEST"
    assert err.value.reason == "kind.section_levels_invalid"
