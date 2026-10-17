"""Pure mapping of retained Docling JSON; no SDK, storage or model construction."""

from collections.abc import Mapping
from typing import Any, cast

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.documents import (
    DocumentProvenance,
    DocumentUnit,
    MappedDocument,
    document_text_domain,
)
from tychora.contracts.evidence import DocumentLocator, Geometry
from tychora.contracts.identity import JsonValue

MAX_ITEMS = 100_000


def map_document(
    export: JsonValue, original: ArtifactRef, *, coordinates=None
) -> MappedDocument:
    from .text_coordinates import project_document

    document = cast(dict[str, Any], export)
    if (
        not isinstance(document, Mapping)
        or document.get("schema_name") != "DoclingDocument"
    ):
        raise ValueError("Expected complete DoclingDocument export")
    projections = project_document(document, coordinates)
    items: dict[str, dict[str, Any]] = {}
    for group in (
        "texts",
        "tables",
        "pictures",
        "groups",
        "key_value_items",
        "form_items",
    ):
        for item in document.get(group, []):
            ref = item["self_ref"]
            if ref in items or len(items) >= MAX_ITEMS:
                raise ValueError("Duplicate or excessive Docling native references")
            items[ref] = item
    for root in ("body", "furniture"):
        if root in document:
            item = document[root]
            if item["self_ref"] in items:
                raise ValueError("Duplicate Docling root reference")
            items[item["self_ref"]] = item
    pages = document.get("pages", {})
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(ref: str, parent: str | None, depth: int = 0) -> None:
        if depth > 128 or ref in visited or ref not in items:
            raise ValueError("Missing, duplicated or cyclic Docling hierarchy")
        visited.add(ref)
        item = items[ref]
        if item.get("parent", {}).get("$ref") != parent:
            raise ValueError("Docling parent/child hierarchy differs")
        if ref not in ("#/body", "#/furniture") and not ref.startswith("#/groups/"):
            ordered.append(item)
        for child in item.get("children", []):
            visit(child["$ref"], ref, depth + 1)

    for root in ("body", "furniture"):
        if root in document:
            visit(document[root]["self_ref"], None)
    if visited != items.keys():
        raise ValueError("Docling items are disconnected from document hierarchy")
    units = []
    emitted_refs: set[str] = set()
    headings: list[tuple[int, str]] = []
    diagnostics = []
    images = [f"#/pages/{page}" for page, value in pages.items() if value.get("image")]
    for item in ordered:
        ref, label = item["self_ref"], item.get("label", "unknown")
        if label not in {
            "text",
            "section_header",
            "title",
            "list_item",
            "caption",
            "table",
            "document_index",
            "picture",
            "code",
            "formula",
            "footnote",
            "page_header",
            "page_footer",
            "checkbox_selected",
            "checkbox_unselected",
            "paragraph",
            "reference",
        }:
            diagnostics.append(
                FrozenJsonMap(
                    {
                        "code": "UNSUPPORTED_NATIVE_ITEM",
                        "native_ref": ref,
                        "message": "Native object retained; specialized extraction semantics are not qualified.",
                    }
                )
            )
        cells: tuple[tuple[str, ...], ...] = ()
        text = item.get("text", "")
        if label in ("table", "document_index"):
            grid = item.get("data", {})
            nr, nc = grid.get("num_rows", 0), grid.get("num_cols", 0)
            if not nr or not nc or nr * nc > MAX_ITEMS:
                diagnostics.append(
                    FrozenJsonMap(
                        {
                            "code": "TABLE_STRUCTURE_UNAVAILABLE",
                            "native_ref": ref,
                            "message": "Table grid unavailable; retained native object requires review.",
                        }
                    )
                )
            else:
                rows = [[""] * nc for _ in range(nr)]
                for cell in grid.get("table_cells", []):
                    r, c = cell["start_row_offset_idx"], cell["start_col_offset_idx"]
                    if not (
                        0 <= r < cell["end_row_offset_idx"] <= nr
                        and 0 <= c < cell["end_col_offset_idx"] <= nc
                    ):
                        raise ValueError("Impossible table cell grid range")
                    rows[r][c] = cell["text"]
                cells = tuple(tuple(row) for row in rows)
                text = "\n".join("\t".join(row) for row in cells)
        level = item.get("level", 1) if label in ("section_header", "title") else 0
        if level:
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, text))
        provenance = []
        locators = []
        for prov_index, prov in enumerate(item.get("prov", [])):
            page = pages.get(str(prov["page_no"]))
            if page is None or page.get("page_no") != prov["page_no"]:
                raise ValueError("Docling provenance references unavailable page")
            width, height = page["size"]["width"], page["size"]["height"]
            bbox = prov["bbox"]
            if bbox["coord_origin"] == "BOTTOMLEFT":
                y0, y1, transform = (
                    height - bbox["t"],
                    height - bbox["b"],
                    "bottom_left_to_top_left",
                )
            elif bbox["coord_origin"] == "TOPLEFT":
                y0, y1, transform = bbox["t"], bbox["b"], "identity_top_left"
            else:
                raise ValueError("Unknown Docling coordinate origin")
            span = tuple(prov["charspan"])
            qualified = ref in projections
            domain, source_length, text_offset = (
                ("docling_assembly_step", 0, 0)
                if qualified
                else document_text_domain(FrozenJsonMap(item))
            )
            if (
                not qualified
                and domain != "docling_nontext_item"
                and not (0 <= span[0] <= span[1] <= source_length)
            ):
                raise ValueError("Impossible Docling text-item charspan")
            geometry = Geometry(
                domain="points", x0=bbox["l"], y0=y0, x1=bbox["r"], y1=y1
            )
            provenance.append(
                DocumentProvenance(
                    char_domain=domain,
                    charspan=span,
                    physical_page=prov["page_no"],
                    page_width=width,
                    page_height=height,
                    raw_bbox=FrozenJsonMap(bbox),
                    geometry=geometry,
                    transform=transform,
                )
            )
            start = (
                max(span[0], text_offset) - text_offset
                if domain != "docling_nontext_item"
                else 0
            )
            end = (
                min(span[1], text_offset + len(text)) - text_offset
                if domain != "docling_nontext_item"
                else len(text)
            )
            if qualified:
                start, end = projections[ref][prov_index]
            if domain != "docling_orig_text_item" or end > start:
                locators.append(
                    DocumentLocator(
                        artifact_ref=original.sha256,
                        native_ref=ref,
                        char_start=start,
                        char_end=end,
                        physical_page=prov["page_no"],
                        geometry=geometry,
                    )
                )
        if not item.get("prov"):
            locators.append(
                DocumentLocator(
                    artifact_ref=original.sha256,
                    native_ref=ref,
                    char_start=0,
                    char_end=len(text),
                )
            )
        parent = item.get("parent", {}).get("$ref")
        # Structural groups remain in raw JSON; canonical parent chooses closest
        # emitted ancestor, preserving complete raw parent references separately.
        while parent and parent not in emitted_refs:
            parent = items[parent].get("parent", {}).get("$ref")
        units.append(
            DocumentUnit(
                unit_type=label,
                native_ref=ref,
                parent_ref=parent,
                ordinal=len(units),
                text=text,
                heading_path=tuple(value for _, value in headings),
                heading_level=level,
                raw_item=FrozenJsonMap(item),
                provenance=tuple(provenance),
                locators=tuple(locators),
                cells=cells,
            )
        )
        emitted_refs.add(ref)
        if item.get("image"):
            images.append(ref)
    return MappedDocument(
        original_ref=original,
        document_json=FrozenJsonMap(document),
        units=tuple(units),
        image_items=tuple(images),
        diagnostics=tuple(diagnostics),
    )
