"""Windows counted from one tokenisation per interval equal the counted split."""

import random
import re
from hashlib import sha256
from itertools import pairwise

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
    """One token per code point, counted only."""

    model = "synthetic-codepoint-counter"
    tokenizer_id = "synthetic-codepoint-counter@1"

    def count(self, text: str) -> int:
        """Count code points."""
        return len(text)


class OffsetCharacters(Characters):
    """One token per code point, with spans: additive, so the linear split applies."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """One span per code point."""
        return tuple((index, index + 1) for index in range(len(text)))


class Words:
    """One token per whitespace-separated word, counted only."""

    model = "synthetic-word-counter"
    tokenizer_id = "synthetic-word-counter@1"

    def count(self, text: str) -> int:
        """Count words."""
        return len(re.findall(r"\S+", text))


class OffsetWords(Words):
    """One token per word, with spans."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """One span per word."""
        return tuple(match.span() for match in re.finditer(r"\S+", text))


class Unordered(Characters):
    """Spans out of text order, so the counted split must be kept."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """Code point spans in reverse order."""
        return tuple((index, index + 1) for index in reversed(range(len(text))))


RUN = re.compile(r"\w+|\s+|[^\w\s]")


class Runs:
    """One token per word, whitespace or punctuation run, plus a fixed overhead."""

    model = "synthetic-run-counter"
    tokenizer_id = "synthetic-run-counter@1"
    overhead = 0

    def count(self, text: str) -> int:
        """Count runs and add the overhead."""
        return len(RUN.findall(text)) + self.overhead


class OffsetRuns(Runs):
    """Run spans: additive, with tokens that straddle ownership cuts."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """One span per run."""
        return tuple(match.span() for match in RUN.finditer(text))


class Runs2(Runs):
    """Runs with two fixed tokens, so ``count("")`` is not zero."""

    overhead = 2


class OffsetRuns2(OffsetRuns):
    """Run spans with two fixed tokens."""

    overhead = 2


class Doubled(Characters):
    """Two tokens per code point."""

    def count(self, text: str) -> int:
        """Count each code point twice."""
        return 2 * len(text)


class HalfSpans(Doubled):
    """Breaks the contract: one span per code point while ``count`` says two."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """One span per code point."""
        return tuple((index, index + 1) for index in range(len(text)))


class SpacelessSpans(Characters):
    """Breaks the contract: whitespace is counted but has no span."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """Spans for non-whitespace code points only."""
        return tuple(
            (index, index + 1)
            for index, character in enumerate(text)
            if not character.isspace()
        )


class NoSpans(Characters):
    """Reports no spans for nonempty text."""

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """No spans at all."""
        return ()


class Spy(OffsetCharacters):
    """Counts every character handed to the tokenizer."""

    def __init__(self) -> None:
        self.characters = 0

    def count(self, text: str) -> int:
        """Count code points and record the characters read."""
        self.characters += len(text)
        return len(text)

    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        """Code point spans, recording the characters read."""
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
            for start, end in pairwise(edges)
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
            pieces.append(
                "".join(
                    rng.choice("漢字仮名交じり文書") for _ in range(rng.randint(5, 300))
                )
                + "。"
            )
        elif roll < 0.8:
            pieces.append("é" * rng.randint(1, 200) + " ")
        elif roll < 0.9:
            pieces.append("x" * rng.randint(100, 1800))
        else:
            pieces.append(
                " ".join(rng.choice(WORDS) for _ in range(rng.randint(50, 900))) + " "
            )
    return "".join(pieces)


def _outcome(prepared: RepresentationUnit, counter) -> object:
    try:
        return window_unit(prepared, counter)
    except (ValueError, TychoraError) as error:
        return type(error).__name__, str(error), getattr(error, "reason", None)


@pytest.mark.parametrize(
    "fast,counted",
    [
        (OffsetCharacters, Characters),
        (OffsetWords, Words),
        (OffsetRuns, Runs),
        (OffsetRuns2, Runs2),
    ],
)
def test_linear_windows_equal_reference_on_1000_random_texts(fast, counted):
    """The offsets split gives the counted split's windows, counts and errors."""
    rng = random.Random(f"windows:{fast.__name__}")
    for _ in range(1000):
        text = _random_text(rng)
        cuts = tuple(rng.randrange(len(text)) for _ in range(rng.randint(0, 3)))
        prepared = _representation(text, cuts)
        assert _outcome(prepared, fast()) == _outcome(prepared, counted()), text[:80]


def test_long_unit_counts_tokens_in_linear_time():
    """A unit with hundreds of boundaries is tokenised in fewer than 2x its length."""
    text = "The pump seal is inspected every quarter and recorded. " * 400
    spy = Spy()
    windows = window_unit(_representation(text), spy)
    assert len(windows) > 20
    assert "".join(window.text for window in windows) == text
    assert all(window.token_count == len(window.text) for window in windows)
    assert spy.characters == len(text) + len(windows[0].text)
    assert spy.characters < 2 * len(text)


@pytest.mark.parametrize(
    "liar,counted",
    [
        (HalfSpans, Doubled),
        (SpacelessSpans, Characters),
        (NoSpans, Characters),
    ],
)
def test_spans_that_disagree_with_counts_fall_back_to_counting(liar, counted):
    """A counter breaking the offsets contract still gets the counted windows."""
    for text in ("Seal is fine. " * 200, ("word " * 150 + ". ") * 10):
        prepared = _representation(text, (len(text) // 3,))
        windows = window_unit(prepared, liar())
        assert windows == window_unit(prepared, counted())
        assert all(
            window.token_count == counted().count(window.text) for window in windows
        )


def test_tokens_straddling_cuts_and_fixed_tokens_keep_counted_windows():
    """Runs cut by ownership edges and a nonzero ``count("")`` match the counted split."""
    text = ("Inspect" + "x" * 900 + " the pump seal. ") * 40 + "valve" * 400
    cuts = tuple(range(3, len(text), 997))
    prepared = _representation(text, cuts)
    for fast, counted in ((OffsetRuns, Runs), (OffsetRuns2, Runs2)):
        assert _outcome(prepared, fast()) == _outcome(prepared, counted())


def test_counter_without_offsets_keeps_counted_windows():
    """Counters without usable spans, including the bench counter, keep the counted split."""
    assert isinstance(OffsetCharacters(), OffsetTokenCounter)
    assert not isinstance(Characters(), OffsetTokenCounter)
    assert not isinstance(BenchCounter(), OffsetTokenCounter)
    prepared = _representation("Seal. " * 300)
    assert window_unit(prepared, Unordered()) == window_unit(prepared, Characters())


def test_atomic_code_point_above_the_ceiling_has_a_reason():
    """Both splits refuse a code point counted above the window target."""

    class Heavy(Characters):
        def count(self, text: str) -> int:
            return 1000 * len(text)

    class OffsetHeavy(Heavy):
        def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
            return tuple(
                (index, index + 1) for index in range(len(text)) for _ in range(1000)
            )

    prepared = _representation("ab")
    for counter in (Heavy(), OffsetHeavy()):
        with pytest.raises(TychoraError) as raised:
            window_unit(prepared, counter)
        assert raised.value.reason == "tokens.atomic_limit"


def test_add_emits_windowing_commit_and_embedding_stages(tmp_path):
    """Prepare marks windowing, the commit and its own end before embedding."""
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
    assert (
        stages.index("windowing")
        < stages.index("normalize_commit")
        < stages.index("embedding")
        < stages.index("project")
    )
