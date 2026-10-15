"""Rich-document facts in SDK item and physical-page coordinate domains."""

from typing import Literal, Self

from pydantic import model_serializer, model_validator

from .acquisition import CaptureManifest
from .artifacts import ArtifactRef, physical_identity
from .base import (
    Contract,
    FrozenJsonMap,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
)
from .corpus import CollectionRow, RevisionRow, SourceRow, WorkspaceRow
from .evidence import DocumentLocator, Geometry, ImageLocator
from .identity import digest, normalize_source_key


def image_pixel_identity(
    pixels: bytes, width: int, height: int, color_mode: str, transform: str
) -> str:
    """Versioned decoded-pixel identity including shape, color and transform."""
    from hashlib import sha256

    return digest(
        "image-pixels-v1",
        width,
        height,
        color_mode,
        transform,
        sha256(pixels).hexdigest(),
    )


class DocumentRegistration(Contract):
    capture: CaptureManifest
    workspace: WorkspaceRow
    collection: CollectionRow
    source: SourceRow
    revision: RevisionRow
    format: Literal["pdf", "docx", "png", "jpeg"]

    @model_validator(mode="after")
    def binding(self) -> Self:
        c, r, s = self.capture, self.revision, self.source
        if (
            c.source.get("type") in ("folder", "repository")
            or (
                c.entries
                and (
                    len(c.entries) != 1
                    or c.entries[0].source_key != s.source_key
                    or physical_identity(c.entries[0].artifact_ref)
                    != physical_identity(c.original_ref)
                )
            )
            or self.collection.workspace_id != self.workspace.workspace_id
            or s.collection_id != self.collection.collection_id
            or r.source_id != s.source_id
            or s.source_key != normalize_source_key(s.source_key)
            or c.source_key != s.source_key
            or c.source.get("source_key") != s.source_key
            or c.source.get("collection_id") != self.collection.collection_id
            or c.source.get("kind") != r.kind_id
            or c.source.get("metadata") != r.metadata_json
            or c.original_sha != r.original_sha
            or r.metadata_sha != digest("metadata-v1", r.metadata_json)
            or c.source.get("format") not in (None, self.format)
            or self.format in ("png", "jpeg")
            and c.source.get("media_type")
            not in (None, "image/png" if self.format == "png" else "image/jpeg")
        ):
            raise ValueError(
                "Document registration differs from captured source identity"
            )
        return self


class DocumentProvenance(Contract):
    char_domain: Literal[
        "docling_text_item",
        "docling_orig_text_item",
        "docling_nontext_item",
        "docling_assembly_step",
    ]
    charspan: tuple[NonNegativeInt, NonNegativeInt]
    physical_page: PositiveInt
    page_width: float
    page_height: float
    raw_bbox: FrozenJsonMap
    geometry: Geometry
    transform: Literal["identity_top_left", "bottom_left_to_top_left"]

    @model_validator(mode="after")
    def bounds(self) -> Self:
        import math

        if (
            not math.isfinite(self.page_width)
            or not math.isfinite(self.page_height)
            or min(self.page_width, self.page_height) <= 0
            or self.geometry.x1 > self.page_width
            or self.geometry.y1 > self.page_height
            or self.charspan[1] < self.charspan[0]
        ):
            raise ValueError("Impossible Docling page or item coordinates")
        return self


class DocumentUnit(Contract):
    unit_type: NonEmptyStr
    native_ref: NonEmptyStr
    parent_ref: str | None
    ordinal: NonNegativeInt
    text: str
    heading_path: tuple[str, ...] = ()
    heading_level: NonNegativeInt = 0
    raw_item: FrozenJsonMap
    provenance: tuple[DocumentProvenance, ...] = ()
    locators: tuple[DocumentLocator, ...]
    cells: tuple[tuple[str, ...], ...] = ()

    text_origin: Literal["source", "ocr"] = "source"

    @model_serializer(mode="wrap")
    def legacy_origin(self, handler):
        data = handler(self)
        if self.text_origin == "source":
            data.pop("text_origin", None)
        return data


class DocumentImageBinding(Contract):
    native_ref: NonEmptyStr
    artifact_ref: ArtifactRef


