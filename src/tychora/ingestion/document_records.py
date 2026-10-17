"""Literal rich-document records with explicit original field ownership."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, cast

from tychora.adapters.docling.ocr_evidence import normalize_native_document_units
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    ExtractionBundle,
    IdentifierRow,
    LocatorRow,
    StructuredRecordRow,
)
from tychora.contracts.documents import (
    DocumentRegistration,
    DocumentUnit,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import canonical_bytes, digest, extraction_id, unit_id

from .document_identity import DocumentNormalizationContext
from .document_parts import (
    _Budget,
    document_overlap_owners,
    represent_document_parts,
    validate_document_ownership,
)
from .documents import assemble_document_bundle
from .normalize import DEFAULT_LABELS, RECORD_FIELDS, RECORD_TYPES


def document_normalizer_fingerprint(
    kind: Mapping[str, Any], registry: Registry, *, legacy_v1: bool = False
) -> str:
    version = "v1" if legacy_v1 else "v2"
    version = (
        f"docling-normalizer-{version}"
        if kind["normalizer"] == "structured_sections@1"
        else f"docling-record-normalizer-{version}"
    )
    return digest(version, kind, registry.schema_digests)


@dataclass
class _Group:
    originals: tuple[DocumentUnit, ...]
    native_ref: str
    parent_ref: str | None
    unit_type: str
    identifier: str | None = None
    value: dict[str, Any] | None = None
    diagnostics: list[dict[str, str]] = field(default_factory=list)


def _fields(
    items: tuple[DocumentUnit, ...],
    strategy: str,
    options: Mapping[str, Any],
    budget: _Budget,
) -> tuple[dict[str, str], bool]:
    aliases: dict[str, list[str]] = {}
    for name in RECORD_FIELDS[strategy]:
        defaults = (
            ("Foundation", "Foundations")
            if name == "foundation"
            else DEFAULT_LABELS[name]
        )
        for label in options.get(name + "_labels", defaults):
            aliases.setdefault(label, []).append(name)
    values: dict[str, list[str]] = {}
    current: str | None = None
    ambiguous = False
    for item in items:
        if item.heading_level:
            break
        previous_ending = "\n\n"
        text = item.text
        marker = cast(str, item.raw_item.get("marker", ""))
        if marker and not (
            text.startswith(marker) and item.raw_item.get("orig") == text
        ):
            text = marker + " " + text
        for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text):
            if not match.group():
                continue
            budget.add(work=1)
            line = match.group().rstrip("\r\n")
            ending = match.group()[len(line) :]
            label, colon, value = line.partition(":")
            names = aliases.get(label, ()) if colon else ()
            if colon and item.unit_type not in ("table", "document_index"):
                current = None
                if len(names) != 1 or names[0] in values:
                    ambiguous = True
                else:
                    current = names[0]
                    literal = value.lstrip(" \t")
                    values[current] = [literal] if literal else []
                    if any(" " + alias + ":" in literal for alias in aliases):
                        ambiguous = True
            elif current is not None:
                if values[current]:
                    values[current].append(previous_ending)
                values[current].append(line)
            previous_ending = ending
    return {name: "".join(chunks) for name, chunks in values.items()}, ambiguous


def _number(item: DocumentUnit) -> str | None:
    if item.heading_level:
        match = re.match(
            r"^(Article [0-9]+(?:[.-][0-9]+)*|[0-9]+(?:\.[0-9]+)*[.)]?)(?:\s|$)",
            item.text,
        )
    else:
        marker = cast(str, item.raw_item.get("marker", ""))
        match = re.match(r"^([0-9]+[.)])(?:\s|$)", marker or item.text)
    return match.group(1) if match else None


def _section_ends(items: tuple[DocumentUnit, ...], budget: _Budget) -> dict[int, int]:
    ends = {}
    stack: list[int] = []
    for index, item in enumerate(items):
        budget.add(work=1)
        if not item.heading_level:
            continue
        number = (_number(item) or "").removeprefix("Article ")
        while stack:
            parent = items[stack[-1]]
            parent_number = (_number(parent) or "").removeprefix("Article ")
            if item.heading_level > parent.heading_level or (
                parent_number and number.startswith(parent_number + ".")
            ):
                break
            ends[stack.pop()] = index
        if len(stack) >= 128:
            raise TychoraError(
                "INVALID_REQUEST",
                "SEMANTIC_EXPANSION_LIMIT: heading depth exceeds capacity",
            )
        stack.append(index)
    ends.update({index: len(items) for index in stack})
    return ends


def _native_subtree_ends(
    items: tuple[DocumentUnit, ...], budget: _Budget
) -> dict[int, int]:
    ends: dict[int, int] = {}
    stack: list[int] = []
    for index, item in enumerate(items):
        budget.add(work=1)
        while stack and items[stack[-1]].native_ref != item.parent_ref:
            ends[stack.pop()] = index
        stack.append(index)
    ends.update({index: len(items) for index in stack})
    return ends


def _groups(
    items: tuple[DocumentUnit, ...],
    strategy: str,
    options: Mapping[str, Any],
    budget: _Budget,
) -> list[_Group]:
    groups: list[_Group] = []
    ends = _section_ends(items, budget)
    subtree_ends = (
        _native_subtree_ends(items, budget)
        if strategy == "numbered_provisions@1"
        else {}
    )
    parents: list[tuple[int, str]] = []
    index = 0
    while index < len(items):
        budget.add(work=1)
        heading = items[index]
        while parents and index >= parents[-1][0]:
            parents.pop()
        parent = parents[-1][1] if parents else heading.parent_ref
        if strategy == "numbered_provisions@1":
            identifier = _number(heading)
            if identifier:
                if re.search(r"[\r\n][ \t]*[0-9]+[.)](?:[ \t]|$)", heading.text):
                    raise TychoraError(
                        "CAPABILITY_MISSING",
                        "Multiple numbered lines in one retained SDK item are not qualified",
                    )
                end = ends[index] if heading.heading_level else subtree_ends[index]
                ref = "docling-record:" + strategy + ":" + heading.native_ref
                budget.add(work=end - index)
                groups.append(
                    _Group(
                        items[index:end],
                        ref,
                        parent,
                        "section" if heading.heading_level else "paragraph",
                        identifier,
                    )
                )
                if end > index + 1:
                    parents.append((end, ref))
            elif not parents:
                groups.append(
                    _Group((heading,), heading.native_ref, parent, heading.unit_type)
                )
            index += 1
            continue
        identifier = heading.text.split(maxsplit=1)[0] if heading.text else ""
        recognized = bool(heading.heading_level) and (
            strategy != "rule_records@1"
            or any(
                identifier.startswith(p) and len(identifier) > len(p)
                for p in options.get("identifier_prefixes", ("STD-",))
            )
        )
        if not recognized:
            groups.append(
                _Group((heading,), heading.native_ref, parent, heading.unit_type)
            )
            index += 1
            continue
        end = index + 1
        while end < len(items) and not items[end].heading_level:
            budget.add(work=1)
            end += 1
        budget.add(work=end - index)
        selected = items[index:end]
        fields, ambiguous = _fields(selected[1:], strategy, options, budget)
        if strategy == "template_slots@1":
            fields.setdefault("slot", heading.text)
        required = (
            "requirement"
            if strategy == "rule_records@1"
            else "comment"
            if strategy == "feedback_records@1"
            else "instruction"
        )
        value: dict[str, Any] | None = None
        diagnostics = []
        if ambiguous or not fields.get(required):
            diagnostics.append(
                {
                    "code": "ambiguous_record",
                    "native_ref": heading.native_ref,
                    "message": "Missing, unknown or duplicate literal field labels; original text retained",
                }
            )
        else:
            value = {
                name: fields.get(name)
                for name in RECORD_FIELDS[strategy]
                if name != "foundation"
            }
            value.update(
                foundation_ids=[],
                foundation_refs=[
                    p.strip()
                    for p in fields.get("foundation", "").split(",")
                    if p.strip()
                ],
            )
        groups.append(
            _Group(
                selected,
                "docling-record:" + strategy + ":" + heading.native_ref,
                parent,
                RECORD_TYPES[strategy] if value else heading.unit_type,
                identifier if strategy == "rule_records@1" else None,
                value,
                diagnostics,
            )
        )
        index = end
    if groups and not any(
        group.identifier or group.value or group.diagnostics for group in groups
    ):
        groups[0].diagnostics.append(
            {
                "code": "unsupported_record_structure",
                "native_ref": items[0].native_ref,
                "message": "No registered source heading/field grammar; original text retained",
            }
        )
    return groups


def normalize_document_records(
    parsed: ExtractionArtifact,
    revision_id: str,
    registry: Registry,
    *,
    context: DocumentNormalizationContext | None = None,
) -> ExtractionBundle:
    parsed = ExtractionArtifact.model_validate(parsed.model_dump())
    registration = cast(DocumentRegistration, parsed.registration)
    if (
        revision_id != registration.revision.revision_id
        or registry.registry_digest != parsed.registry_digest
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Document normalization revision or registry binding differs",
        )
    kind = cast(Mapping[str, Any], registry.kinds[registration.revision.kind_id])
    strategy = kind["normalizer"]
    if strategy not in (*RECORD_FIELDS, "numbered_provisions@1"):
        raise TychoraError(
            "CAPABILITY_MISSING", "Requested rich-document normalizer is not qualified"
        )
    if parsed.normalizer_fingerprint != document_normalizer_fingerprint(
        kind, registry, legacy_v1=context.legacy_v1 if context else False
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "Document semantic normalizer fingerprint differs"
        )
    if context is not None:
        context.verify(parsed, registry)
    fingerprint = (
        context.normalizer_fingerprint if context else parsed.normalizer_fingerprint
    )
    eid = extraction_id(revision_id, parsed.parser_fingerprint, fingerprint)
    base = assemble_document_bundle(
        parsed, (), (), {u.native_ref: () for u in parsed.units}, context=context
    )
    budget = _Budget()
    budget.add(size=len(canonical_bytes(base.model_dump(mode="json"))))
    groups = _groups(
        normalize_native_document_units(parsed),
        strategy,
        kind.get("normalizer_options", {}),
        budget,
    )
    units = []
    locators = []
    records = []
    identifiers = []
    owners: dict[str, list[str]] = {}
    for ordinal, group in enumerate(groups):
        text, parts, separators, fragments = represent_document_parts(
            group.originals,
            revision_id,
            eid,
            registration.source.source_key,
            budget,
            parsed.original_ref.sha256,
            legacy_v1=context.legacy_v1 if context else False,
        )
        uid = unit_id(eid, group.native_ref, ordinal, sha256(text.encode()).hexdigest())
        for item in group.originals:
            owners.setdefault(item.native_ref, []).append(uid)
        units.append(
            EvidenceUnit(
                unit_id=uid,
                extraction_id=eid,
                parent_id=None,
                ordinal=ordinal,
                unit_type=group.unit_type,
                native_ref=group.native_ref,
                boundary_key=group.native_ref,
                heading_path=group.originals[0].heading_path,
                text=text,
                text_sha=sha256(text.encode()).hexdigest(),
                locators=fragments,
                attributes=FrozenJsonMap(
                    {
                        "document_representation": "docling-source-parts@1",
                        "source_parts": [p.model_dump(mode="json") for p in parts],
                        "structural_separators": [
                            s.model_dump(mode="json") for s in separators
                        ],
                        "source_refs": [i.native_ref for i in group.originals],
                        "semantic_diagnostics": group.diagnostics,
                    }
                ),
            )
        )
        locators.extend(
            LocatorRow(
                locator_id=digest("locator-v1", uid, i),
                unit_id=uid,
                fragment_ordinal=i,
                locator=loc,
            )
            for i, loc in enumerate(fragments)
        )
        if group.identifier:
            identifiers.append(
                IdentifierRow(
                    namespace="provision"
                    if strategy == "numbered_provisions@1"
                    else "rule",
                    normalized_value=group.identifier,
                    original_value=group.identifier,
                    unit_id=uid,
                )
            )
        if group.value is not None:
            group.value["provenance"] = [
                p.source_domain + ":" + str(p.source_start) + ":" + str(p.source_end)
                for p in parts
            ]
            registry.validate_record(registration.revision.kind_id, group.value)
            bindings = cast(
                Mapping[str, Any], registry.artifact["definition_schema_bindings"]
            )
            schema = bindings["kinds"][registration.revision.kind_id]["record_schema"]
            records.append(
                StructuredRecordRow(
                    unit_id=uid,
                    record_type=schema,
                    schema_hash=cast(str, registry.schema_digests[schema]),
                    data_json=FrozenJsonMap(group.value),
                )
            )
        budget.account("units", ordinal, units[-1])
    lookup: dict[str, list[str]] = {}
    for row in identifiers:
        lookup.setdefault(row.original_value, []).append(row.unit_id)
    for index, group in enumerate(groups):
        if group.identifier and len(lookup[group.identifier]) > 1:
            group.diagnostics.append(
                {
                    "code": "duplicate_identifier",
                    "native_ref": group.originals[0].native_ref,
                    "message": "Original identifier has multiple source owners",
                }
            )
        if group.value is not None:
            for ref in group.value["foundation_refs"]:
                candidates = lookup.get(ref, [])
                if len(candidates) == 1:
                    if candidates[0] not in group.value["foundation_ids"]:
                        group.value["foundation_ids"].append(candidates[0])
                else:
                    group.diagnostics.append(
                        {
                            "code": "ambiguous_foundation"
                            if candidates
                            else "unresolved_foundation",
                            "native_ref": group.originals[0].native_ref,
                            "message": "Literal foundation has no unique original owner in this extraction",
                        }
                    )
        attrs = dict(units[index].attributes)
        attrs["semantic_diagnostics"] = group.diagnostics
        units[index] = units[index].model_copy(
            update={"attributes": FrozenJsonMap(attrs)}
        )
    values = {
        units[i].unit_id: group.value
        for i, group in enumerate(groups)
        if group.value is not None
    }
    records = [
        row.model_copy(update={"data_json": FrozenJsonMap(values[row.unit_id])})
        for row in records
    ]
    canonical_ids = {u.native_ref: u.unit_id for u in units}
    for index, group in enumerate(groups):
        parent = canonical_ids.get(group.parent_ref) if group.parent_ref else None
        units[index] = units[index].model_copy(update={"parent_id": parent})
    overlaps = document_overlap_owners(tuple(units), budget)
    for index, unit in enumerate(units):
        attrs = dict(unit.attributes)
        attrs["overlapping_owner_ids"] = sorted(overlaps[unit.unit_id])
        units[index] = unit.model_copy(update={"attributes": FrozenJsonMap(attrs)})
    validate_document_ownership(tuple(units), parsed)
    for name, rows in (
        ("units", units),
        ("locators", locators),
        ("records", records),
        ("identifiers", identifiers),
    ):
        for index, row in enumerate(rows):
            budget.account(name, index, row)
    budget.add(
        work=sum(
            len(owners.get(ref, ()))
            for asset in parsed.assets
            for ref in asset.source_units
        )
    )
    bundle = assemble_document_bundle(
        parsed,
        tuple(units),
        tuple(locators),
        {ref: tuple(ids) for ref, ids in owners.items()},
        tuple(records),
        tuple(identifiers),
        context=context,
    )
    for index, row in enumerate(bundle.unit_assets):
        budget.account("unit_assets", index, row)
    return bundle
