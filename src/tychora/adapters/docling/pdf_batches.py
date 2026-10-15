"""Bounded Docling export merges preserving original physical page numbers."""

import json
from collections.abc import Mapping
from typing import Any, NoReturn

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.documents import validate_document_units
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes

DOMAINS = ("texts", "tables", "pictures", "groups", "key_value_items", "form_items")
MAX_METADATA_BYTES = 268435456


def merge_pdf_page_exports(exports: tuple[FrozenJsonMap, ...]) -> FrozenJsonMap:
    from .mapping import map_document

    def fail() -> NoReturn:
        raise TychoraError(
            "INTEGRITY_ERROR", "Invalid PDF batch references or coverage"
        )

    if not exports:
        fail()
    if sum(len(canonical_bytes(e)) for e in exports) > MAX_METADATA_BYTES:
        raise TychoraError("LIMIT_EXCEEDED", "PDF metadata exceeds capacity")
    merged = None
    dummy = ArtifactRef(
        sha256="0" * 64,
        storage_key="0" * 64,
        size_bytes=0,
        media_type="application/pdf",
    )
    try:
        for export in exports:
            raw = json.loads(canonical_bytes(export))
            mapped = map_document(raw, dummy)
            validate_document_units(FrozenJsonMap(raw), mapped.units)
            objects = {
                item["self_ref"]: item
                for domain in DOMAINS
                for item in raw.get(domain, [])
            }
            objects.update(
                {
                    raw[root]["self_ref"]: raw[root]
                    for root in ("body", "furniture")
                    if root in raw
                }
            )
            visited = set()

            def visit(ref, visited=visited, objects=objects):
                if ref in visited or ref not in objects:
                    fail()
                visited.add(ref)
                for child in objects[ref].get("children", []):
                    if objects[child["$ref"]].get("parent", {}).get("$ref") != ref:
                        fail()
                    visit(child["$ref"])

            for root in ("body", "furniture"):
                if root in raw:
                    visit("#/" + root)
            if visited != objects.keys():
                fail()
            offsets = {
                domain: len(merged.get(domain, [])) if merged else 0
                for domain in DOMAINS
            }

            def remap(ref, raw=raw, offsets=offsets):
                if ref in ("#/body", "#/furniture"):
                    return ref
                bits = ref.split("/")
                if (
                    len(bits) != 3
                    or bits[0] != "#"
                    or bits[1] not in DOMAINS
                    or not bits[2].isdigit()
                ):
                    fail()
                index = int(bits[2])
                if (
                    index >= len(raw.get(bits[1], []))
                    or raw[bits[1]][index]["self_ref"] != ref
                ):
                    fail()
                return f"#/{bits[1]}/{index + offsets[bits[1]]}"

            def walk(value):
                if isinstance(value, Mapping):
                    return {
                        key: remap(item) if key in ("self_ref", "$ref") else walk(item)
                        for key, item in value.items()
                    }
                if isinstance(value, list):
                    return [walk(item) for item in value]
                return value

            adjusted = walk(raw)
            if merged is None:
                merged = adjusted
                continue
            special = {*DOMAINS, "pages", "body", "furniture"}
            if {k: v for k, v in merged.items() if k not in special} != {
                k: v for k, v in adjusted.items() if k not in special
            }:
                fail()
            if set(merged.get("pages", {})) & set(adjusted.get("pages", {})):
                fail()
            merged.setdefault("pages", {}).update(adjusted.get("pages", {}))
            for domain in DOMAINS:
                if domain in adjusted:
                    merged.setdefault(domain, []).extend(adjusted[domain])
            for root in ("body", "furniture"):
                if root not in adjusted or root not in merged:
                    fail()
                if {k: v for k, v in merged[root].items() if k != "children"} != {
                    k: v for k, v in adjusted[root].items() if k != "children"
                }:
                    fail()
                merged[root].setdefault("children", []).extend(
                    adjusted[root].get("children", [])
                )
        result = FrozenJsonMap(merged)
        mapped = map_document(result, dummy)
        validate_document_units(result, mapped.units)
        return result
    except (KeyError, TypeError, ValueError, RecursionError, IndexError):
        fail()


def validate_conversion(converted, expected_pages: tuple[int, ...]) -> None:
    """SUCCESS alone does not establish page coverage in Docling."""
    from docling.datamodel.base_models import ConversionStatus

    actual = tuple(page.page_no for page in converted.pages)
    exported = tuple(sorted(converted.document.pages))
    if (
        converted.status != ConversionStatus.SUCCESS
        or converted.errors
        or converted.has_timeout_errors()
        or actual != expected_pages
        or exported != expected_pages
    ):
        raise TychoraError(
            "INDEX_INCOMPLETE", "PDF processing did not cover every selected page"
        )