class OcrPageEvidence(Contract):
    physical_page: PositiveInt
    model_input_image: ArtifactRef
    model_input_pixel_sha256: Sha256
    original_pixel_sha256: Sha256
    original_sha256: Sha256
    width: PositiveInt
    height: PositiveInt
    region_pixel_transform: NonEmptyStr
    renderer: FrozenJsonMap
    channel_interpretation: Literal["pdfium-rgb-array-rapidocr-ndarray-no-channel-swap"]
    policy_sha256: Sha256
    model_identity: Sha256
    runtime_inventory: FrozenJsonMap
    cells: tuple[FrozenJsonMap, ...]
    transcript: str
    transcript_order: Literal["rapidocr-reader-order@1"] = "rapidocr-reader-order@1"
    detected_boxes: NonNegativeInt
    diagnostic: Literal["transcribed", "no-text-detected"]

    @model_validator(mode="after")
    def checked(self):
        if (
            self.width * self.height > 40000000
            or self.model_input_image.media_type != "image/png"
            or self.region_pixel_transform
            != f"exif-transpose-rgb-v1:crop:0,0,{self.width},{self.height}"
            or any(cell.get("from_ocr") is not True for cell in self.cells)
            or (self.diagnostic == "transcribed") != bool(self.transcript.strip())
            or (self.diagnostic == "no-text-detected") != (self.detected_boxes == 0)
        ):
            raise ValueError("OCR evidence coverage or input binding differs")
        return self


class DocumentSourcePart(Contract):
    """An original SDK field interval represented in a canonical unit."""

    native_ref: NonEmptyStr
    field: Literal["text", "marker", "table_cell"]
    cell_index: NonNegativeInt | None = None
    source_start: NonNegativeInt
    source_end: NonNegativeInt
    unit_start: NonNegativeInt
    unit_end: NonNegativeInt
    source_domain: NonEmptyStr
    provenance: tuple[DocumentProvenance, ...] = ()

    @model_validator(mode="after")
    def spans(self) -> Self:
        if (
            self.source_end < self.source_start
            or self.unit_end < self.unit_start
            or self.source_end - self.source_start != self.unit_end - self.unit_start
            or (self.field == "table_cell") != (self.cell_index is not None)
        ):
            raise ValueError("Invalid document source-part spans or cell domain")
        return self


class DocumentSeparator(Contract):
    """Canonical punctuation supplied by source structure, with no original span."""

    unit_start: NonNegativeInt
    unit_end: NonNegativeInt
    text: Literal["\n\n", "\n", "\t", " "]

    @model_validator(mode="after")
    def span(self) -> Self:
        if self.unit_end - self.unit_start != len(self.text):
            raise ValueError("Invalid structural separator span")
        return self


class DocumentAsset(Contract):
    native_ref: NonEmptyStr
    asset_type: Literal["page", "picture", "table"]
    artifact_ref: ArtifactRef
    locator: ImageLocator
    width: PositiveInt
    height: PositiveInt
    caption: str = ""
    source_units: tuple[str, ...] = ()


class OriginalImage(Contract):
    """Pixel-grounded owner for a standalone encoded image original."""

    native_ref: NonEmptyStr
    artifact_ref: ArtifactRef
    encoded_sha256: Sha256
    pixel_sha256: Sha256
    width: PositiveInt
    height: PositiveInt
    color_mode: Literal["RGB"]
    pixel_transform: Literal["exif-transpose-rgb-v1"]
    locator: ImageLocator

    @model_validator(mode="after")
    def exact_source(self) -> Self:
        geometry = self.locator.geometry
        if (
            self.encoded_sha256 != self.artifact_ref.sha256
            or self.artifact_ref.storage_key != self.encoded_sha256
            or self.locator.artifact_ref != self.encoded_sha256
            or self.locator.physical_page is not None
            or geometry is None
            or geometry.domain != "pixels"
            or (geometry.x0, geometry.y0, geometry.x1, geometry.y1)
            != (0, 0, self.width, self.height)
        ):
            raise ValueError("Standalone image source differs from its pixel owner")
        return self


