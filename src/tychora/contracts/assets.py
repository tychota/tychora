"""Noncitable asset proxies with explicit canonical ownership."""

from typing import Self

from pydantic import model_validator

from .artifacts import ArtifactRef
from .base import Contract
from .corpus import AssetProxyRow
from .evidence import EvidenceUnit


class ProxyRecord(Contract):
    unit: EvidenceUnit
    binding: AssetProxyRow
    derivation: ArtifactRef

    @model_validator(mode="after")
    def matching_binding(self) -> Self:
        if (
            self.unit.citable
            or self.unit.unit_id != self.binding.proxy_unit_id
            or self.derivation.sha256 != self.binding.derivation_sha
            or not self.binding.required_unit_ids
            or self.unit.unit_id in self.binding.required_unit_ids
        ):
            raise ValueError("Proxy requires noncitable independent ownership")
        return self
