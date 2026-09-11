"""Verified streaming reads; lifecycle and leases belong to the artifact owner."""

from contextlib import AbstractContextManager as ContextManager
from typing import BinaryIO, Protocol

from tychora.contracts.artifacts import ArtifactRef


class ArtifactReader(Protocol):
    def open_verified(self, ref: ArtifactRef) -> ContextManager[BinaryIO]: ...
