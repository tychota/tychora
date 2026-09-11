"""Pure conversion from captured parser facts to canonical corpus rows."""

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, cast

from markdown_it import MarkdownIt

from tychora.configuration.registry import Registry
from tychora.contracts import identity
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    ExtractionBundle,
    ExtractionRow,
    IdentifierRow,
    LocatorRow,
    RevisionArtifactRow,
    StructuredRecordRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit, TextLocator
from tychora.contracts.extraction import (
    Diagnostic,
    ExtractionArtifact,
    NativeRegistration,
    ParsedRecord,
    ParsedUnit,
)


def normalize(
    parsed: ExtractionArtifact, revision_id: str, registry: Registry
) -> ExtractionBundle:
    parsed = ExtractionArtifact.model_validate(parsed.model_dump())
    registration = NativeRegistration.model_validate(parsed.registration.model_dump())
    if (
        revision_id != registration.revision.revision_id
        or registry.registry_digest != parsed.registry_digest
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Normalization revision or registry binding differs"
        )
    registry.validate_metadata(
        registration.revision.kind_id, registration.revision.metadata_json
    )
    seen = set()
    for unit in parsed.units:
        if (
            unit.native_ref in seen
            or parsed.source_text[unit.char_start : unit.char_end]
            != unit.source_substring
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Duplicate native reference or impossible original range",
            )
        seen.add(unit.native_ref)
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    if parsed.normalizer_fingerprint != identity.digest(
        "native-normalizer-v1", kind, registry.schema_digests
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Normalizer fingerprint differs from original registry/kind binding",
        )
    eid = identity.extraction_id(
        revision_id, parsed.parser_fingerprint, parsed.normalizer_fingerprint
    )
    units = []
    locators = []
    identifiers = {
        p.native_ref: identity.unit_id(
            eid, p.native_ref, i, sha256(p.source_substring.encode()).hexdigest()
        )
        for i, p in enumerate(parsed.units)
    }
    for ordinal, unit in enumerate(parsed.units):
        uid = identifiers[unit.native_ref]
        loc = TextLocator(
            artifact_ref=parsed.original_ref.sha256,
            native_ref=registration.source.source_key,
            char_start=unit.char_start,
            char_end=unit.char_end,
        )
        units.append(
            EvidenceUnit(
                unit_id=uid,
                extraction_id=eid,
                parent_id=identifiers.get(unit.parent_ref) if unit.parent_ref else None,
                ordinal=ordinal,
                unit_type=unit.unit_type,
                native_ref=unit.native_ref,
                boundary_key=unit.native_ref,
                heading_path=unit.heading_path,
                text=unit.source_substring,
                text_sha=sha256(unit.source_substring.encode()).hexdigest(),
                locators=(loc,),
                attributes=FrozenJsonMap(
                    {
                        "token_kind": unit.token_kind,
                        "line_start": unit.line_start,
                        "line_end": unit.line_end,
                        "cells": unit.cells,
                    }
                ),
            )
        )
        locators.append(
            LocatorRow(
                locator_id=identity.digest("locator-v1", uid, 0),
                unit_id=uid,
                fragment_ordinal=0,
                locator=loc,
            )
        )
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    bindings = cast(Mapping[str, Any], registry.artifact["definition_schema_bindings"])
    records = []
    for record in parsed.records:
        registry.validate_record(registration.revision.kind_id, record.value)
        if any(
            uid not in identifiers.values()
            for uid in cast(tuple[str, ...], record.value.get("foundation_ids", ()))
        ):
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Foundation is not an original unit in this extraction",
            )
        schema = bindings["kinds"][registration.revision.kind_id]["record_schema"]
        records.append(
            StructuredRecordRow(
                unit_id=identifiers[record.native_ref],
                record_type=schema,
                schema_hash=cast(str, registry.schema_digests[schema]),
                data_json=record.value,
            )
        )
    identifier_rows = tuple(
        IdentifierRow(
            namespace="provision"
            if kind["normalizer"] == "numbered_provisions@1"
            else "rule",
            normalized_value=p.identifier,
            original_value=p.identifier,
            unit_id=identifiers[p.native_ref],
        )
        for p in parsed.units
        if p.identifier
    )
    refs = {
        ref.sha256: ref
        for ref in (
            parsed.original_ref,
            *registration.capture.sidecar_refs,
            parsed.raw_document_ref,
        )
    }
    return ExtractionBundle(
        workspace=registration.workspace,
        collection=registration.collection,
        source=registration.source,
        revision=registration.revision,
        artifacts=tuple(refs.values()),
        revision_artifacts=(
            RevisionArtifactRow(
                revision_id=revision_id,
                role="original",
                source_ref=registration.source.source_key,
                artifact_sha=parsed.original_ref.sha256,
            ),
            *(
                RevisionArtifactRow(
                    revision_id=revision_id,
                    role="sidecar",
                    source_ref=f"sidecar:{i}",
                    artifact_sha=ref.sha256,
                )
                for i, ref in enumerate(registration.capture.sidecar_refs)
            ),
        ),
        extraction=ExtractionRow(
            extraction_id=eid,
            revision_id=revision_id,
            parser_fingerprint=parsed.parser_fingerprint,
            normalizer_fingerprint=parsed.normalizer_fingerprint,
            document_artifact_sha=parsed.raw_document_ref.sha256,
            created_at=registration.revision.captured_at,
        ),
        units=tuple(units),
        locators=tuple(locators),
        records=tuple(records),
        identifiers=identifier_rows,
    )


