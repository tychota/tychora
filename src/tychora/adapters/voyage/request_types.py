"""Minimal raw Voyage transport protocol shared without adapter imports."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawVoyageResponse:
    status_code: int
    body: bytes


class VoyageTransport(Protocol):
    def post_json(
        self, path: str, body: bytes, timeout: float
    ) -> RawVoyageResponse: ...

    def close(self) -> None: ...
