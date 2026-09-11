"""Parser adapters consume verified originals without canonical database writes."""

from typing import Protocol

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.extraction import ExtractionArtifact
from tychora.ports.execution import ExecutionContext


class Parser(Protocol):
    def extract(
        self, original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext
    ) -> ExtractionArtifact: ...
