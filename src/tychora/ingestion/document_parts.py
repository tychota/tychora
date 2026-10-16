"""Canonical document source coordinates and bounded ownership validation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from tychora.adapters.docling.ocr_evidence import normalize_native_document_units
from tychora.contracts.base import Contract
from tychora.contracts.documents import (
    DocumentSeparator,
    DocumentSourcePart,
    DocumentUnit,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import DocumentLocator, EvidenceUnit
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import canonical_bytes, digest

# Documented ingestion limits: aggregate semantic expansion ceilings. They count
# repeated ancestor representations as output, not just distinct original items.
MAX_SEMANTIC_WORK = 100_000
MAX_SEMANTIC_BYTES = 256 * 1024 * 1024


@dataclass
class _Budget:
    work: int = 0
    size: int = 0
    pending: int = 0
    rows: dict[tuple[str, int], int] = field(default_factory=dict)

    def add(self, work: int = 0, size: int = 0) -> None:
        self.work += work
        self.size += size
        if self.work > MAX_SEMANTIC_WORK or self.size > MAX_SEMANTIC_BYTES:
            raise TychoraError(
                "INVALID_REQUEST",
                "SEMANTIC_EXPANSION_LIMIT: canonical document output exceeds aggregate capacity",
            )

    def reserve(self, size: int) -> None:
        self.pending += size
        if self.size + self.pending > MAX_SEMANTIC_BYTES:
            self.add(size=MAX_SEMANTIC_BYTES + 1)

    def account(self, collection: str, index: int, row: Contract) -> None:
        size = len(canonical_bytes(row.model_dump(mode="json")))
        key = collection, index
        previous = self.rows.get(key)
        self.add(
            size=size - previous if previous is not None else size + int(index > 0)
        )
        self.rows[key] = size
        self.pending = 0


def _part_locators(
    item: DocumentUnit,
    part: DocumentSourcePart,
    source_key: str,
    artifact_sha: str,
) -> tuple[DocumentLocator, ...]:
    """Project a proven original field slice into its canonical unit range."""
    projected: list[DocumentLocator] = []
    native_locators = item.locators
    if part.field == "marker" and item.provenance:
        native_locators = tuple(
            DocumentLocator(
                artifact_ref=artifact_sha,
                native_ref=source_key,
                char_start=max(part.source_start, prov.charspan[0])
                if prov.char_domain == "docling_orig_text_item"
                else part.source_start,
                char_end=min(part.source_end, prov.charspan[1])
                if prov.char_domain == "docling_orig_text_item"
                else part.source_end,
                physical_page=prov.physical_page,
                geometry=prov.geometry,
            )
            for index, prov in enumerate(item.provenance)
            if prov.char_domain != "docling_assembly_step" or index == 0
            if prov.char_domain != "docling_orig_text_item"
            or min(part.source_end, prov.charspan[1])
            > max(part.source_start, prov.charspan[0])
        )
    for loc in native_locators:
        if part.field in ("text", "marker") and item.provenance:
            left, right = (
                max(part.source_start, loc.char_start),
                min(part.source_end, loc.char_end),
            )
            if right <= left:
                continue
            canonical_start, canonical_end = (
                part.unit_start + left - part.source_start,
                part.unit_start + right - part.source_start,
            )
        else:
            canonical_start, canonical_end = part.unit_start, part.unit_end
        projected.append(
            loc.model_copy(
                update={
                    "native_ref": source_key,
                    "char_start": canonical_start,
                    "char_end": canonical_end,
                }
            )
        )
    return tuple(projected)


def _point_locators(
    item: DocumentUnit, offset: int, source_key: str
) -> tuple[DocumentLocator, ...]:
    return tuple(
        loc.model_copy(
            update={"native_ref": source_key, "char_start": offset, "char_end": offset}
        )
        for loc in item.locators
    )


def _source_field_order(
    item: DocumentUnit, domain: str, cell_index: int | None
) -> tuple[int, int, int, int, int]:
    """Order by retained source structure, independently of canonical offsets."""
    if domain == "table_cell":
        cells = cast(
            tuple[Mapping[str, Any], ...],
            cast(Mapping[str, Any], item.raw_item.get("data", {})).get(
                "table_cells", ()
            ),
        )
        if cell_index is None or cell_index >= len(cells):
            raise ValueError("Missing original table cell")
        cell = cells[cell_index]
        return (
            item.ordinal,
            0,
            cell["start_row_offset_idx"],
            cell["start_col_offset_idx"],
            cell_index,
        )
    return item.ordinal, 0 if domain == "marker" else 1, 0, 0, 0


def represent_document_parts(
    originals: tuple[DocumentUnit, ...],
    revision: str,
    eid: str,
    source_key: str,
    budget: _Budget,
    artifact_sha: str,
    *,
    legacy_v1: bool = False,
) -> tuple[
    str,
    tuple[DocumentSourcePart, ...],
    tuple[DocumentSeparator, ...],
    tuple[DocumentLocator, ...],
]:
    fragments: list[str] = []
    parts: list[DocumentSourcePart] = []
    separators: list[DocumentSeparator] = []
    locators: list[DocumentLocator] = []
    offset = 0

    def separator(text: str) -> None:
        nonlocal offset
        budget.add(work=1)
        budget.reserve(len(text.encode()))
        separators.append(
            DocumentSeparator.model_validate(
                {"unit_start": offset, "unit_end": offset + len(text), "text": text}
            )
        )
        fragments.append(text)
        offset += len(text)

    def original(
        item: DocumentUnit,
        domain: str,
        value: str,
        cell: int | None = None,
        start: int = 0,
    ) -> None:
        nonlocal offset
        budget.add(work=1)
        if not value and not legacy_v1:
            return
        budget.reserve(len(value.encode()))
        part = DocumentSourcePart.model_validate(
            {
                "native_ref": item.native_ref,
                "field": domain,
                "cell_index": cell,
                "source_start": start,
                "source_end": start + len(value),
                "unit_start": offset,
                "unit_end": offset + len(value),
                "source_domain": digest(
                    "document-source-domain-v1",
                    revision,
                    eid,
                    item.native_ref,
                    domain,
                    cell,
                ),
                "provenance": item.provenance,
            }
        )
        budget.reserve(len(canonical_bytes(part.model_dump(mode="json"))))
        parts.append(part)
        fragments.append(value)
        locators.extend(_part_locators(item, part, source_key, artifact_sha))
        offset += len(value)

    for index, item in enumerate(originals):
        if index:
            separator("\n\n")
        if item.unit_type in ("table", "document_index"):
            cells = cast(
                tuple[Mapping[str, Any], ...],
                cast(Mapping[str, Any], item.raw_item.get("data", {})).get(
                    "table_cells", ()
                ),
            )
            ordered = sorted(
                enumerate(cells),
                key=lambda pair: _source_field_order(item, "table_cell", pair[0]),
            )
            row = None
            for cell_index, cell in ordered:
                if row is not None:
                    separator("\n" if cell["start_row_offset_idx"] != row else "\t")
                original(item, "table_cell", cell["text"], cell_index)
                row = cell["start_row_offset_idx"]
        else:
            marker = cast(str, item.raw_item.get("marker", ""))
            text_start = 0
            if marker:
                original(item, "marker", marker)
                if (
                    item.text.startswith(marker)
                    and item.raw_item.get("orig") == item.text
                ):
                    text_start = len(marker)
                elif item.text:
                    separator(" ")
            if "text" in item.raw_item:
                original(item, "text", item.text[text_start:], start=text_start)
        if "text" not in item.raw_item and item.unit_type not in (
            "table",
            "document_index",
        ):
            locators.extend(_point_locators(item, offset, source_key))
    return "".join(fragments), tuple(parts), tuple(separators), tuple(locators)


def validate_document_source_parts(
    unit: EvidenceUnit, parsed: ExtractionArtifact
) -> None:
    """Validate canonical ownership against complete verified SDK facts."""
    items = {
        item.native_ref: cast(DocumentUnit, item)
        for item in normalize_native_document_units(parsed)
    }
    _validate_source_parts(unit, parsed, items)


def _validate_source_parts(
    unit: EvidenceUnit, parsed: ExtractionArtifact, items: Mapping[str, DocumentUnit]
) -> None:
    attrs = unit.attributes
    refs = cast(tuple[str, ...], attrs.get("source_refs", ()))
    if not refs or len(set(refs)) != len(refs) or any(ref not in items for ref in refs):
        raise ValueError("Missing or duplicate grouped document source references")
    ref_set = set(refs)
    ordinals = [items[ref].ordinal for ref in refs]
    if ordinals != list(range(ordinals[0], ordinals[0] + len(ordinals))):
        raise ValueError("Grouped document source ancestry/order differs")
    parts = tuple(
        DocumentSourcePart.model_validate_json(canonical_bytes(p))
        for p in cast(tuple[Any, ...], attrs.get("source_parts", ()))
    )
    separators = tuple(
        DocumentSeparator.model_validate_json(canonical_bytes(s))
        for s in cast(tuple[Any, ...], attrs.get("structural_separators", ()))
    )
    ranges = []
    seen: dict[tuple[str, str, int | None], list[tuple[int, int]]] = {}
    previous_order: tuple[int, ...] = (-1,)
    previous_end = 0
    represented = set()
    for part in parts:
        if part.native_ref not in ref_set:
            raise ValueError("Document part has no source owner")
        item = items[part.native_ref]
        if part.field == "table_cell":
            cells = cast(
                tuple[Mapping[str, Any], ...],
                cast(Mapping[str, Any], item.raw_item.get("data", {})).get(
                    "table_cells", ()
                ),
            )
            if part.cell_index is None or part.cell_index >= len(cells):
                raise ValueError("Missing original table cell")
            source = cells[part.cell_index]["text"]
        else:
            source = item.raw_item.get(part.field)
        if not isinstance(source, str) or part.source_end > len(source):
            raise ValueError("Missing or out-of-range original field")
        order = (
            *_source_field_order(item, part.field, part.cell_index),
            part.source_start,
        )
        if order < previous_order or part.unit_start < previous_end:
            raise ValueError("Document source part order differs")
        previous_order = order
        previous_end = part.unit_end
        if (
            part.unit_end > len(unit.text)
            or source[part.source_start : part.source_end]
            != unit.text[part.unit_start : part.unit_end]
            or part.provenance != item.provenance
            or part.source_domain
            != digest(
                "document-source-domain-v1",
                parsed.registration.revision.revision_id,
                unit.extraction_id,
                part.native_ref,
                part.field,
                part.cell_index,
            )
        ):
            raise ValueError("Document source part text/domain/provenance differs")
        key = part.native_ref, part.field, part.cell_index
        intervals = seen.setdefault(key, [])
        if any(
            part.source_start < end
            and part.source_end > start
            or (part.source_start, part.source_end) == (start, end)
            for start, end in intervals
        ):
            raise ValueError("Duplicate or conflicting source part ownership")
        intervals.append((part.source_start, part.source_end))
        represented.add(part.native_ref)
        ranges.append((part.unit_start, part.unit_end))
    for sep in separators:
        if unit.text[sep.unit_start : sep.unit_end] != sep.text:
            raise ValueError("Structural separator differs from canonical text")
        ranges.append((sep.unit_start, sep.unit_end))
    ranges.sort()
    cursor = 0
    for start, end in ranges:
        if start != cursor:
            raise ValueError("Missing, duplicate or conflicting canonical source parts")
        cursor = end
    if cursor != len(unit.text) or any(
        items[ref].text and ref not in represented for ref in refs
    ):
        raise ValueError("Incomplete grouped document source ownership")

    by_ref: dict[str, list[DocumentSourcePart]] = {ref: [] for ref in refs}
    for part in parts:
        by_ref[part.native_ref].append(part)
    expected_locators: list[DocumentLocator] = []
    offset = 0
    for index, ref in enumerate(refs):
        item = items[ref]
        if index:
            offset += 2  # The canonical item boundary is structural "\n\n".
        for part in by_ref[ref]:
            expected_locators.extend(
                _part_locators(
                    item,
                    part,
                    parsed.registration.source.source_key,
                    parsed.original_ref.sha256,
                )
            )
            offset = part.unit_end
        if "text" not in item.raw_item and item.unit_type not in (
            "table",
            "document_index",
        ):
            expected_locators.extend(
                _point_locators(item, offset, parsed.registration.source.source_key)
            )
    if unit.locators != tuple(expected_locators):
        raise ValueError(
            "Canonical document locator projection differs from original source"
        )


def document_overlap_owners(
    units: tuple[EvidenceUnit, ...], budget: _Budget
) -> dict[str, set[str]]:
    """Every owner of an overlapping original field interval is a dependency."""
    overlaps = {u.unit_id: {u.unit_id} for u in units}
    domains: dict[str, list[tuple[int, int, str]]] = {}
    for unit in units:
        for part in cast(
            tuple[Mapping[str, Any], ...], unit.attributes["source_parts"]
        ):
            domains.setdefault(part["source_domain"], []).append(
                (part["source_start"], part["source_end"], unit.unit_id)
            )
    for intervals in domains.values():
        active: list[tuple[int, str]] = []
        for start, end, uid in sorted(intervals):
            retained = []
            for stop, owner in active:
                budget.add(work=1)
                if stop > start and end > start:
                    retained.append((stop, owner))
                    overlaps[uid].add(owner)
                    overlaps[owner].add(uid)
            active = [*retained, (end, uid)]
    return overlaps


def validate_document_ownership(
    units: tuple[EvidenceUnit, ...], parsed: ExtractionArtifact
) -> None:
    """Pure normalization/restore boundary; never degrade to parent-only ownership."""
    items = {
        item.native_ref: cast(DocumentUnit, item)
        for item in normalize_native_document_units(parsed)
    }
    covered = set()
    for unit in units:
        if unit.attributes.get("document_representation") != "docling-source-parts@1":
            raise ValueError("Missing supported document source representation")
        _validate_source_parts(unit, parsed, items)
        covered.update(cast(tuple[str, ...], unit.attributes["source_refs"]))
    if covered != items.keys():
        raise ValueError("Missing original document source owners")
    overlaps = document_overlap_owners(units, _Budget())
    for unit in units:
        if unit.attributes.get("overlapping_owner_ids") != tuple(
            sorted(overlaps[unit.unit_id])
        ):
            raise ValueError("Missing or conflicting overlapping source owners")
