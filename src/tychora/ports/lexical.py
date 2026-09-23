"""Snapshot-scoped lexical retrieval port."""

from typing import Protocol

from tychora.contracts.candidates import ChannelCandidates
from tychora.contracts.graph import EligibilityView
from tychora.ports.execution import ExecutionContext


class LexicalIndex(Protocol):
    def search(
        self,
        view: EligibilityView,
        query: str,
        candidate_limit: int,
        ctx: ExecutionContext,
    ) -> ChannelCandidates: ...
