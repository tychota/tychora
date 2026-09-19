"""Verified per-file preparation facts carried with a code extraction bundle."""

from typing import Literal, Self

from pydantic import field_validator, model_validator

from .base import (
    Contract,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .identity import normalize_source_key


class CodeFileDescriptor(Contract):
    relative_path: NonEmptyStr
    artifact_ref: Sha256
    capture_entry_digest: Sha256
    language: NonEmptyStr
    encoding: NonEmptyStr
    coverage: Literal["parsed", "partial", "text_fallback", "empty"]
    adapter_fingerprint: Sha256
    byte_length: NonNegativeInt
    char_length: NonNegativeInt
    codepoint_to_byte: tuple[NonNegativeInt, ...]
    byte_to_codepoint: tuple[int | None, ...]
    line_starts: tuple[NonNegativeInt, ...]
    byte_to_line: tuple[PositiveInt, ...]

    @field_validator("relative_path")
    @classmethod
    def normalized_path(cls, value: str) -> str:
        return normalize_source_key(value)

    @model_validator(mode="after")
    def complete_maps(self) -> Self:
        if (
            len(self.codepoint_to_byte) != self.char_length + 1
            or self.codepoint_to_byte[-1] != self.byte_length
            or len(self.byte_to_codepoint) != self.byte_length + 1
            or len(self.byte_to_line) != self.byte_length + 1
            or not self.line_starts
            or self.line_starts[0] != 0
        ):
            raise ValueError("Code file descriptor maps are incomplete")
        return self