DEFAULT_LABELS = {
    "requirement": ("Requirement",),
    "proof": ("Expected evidence",),
    "control": ("Control",),
    "severity": ("Severity",),
    "target": ("Target",),
    "status": ("Status",),
    "foundation": ("Foundations",),
    "comment": ("Comment",),
    "author": ("Author",),
    "decision": ("Decision",),
    "observation": ("Observation",),
    "slot": ("Slot",),
    "instruction": ("Instruction",),
}
RECORD_FIELDS = {
    "rule_records@1": (
        "requirement",
        "proof",
        "control",
        "severity",
        "target",
        "status",
        "foundation",
    ),
    "feedback_records@1": (
        "comment",
        "author",
        "decision",
        "observation",
        "target",
        "status",
        "foundation",
    ),
    "template_slots@1": ("slot", "instruction", "target", "status", "foundation"),
}
RECORD_TYPES = {
    "rule_records@1": "rule",
    "feedback_records@1": "review",
    "template_slots@1": "slot",
}


def _label_span(
    line: str, aliases: Mapping[str, list[str]], parser: MarkdownIt
) -> tuple[list[str], int, bool]:
    """Recognize inline label structure; return an offset into the original line."""
    indent = len(line) - len(line.lstrip(" \t"))
    raw = line[indent:]
    children = [
        token
        for token in (parser.parseInline(raw)[0].children or ())
        if token.type != "text" or token.content
    ]
    if not children:
        return [], 0, False
    first = children[0]
    if first.type == "text":
        label, separator, _ = raw.partition(":")
        # A bare label must be literal text, not markup containing a colon.
        if separator and first.content.startswith(label + ":"):
            names = aliases.get(label, [])
            return names, indent + len(label) + 1, False
        return [], 0, False
    if first.type not in ("strong_open", "em_open"):
        return [], 0, False
    if (
        len(children) < 3
        or children[1].type != "text"
        or children[2].type != first.type.replace("_open", "_close")
    ):
        return [], 0, True
    label = children[1].content
    marker = first.markup
    close = raw.find(marker, len(marker))
    if close < 0:
        return [], 0, True
    end = close + len(marker)
    if label.endswith((":", ".")):
        label = label[:-1]
    elif raw[end : end + 1] == ":":
        end += 1
    else:
        return [], 0, label in aliases
    names = aliases.get(label, [])
    return names, indent + end, not bool(names)


