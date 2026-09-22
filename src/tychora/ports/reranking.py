"""Typed reranking port independent of provider transport."""

from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from tychora.contracts.base import (
    Contract,
    FiniteFloat,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from tychora.contracts.candidates import CandidateKey
from tychora.contracts.evidence import AnchorRange
from tychora.ports.execution import ExecutionContext


class RankingSpan(Contract):
    unit_id: Identifier
    char_start: NonNegativeInt
    char_end: PositiveInt
    text: NonEmptyStr
    role: Literal["match", "context"] = "match"
    dependency_ids: frozenset[Identifier] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_length(self) -> Self:
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("Ranking span differs from exact source range")
        return self


def render_ranking_input(
    source_kind: str,
    title: str,
    heading_path: tuple[str, ...],
    spans: tuple[RankingSpan, ...],
) -> str:
    lines = [f"Kind: {source_kind}", f"Title: {title}"]
    if heading_path:
        lines.append("Heading: " + " / ".join(heading_path))
    lines.extend(
        f"{span.role.title()} [{span.unit_id}:{span.char_start}:{span.char_end}]: {span.text}"
        for span in spans
    )
    return "\n".join(lines)


class RankingInput(Contract):
    key: CandidateKey
    text: NonEmptyStr
    source_kind: NonEmptyStr
    title: NonEmptyStr
    heading_path: tuple[str, ...] = ()
    spans: tuple[RankingSpan, ...] = Field(min_length=1, max_length=64)
    anchors: tuple[AnchorRange, ...] = ()
    dependency_ids: frozenset[Identifier] = Field(min_length=1)
    token_count: PositiveInt
    omitted_match_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def exact_render(self) -> Self:
        if not any(
            span.role == "match" for span in self.spans
        ) or self.text != render_ranking_input(
            self.source_kind, self.title, self.heading_path, self.spans
        ):
            raise ValueError("Ranking text differs from selected exact spans")
        return self


class RankedIndex(Contract):
    index: NonNegativeInt
    score: FiniteFloat


class RerankCapabilities(Contract):
    provider: NonEmptyStr
    endpoint: NonEmptyStr
    model: NonEmptyStr
    model_revision: NonEmptyStr
    tokenizer_id: NonEmptyStr
    tokenizer_sha256: Sha256
    truncation: Literal[False] = False
    max_query_tokens: PositiveInt
    max_representation_tokens: PositiveInt
    max_candidates: PositiveInt
    max_pair_tokens: PositiveInt
    max_aggregate_tokens: PositiveInt


class Reranker(Protocol):
    def rank(
        self,
        query: str,
        representations: tuple[RankingInput, ...],
        ctx: ExecutionContext,
    ) -> tuple[RankedIndex, ...]: ...
