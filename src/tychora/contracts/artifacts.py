"""Immutable artifact identity shared by corpus verification and byte storage."""

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import model_validator

from .base import Contract, NonEmptyStr, NonNegativeInt, Sha256


class ArtifactRef(Contract):
    """Physical bytes plus a binding-level media hint, never a format guarantee."""

    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: NonEmptyStr
    storage_key: NonEmptyStr

    @classmethod
    def from_row(cls, row: Sequence[object]) -> Self:
        """Build a reference from a (sha256, size_bytes, media_type, storage_key) row."""
        if len(row) < 4:
            raise ValueError("Artifact row needs four columns")
        return cls.model_validate(
            {
                "sha256": row[0],
                "size_bytes": row[1],
                "media_type": row[2],
                "storage_key": row[3],
            }
        )


def physical_identity(ref: ArtifactRef | None) -> tuple[str, int, str] | None:
    """Media interpretation does not change physical identity or grant trust."""
    return (ref.sha256, ref.size_bytes, ref.storage_key) if ref is not None else None


class ArtifactStat(ArtifactRef):
    """Physical availability; durable deletion state comes from the owner database."""

    available: bool
    payload_state: Literal["present", "delete_pending", "deleted"] | None = None
    payload_deleted_at: str | None = None

    @model_validator(mode="after")
    def deletion_timestamp_matches_state(self) -> Self:
        if (self.payload_state == "deleted") != (self.payload_deleted_at is not None):
            raise ValueError("Deletion timestamp requires deleted payload state")
        return self


MANIFEST_VERSIONS: dict[str, str | int] = {
    "repository_capture": "1",
    "extraction": "1",
    "assertion": "1",
    "assertion_index": "1",
    "registry": "1",
    "index_input": "1",
    "snapshot": "2",
    "membership_page": "2",
    "publication_plan": "2",
    "vector_cache": "1",
    "job_capture": "1",
    "retained_run": "1",
    "evaluation": "1",
    "backup": 1,
}
CAPTURE_TYPES = frozenset({"repository_capture", "job_capture"})


def validate_manifest_schema(kind: object, version: object) -> None:
    """Reject a manifest type or schema version the format does not register."""
    if not isinstance(kind, str) or kind not in MANIFEST_VERSIONS:
        raise ValueError("Unknown required manifest type")
    expected = MANIFEST_VERSIONS[kind]
    if type(version) is not type(expected) or version != expected:
        raise ValueError("Unknown required manifest schema version")


def registered_manifest(artifact: "ArtifactRef", kind: str) -> "ManifestRef":
    """Link an artifact as its manifest type at the registered schema version."""
    return ManifestRef(
        artifact=artifact, type=kind, schema_version=MANIFEST_VERSIONS[kind]
    )


class ManifestRef(Contract):
    """A typed link to a verified JSON manifest or assertion JSONL artifact."""

    artifact: ArtifactRef
    type: NonEmptyStr
    schema_version: str | int

    @model_validator(mode="after")
    def registered_schema(self) -> Self:
        validate_manifest_schema(self.type, self.schema_version)
        if self.artifact.storage_key != self.artifact.sha256:
            raise ValueError("Manifest artifact storage key must match its hash")
        return self
