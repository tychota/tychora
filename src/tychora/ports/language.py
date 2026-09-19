"""Immutable syntax contracts and the source-language adapter boundary."""

from typing import Literal, Protocol, Self

from pydantic import field_validator, model_validator

from tychora.contracts.base import (
    Contract,
    FrozenJsonMap,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from tychora.contracts.identity import JsonValue

CoverageState = Literal["parsed", "partial", "text_fallback", "empty"]
DiagnosticSeverity = Literal["info", "warning", "error"]
ContextReason = Literal["parent_declaration", "referenced_import"]


class SourceSpan(Contract):
    """One half-open span in every supported coordinate domain."""

    native_ref: NonEmptyStr
    kind: NonEmptyStr
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            self.byte_end < self.byte_start
            or self.char_end < self.char_start
            or self.line_end < self.line_start
        ):
            raise ValueError("Source span end precedes its start")
        return self


class SyntaxDiagnostic(Contract):
    code: NonEmptyStr
    message: NonEmptyStr
    severity: DiagnosticSeverity
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt


class ContextSpan(Contract):
    source_native_ref: NonEmptyStr
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt
    reason: ContextReason
    dependency_native_refs: tuple[str, ...]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            self.byte_end < self.byte_start
            or self.char_end < self.char_start
            or self.line_end < self.line_start
        ):
            raise ValueError("Context span end precedes its start")
        return self


class Definition(Contract):
    kind: NonEmptyStr
    name: NonEmptyStr
    qualified_name: NonEmptyStr
    scope: tuple[str, ...]
    native_ref: NonEmptyStr
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt
    decorator_span: SourceSpan | None = None
    export_span: SourceSpan | None = None
    declaration_span: SourceSpan
    docstring_span: SourceSpan | None = None
    parent_name: str | None = None
    parent_native_ref: str | None = None
    overload_discriminator: str | int | None = None
    context: tuple[ContextSpan, ...] = ()

    @model_validator(mode="after")
    def valid_ranges(self) -> Self:
        if self.byte_end <= self.byte_start or self.char_end <= self.char_start:
            raise ValueError("Definition must contain source text")
        if not (
            self.byte_start
            <= self.declaration_span.byte_start
            < self.declaration_span.byte_end
            <= self.byte_end
        ):
            raise ValueError("Declaration span must be contained by definition")
        if self.decorator_span is not None and not (
            self.byte_start
            <= self.decorator_span.byte_start
            < self.decorator_span.byte_end
            <= self.declaration_span.byte_start
        ):
            raise ValueError("Decorator span must precede the declaration")
        if self.export_span is not None and not (
            self.byte_start
            <= self.export_span.byte_start
            < self.export_span.byte_end
            <= self.byte_end
        ):
            raise ValueError("Export span must be contained by definition")
        if self.docstring_span is not None and not (
            self.declaration_span.byte_end
            <= self.docstring_span.byte_start
            < self.docstring_span.byte_end
            <= self.byte_end
        ):
            raise ValueError("Docstring span must be inside the definition body")
        return self


class Reference(Contract):
    form: NonEmptyStr
    spelling: NonEmptyStr
    native_ref: NonEmptyStr
    source_scope: tuple[str, ...]
    byte_start: NonNegativeInt
    byte_end: NonNegativeInt
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt
    import_module: str | None = None
    import_name: str | None = None
    local_name: str | None = None
    candidate_names: tuple[str, ...] = ()
    supported: bool = True
    statement_span: SourceSpan | None = None


class LanguageCapabilities(Contract):
    language: NonEmptyStr
    dialects: tuple[str, ...]
    extensions: tuple[str, ...]
    grammar_package: NonEmptyStr
    grammar_version: NonEmptyStr
    declaration_forms: tuple[str, ...]
    reference_forms: tuple[str, ...]
    limitations: tuple[str, ...]
    adapter_fingerprint: Sha256

    @field_validator(
        "dialects",
        "extensions",
        "declaration_forms",
        "reference_forms",
        "limitations",
    )
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("Capability values must be nonempty and unique")
        return value


class SyntaxResult(Contract):
    """Serializable parse facts owning the exact immutable captured bytes."""

    language: NonEmptyStr
    grammar: NonEmptyStr
    adapter_fingerprint: Sha256
    file_bytes: bytes
    text: str
    encoding: NonEmptyStr
    coverage: CoverageState
    options: FrozenJsonMap
    codepoint_to_byte: tuple[NonNegativeInt, ...]
    byte_to_codepoint: tuple[int | None, ...]
    line_starts: tuple[NonNegativeInt, ...]
    byte_to_line: tuple[PositiveInt, ...]
    root_type: NonEmptyStr
    definitions_found: tuple[Definition, ...] = ()
    references_found: tuple[Reference, ...] = ()
    uncovered_spans: tuple[SourceSpan, ...] = ()
    diagnostics: tuple[SyntaxDiagnostic, ...] = ()

    @model_validator(mode="after")
    def exact_maps(self) -> Self:
        byte_length = len(self.file_bytes)
        if (
            len(self.codepoint_to_byte) != len(self.text) + 1
            or not self.codepoint_to_byte
            or self.codepoint_to_byte[-1] != byte_length
            or len(self.byte_to_codepoint) != byte_length + 1
            or len(self.byte_to_line) != byte_length + 1
            or not self.line_starts
            or self.line_starts[0] != 0
        ):
            raise ValueError("Syntax result source maps do not cover captured bytes")
        for char_offset, byte_offset in enumerate(self.codepoint_to_byte):
            if self.byte_to_codepoint[byte_offset] != char_offset:
                raise ValueError("Byte/codepoint maps do not round-trip")
        if any(
            earlier >= later
            for earlier, later in zip(
                self.codepoint_to_byte, self.codepoint_to_byte[1:], strict=False
            )
        ):
            raise ValueError("Codepoint map must be strictly increasing")
        return self

    def byte_offset(self, char_offset: int) -> int:
        return self.codepoint_to_byte[char_offset]

    def char_offset(self, byte_offset: int) -> int:
        value = self.byte_to_codepoint[byte_offset]
        if value is None:
            raise ValueError("Byte offset is inside an encoded codepoint")
        return value

    def line_for_byte(self, byte_offset: int) -> int:
        return self.byte_to_line[byte_offset]


class LanguageAdapter(Protocol):
    def capabilities(self) -> LanguageCapabilities: ...

    def parse(self, file_bytes: bytes, options: JsonValue) -> SyntaxResult: ...

    def definitions(self, syntax: SyntaxResult) -> tuple[Definition, ...]: ...

    def references(self, syntax: SyntaxResult) -> tuple[Reference, ...]: ...

    def context_spans(self, definition: Definition) -> tuple[ContextSpan, ...]: ...
