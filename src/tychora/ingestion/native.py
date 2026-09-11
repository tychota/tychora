"""Native block parsing against captured original text."""

import json
import re
from bisect import bisect_right
from collections.abc import Mapping
from importlib.metadata import version
from io import BytesIO
from typing import Any, cast

from markdown_it import MarkdownIt
from markdown_it.token import Token

from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef, physical_identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.extraction import (
    ExtractionArtifact,
    NativeRegistration,
    ParsedRecord,
    ParsedUnit,
    extraction_document,
)
from tychora.contracts.identity import digest
from tychora.ports.artifacts import Artifacts
from tychora.ports.execution import ExecutionContext

MAX_NATIVE_BYTES = 4 * 1024 * 1024
MAX_BLOCK_TOKENS = 100000
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


def block_tokens(text: str) -> tuple[Token, ...]:
    return tuple(MarkdownIt("commonmark").enable("table").parse(text))


def _line_offsets(text: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", text)), len(text)]


def _table_cells(tokens: tuple[Token, ...], start: int) -> tuple[tuple[str, ...], ...]:
    rows: list[tuple[str, ...]] = []
    cells: list[str] = []
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token.type == "table_close":
            break
        if token.type == "tr_open":
            cells = []
        elif token.type == "inline":
            cells.append(token.content)
        elif token.type == "tr_close":
            rows.append(tuple(cells))
    return tuple(rows)


def parse_markdown(text: str) -> tuple[ParsedUnit, ...]:
    if len(text.encode("utf-8")) > MAX_NATIVE_BYTES:
        raise TychoraError(
            "INVALID_REQUEST", "Native document exceeds parser byte limit"
        )
    tokens = block_tokens(text)
    if len(tokens) > MAX_BLOCK_TOKENS:
        raise TychoraError(
            "INVALID_REQUEST", "Native document exceeds parser token limit"
        )
    offsets = _line_offsets(text)
    units: list[ParsedUnit] = []
    headings: list[tuple[int, str]] = []
    parents: list[str] = []
    table_depth = 0
    kinds = {
        "heading_open": "heading",
        "paragraph_open": "paragraph",
        "list_item_open": "list_item",
        "table_open": "table",
        "fence": "code_block",
        "code_block": "code_block",
        "html_block": "html_block",
    }
    for index, token in enumerate(tokens):
        if token.type == "list_item_close":
            parents.pop()
        if token.type == "table_close":
            table_depth -= 1
        if token.type not in kinds or token.map is None or table_depth:
            continue
        start, end = token.map
        kind = kinds[token.type]
        content = (
            tokens[index + 1].content
            if token.type in ("heading_open", "paragraph_open")
            else token.content
        )
        level = int(token.tag[1:]) if kind == "heading" else 0
        if level:
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, content))
        native_ref = f"block:{index}"
        units.append(
            ParsedUnit(
                unit_type=kind,
                token_kind=token.type,
                native_ref=native_ref,
                parent_ref=parents[-1] if parents else None,
                text=content,
                source_substring=text[offsets[start] : offsets[end]],
                char_start=offsets[start],
                char_end=offsets[end],
                line_start=start + 1,
                line_end=end,
                heading_path=tuple(label for _, label in headings),
                heading_level=level,
                heading_char_end=offsets[end] if level else None,
                cells=_table_cells(tokens, index) if kind == "table" else (),
            )
        )
        if kind == "list_item":
            parents.append(native_ref)
        if kind == "table":
            table_depth += 1
    return tuple(units)