class MappedDocument(Contract):
    original_ref: ArtifactRef
    document_json: FrozenJsonMap
    units: tuple[DocumentUnit, ...]
    # Image payloads remain in the complete SDK JSON; parser extracts them on
    # the canonical owner lane and replaces them with verified CAS references.
    image_items: tuple[str, ...] = ()
    diagnostics: tuple[FrozenJsonMap, ...] = ()


def document_text_domain(
    item: FrozenJsonMap,
) -> tuple[
    Literal["docling_text_item", "docling_orig_text_item", "docling_nontext_item"],
    int,
    int,
]:
    """Prove a list's original marker prefix before projecting its text suffix."""
    text = item.get("text")
    if not isinstance(text, str):
        return "docling_nontext_item", 0, 0
    original, marker = item.get("orig"), item.get("marker")
    if item.get("label") == "list_item" and marker and original != text:
        if not isinstance(original, str) or not isinstance(marker, str) or not text:
            raise ValueError("Unsupported Docling list original/text relationship")
        offset = len(original) - len(text)
        whitespace = original[len(marker) : offset]
        if (
            not original.startswith(marker)
            or not original.endswith(text)
            or not whitespace
            or not whitespace.isspace()
        ):
            raise ValueError("Unsupported Docling list original/text relationship")
        return "docling_orig_text_item", len(original), offset
    return "docling_text_item", len(text), 0


