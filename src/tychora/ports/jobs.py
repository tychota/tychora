"""Durable persistence and trusted canonical publication receipt lookup."""

from typing import Protocol

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue
from tychora.contracts.jobs import JobStatus, PublicationReceipt
from tychora.contracts.operations import JobEvent, JobHandle
from tychora.contracts.requests import RequestScope


class PublicationReceiptQuery(Protocol):
    def lookup(self, job_id: str) -> PublicationReceipt | None:
        """Return a verified committed receipt, None only for known nonpublication."""
        ...


class JobRepository(Protocol):
    def accept(
        self,
        operation: str,
        scope: RequestScope,
        key: str | None,
        intent: JsonValue,
        effective_config: ArtifactRef,
    ) -> JobHandle: ...
    def capture(self, job_id: str, manifest: ArtifactRef) -> None: ...
    def checkpoint(
        self, job_id: str, step_key: str, input_sha: str, output: ArtifactRef
    ) -> None: ...
    def append_event(self, job_id: str, event: JobEvent) -> int: ...
    def events_after(
        self, job_id: str, sequence: int, limit: int
    ) -> tuple[JobEvent, ...]: ...
    def get(self, job_id: str) -> JobStatus: ...
    def authorize(self, job_id: str, scope: RequestScope, *, write: bool) -> None: ...
    def cancel(self, job_id: str) -> JobStatus: ...
    def retry(self, job_id: str) -> JobStatus: ...
    def recover(self, job_id: str) -> JobStatus: ...

    def start(self, job_id: str) -> JobStatus: ...
    def stage(self, job_id: str, stage: str) -> None: ...
    def completed_step(
        self, job_id: str, step_key: str, input_sha: str
    ) -> ArtifactRef | None: ...
    def fail(self, job_id: str, error: TychoraError) -> JobStatus: ...
    def complete(
        self,
        job_id: str,
        *,
        result: ArtifactRef | None = None,
        result_snapshot_id: str | None = None,
    ) -> JobStatus: ...