def _fields(
    blocks: tuple[ParsedUnit, ...],
    names: tuple[str, ...],
    options: Mapping[str, Any],
    text: str,
) -> tuple[dict[str, str], bool]:
    aliases: dict[str, list[str]] = {}
    for name in names:
        for label in options.get(name + "_labels", DEFAULT_LABELS[name]):
            aliases.setdefault(label, []).append(name)
    parser = MarkdownIt("commonmark")
    result: dict[str, str] = {}
    ambiguous = False
    current: str | None = None
    start = end = 0

    def finish() -> None:
        if current is not None:
            result[current] = text[start:end]

    for block in blocks:
        if block.unit_type == "table" and current is not None:
            if start == end:
                start = block.char_start
            end = block.char_end
            continue
        if block.unit_type != "paragraph" or block.parent_ref:
            finish()
            current = None
            continue
        for match in re.finditer(r"[^\r\n]+(?:\r\n|\r|\n|$)", block.source_substring):
            line = match.group().rstrip("\r\n")
            line_start = block.char_start + match.start()
            line_end = line_start + len(line)
            matches, value_offset, unsupported = _label_span(line, aliases, parser)
            if matches or unsupported:
                finish()
                current = None
                if unsupported or len(matches) != 1 or matches[0] in result:
                    ambiguous = True
                    continue
                current = matches[0]
                value_offset += len(line[value_offset:]) - len(
                    line[value_offset:].lstrip(" \t")
                )
                start = line_start + value_offset
                end = line_end
            elif current is not None:
                if start == end:
                    start = line_start
                end = line_end
    finish()
    return result, ambiguous


def prepare_records(
    units: tuple[ParsedUnit, ...],
    records: tuple[ParsedRecord, ...],
    text: str,
    registration: NativeRegistration,
    registry: Registry,
) -> tuple[tuple[ParsedUnit, ...], tuple[ParsedRecord, ...], tuple[Diagnostic, ...]]:
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    strategy = kind["normalizer"]
    diagnostics: list[Diagnostic] = []
    if strategy == "code_symbols@1":
        raise TychoraError(
            "CAPABILITY_MISSING", "Code symbols require an installed language adapter"
        )
    if registration.format == "json":
        return units, records, ()
    if strategy == "numbered_provisions@1":
        return numbered_provisions(units, text), (), ()
    if strategy not in RECORD_FIELDS:
        return units, (), ()
    options = kind.get("normalizer_options", {})
    output: list[ParsedUnit] = []
    values: list[ParsedRecord] = []
    index = 0
    while index < len(units):
        heading = units[index]
        if heading.unit_type != "heading":
            output.append(heading)
            index += 1
            continue
        end_index = index + 1
        while end_index < len(units) and not (
            units[end_index].unit_type == "heading"
            and units[end_index].heading_level <= heading.heading_level
        ):
            end_index += 1
        identifier = None
        if strategy == "rule_records@1":
            spelling = heading.text.split(maxsplit=1)[0] if heading.text else ""
            if any(
                spelling.startswith(prefix) and len(spelling) > len(prefix)
                for prefix in options.get("identifier_prefixes", ("STD-",))
            ):
                identifier = spelling
            else:
                output.append(heading)
                index += 1
                continue
        direct_end = index + 1
        while direct_end < end_index and units[direct_end].unit_type != "heading":
            direct_end += 1
        fields, ambiguous = _fields(
            units[index + 1 : direct_end], RECORD_FIELDS[strategy], options, text
        )
        required = (
            ("requirement",)
            if strategy == "rule_records@1"
            else ("comment",)
            if strategy == "feedback_records@1"
            else ("slot", "instruction")
        )
        if not fields and strategy != "rule_records@1":
            output.append(heading)
            index += 1
            continue
        if ambiguous or any(not fields.get(key) for key in required):
            diagnostics.append(
                Diagnostic(
                    code="ambiguous_record",
                    native_ref=heading.native_ref,
                    message="Missing or duplicate literal field labels; block remains original text",
                )
            )
            output.extend(units[index:end_index])
            index = end_index
            continue
        end = units[end_index].char_start if end_index < len(units) else len(text)
        raw = text[heading.char_start : end]
        merged = heading.model_copy(
            update={
                "unit_type": RECORD_TYPES[strategy],
                "source_substring": raw,
                "char_end": end,
                "line_end": heading.line_start + _physical_line_count(raw) - 1,
                "identifier": identifier,
            }
        )
        value: dict[str, Any] = {
            name: fields.get(name)
            for name in RECORD_FIELDS[strategy]
            if name != "foundation"
        }
        value.update(
            provenance=[
                f"{registration.capture.original_sha}:{registration.source.source_key}:{heading.char_start}:{end}"
            ],
            foundation_ids=[],
            foundation_refs=[
                part.strip()
                for part in fields.get("foundation", "").split(",")
                if part.strip()
            ],
        )
        registry.validate_record(registration.revision.kind_id, value)
        output.append(merged)
        values.append(
            ParsedRecord(native_ref=heading.native_ref, value=FrozenJsonMap(value))
        )
        index = end_index
    if not values and not diagnostics and units:
        diagnostics.append(
            Diagnostic(
                code="unsupported_record_structure",
                native_ref=units[0].native_ref,
                message="No unambiguous heading and registered literal fields; original blocks retained",
            )
        )
    return tuple(output), tuple(values), tuple(diagnostics)


