"""Canonical corpus operations; policy authorization belongs to the owner."""

from typing import Protocol

from tychora.contracts.candidates import ChannelCandidates
from tychora.contracts.corpus import ExtractionBundle, ReviewTarget
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import EligibilityView


class CorpusStore(Protocol):
    def commit_extraction(self, bundle: ExtractionBundle) -> str: ...
    def hydrate(self, unit_ids: tuple[str, ...]) -> tuple[EvidenceUnit, ...]: ...
    def record_review(self, target: ReviewTarget, state: str, actor: str) -> int: ...
    def record_assertion_decision(
        self,
        assertion_id: str,
        artifact_sha: str,
        record_id: str,
        state: str,
        actor: str,
    ) -> int: ...
    def lookup_identifiers(
        self, namespaced: tuple[str, ...], view: EligibilityView, limit: int
    ) -> ChannelCandidates: ...
