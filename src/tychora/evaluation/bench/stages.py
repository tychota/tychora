"""Where stage durations come from: job events now, query traces later."""

from collections.abc import Callable
from itertools import pairwise
from typing import Annotated, Protocol

from pydantic import Field

from tychora.contracts.base import Contract, NonEmptyStr
from tychora.contracts.operations import JobEvent

_INGEST_BUCKETS = {
    "acquire": "acquire_register",
    "capture": "acquire_register",
    "parse": "parse",
    "project": "projection",
    "publish": "publication",
    "total": "total",
}


class StageSample(Contract):
    """One stage duration with the status it ended in."""

    stage: NonEmptyStr
    duration_ms: Annotated[float, Field(ge=0)]
    status: NonEmptyStr


class StageSource(Protocol):
    """Per-stage durations of one query run or one ingestion job."""

    def query_stages(self, run_id: str) -> tuple[StageSample, ...]:
        """Stage durations of one query run."""
        ...

    def ingest_stages(self, job_id: str) -> tuple[StageSample, ...]:
        """Stage durations of one ingestion job."""
        ...


def ingest_bucket(stage: str) -> str:
    """Map a job-event stage to its ingestion budget row."""
    if stage.startswith("projection/"):
        return "projection"
    return _INGEST_BUCKETS.get(stage, "queue")


class WallClockStageSource:
    """Stage durations from consecutive job-event timestamps; no query stages."""

    def __init__(self, events: Callable[[str], tuple[JobEvent, ...]]) -> None:
        self._events = events

    def query_stages(self, run_id: str) -> tuple[StageSample, ...]:
        """Report no query stages until a query trace exists."""
        return ()

    def ingest_stages(self, job_id: str) -> tuple[StageSample, ...]:
        """Durations between consecutive job events, plus the whole job as ``total``."""
        events = self._events(job_id)
        samples = [
            StageSample(
                stage=current.stage,
                duration_ms=(following.timestamp - current.timestamp).total_seconds()
                * 1000,
                status=current.state,
            )
            for current, following in pairwise(events)
        ]
        if len(events) >= 2:
            samples.append(
                StageSample(
                    stage="total",
                    duration_ms=(
                        events[-1].timestamp - events[0].timestamp
                    ).total_seconds()
                    * 1000,
                    status=events[-1].state,
                )
            )
        return tuple(samples)
