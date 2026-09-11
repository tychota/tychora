"""Acyclic source-selected plans for complete document normalization."""

from typing import Literal, Self

from pydantic import model_validator

from .artifacts import ArtifactRef
from .base import Contract, NonEmptyStr, NonNegativeInt, Sha256


class SourceSelector(Contract):
    native_ref: NonEmptyStr
    field: Literal["text", "marker", "table_cell"] | None = None
    cell_index: NonNegativeInt | None = None
    source_start: NonNegativeInt | None = None
    source_end: NonNegativeInt | None = None

    @model_validator(mode="after")
    def exact_input_domain(self) -> Self:
        if (
            self.field is None
            and any(
                value is not None
                for value in (self.cell_index, self.source_start, self.source_end)
            )
            or self.field is not None
            and (self.source_start is None or self.source_end is None)
            or (self.field == "table_cell") != (self.cell_index is not None)
            or self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
            or not self.native_ref.startswith("#/")
        ):
            raise ValueError(
                "Composition dependencies require original SDK field coordinates"
            )
        return self


class PlannedProxy(Contract):
    asset_native_ref: NonEmptyStr
    kind: Literal["caption_copy", "ocr", "description", "structured_values"]
    text: str
    text_sha: Sha256
    dependencies: tuple[SourceSelector, ...]
    derivation: ArtifactRef

    @model_validator(mode="after")
    def exact_text_input(self) -> Self:
        from hashlib import sha256

        if (
            not self.dependencies
            or sha256(self.text.encode()).hexdigest() != self.text_sha
        ):
            raise ValueError(
                "Planned proxy must bind literal input text and original selectors"
            )
        if self.derivation.storage_key != self.derivation.sha256:
            raise ValueError("Planned derivation requires immutable content identity")
        return self


class DocumentCompositionPlan(Contract):
    base_normalizer_fingerprint: Sha256
    table_strategy: Literal["table-fragments@1"] = "table-fragments@1"
    proxy_strategy: Literal["asset-proxies@1"] = "asset-proxies@1"
    infer_table_continuations: bool = True
    caption_copies: bool = True
    proxies: tuple[PlannedProxy, ...] = ()

    @model_validator(mode="after")
    def bounded_unique_inputs(self) -> Self:
        from .identity import canonical_bytes

        if (
            len(self.proxies) > 10_000
            or len(canonical_bytes(self.model_dump(mode="json"))) > 16 * 1024 * 1024
        ):
            raise ValueError("Document composition plan exceeds input capacity")
        keys = [
            canonical_bytes(proxy.model_dump(mode="json")) for proxy in self.proxies
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate planned proxy input")
        return self
