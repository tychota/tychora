"""Durable job status and publication reconciliation records."""

from .artifacts import ArtifactRef
from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt, Sha256
from .operations import JobHandle


class JobStatus(JobHandle):
    request_digest: Sha256
    effective_config: ArtifactRef
    captured_inputs: ArtifactRef | None = None
    base_snapshot_id: Identifier | None = None
    result_snapshot_id: Identifier | None = None
    result: ArtifactRef | None = None
    cancellation_requested: bool = False
    latest_sequence: NonNegativeInt = 0
    retry_count: NonNegativeInt = 0
    error_code: str | None = None
    retention_limited: bool = False


class JobPage(Contract):
    jobs: tuple[JobStatus, ...] = ()
    next_cursor: Identifier | None = None


class PublicationReceipt(Contract):
    job_id: Identifier
    result_snapshot_id: Identifier
    result_artifact: ArtifactRef | None = None


class JobAuthorization(Contract):
    """Minimal durable disclosure scope, independent of request diagnostics."""

    principal_id: Identifier
    collection_ids: frozenset[Identifier]
    access_ceiling: NonNegativeInt
    uses: frozenset[NonEmptyStr]
    capability: NonEmptyStr
