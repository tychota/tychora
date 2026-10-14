"""Public PDF intake DTOs contain no local paths or artifact access keys."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from .base import Contract, FrozenJsonMap, NonEmptyStr, Sha256
from .errors import ErrorEnvelope
from .identity import normalize_source_key
from .operations import JobHandle
from .pdf_policy import PdfProcessingPolicy


class PdfIntakeDescriptor(Contract):
    source_key: NonEmptyStr
    collection_id: UUID
    kind: NonEmptyStr
    metadata: FrozenJsonMap = Field(default_factory=FrozenJsonMap)
    expected_size_bytes: int = Field(gt=0, le=104857600)
    expected_sha256: Sha256 | None = None
    format: Literal["pdf"] = "pdf"

    @field_validator("source_key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return normalize_source_key(value)


class PdfAnalysis(Contract):
    original_sha256: Sha256
    size_bytes: int
    page_count: int
    native_text_pages: tuple[int, ...]
    image_only_pages: tuple[int, ...]
    blank_pages: tuple[int, ...]
    unknown_pages: tuple[int, ...]
    requires_ocr: bool
    blockers: tuple[NonEmptyStr, ...]
    proposed_policy: PdfProcessingPolicy | None
    analysis_sha256: Sha256


class PdfIntakeSubmission(Contract):
    analysis_sha256: Sha256
    policy_sha256: Sha256


class PdfIntakeStatus(Contract):
    intake_id: NonEmptyStr
    state: Literal[
        "prepared",
        "receiving",
        "staged",
        "analysing",
        "analysed",
        "submitted",
        "aborted",
        "expired",
        "failed",
    ]
    byte_ceiling: int
    received_bytes: int
    expires_at: datetime
    analysis: PdfAnalysis | None = None
    job: JobHandle | None = None
    error: ErrorEnvelope | None = None
