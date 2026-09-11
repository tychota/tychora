"""Immutable capture identities and their retained source dependencies."""

from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from .artifacts import ArtifactRef
from .base import Contract, FrozenJsonMap, NonEmptyStr, Sha256
from .identity import normalize_source_key


class AcquisitionLimits(Contract):
    """Optional request ceilings; omitted fields preserve caller intent."""

    max_file_bytes: Annotated[int, Field(gt=0)] | None = None
    max_files: Annotated[int, Field(gt=0, le=90000)] | None = None
    max_total_bytes: Annotated[int, Field(gt=0)] | None = None
    max_inline_bytes: Annotated[int, Field(gt=0)] | None = None
    max_redirects: Annotated[int, Field(ge=0, le=100)] | None = None
    download_timeout_seconds: (
        Annotated[float, Field(gt=0, le=3600, allow_inf_nan=False)] | None
    ) = None


class CapturedFile(Contract):
    relative_path: NonEmptyStr
    source_key: NonEmptyStr
    mode: Annotated[int, Field(ge=0, le=0o7777)]
    blob_sha: Sha256
    artifact_ref: ArtifactRef

    @field_validator("relative_path", "source_key")
    @classmethod
    def normalized_key(cls, value: str) -> str:
        return normalize_source_key(value)

    @model_validator(mode="after")
    def matching_blob(self) -> Self:
        if (
            self.blob_sha != self.artifact_ref.sha256
            or self.artifact_ref.storage_key != self.blob_sha
        ):
            raise ValueError("Captured blob must match its physical artifact")
        return self


class CaptureManifest(Contract):
    source_key: NonEmptyStr
    original_ref: ArtifactRef
    original_sha: Sha256
    sidecar_refs: tuple[ArtifactRef, ...] = ()
    entries: Annotated[tuple[CapturedFile, ...], Field(max_length=90000)] = ()
    commit: str | None = None
    dirty: bool = False
    captured_at: datetime
    acquire_fingerprint: Sha256
    config_sha: Sha256
    manifest_ref: ArtifactRef
    source: FrozenJsonMap

    @model_validator(mode="after")
    def matching_identity(self) -> Self:
        if self.original_sha != self.original_ref.sha256:
            raise ValueError("Original digest must match its artifact")
        if self.captured_at.tzinfo is None:
            raise ValueError("Capture time requires a timezone")
        paths = [entry.relative_path for entry in self.entries]
        if paths != sorted(set(paths)) or len(
            {entry.source_key for entry in self.entries}
        ) != len(self.entries):
            raise ValueError("Capture entries must be sorted with unique source keys")
        return self
