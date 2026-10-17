"""Independent, immutable server-resolved PDF processing intent."""

from typing import Literal, Self

from pydantic import model_serializer, model_validator

from .base import Contract, FrozenJsonMap, Sha256


class PdfProcessingPolicy(Contract):
    version: Literal["pdf-processing@1"] = "pdf-processing@1"
    mode: Literal["native", "local_ocr"]
    native_pages: tuple[int, ...]
    ocr_pages: tuple[int, ...]
    blank_pages: tuple[int, ...]
    backend_identity: FrozenJsonMap
    runtime_inventory: FrozenJsonMap
    model_inventory_sha256: Sha256
    options: FrozenJsonMap
    limits: FrozenJsonMap
    origin_proof: Literal["ordered-ocr-cells@1"] | None = None
    text_coordinates: Literal["docling-assembly@1"] | None = None

    @model_serializer(mode="wrap")
    def legacy_origin_proof(self, handler):
        data = handler(self)
        if self.origin_proof is None:
            data.pop("origin_proof", None)
        if self.text_coordinates is None:
            data.pop("text_coordinates", None)
        return data

    @model_validator(mode="after")
    def page_partition(self) -> Self:
        if self.text_coordinates is not None and self.origin_proof is None:
            raise ValueError("Assembly coordinates require ordered origin proof")
        pages = (*self.native_pages, *self.ocr_pages, *self.blank_pages)
        for group in (self.native_pages, self.ocr_pages, self.blank_pages):
            if tuple(sorted(set(group))) != group or any(page < 1 for page in group):
                raise ValueError(
                    "PDF page selections must be sorted, unique and positive"
                )
        if len(set(pages)) != len(pages) or sorted(pages) != list(
            range(1, len(pages) + 1)
        ):
            raise ValueError("PDF page selections must partition the complete document")
        if not pages or (self.mode == "native" and self.ocr_pages):
            raise ValueError("PDF mode and page selections differ")
        return self
