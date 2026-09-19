"""Cooperative execution and upload-authorization ports."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from threading import Event
from typing import Protocol

from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope


@dataclass(frozen=True)
class ExecutionContext:
    deadline: float | None = None
    cancelled: Event = field(default_factory=Event)

    def __post_init__(self) -> None:
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("Deadline must be a finite monotonic time")

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise TychoraError("CANCELLED", "Operation was cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TychoraError("DEADLINE_EXCEEDED", "Operation deadline exceeded")


class UploadPolicy(Protocol):
    def authorize(
        self,
        scope: RequestScope,
        provider: str,
        dependencies: frozenset[str],
        policy_epoch: int,
    ) -> None: ...

    def register_captured_source(
        self,
        registration: CapturedSourceRegistration,
        scope: RequestScope,
        *,
        policy_epoch: int,
    ) -> None: ...

    def authorize_captured(
        self,
        scope: RequestScope,
        provider: str,
        authorization: CapturedRegionAuthorization,
        policy_epoch: int,
    ) -> None: ...

    def authorize_query(
        self,
        scope: RequestScope,
        provider: str,
        authorization: QueryUploadAuthorization,
    ) -> None: ...


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tychora.contracts.embeddings import QueryUploadAuthorization
    from tychora.ports.vision import (
        CapturedRegionAuthorization,
        CapturedSourceRegistration,
    )
