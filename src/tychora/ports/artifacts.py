"""Verified streaming reads; lifecycle and leases belong to the artifact owner."""

from contextlib import AbstractContextManager as ContextManager
from typing import BinaryIO, Protocol

from tychora.contracts.artifacts import ArtifactRef, ArtifactStat


class ArtifactReader(Protocol):
    def open_verified(self, ref: ArtifactRef) -> ContextManager[BinaryIO]: ...


class Artifacts(ArtifactReader, Protocol):
    def put_stream(self, stream: BinaryIO, media_type: str) -> ArtifactRef: ...

    def stat(self, ref: ArtifactRef) -> ArtifactStat: ...

    def pin_payloads(self, shas: frozenset[str]) -> ContextManager[None]: ...

    def payload_guard(self, sha: str) -> ContextManager[None]: ...

    def payload_is_pinned(self, sha: str) -> bool: ...
