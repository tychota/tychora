"""Immutable acquisition boundary, independent of parsers and job scheduling."""

from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol

from tychora.configuration.models import AcquisitionCeilings, UrlAcquisitionSettings
from tychora.contracts.acquisition import CaptureManifest
from tychora.contracts.requests import SourceInput
from tychora.ports.execution import ExecutionContext


class Acquisition(Protocol):
    def capture(
        self, source: SourceInput, ctx: ExecutionContext
    ) -> CaptureManifest: ...


class UrlReader(Protocol):
    def open(
        self,
        url: str,
        settings: UrlAcquisitionSettings,
        limits: AcquisitionCeilings,
        ctx: ExecutionContext,
    ) -> AbstractContextManager[BinaryIO]: ...