def run_pdf_batches(call, paths, report):
    """One child, cached mode-specific pipelines, bounded acknowledged exports."""
    import base64
    import os
    from math import ceil
    from pathlib import Path

    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        ORT_DISABLE_TELEMETRY="1",
    )
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        LayoutObjectDetectionOptions,
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling_core.types.doc import ImageRef

    from tychora.adapters.vision.local import _verify_models
    from tychora.application.owner import StagedOutput
    from tychora.application.runtime import ParserOutputBatch

    from .ocr_evidence import CapturingReader
    from .parser import _check_pdf_image_capacity, _image_decode_guard

    policy = call.policy
    original = paths[0].with_name("input.pdf")
    paths[0].rename(original)
    _check_pdf_image_capacity(original)
    captures = {}
    from .ocr_lineage import merge_lineage
    from .ocr_lineage_capture import (
        LineageCapture,
        LineageTableStage,
        instrument_reading_order,
    )

    lineage = LineageCapture(captures)

    class OcrStage:
        def __init__(self, model):
            self.model = model
            self.reader = CapturingReader(model.reader)
            model.reader = self.reader

        def __call__(self, result, pages):
            for page in pages:
                if page.page_no in captures:
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Repeated OCR page invocation"
                    )
                # Capture before backend release, with exactly one full-page reader call.
                self.reader.record = {}
                before = self.reader.invocations
                yielded = list(self.model(result, (page,)))
                if (
                    len(yielded) != 1
                    or not self.reader.record
                    or self.reader.invocations - before != 1
                ):
                    raise TychoraError(
                        "INDEX_INCOMPLETE", "Missing full-page OCR invocation"
                    )
                record = self.reader.record
                if any(not cell.from_ocr for cell in page.cells):
                    raise TychoraError(
                        "INTEGRITY_ERROR", "Full-page OCR retained native cells"
                    )
                record["cells"] = [
                    {
                        **({"index": cell.index} if policy.origin_proof else {}),
                        "text": cell.text,
                        "from_ocr": cell.from_ocr,
                        "confidence": cell.confidence,
                        "rect": cell.rect.model_dump(mode="json"),
                    }
                    for cell in page.cells
                ]
                captures[page.page_no] = record
                yield page

    class EvidencePipeline(StandardPdfPipeline):
        def _init_models(self):
            super()._init_models()
            if policy.origin_proof:
                self.table_model = LineageTableStage(self.table_model, lineage)
                self.reading_order_model = instrument_reading_order(
                    self.reading_order_model, lineage
                )

        def _make_ocr_model(self, art_path):
            return OcrStage(super()._make_ocr_model(art_path))

    layout = LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    layout.model_spec.revision = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
    common: dict[str, Any] = {
        "artifacts_path": Path(call.model_path),
        "do_table_structure": True,
        "table_structure_options": TableStructureOptions(),
        "layout_options": layout,
        "generate_page_images": True,
        "generate_picture_images": True,
        "generate_parsed_pages": True,
        "images_scale": 1.0,
        "enable_remote_services": False,
        "allow_external_plugins": False,
        "document_timeout": 120,
        "ocr_batch_size": 1,
        "layout_batch_size": 1,
        "table_batch_size": 1,
        "queue_max_size": 2,
        "accelerator_options": AcceleratorOptions(
            device=AcceleratorDevice.CPU, num_threads=2
        ),
    }
    native = DocumentConverter(
        allowed_formats=[InputFormat.PDF],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=PdfPipelineOptions(do_ocr=False, **common),
                backend_options=ThreadedDoclingParseBackendOptions(render_scale=1.0),
            )
        },
    )
    scan = None
    if policy.ocr_pages:
        det, rec, cls = _verify_models(Path(call.model_path) / "rapidocr/3.9.2")
        scan = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    backend=PyPdfiumDocumentBackend,
                    pipeline_cls=EvidencePipeline,
                    pipeline_options=PdfPipelineOptions(
                        do_ocr=True,
                        ocr_options=RapidOcrOptions(
                            backend="onnxruntime",
                            mode=OcrMode.FULL_PAGE,
                            scale=3.0,
                            det_model_path=str(det),
                            rec_model_path=str(rec),
                            cls_model_path=str(cls),
                            use_det=True,
                            use_cls=True,
                            use_rec=True,
                            rapidocr_params={
                                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                                "Global.log_level": "error",
                            },
                        ),
                        **common,
                    ),
                )
            },
        )
    count = len(policy.native_pages) + len(policy.ocr_pages) + len(policy.blank_pages)
    # PDFium renders an intermediate raster at 1.5 times requested scale.
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(original) as pdf:
        if len(pdf) != count or count > 1000:
            raise TychoraError("INDEX_INCOMPLETE", "PDF page count differs from policy")
        for number in policy.ocr_pages:
            page = pdf[number - 1]
            try:
                width, height = page.get_size()
                if ceil(width * 4.5) * ceil(height * 4.5) > 40000000:
                    raise TychoraError(
                        "LIMIT_EXCEEDED",
                        "PDF OCR intermediate raster exceeds pixel capacity",
                    )
            finally:
                page.close()
    for start in range(1, count + 1, 16):
        stop = min(start + 15, count)
        exports, evidence, outputs, proofs = [], [], [], []

        def stage(name, payload, media, outputs=outputs):
            target = original.with_name(name)
            with target.open("xb") as out:
                target.chmod(0o600)
                out.write(payload)
            outputs.append(StagedOutput(name, media, len(payload)))
            if sum(item.size_bytes for item in outputs) > 268435456:
                raise TychoraError(
                    "LIMIT_EXCEEDED", "PDF output batch exceeds capacity"
                )
            from hashlib import sha256

            hashed = sha256(payload).hexdigest()
            return {
                "sha256": hashed,
                "storage_key": hashed,
                "size_bytes": len(payload),
                "media_type": media,
            }

        current = start
        while current <= stop:
            ocr = current in policy.ocr_pages
            end = current
            while end < stop and ((end + 1 in policy.ocr_pages) == ocr):
                end += 1
            converter = scan if ocr else native
            assert converter is not None
            with _image_decode_guard():
                converted = converter.convert(
                    str(original), page_range=(current, end), max_num_pages=1000
                )
                validate_conversion(converted, tuple(range(current, end + 1)))
                for table in converted.document.tables:
                    picture = table.get_image(converted.document)
                    if picture is not None:
                        table.image = ImageRef.from_pil(picture, dpi=72)
                raw = converted.document.export_to_dict()
                proofs.append(
                    list(lineage.records.values())
                    if ocr and policy.origin_proof
                    else []
                )
                if ocr:
                    for number in range(current, end + 1):
                        record = captures.pop(number)
                        png = record.pop("input_png")
                        ref = stage(f"page-{number}.png", png, "image/png")
                        raw["pages"][str(number)]["image"] = {
                            "uri": "urn:tychora:artifact:sha256:" + ref["sha256"],
                            "mimetype": "image/png",
                            "size": {
                                "width": record["width"],
                                "height": record["height"],
                            },
                            "dpi": 216,
                        }
                        evidence.append(
                            {
                                "physical_page": number,
                                "model_input_image": ref,
                                "model_input_pixel_sha256": record[
                                    "model_input_pixel_sha256"
                                ],
                                "original_pixel_sha256": record[
                                    "original_pixel_sha256"
                                ],
                                "width": record["width"],
                                "height": record["height"],
                                "region_pixel_transform": record[
                                    "region_pixel_transform"
                                ],
                                "channel_interpretation": record[
                                    "channel_interpretation"
                                ],
                                "renderer": {
                                    "backend": "pdfium",
                                    "scale": 3.0,
                                    "intermediate_scale": 4.5,
                                    "resize": "rounded-crop-dimensions",
                                    "pdf_page_size": raw["pages"][str(number)]["size"],
                                    "crop": "full-page-top-left",
                                },
                                "cells": record["cells"],
                                "transcript": "\n".join(record["texts"]),
                                "detected_boxes": record["detected_boxes"],
                                "diagnostic": record["diagnostic"],
                            }
                        )
                # Externalize every export image in this bounded window, preserving
                # native scale-1 pixels and replacing only OCR page images above.
                nodes = list(raw.get("pages", {}).values()) + [
                    item for domain in DOMAINS for item in raw.get(domain, [])
                ]
                for index, node in enumerate(nodes):
                    image = node.get("image")
                    if not image or image["uri"].startswith(
                        "urn:tychora:artifact:sha256:"
                    ):
                        continue
                    header, encoded = image["uri"].split(",", 1)
                    if header != "data:image/png;base64":
                        raise TychoraError(
                            "INTEGRITY_ERROR", "Unexpected SDK image encoding"
                        )
                    ref = stage(
                        f"image-{current}-{index}.png",
                        base64.b64decode(encoded, validate=True),
                        "image/png",
                    )
                    image["uri"] = "urn:tychora:artifact:sha256:" + ref["sha256"]
                exports.append(FrozenJsonMap(raw))
                lineage.tables.clear()
                del converted, raw, nodes
            current = end + 1
        merged = merge_pdf_page_exports(tuple(exports))
        stage(
            f"batch-{start}.json",
            canonical_bytes(
                {
                    "document": merged,
                    "evidence": evidence,
                    **(
                        {"lineage": merge_lineage(exports, proofs)}
                        if policy.origin_proof
                        else {}
                    ),
                }
            ),
            "application/json",
        )
        report(ParserOutputBatch(tuple(outputs), tuple(range(start, stop + 1))))
        for output in outputs:
            original.with_name(output.name).unlink()
        del merged, exports, evidence, outputs
    return ()
