"""Private trusted acquisition binding; never accepted as a public request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.pdf_policy import PdfProcessingPolicy

if TYPE_CHECKING:
    from tychora.contracts.requests import RequestScope, SourceInput


@dataclass(frozen=True)
class AcquiredSource:
    workspace_id: str
    scope: RequestScope
    source: SourceInput
    original_ref: ArtifactRef
    receipt_binding_sha256: str
    policy: PdfProcessingPolicy