def validate_document_units(
    document: FrozenJsonMap, units: tuple[DocumentUnit, ...], *, coordinates=None
) -> None:
    """Bind every emitted fact to its retained native object and physical page."""
    from collections.abc import Mapping
    from typing import Any, cast

    from tychora.adapters.docling.text_coordinates import project_document

    raw = cast(Mapping[str, Any], document)
    projections = project_document(raw, coordinates)
    objects = {
        item["self_ref"]: item
        for name in (
            "texts",
            "tables",
            "pictures",
            "groups",
            "key_value_items",
            "form_items",
        )
        for item in raw.get(name, ())
    }
    objects.update(
        {
            raw[name]["self_ref"]: raw[name]
            for name in ("body", "furniture")
            if name in raw
        }
    )
    expected_count = sum(
        len(raw.get(name, ()))
        for name in (
            "texts",
            "tables",
            "pictures",
            "groups",
            "key_value_items",
            "form_items",
        )
    ) + sum(name in raw for name in ("body", "furniture"))
    if len(objects) != expected_count:
        raise ValueError("Duplicate retained Docling object reference")
    refs = {u.native_ref for u in units}
    expected_refs = {
        ref
        for ref in objects
        if ref not in ("#/body", "#/furniture") and not ref.startswith("#/groups/")
    }
    if refs != expected_refs:
        raise ValueError("Document source item coverage differs from retained JSON")
    if len(refs) != len(units):
        raise ValueError("Duplicate document unit reference")
    order = []
    seen = set()

    def visit(ref, depth=0):
        if depth > 128 or ref in seen or ref not in objects:
            raise ValueError("Invalid retained document hierarchy")
        seen.add(ref)
        if ref in refs:
            order.append(ref)
        for child in objects[ref].get("children", ()):
            visit(child["$ref"], depth + 1)

    for name in ("body", "furniture"):
        if name in raw:
            visit(raw[name]["self_ref"])
    if tuple(order) != tuple(u.native_ref for u in units):
        raise ValueError("Document unit reading order differs from retained JSON")
    headings = []
    for ordinal, unit in enumerate(units):
        item = objects[unit.native_ref]
        if (
            item != unit.raw_item
            or unit.ordinal != ordinal
            or item["label"] != unit.unit_type
        ):
            raise ValueError("Document unit differs from retained native item")
        expected_text = item.get("text", "")
        grid = item.get("data", {})
        if (
            unit.unit_type in ("table", "document_index")
            and grid.get("num_rows", 0)
            and grid.get("num_cols", 0)
            and grid["num_rows"] * grid["num_cols"] <= 100_000
            and not unit.cells
        ):
            raise ValueError("Retained table grid cannot be omitted")
        if unit.cells:
            grid = item["data"]
            rows = [[""] * grid["num_cols"] for _ in range(grid["num_rows"])]
            for cell in grid["table_cells"]:
                rows[cell["start_row_offset_idx"]][cell["start_col_offset_idx"]] = cell[
                    "text"
                ]
            if unit.cells != tuple(tuple(row) for row in rows):
                raise ValueError("Table cells differ from retained grid")
            expected_text = "\n".join("\t".join(row) for row in rows)
        level = (
            item.get("level", 1) if item["label"] in ("section_header", "title") else 0
        )
        if level:
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, expected_text))
        if unit.heading_level != level or unit.heading_path != tuple(
            text for _, text in headings
        ):
            raise ValueError("Document heading context differs from retained JSON")
        if expected_text != unit.text:
            raise ValueError("Document text differs from retained native item")
        parent = item.get("parent", {}).get("$ref")
        ancestors = set()
        while parent is not None and parent not in refs:
            if parent in ancestors or parent not in objects:
                raise ValueError("Invalid retained document parent")
            ancestors.add(parent)
            parent = objects[parent].get("parent", {}).get("$ref")
        if parent != unit.parent_ref:
            raise ValueError("Document parent differs from retained hierarchy")
        native_prov = item.get("prov", ())
        if len(native_prov) != len(unit.provenance):
            raise ValueError("Document provenance coverage differs")
        expected_locators = []
        for prov_index, (native, prov) in enumerate(zip(native_prov, unit.provenance)):
            page = raw["pages"][str(native["page_no"])]
            if page.get("page_no") != native["page_no"]:
                raise ValueError(
                    "Retained Docling page identity differs from provenance"
                )
            box = native["bbox"]
            if box["coord_origin"] not in ("TOPLEFT", "BOTTOMLEFT"):
                raise ValueError("Unknown retained Docling coordinate origin")
            width, height = page["size"]["width"], page["size"]["height"]
            bottom = box["coord_origin"] == "BOTTOMLEFT"
            expected = Geometry(
                domain="points",
                x0=box["l"],
                x1=box["r"],
                y0=height - box["t"] if bottom else box["t"],
                y1=height - box["b"] if bottom else box["b"],
            )
            qualified = unit.native_ref in projections
            domain, source_length, text_offset = (
                ("docling_assembly_step", 0, 0)
                if qualified
                else document_text_domain(FrozenJsonMap(item))
            )
            span = tuple(native["charspan"])
            if (
                not qualified
                and domain != "docling_nontext_item"
                and not 0 <= span[0] <= span[1] <= source_length
            ):
                raise ValueError("Impossible retained Docling item span")
            if (
                prov.physical_page != native["page_no"]
                or prov.page_width != width
                or prov.page_height != height
                or prov.raw_bbox != box
                or prov.charspan != tuple(native["charspan"])
                or prov.char_domain != domain
                or prov.transform
                != ("bottom_left_to_top_left" if bottom else "identity_top_left")
                or prov.geometry != expected
            ):
                raise ValueError("Document coordinates differ from retained provenance")
            start = (
                max(span[0], text_offset) - text_offset
                if domain != "docling_nontext_item"
                else 0
            )
            end = (
                min(span[1], text_offset + len(unit.text)) - text_offset
                if domain != "docling_nontext_item"
                else len(unit.text)
            )
            if qualified:
                start, end = projections[unit.native_ref][prov_index]
            if domain != "docling_orig_text_item" or end > start:
                expected_locators.append((start, end, prov.physical_page, expected))
        if (
            native_prov
            and [
                (loc.char_start, loc.char_end, loc.physical_page, loc.geometry)
                for loc in unit.locators
            ]
            != expected_locators
        ):
            raise ValueError("Document coordinates differ from retained provenance")
        if not native_prov and (
            len(unit.locators) != 1
            or unit.locators[0].physical_page is not None
            or unit.locators[0].geometry is not None
            or unit.locators[0].char_start != 0
            or unit.locators[0].char_end != len(unit.text)
        ):
            raise ValueError("Unrendered document acquired invented page coordinates")