def resolve_records(
    units: tuple[ParsedUnit, ...], records: tuple[ParsedRecord, ...], eid: str
) -> tuple[tuple[ParsedRecord, ...], tuple[Diagnostic, ...]]:
    lookup: dict[str, list[str]] = {}
    ids = {
        unit.native_ref: identity.unit_id(
            eid,
            unit.native_ref,
            index,
            sha256(unit.source_substring.encode()).hexdigest(),
        )
        for index, unit in enumerate(units)
    }
    for unit in units:
        if unit.identifier:
            lookup.setdefault(unit.identifier, []).append(ids[unit.native_ref])
    result = []
    diagnostics = []
    for record in records:
        value: dict[str, Any] = dict(record.value)
        foundations = list(cast(tuple[str, ...], value.get("foundation_ids", ())))
        for ref in cast(tuple[str, ...], value.get("foundation_refs", ())):
            candidates = lookup.get(ref, [])
            if len(candidates) == 1:
                if candidates[0] not in foundations:
                    foundations.append(candidates[0])
            else:
                diagnostics.append(
                    Diagnostic(
                        code="ambiguous_foundation"
                        if candidates
                        else "unresolved_foundation",
                        native_ref=record.native_ref,
                        message="Literal foundation reference has no unique original unit in this extraction",
                    )
                )
        value["foundation_ids"] = foundations
        result.append(
            ParsedRecord(native_ref=record.native_ref, value=FrozenJsonMap(value))
        )
    return tuple(result), tuple(diagnostics)


def numbered_provisions(
    units: tuple[ParsedUnit, ...], text: str
) -> tuple[ParsedUnit, ...]:
    """Keep every independently numbered original span and its enclosing provision."""
    section_ends: dict[str, int] = {}
    headings: list[ParsedUnit] = []
    for unit in units:
        if unit.unit_type != "heading":
            continue
        while headings and headings[-1].heading_level >= unit.heading_level:
            section_ends[headings.pop().native_ref] = unit.char_start
        headings.append(unit)
    section_ends.update({unit.native_ref: len(text) for unit in headings})

    output: list[ParsedUnit] = []
    parents: list[ParsedUnit] = []
    for unit in units:
        while parents and unit.char_start >= parents[-1].char_end:
            parents.pop()
        numbered = (
            re.match(r"^\s*([0-9]+[.)])\s", unit.source_substring)
            if unit.unit_type == "list_item"
            else None
        )
        heading = (
            re.match(
                r"^(Article [0-9]+(?:[.-][0-9]+)*|[0-9]+(?:\.[0-9]+)*[.)]?)(?:\s|$)",
                unit.text,
            )
            if unit.unit_type == "heading"
            else None
        )
        identifier = numbered or heading
        if identifier is None:
            if not parents:
                output.append(unit)
            continue
        end = section_ends[unit.native_ref] if heading else unit.char_end
        raw = text[unit.char_start : end]
        provision = unit.model_copy(
            update={
                "unit_type": "section" if heading else "paragraph",
                "identifier": identifier.group(1),
                "parent_ref": parents[-1].native_ref if parents else unit.parent_ref,
                "source_substring": raw,
                "char_end": end,
                "line_end": unit.line_start + _physical_line_count(raw) - 1,
            }
        )
        output.append(provision)
        parents.append(provision)
    return tuple(output)


def _physical_line_count(text: str) -> int:
    return len(re.findall(r"\r\n|\r|\n", text)) + int(not text.endswith(("\r", "\n")))
