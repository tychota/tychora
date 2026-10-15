"""Native parser facts with explicit original coordinate domains."""

from typing import Literal, Self, cast

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
from .documents import (
    DocumentAsset,
    DocumentImageBinding,
    DocumentRegistration,
    DocumentUnit,
    OcrPageEvidence,
    OriginalImage,
    validate_document_assets,
    validate_document_units,
)
from .identity import JsonValue, canonical_bytes, digest, normalize_source_key


class ParsedUnit(Contract):
    unit_type: NonEmptyStr
    token_kind: NonEmptyStr
    native_ref: NonEmptyStr
    parent_ref: str | None = None
    text: str
    source_substring: str
    char_start: NonNegativeInt
    char_end: NonNegativeInt
    line_start: PositiveInt
    line_end: PositiveInt
    heading_path: tuple[str, ...] = ()
    heading_level: NonNegativeInt = 0
    heading_char_end: NonNegativeInt | None = None
    identifier: str | None = None
    cells: tuple[tuple[str, ...], ...] = ()

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.char_end - self.char_start != len(self.source_substring):
            raise ValueError("Parsed character span must match original substring")
        if self.line_end < self.line_start:
            raise ValueError("Parsed line span is reversed")
        return self


class NativeRegistration(Contract):
    capture: CaptureManifest
    workspace: WorkspaceRow
    collection: CollectionRow
    source: SourceRow
    revision: RevisionRow
    format: Literal["markdown", "text", "json"]
    encoding: Literal["utf-8"] = "utf-8"

    @model_validator(mode="after")
    def matching_registration(self) -> Self:
        capture, revision, source = self.capture, self.revision, self.source
        if capture.source.get("type") in ("folder", "repository") or (
            capture.entries
            and (
                len(capture.entries) != 1
                or capture.entries[0].source_key != source.source_key
                or physical_identity(capture.entries[0].artifact_ref)
                != physical_identity(capture.original_ref)
            )
        ):
            raise ValueError(
                "Native parser requires one explicitly registered document"
            )
        if (
            self.collection.workspace_id != self.workspace.workspace_id
            or source.collection_id != self.collection.collection_id
            or revision.source_id != source.source_id
            or source.source_key != normalize_source_key(source.source_key)
            or capture.source_key != source.source_key
            or capture.source.get("source_key") != source.source_key
            or capture.source.get("collection_id") != self.collection.collection_id
            or capture.source.get("kind") != revision.kind_id
            or capture.source.get("metadata") != revision.metadata_json
            or capture.original_sha != revision.original_sha
            or revision.metadata_sha != digest("metadata-v1", revision.metadata_json)
            or capture.source.get("format") not in (None, self.format)
            or capture.source.get("encoding") not in (None, self.encoding)
        ):
            raise ValueError(
                "Native registration differs from captured source identity"
            )
        return self


class Diagnostic(Contract):
    code: NonEmptyStr
    native_ref: NonEmptyStr
    message: NonEmptyStr


class ParsedRecord(Contract):
    native_ref: NonEmptyStr
    value: FrozenJsonMap


