"""Canonical corpus operations; policy authorization belongs to the owner."""

from datetime import datetime
from typing import Protocol

from tychora.contracts.corpus import AllowedUsesOverride, ExtractionBundle, ReviewTarget
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.indexing import RepresentationFacts


class CorpusStore(Protocol):
    def commit_extraction(
        self, bundle: ExtractionBundle, facts: RepresentationFacts | None = None
    ) -> str: ...
    def hydrate(self, unit_ids: tuple[str, ...]) -> tuple[EvidenceUnit, ...]: ...
    def record_review(
        self,
        target: ReviewTarget,
        state: str,
        actor: str,
        *,
        decided_at: datetime | None = None,
    ) -> int: ...
    def set_allowed_uses_override(
        self,
        source_id: str,
        override: AllowedUsesOverride,
        *,
        collection_ids: frozenset[str] | None = None,
    ) -> None:
        """Store a source's narrowed uses and bump the policy epoch in one transaction."""
        ...

    def record_assertion_decision(
        self,
        assertion_id: str,
        artifact_sha: str,
        record_id: str,
        state: str,
        actor: str,
    ) -> int: ...
