"""Exact original table/row/caption views for pre-commit composition."""

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, cast

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import ExtractionBundle, LocatorRow
from tychora.contracts.documents import DocumentUnit
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import digest, extraction_id, unit_id
from tychora.contracts.tables import TableFragment

from .document_identity import DocumentNormalizationContext
from .document_parts import (
    _Budget,
    _part_locators,
    document_overlap_owners,
    represent_document_parts,
    validate_document_ownership,
)
from .documents import assemble_document_bundle


def source_views(
    parsed: ExtractionArtifact,
    context: DocumentNormalizationContext,
    base: ExtractionBundle | None,
) -> tuple[ExtractionBundle, tuple[TableFragment, ...]]:
    revision = parsed.registration.revision.revision_id
    eid = extraction_id(
        revision, parsed.parser_fingerprint, context.normalizer_fingerprint
    )
    source_key = parsed.registration.source.source_key
    items = cast(tuple[DocumentUnit, ...], parsed.units)
    budget = _Budget()
    units = list(base.units) if base else []
    for index, unit in enumerate(units):
        budget.add(
            work=1
            + len(cast(tuple[Any, ...], unit.attributes["source_parts"]))
            + len(cast(tuple[Any, ...], unit.attributes["structural_separators"]))
        )
        budget.account("units", index, unit)
    tables: list[tuple[DocumentUnit, str]] = []

    def view(
        item: DocumentUnit, kind: str, row: int | None = None, *, original: bool = False
    ) -> str:
        text, parts, separators, locators = represent_document_parts(
            (item,), revision, eid, source_key, budget, parsed.original_ref.sha256
        )
        if row is not None:
            cells = cast(Mapping[str, Any], item.raw_item["data"])["table_cells"]
            selected = [
                part
                for part in parts
                if cells[part.cell_index]["start_row_offset_idx"] == row
            ]
            fragments, output_parts, output_locators, output_separators = [], [], [], []
            offset = 0
            for index, part in enumerate(selected):
                if index:
                    output_separators.append(
                        {"unit_start": offset, "unit_end": offset + 1, "text": "\t"}
                    )
                    fragments.append("\t")
                    offset += 1
                value = text[part.unit_start : part.unit_end]
                part = part.model_copy(
                    update={"unit_start": offset, "unit_end": offset + len(value)}
                )
                output_parts.append(part)
                output_locators.extend(
                    _part_locators(item, part, source_key, parsed.original_ref.sha256)
                )
                fragments.append(value)
                offset += len(value)
            # A fully covered merged row has no independent source cell anchor.
            if not selected:
                return ""
            text, parts, locators = (
                "".join(fragments),
                tuple(output_parts),
                tuple(output_locators),
            )
            serialized_separators = output_separators
        else:
            serialized_separators = [sep.model_dump(mode="json") for sep in separators]
        for unit in units:
            if (
                unit.unit_type == kind
                and unit.text == text
                and unit.attributes.get("source_refs") == (item.native_ref,)
                and unit.attributes.get("source_parts")
                == tuple(FrozenJsonMap(part.model_dump(mode="json")) for part in parts)
            ):
                return unit.unit_id
        ref = (
            item.native_ref
            if original
            else f"docling-view:{kind}:{item.native_ref}"
            + (f":{row}" if row is not None else "")
        )
        ordinal = len(units)
        uid = unit_id(eid, ref, ordinal, sha256(text.encode()).hexdigest())
        unit = EvidenceUnit(
            unit_id=uid,
            extraction_id=eid,
            parent_id=None,
            ordinal=ordinal,
            unit_type=kind,
            native_ref=ref,
            boundary_key=ref,
            heading_path=item.heading_path,
            text=text,
            text_sha=sha256(text.encode()).hexdigest(),
            locators=locators,
            attributes=FrozenJsonMap(
                {
                    "document_representation": "docling-source-parts@1",
                    "source_refs": [item.native_ref],
                    "source_parts": [part.model_dump(mode="json") for part in parts],
                    "structural_separators": serialized_separators,
                    "semantic_diagnostics": [],
                }
            ),
        )
        budget.add(work=1)
        budget.account("units", ordinal, unit)
        units.append(unit)
        return uid

    if base is None:
        for item in items:
            view(item, item.unit_type, original=True)
        ids = {unit.native_ref: unit.unit_id for unit in units}
        units = [
            unit.model_copy(update={"parent_id": ids.get(item.parent_ref)})
            for unit, item in zip(units, items)
        ]
    for item in items:
        if item.unit_type == "table":
            if not item.cells:
                raise ValueError("Table composition requires a retained nonempty grid")
            tables.append((item, view(item, "table")))
            for row in range(len(item.cells)):
                view(item, "table_row", row)
        if item.unit_type == "caption":
            view(item, "caption")
    # Raw caption edges are authoritative; retain linked paragraph captions too.
    caption_refs = {
        ref["$ref"]
        for item in items
        for ref in cast(
            tuple[Mapping[str, Any], ...], item.raw_item.get("captions", ())
        )
    }
    for item in items:
        if item.native_ref in caption_refs:
            view(item, "caption")
    overlaps = document_overlap_owners(tuple(units), budget)
    units = [
        unit.model_copy(
            update={
                "attributes": FrozenJsonMap(
                    {
                        **unit.attributes,
                        "overlapping_owner_ids": sorted(overlaps[unit.unit_id]),
                    }
                )
            }
        )
        for unit in units
    ]
    validate_document_ownership(tuple(units), parsed)
    owners: dict[str, list[str]] = {}
    for unit in units:
        for ref in cast(tuple[str, ...], unit.attributes["source_refs"]):
            owners.setdefault(ref, []).append(unit.unit_id)
    locators = tuple(
        LocatorRow(
            locator_id=digest("locator-v1", unit.unit_id, i),
            unit_id=unit.unit_id,
            fragment_ordinal=i,
            locator=loc,
        )
        for unit in units
        for i, loc in enumerate(unit.locators)
    )
    bundle = assemble_document_bundle(
        parsed,
        tuple(units),
        locators,
        {ref: tuple(ids) for ref, ids in owners.items()},
        base.records if base else (),
        base.identifiers if base else (),
        context=context,
    )
    lookup = {item.native_ref: item for item in items}
    canonical = {unit.unit_id: unit for unit in units}
    fragments = []
    previous_ordinal = -1
    for item, uid in tables:
        prov = item.provenance[0] if len(item.provenance) == 1 else None
        caption_refs = [
            ref["$ref"]
            for ref in cast(
                tuple[Mapping[str, Any], ...], item.raw_item.get("captions", ())
            )
        ]
        caption = "\n".join(lookup[ref].text for ref in caption_refs)
        number = re.match(r"(?i)^Table\s+([0-9]+(?:[.-][0-9]+)*)(?:\s|[:.]|$)", caption)
        cells = cast(Mapping[str, Any], item.raw_item["data"])["table_cells"]
        headers = tuple(
            sorted(
                {
                    cell["start_row_offset_idx"]
                    for cell in cells
                    if cell.get("column_header")
                }
            )
        )
        supports = set(overlaps[uid])
        for ref in caption_refs:
            supports.update(owners[ref])
        headings = [
            other.heading_level
            for other in items[previous_ordinal + 1 : item.ordinal]
            if other.heading_level
        ]
        fragments.append(
            TableFragment(
                fragment_id=digest("table-fragment-v1", eid, item.native_ref),
                revision_id=revision,
                extraction_id=eid,
                unit_id=uid,
                native_ref=item.native_ref,
                page=prov.physical_page if prov else None,
                column_count=len(item.cells[0]),
                grid=item.cells,
                source_cells=tuple(FrozenJsonMap(cell) for cell in cells),
                header_rows=headers,
                caption=caption,
                table_number=number.group(1) if number else None,
                continuation_marker=bool(re.search(r"(?i)\bcontinued\b", caption)),
                heading_path=item.heading_path,
                heading_level=next(
                    (
                        other.heading_level
                        for other in reversed(items[: item.ordinal + 1])
                        if other.heading_level
                    ),
                    0,
                ),
                intervening_heading_level=min(headings)
                if headings and previous_ordinal >= 0
                else None,
                bbox=prov.geometry if prov else None,
                page_width=prov.page_width if prov else None,
                page_height=prov.page_height if prov else None,
                required_unit_ids=frozenset(supports),
                locators=cast(Any, canonical[uid].locators),
                asset_ids=tuple(
                    asset.asset_id
                    for asset in bundle.assets
                    if asset.native_ref == item.native_ref
                ),
            )
        )
        previous_ordinal = item.ordinal
    for index, unit in enumerate(units):
        budget.account("units", index, unit)
    for index, fragment in enumerate(fragments):
        budget.account("fragments", index, fragment)
    return bundle, tuple(fragments)