class ExtractionArtifact(Contract):
    registration: NativeRegistration | DocumentRegistration
    original_ref: ArtifactRef
    raw_document_ref: ArtifactRef
    source_text: str | None
    parser_fingerprint: Sha256
    normalizer_fingerprint: Sha256
    registry_digest: Sha256
    units: tuple[ParsedUnit | DocumentUnit, ...]
    blocks: tuple[ParsedUnit, ...] = ()
    # Native documents have no generated assets. Rich-document assets retain
    # actual pixels; relationship assertions remain their owning task's work.
    assets: tuple[DocumentAsset, ...] = ()
    document_json: FrozenJsonMap | None = None
    docling_json_ref: ArtifactRef | None = None
    original_image: OriginalImage | None = None
    assertions: tuple[()] = ()
    records: tuple[ParsedRecord, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    pdf_processing: FrozenJsonMap | None = None
    image_representation: Literal["scan-external-images@1"] | None = None
    image_bindings: tuple[DocumentImageBinding, ...] = ()
    ocr_pages: tuple[OcrPageEvidence, ...] = ()
    ocr_evidence_ref: ArtifactRef | None = None
    image_bindings_ref: ArtifactRef | None = None
    ocr_lineage: tuple[FrozenJsonMap, ...] | None = None

    @model_serializer(mode="wrap")
    def legacy_pdf_fields(self, handler):
        data = handler(self)
        if self.ocr_lineage is None:
            data.pop("ocr_lineage", None)
        if self.pdf_processing is None:
            for key in (
                "pdf_processing",
                "image_representation",
                "image_bindings",
                "ocr_pages",
                "ocr_evidence_ref",
                "image_bindings_ref",
            ):
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def original_facts(self) -> Self:
        import re
        from bisect import bisect_right
        from hashlib import sha256

        from .artifacts import physical_identity

        if isinstance(self.registration, DocumentRegistration):
            if (
                self.source_text is not None
                or self.blocks
                or self.records
                or self.document_json is None
                or self.docling_json_ref is None
                or physical_identity(self.original_ref)
                != physical_identity(self.registration.capture.original_ref)
                or self.original_image is not None
                and physical_identity(self.original_image.artifact_ref)
                != physical_identity(self.original_ref)
                or any(not isinstance(unit, DocumentUnit) for unit in self.units)
                or (self.registration.format in ("png", "jpeg"))
                != (self.original_image is not None)
            ):
                raise ValueError("Invalid binary document extraction domains")
            raw = canonical_bytes(self.document_json)
            if (
                sha256(raw).hexdigest() != self.docling_json_ref.sha256
                or len(raw) != self.docling_json_ref.size_bytes
            ):
                raise ValueError("Docling JSON differs from retained artifact")
            document_units = cast(tuple[DocumentUnit, ...], self.units)
            validate_document_units(self.document_json, document_units)
            validate_document_assets(
                self.document_json,
                document_units,
                self.assets,
                self.original_ref,
                self.registration.source.source_key,
                self.original_image,
                self.image_representation,
                self.image_bindings,
            )
            if self.pdf_processing is not None:
                from tychora.adapters.docling.ocr_evidence import validate_ocr_origins

                validate_ocr_origins(self)
            elif (
                self.ocr_pages
                or self.ocr_lineage is not None
                or self.image_bindings
                or self.image_representation
                or any(u.text_origin != "source" for u in document_units)
            ):
                raise ValueError("Unexpected explicit PDF evidence")
            if any(
                loc.artifact_ref != self.original_ref.sha256
                or loc.native_ref != unit.native_ref
                for unit in document_units
                for loc in unit.locators
            ):
                raise ValueError(
                    "Document locators differ from original/native binding"
                )
            if self.registration.format == "docx" and (
                self.document_json.get("pages")
                or any(unit.provenance for unit in document_units)
            ):
                raise ValueError(
                    "DOCX physical pages require a separately qualified renderer"
                )
            encoded = extraction_document(
                self.model_dump(
                    mode="json", exclude={"raw_document_ref", "document_json"}
                )
            )
            if (
                sha256(encoded).hexdigest() != self.raw_document_ref.sha256
                or len(encoded) != self.raw_document_ref.size_bytes
            ):
                raise ValueError(
                    "Retained extraction artifact differs from parser facts"
                )
            return self
        if (
            self.source_text is None
            or self.document_json is not None
            or self.docling_json_ref is not None
            or self.assets
            or self.original_image is not None
        ):
            raise ValueError("Native extraction has incompatible document fields")
        if any(not isinstance(unit, ParsedUnit) for unit in self.units):
            raise ValueError("Native extraction requires native units")
        if (
            physical_identity(self.original_ref)
            != physical_identity(self.registration.capture.original_ref)
            or sha256(self.source_text.encode("utf-8")).hexdigest()
            != self.original_ref.sha256
        ):
            raise ValueError("Extraction text differs from captured original")
        offsets = [
            0,
            *(match.end() for match in re.finditer(r"\r\n|\r|\n", self.source_text)),
        ]
        for group in (cast(tuple[ParsedUnit, ...], self.units), self.blocks):
            refs = {unit.native_ref: unit for unit in group}
            if len(refs) != len(group):
                raise ValueError("Duplicate parser native reference")
            for unit in group:
                if (
                    unit.char_end > len(self.source_text)
                    or unit.char_end < unit.char_start
                    or self.source_text[unit.char_start : unit.char_end]
                    != unit.source_substring
                    or unit.line_start != bisect_right(offsets, unit.char_start)
                    or unit.line_end
                    != bisect_right(offsets, max(unit.char_start, unit.char_end - 1))
                    or unit.heading_char_end is not None
                    and not unit.char_start <= unit.heading_char_end <= unit.char_end
                ):
                    raise ValueError("Impossible native source coordinates")
                visited = {unit.native_ref}
                parent = unit.parent_ref
                while parent is not None:
                    if parent in visited or parent not in refs:
                        raise ValueError("Missing or cyclic parser parent")
                    visited.add(parent)
                    parent = refs[parent].parent_ref
        encoded = extraction_document(
            self.model_dump(
                mode="json",
                exclude={
                    "raw_document_ref",
                    "document_json",
                    "docling_json_ref",
                    "original_image",
                },
            )
        )
        if (
            sha256(encoded).hexdigest() != self.raw_document_ref.sha256
            or len(encoded) != self.raw_document_ref.size_bytes
        ):
            raise ValueError("Retained extraction artifact differs from parser facts")
        record_refs = [record.native_ref for record in self.records]
        if len(record_refs) != len(set(record_refs)) or set(record_refs) - {
            u.native_ref for u in self.units
        }:
            raise ValueError("Records require unique existing parser units")
        return self


def extraction_document(data: JsonValue) -> bytes:
    """Exact self-contained extraction-v1 framing; source refs are provenance."""
    from collections.abc import Mapping

    required = []
    if isinstance(data, Mapping) and data.get("docling_json_ref") is not None:
        required = [
            data["docling_json_ref"],
            *[asset["artifact_ref"] for asset in data.get("assets", [])],
            *[
                data[key]
                for key in ("ocr_evidence_ref", "image_bindings_ref")
                if data.get(key) is not None
            ],
        ]
        data = {
            **data,
            "units": [
                {key: value for key, value in unit.items() if key != "raw_item"}
                for unit in data.get("units", [])
            ],
        }
    return canonical_bytes(
        {
            "type": "extraction",
            "schema_version": "1",
            "required_artifacts": required,
            "data": data,
        }
    )
