"""Independent, immutable server-resolved PDF processing intent."""

from typing import Literal, Self

from pydantic import model_validator

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

    @model_validator(mode="after")
    def page_partition(self) -> Self:
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