class NativeParser:
    def __init__(self, artifacts: Artifacts, registration: NativeRegistration):
        self.artifacts = artifacts
        self.registration = NativeRegistration.model_validate(registration.model_dump())

    def extract(
        self, original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext
    ) -> ExtractionArtifact:
        ctx.checkpoint()
        if config.values.extraction.parser != "markdown":
            raise TychoraError(
                "CAPABILITY_MISSING", "Configured parser requires its own adapter"
            )
        if physical_identity(original) != physical_identity(
            self.registration.capture.original_ref
        ):
            raise TychoraError(
                "INTEGRITY_ERROR", "Parser original differs from capture binding"
            )
        limit = min(MAX_NATIVE_BYTES, config.values.acquisition.limits.max_file_bytes)
        if original.size_bytes > limit:
            raise TychoraError(
                "INVALID_REQUEST", "Native document exceeds parser byte limit"
            )
        with self.artifacts.open_verified(original) as stream:
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise TychoraError(
                "INVALID_REQUEST", "Native document exceeds parser byte limit"
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise TychoraError(
                "INVALID_REQUEST", "Native text requires valid UTF-8 bytes"
            ) from None
        ctx.checkpoint()
        registry = Registry.from_config(config)
        registry.validate_metadata(
            self.registration.revision.kind_id, self.registration.revision.metadata_json
        )
        kind = cast(
            Mapping[str, Any], registry.kinds[self.registration.revision.kind_id]
        )
        if kind["normalizer"] == "code_symbols@1":
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Code symbols require an installed language adapter",
            )
        parser = digest(
            "native-parser-v1",
            version("markdown-it-py"),
            self.registration.format,
            config.values.extraction.model_dump(mode="json"),
        )
        normalizer = digest("native-normalizer-v1", kind, registry.schema_digests)
        from .normalize import prepare_records, resolve_records

        if self.registration.format == "json":
            units, records = parse_json(text, self.registration, registry)
        else:
            units = (
                parse_text(text)
                if self.registration.format == "text"
                else parse_markdown(text)
            )
            records = ()
        blocks = units
        units, records, diagnostics = prepare_records(
            units, records, text, self.registration, registry
        )
        from tychora.contracts.identity import extraction_id

        records, ref_diagnostics = resolve_records(
            units,
            records,
            extraction_id(self.registration.revision.revision_id, parser, normalizer),
        )
        diagnostics += ref_diagnostics
        data = {
            "registration": self.registration.model_dump(mode="json"),
            "original_ref": original.model_dump(mode="json"),
            "source_text": text,
            "parser_fingerprint": parser,
            "normalizer_fingerprint": normalizer,
            "registry_digest": registry.registry_digest,
            "units": [u.model_dump(mode="json") for u in units],
            "blocks": [u.model_dump(mode="json") for u in blocks],
            "assets": [],
            "assertions": [],
            "records": [r.model_dump(mode="json") for r in records],
            "diagnostics": [d.model_dump(mode="json") for d in diagnostics],
        }
        encoded = extraction_document(data)
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise TychoraError(
                "INVALID_REQUEST", "Parsed extraction exceeds manifest byte limit"
            )
        ctx.checkpoint()
        document = self.artifacts.put_stream(BytesIO(encoded), "application/json")
        ctx.checkpoint()
        return ExtractionArtifact(
            registration=self.registration,
            original_ref=original,
            raw_document_ref=document,
            source_text=text,
            parser_fingerprint=parser,
            normalizer_fingerprint=normalizer,
            registry_digest=registry.registry_digest,
            units=units,
            blocks=blocks,
            records=records,
            diagnostics=diagnostics,
        )


def parse_text(text: str) -> tuple[ParsedUnit, ...]:
    offsets = _line_offsets(text)
    units = []
    start = None
    for line in range(len(offsets) - 1):
        nonblank = bool(text[offsets[line] : offsets[line + 1]].strip())
        if nonblank and start is None:
            start = line
        if start is not None and (not nonblank or line == len(offsets) - 2):
            end = line if not nonblank else line + 1
            original = text[offsets[start] : offsets[end]]
            units.append(
                ParsedUnit(
                    unit_type="paragraph",
                    token_kind="text_paragraph",
                    native_ref=f"paragraph:{start}",
                    text=original,
                    source_substring=original,
                    char_start=offsets[start],
                    char_end=offsets[end],
                    line_start=start + 1,
                    line_end=end,
                )
            )
            start = None
    return tuple(units)


def parse_json(
    text: str, registration: NativeRegistration, registry: Registry
) -> tuple[tuple[ParsedUnit, ...], tuple[ParsedRecord, ...]]:
    from .normalize import RECORD_TYPES

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    offsets = _line_offsets(text)[:-1]
    decoder = json.JSONDecoder(object_pairs_hook=pairs)
    try:
        decoded = decoder.decode(text)
        values = decoded if isinstance(decoded, list) else [decoded]
        if len(values) > 4096 or any(not isinstance(value, dict) for value in values):
            raise ValueError("Expected at most4096 record objects")
    except (ValueError, RecursionError):
        raise TychoraError(
            "INVALID_REQUEST", "Malformed structured record JSON"
        ) from None
    units = []
    records = []

    def skip_space(position: int) -> int:
        match = re.compile(r"\s*").match(text, position)
        assert match is not None
        return match.end()

    position = skip_space(0)
    array = isinstance(decoded, list)
    if array:
        position = skip_space(position + 1)
    for value in values:
        _, end = decoder.raw_decode(text, position)
        registry.validate_record(registration.revision.kind_id, value)
        native_ref = f"record:{len(records)}"
        value.setdefault("foundation_refs", [])
        raw = text[position:end]
        kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
        units.append(
            ParsedUnit(
                unit_type=RECORD_TYPES.get(kind["normalizer"], "record"),
                token_kind="json_record",
                native_ref=native_ref,
                text=raw,
                source_substring=raw,
                char_start=position,
                char_end=end,
                line_start=bisect_right(offsets, position),
                line_end=bisect_right(offsets, max(position, end - 1)),
            )
        )
        records.append(ParsedRecord(native_ref=native_ref, value=FrozenJsonMap(value)))
        position = skip_space(end)
        if array and text[position : position + 1] == ",":
            position = skip_space(position + 1)
    return tuple(units), tuple(records)