def validate_document_assets(
    document: FrozenJsonMap,
    units: tuple[DocumentUnit, ...],
    assets: tuple[DocumentAsset, ...],
    original: ArtifactRef,
    source_key: str,
    original_image: OriginalImage | None = None,
    image_representation: str | None = None,
    image_bindings: tuple[DocumentImageBinding, ...] = (),
) -> None:
    import base64
    from hashlib import sha256
    from io import BytesIO
    from typing import Any, cast

    raw = cast(dict[str, Any], document)
    objects = {
        f"#/pages/{key}": value
        for key, value in raw.get("pages", {}).items()
        if value.get("image")
    }
    objects.update(
        {
            item["self_ref"]: item
            for name in ("pictures", "tables", "texts")
            for item in raw.get(name, ())
            if item.get("image")
        }
    )
    if len(assets) != len(objects) or {a.native_ref for a in assets} != objects.keys():
        raise ValueError("Document asset coverage differs from retained images")
    unit_refs = {u.native_ref: u for u in units}
    for asset in assets:
        node = objects[asset.native_ref]
        image = node["image"]
        ref = asset.artifact_ref
        external = image["uri"].startswith("urn:tychora:artifact:sha256:")
        if external:
            bindings = {
                binding.native_ref: binding.artifact_ref for binding in image_bindings
            }
            if (
                image_representation != "scan-external-images@1"
                or len(bindings) != len(image_bindings)
                or bindings.get(asset.native_ref) != ref
                or image["uri"] != "urn:tychora:artifact:sha256:" + ref.sha256
                or image.get("mimetype") != ref.media_type
            ):
                raise ValueError("Undeclared external document image")
            payload = b""
            header = "data:" + ref.media_type + ";base64"
        else:
            header, encoded = image["uri"].split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise ValueError("Document images require embedded payloads")
            payload = base64.b64decode(encoded, validate=True)
        unit = unit_refs.get(asset.native_ref)
        page = unit.locators[0].physical_page if unit else node["page_no"]
        geometry = (
            unit.locators[0].geometry
            if unit
            else Geometry(
                domain="points",
                x0=0,
                y0=0,
                x1=node["size"]["width"],
                y1=node["size"]["height"],
            )
        )
        if original_image is not None and asset.native_ref.startswith("#/pages/"):
            page = None
            geometry = original_image.locator.geometry
        captions = tuple(r["$ref"] for r in node.get("captions", ()))
        caption = "\n".join(u.text for u in units if u.native_ref in captions)
        pixel_owner_matches = True
        if original_image is not None and asset.native_ref.startswith("#/pages/"):
            from PIL import Image, ImageOps

            with Image.open(BytesIO(payload)) as encoded_image:
                if encoded_image.width * encoded_image.height > 40_000_000:
                    raise ValueError("Retained image pixels exceed capacity")
                decoded = ImageOps.exif_transpose(encoded_image).convert("RGB")
                decoded.load()
            pixel_owner_matches = (
                original_image.artifact_ref == original
                and original_image.encoded_sha256 == original.sha256
                and original_image.locator.artifact_ref == original.sha256
                and original_image.locator.native_ref == source_key
                and decoded.size == (original_image.width, original_image.height)
                and original_image.color_mode == "RGB"
                and image_pixel_identity(
                    decoded.tobytes(),
                    decoded.width,
                    decoded.height,
                    "RGB",
                    original_image.pixel_transform,
                )
                == original_image.pixel_sha256
            )
        if (
            not external
            and (
                sha256(payload).hexdigest() != ref.sha256
                or len(payload) != ref.size_bytes
            )
            or ref.storage_key != ref.sha256
            or ref.media_type != header[5:].split(";", 1)[0]
            or asset.width != image["size"]["width"]
            or asset.height != image["size"]["height"]
            or asset.width * asset.height > 40_000_000
            or asset.locator.physical_page != page
            or asset.locator.geometry != geometry
            or asset.locator.artifact_ref != original.sha256
            or asset.locator.native_ref != source_key
            or asset.caption != caption
            or asset.source_units
            != (
                ((original_image.native_ref,) if original_image is not None else ())
                + ((unit.native_ref,) if unit else ())
                + captions
            )
            or asset.asset_type
            != (
                "page"
                if asset.native_ref.startswith("#/pages/")
                else "table"
                if asset.native_ref.startswith("#/tables/")
                else "picture"
            )
            or not pixel_owner_matches
        ):
            raise ValueError(
                "Document asset or pixel facts differ from retained image and source geometry"
            )
