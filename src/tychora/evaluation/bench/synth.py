"""Seeded synthetic benchmark documents with one unique marker each."""

import random
from typing import Literal

from tychora.contracts.base import Contract, NonEmptyStr, NonNegativeInt

VERIFICATION_SENTENCE = "The verification outcome was recorded for this control."
PROBE_QUESTION = "verification outcome"
WORDS = (
    "account", "action", "annex", "approval", "archive", "assessment", "audit",
    "balance", "boundary", "budget", "calendar", "capacity", "channel", "charter",
    "claim", "clause", "committee", "compliance", "contract", "control", "council",
    "criteria", "decision", "delivery", "deposit", "design", "duty", "edition",
    "evidence", "exception", "filing", "finding", "framework", "guidance", "harbor",
    "incident", "inventory", "issuer", "journal", "ledger", "license", "maintenance",
    "measure", "method", "notice", "obligation", "operator", "outline", "payment",
    "permit", "policy", "procedure", "process", "record", "register", "report",
    "requirement", "review", "schedule", "section", "standard", "statement",
    "supplier", "threshold",
)  # fmt: skip


class SynthSpec(Contract):
    """Which synthetic documents a corpus holds and how they are mixed."""

    seed: NonNegativeInt
    count: NonNegativeInt
    markdown_share: float = 0.6
    verification_share: float = 0.3
    queries: NonNegativeInt = 20


class SynthDocument(Contract):
    """One generated document and where its unique marker sits."""

    source_key: NonEmptyStr
    title: NonEmptyStr
    format: Literal["markdown", "text"]
    text: str
    marker: NonEmptyStr
    marker_start: NonNegativeInt
    has_verification: bool


def _paragraph(rng: random.Random) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(30, 90))]
    return " ".join(words).capitalize() + "."


def _render(title: str, sections: list[tuple[str, list[str]]], markdown: bool) -> str:
    prefix, heading = ("# ", "## ") if markdown else ("", "")
    blocks = [prefix + title] + [
        heading + name + "\n\n" + "\n\n".join(paragraphs)
        for name, paragraphs in sections
    ]
    return "\n\n".join(blocks) + "\n"


def document_at(spec: SynthSpec, index: int) -> SynthDocument:
    """Return the index-th synthetic document; identical for every run of a seed."""
    rng = random.Random(f"{spec.seed}:{index}")
    markdown = rng.random() < spec.markdown_share
    verification = rng.random() < spec.verification_share
    title = f"Synthetic {rng.choice(WORDS)} {index}"
    sections = [
        (
            f"Section {number + 1} {rng.choice(WORDS)}",
            [_paragraph(rng) for _ in range(rng.randint(2, 4))],
        )
        for number in range(rng.randint(2, 5))
    ]
    marker = f"zq{index:06d}x"
    target = sections[rng.randrange(len(sections))][1]
    target[0] = f"{target[0]} The {marker} marker identifies this passage."
    if verification:
        sections[-1][1].append(VERIFICATION_SENTENCE)
    text = _render(title, sections, markdown)
    return SynthDocument(
        source_key=f"synthetic/{index:06d}",
        title=title,
        format="markdown" if markdown else "text",
        text=text,
        marker=marker,
        marker_start=text.index(marker),
        has_verification=verification,
    )


def generate(spec: SynthSpec) -> tuple[SynthDocument, ...]:
    """Return the first ``spec.count`` synthetic documents."""
    return tuple(document_at(spec, index) for index in range(spec.count))
