"""Prepared query results as canonical JSON cache entries."""

from __future__ import annotations

from tychora.contracts.base import Contract
from tychora.contracts.evidence import (
    AnchorRange,
    Coverage,
    DecisionRequirement,
    RelationChecks,
    RelationPath,
    Segment,
    StructuredRecord,
    VisualEvidence,
)
from tychora.contracts.identity import canonical_bytes

CACHE_OVERHEAD_BYTES = 65536


class Selection(Contract):
    """Final segments and the budget they were selected under.

    It lives here, not in ``retrieval.segments``, so the cache entry imports
    no module of the query import cycle.
    """

    segments: tuple[Segment, ...]
    reservation_priority: tuple[str, ...]
    omitted_anchors: tuple[AnchorRange, ...]
    lane_coverage: tuple[str, ...]
    text_budget: int
    visual_reserved_tokens: int


class PreparedEvidence(Contract):
    """The prepared result beside its selection."""

    records: tuple[StructuredRecord, ...]
    checks: RelationChecks
    relations: tuple[RelationPath, ...]
    visuals: tuple[VisualEvidence, ...]
    coverage: Coverage
    warnings: tuple[str, ...]
    decisions: tuple[DecisionRequirement, ...]
    candidate_count: int


class CachedSelection(Selection, PreparedEvidence):
    """A prepared query result kept as canonical JSON; never a pickle."""

    @classmethod
    def of(cls, selection: Selection, evidence: PreparedEvidence) -> CachedSelection:
        """The entry for a selection and the rest of its prepared result."""
        return cls(**dict(selection), **dict(evidence))

    def selection(self) -> Selection:
        """The selection this entry holds."""
        return Selection(
            segments=self.segments,
            reservation_priority=self.reservation_priority,
            omitted_anchors=self.omitted_anchors,
            lane_coverage=self.lane_coverage,
            text_budget=self.text_budget,
            visual_reserved_tokens=self.visual_reserved_tokens,
        )

    def to_bytes(self) -> bytes:
        """Canonical JSON of the entry."""
        return canonical_bytes(self.model_dump(mode="json"))

    @classmethod
    def from_bytes(cls, raw: bytes) -> CachedSelection:
        """Read an entry written by ``to_bytes``."""
        return cls.model_validate_json(raw)
