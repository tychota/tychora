"""Ranked-owner search ports; each store restricts candidates to an eligibility view."""

from collections.abc import Mapping
from typing import Protocol

from tychora.contracts.base import Contract, Identifier, PositiveInt
from tychora.contracts.candidates import CandidateKey
from tychora.contracts.eligibility import EligibilityView
from tychora.ports.execution import ExecutionContext


class RankedOwner(Contract):
    """One evidence owner, its dense rank within its kind and its matching windows.

    ``window_scores``, when given, holds each window's own score in
    ``window_ids`` order; ``score`` is the best of them.
    """

    key: CandidateKey
    rank: PositiveInt
    score: float
    window_ids: tuple[Identifier, ...] = ()
    window_scores: tuple[float, ...] = ()


class RankedOwners(Contract):
    """A channel's owners; ``truncated`` when a window or hit limit bound the scan."""

    owners: tuple[RankedOwner, ...]
    truncated: bool = False
    detail: Mapping[str, int] = {}


class LexicalSearch(Protocol):
    """Full-text and identifier search restricted to a view."""

    def search(
        self,
        view: EligibilityView,
        query: str,
        limit: int,
        ctx: ExecutionContext,
        *,
        stopwords: frozenset[str] = frozenset(),
    ) -> RankedOwners:
        """Rank owners by their best matching window; ``limit`` owners per kind."""
        ...

    def exact(
        self,
        view: EligibilityView,
        identifiers: tuple[str, ...],
        limit: int,
        ctx: ExecutionContext,
    ) -> RankedOwners:
        """Units carrying any of the namespaced identifiers, in identifier order."""
        ...


class VectorSearch(Protocol):
    """Nearest-neighbour search restricted to a view."""

    def search(
        self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Rank owners by their nearest record; ``limit`` owners per kind."""
        ...
