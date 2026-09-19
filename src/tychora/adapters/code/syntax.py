"""Grammar-neutral construction of exact immutable source coordinate maps."""

from __future__ import annotations

import codecs
from bisect import bisect_right
from collections.abc import Mapping

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.identity import JsonValue
from tychora.ports.language import (
    CoverageState,
    SourceSpan,
    SyntaxDiagnostic,
    SyntaxResult,
)


def source_maps(
    file_bytes: bytes, text: str, encoding: str
) -> tuple[tuple[int, ...], tuple[int | None, ...], tuple[int, ...], tuple[int, ...]]:
    """Map decoded codepoint boundaries back to original captured byte offsets."""

    encoder = codecs.getincrementalencoder(encoding)(errors="strict")
    offset = len(encoder.encode("", final=False))
    codepoint_to_byte = [offset]
    for character in text:
        offset += len(encoder.encode(character, final=False))
        codepoint_to_byte.append(offset)
    offset += len(encoder.encode("", final=True))
    if offset != len(file_bytes):
        raise UnicodeError("Decoded text cannot be mapped exactly to captured bytes")

    byte_to_codepoint: list[int | None] = [None] * (len(file_bytes) + 1)
    for char_offset, byte_offset in enumerate(codepoint_to_byte):
        byte_to_codepoint[byte_offset] = char_offset

    line_starts = [0]
    line_starts.extend(
        index + 1 for index, value in enumerate(file_bytes) if value == 10
    )
    byte_to_line = [
        bisect_right(line_starts, byte_offset)
        for byte_offset in range(len(file_bytes) + 1)
    ]
    return (
        tuple(codepoint_to_byte),
        tuple(byte_to_codepoint),
        tuple(line_starts),
        tuple(byte_to_line),
    )


def make_span(
    syntax: SyntaxResult,
    *,
    native_ref: str,
    kind: str,
    byte_start: int,
    byte_end: int,
) -> SourceSpan:
    if byte_end <= byte_start:
        raise ValueError("Source spans must contain bytes")
    return SourceSpan(
        native_ref=native_ref,
        kind=kind,
        byte_start=byte_start,
        byte_end=byte_end,
        char_start=syntax.char_offset(byte_start),
        char_end=syntax.char_offset(byte_end),
        line_start=syntax.line_for_byte(byte_start),
        line_end=syntax.line_for_byte(byte_end - 1),
    )


def base_syntax_result(
    *,
    language: str,
    grammar: str,
    adapter_fingerprint: str,
    file_bytes: bytes,
    text: str,
    encoding: str,
    coverage: CoverageState,
    options: JsonValue,
    root_type: str,
    diagnostics: tuple[SyntaxDiagnostic, ...] = (),
) -> SyntaxResult:
    if not isinstance(options, Mapping):
        raise TypeError("Language parser options must be a JSON object")
    maps = source_maps(file_bytes, text, encoding)
    return SyntaxResult(
        language=language,
        grammar=grammar,
        adapter_fingerprint=adapter_fingerprint,
        file_bytes=file_bytes,
        text=text,
        encoding=encoding,
        coverage=coverage,
        options=FrozenJsonMap(options),
        codepoint_to_byte=maps[0],
        byte_to_codepoint=maps[1],
        line_starts=maps[2],
        byte_to_line=maps[3],
        root_type=root_type,
        diagnostics=diagnostics,
    )
