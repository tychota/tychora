"""Same-invocation OCR input and outcome capture for the pinned SDK only."""

from io import BytesIO
from typing import Any

from tychora.contracts.documents import image_pixel_identity
from tychora.contracts.errors import TychoraError


class _Postprocess:
    def __init__(self, delegate, capture):
        object.__setattr__(self, "delegate", delegate)
        object.__setattr__(self, "capture", capture)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def __setattr__(self, name, value):
        setattr(self.delegate, name, value)

    def __call__(self, *args, **kwargs):
        result = self.delegate(*args, **kwargs)
        self.capture.record["detector_completions"] += 1
        self.capture.record["detected_boxes"] = len(result[0])
        return result


class CapturingReader:
    """Instrument one reader instance; never change SDK classes or run twice."""

    def __init__(self, reader):
        self.reader = reader
        self.invocations = 0
        self.record: dict[str, Any] = {}
        self.postprocess = _Postprocess(reader.text_det.postprocess_op, self)
        reader.text_det.postprocess_op = self.postprocess
        for name in ("detect_and_crop", "cls_and_rotate", "recognize_txt"):
            delegate = getattr(reader, name)

            def stage(*args, _name=name, _delegate=delegate, **kwargs):
                try:
                    result = _delegate(*args, **kwargs)
                except Exception:
                    self.record["stages"][_name] = "failed"
                    raise
                self.record["stages"][_name] = "completed"
                if _name == "recognize_txt":
                    self.record["recognized_texts"] = list(result.txts or ())
                    self.record["recognition_scores"] = [
                        float(s) for s in (result.scores or ())
                    ]
                return result

            setattr(reader, name, stage)

    def __call__(self, array, **kwargs):
        self.invocations += 1
        from PIL import Image

        if (
            array.ndim != 3
            or array.shape[2] != 3
            or str(array.dtype) != "uint8"
            or array.shape[0] * array.shape[1] > 40_000_000
        ):
            raise TychoraError("INTEGRITY_ERROR", "Unsupported OCR input raster")
        height, width = array.shape[:2]
        transform = f"exif-transpose-rgb-v1:crop:0,0,{width},{height}"
        encoded = BytesIO()
        Image.fromarray(array).save(encoded, format="PNG")
        self.record = {
            "width": width,
            "height": height,
            "input_png": encoded.getvalue(),
            "model_input_pixel_sha256": image_pixel_identity(
                array.tobytes(), width, height, "RGB", transform
            ),
            "original_pixel_sha256": image_pixel_identity(
                array.tobytes(), width, height, "RGB", "exif-transpose-rgb-v1"
            ),
            "region_pixel_transform": transform,
            "channel_interpretation": "pdfium-rgb-array-rapidocr-ndarray-no-channel-swap",
            "detector_completions": 0,
            "detected_boxes": None,
            "stages": {},
        }
        result = self.reader(array, **kwargs)
        count = self.record["detected_boxes"]
        texts = list(getattr(result, "txts", None) or ())
        if self.record["detector_completions"] != 1:
            raise TychoraError("INDEX_INCOMPLETE", "OCR detector did not complete")
        if count and (
            any(
                self.record["stages"].get(stage) != "completed"
                for stage in ("detect_and_crop", "cls_and_rotate", "recognize_txt")
            )
            or not "\n".join(texts).strip()
        ):
            raise TychoraError(
                "INDEX_INCOMPLETE", "Detected text produced no usable OCR"
            )
        boxes = getattr(result, "boxes", None)
        scores = getattr(result, "scores", None)
        self.record.update(
            diagnostic="transcribed" if texts else "no-text-detected",
            texts=texts,
            boxes=boxes.tolist() if boxes is not None else [],
            scores=[float(s) for s in scores] if scores is not None else [],
        )
        if len(self.record["boxes"]) != len(texts) or len(self.record["scores"]) != len(
            texts
        ):
            raise TychoraError("INTEGRITY_ERROR", "OCR result cell coverage differs")
        return result


def normalize_native_document_units(parsed):
    """Keep complete parser units retained; normalize only verified native facts."""
    native = tuple(unit for unit in parsed.units if unit.text_origin == "source")
    if parsed.pdf_processing is None:
        return native
    headings, result = [], []
    refs = {unit.native_ref for unit in native}
    for unit in native:
        if unit.heading_level:
            while headings and headings[-1][0] >= unit.heading_level:
                headings.pop()
            headings.append((unit.heading_level, unit.text))
        result.append(
            unit.model_copy(
                update={
                    "heading_path": tuple(text for _, text in headings),
                    "parent_ref": unit.parent_ref if unit.parent_ref in refs else None,
                }
            )
        )
    return tuple(result)


def validate_ordered_ocr_lineage(parsed, evidence):
    from .ocr_lineage import validate_lineage

    if parsed.ocr_lineage is None:
        raise ValueError("Missing ordered OCR cell lineage")
    validate_lineage(parsed.units, evidence, parsed.ocr_lineage)


class OcrOriginError(ValueError):
    """An origin contract failure, distinguishable at the parser boundary."""


def validate_ocr_origins(parsed):
    try:
        _validate_ocr_origins(parsed)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise OcrOriginError(str(exc)) from exc


def _validate_ocr_origins(parsed):
    from hashlib import sha256

    from tychora.application.pdf_processing import policy_sha256
    from tychora.contracts.identity import canonical_bytes, digest
    from tychora.contracts.pdf_policy import PdfProcessingPolicy

    policy = PdfProcessingPolicy.model_validate(dict(parsed.pdf_processing))
    if parsed.parser_fingerprint != digest(
        "docling-pdf-policy-parser-v1", policy_sha256(policy)
    ):
        raise ValueError("PDF origin policy differs from parser identity")
    captured_policy = parsed.registration.capture.source.get("pdf_processing")
    if captured_policy is not None and canonical_bytes(
        captured_policy
    ) != canonical_bytes(policy.model_dump(mode="json")):
        raise ValueError("PDF origin policy differs from captured request")
    if parsed.registration.format != "pdf":
        raise ValueError("PDF policy on non-PDF extraction")
    if tuple(page.physical_page for page in parsed.ocr_pages) != policy.ocr_pages:
        raise ValueError("OCR page evidence coverage differs")
    expected_pages = (
        set(policy.native_pages) | set(policy.ocr_pages) | set(policy.blank_pages)
    )
    if {int(page) for page in parsed.document_json.get("pages", {})} != expected_pages:
        raise ValueError("PDF export page partition differs from policy")
    evidence = {page.physical_page: page for page in parsed.ocr_pages}
    assets = {asset.native_ref: asset for asset in parsed.assets}
    for page in evidence.values():
        if (
            page.original_sha256 != parsed.original_ref.sha256
            or page.policy_sha256 != policy_sha256(policy)
            or page.model_identity != policy.model_inventory_sha256
            or page.runtime_inventory != policy.runtime_inventory
            or assets.get(f"#/pages/{page.physical_page}") is None
            or assets[f"#/pages/{page.physical_page}"].artifact_ref
            != page.model_input_image
        ):
            raise ValueError("OCR page provenance differs")
    for unit in parsed.units:
        pages = {prov.physical_page for prov in unit.provenance}
        if not pages <= expected_pages or (
            pages & set(policy.blank_pages) and (unit.text.strip() or unit.cells)
        ):
            raise ValueError("PDF unit differs from classified page partition")
        ocr = pages & set(policy.ocr_pages)
        if ocr and pages != ocr:
            raise ValueError("Ambiguous mixed-origin document item")
        if unit.text_origin != ("ocr" if ocr else "source"):
            raise ValueError("Document origin differs from retained page evidence")
        if not pages and (unit.text.strip() or unit.cells):
            raise ValueError("PDF text has no page evidence")
        if ocr and policy.origin_proof is None:
            # A full-page scan must contain only OCR cells. Each textual field
            # must be supported by retained transcription, not geometry overlap.
            available = "".join(
                "".join(str(cell["text"]).split())
                for page in sorted(ocr)
                for cell in evidence[page].cells
            )
            values = (
                [
                    cell["text"]
                    for cell in unit.raw_item.get("data", {}).get("table_cells", ())
                ]
                if unit.cells
                else [unit.text]
            )
            for value in values:
                normalized = "".join(value.split())
                if normalized and normalized not in available:
                    raise ValueError("Document text has no exact OCR cell evidence")
    if policy.origin_proof is not None:
        validate_ordered_ocr_lineage(parsed, evidence)
    elif parsed.ocr_lineage is not None:
        raise ValueError("Legacy PDF cannot carry ordered OCR cell lineage")
    if policy.text_coordinates:
        native = parsed.native_text_evidence
        if native is None or tuple(
            page["physical_page"] for page in native["pages"]
        ) != tuple(sorted((*policy.native_pages, *policy.blank_pages))):
            raise ValueError("Native assembly page coverage differs from policy")
    elif parsed.native_text_evidence is not None:
        raise ValueError("Legacy PDF cannot carry native coordinate proof")
    from .ocr_lineage import evidence_document

    if parsed.image_representation != "scan-external-images@1":
        raise ValueError("Explicit PDF image representation missing")
    for ref, data in (
        (
            parsed.ocr_evidence_ref,
            evidence_document(
                parsed.ocr_pages,
                parsed.ocr_lineage,
                policy.origin_proof,
                native=parsed.native_text_evidence,
            ),
        ),
        (
            parsed.image_bindings_ref,
            {
                "version": parsed.image_representation,
                "bindings": [
                    binding.model_dump(mode="json") for binding in parsed.image_bindings
                ],
            },
        ),
    ):
        payload = canonical_bytes(data)
        if (
            ref is None
            or ref.sha256 != sha256(payload).hexdigest()
            or ref.size_bytes != len(payload)
        ):
            raise ValueError("PDF evidence sidecar differs")


def collect_ocr_page_evidence(pages, original, policy):
    from tychora.application.pdf_processing import policy_sha256
    from tychora.contracts.documents import OcrPageEvidence
    from tychora.contracts.identity import canonical_bytes

    result = []
    for page in pages:
        result.append(
            OcrPageEvidence.model_validate_json(
                canonical_bytes(
                    {
                        **page,
                        "original_sha256": original.sha256,
                        "policy_sha256": policy_sha256(policy),
                        "model_identity": policy.model_inventory_sha256,
                        "runtime_inventory": policy.runtime_inventory,
                    }
                )
            )
        )
    return tuple(result)


def retain_ocr_page_observation(owner, region, evidence, ctx, *, budget=None):
    from tychora.adapters.vision.local import retain_local_ocr_observation

    ctx.checkpoint()
    if (
        region.region_pixel_sha256 != evidence.model_input_pixel_sha256
        or region.image_ref != evidence.model_input_image
    ):
        raise TychoraError(
            "INTEGRITY_ERROR", "OCR receipt input differs from retained raster"
        )
    return retain_local_ocr_observation(
        owner,
        region,
        {
            "literal_transcription": evidence.transcript,
            "cells": [dict(cell) for cell in evidence.cells],
            "pdf_evidence": evidence.model_dump(mode="json"),
        },
        model="docling-rapidocr:" + evidence.policy_sha256,
        prompt_schema_version="local-ocr-v1",
        before_retain=budget.reserve_payload if budget is not None else None,
    )


def validate_external_images(parsed, read):
    """Resolve only manifest-declared images; verify each payload independently."""
    from hashlib import sha256

    from PIL import Image

    if parsed.image_representation is None:
        return
    bindings = {
        binding.native_ref: binding.artifact_ref for binding in parsed.image_bindings
    }
    if len(bindings) != len(parsed.image_bindings) or set(bindings) != {
        asset.native_ref for asset in parsed.assets
    }:
        raise ValueError("External image declaration coverage differs")
    for asset in parsed.assets:
        ref = bindings[asset.native_ref]
        if ref != asset.artifact_ref:
            raise ValueError("External image binding differs")
        payload = read(ref)
        if len(payload) != ref.size_bytes or sha256(payload).hexdigest() != ref.sha256:
            raise ValueError("External image payload differs")
        with Image.open(BytesIO(payload)) as image:
            if (
                image.format != "PNG"
                or ref.media_type != "image/png"
                or image.size != (asset.width, asset.height)
                or asset.width * asset.height > 40000000
            ):
                raise ValueError("External image geometry or media differs")
            image.verify()
        page = next(
            (
                page
                for page in parsed.ocr_pages
                if page.model_input_image == ref
                and page.physical_page == asset.locator.physical_page
            ),
            None,
        )
        if page is not None:
            with Image.open(BytesIO(payload)) as image:
                rgb = image.convert("RGB")
                if (
                    image_pixel_identity(
                        rgb.tobytes(),
                        rgb.width,
                        rgb.height,
                        "RGB",
                        page.region_pixel_transform,
                    )
                    != page.model_input_pixel_sha256
                ):
                    raise ValueError("OCR exact raster pixel identity differs")


def plan_pdf_ocr(owner, parsed, ctx, *, budget=None):
    """Build existing local receipts and composition inputs from retained evidence."""
    from tychora.adapters.vision.local import local_ocr_identities
    from tychora.application.pdf_processing import PdfRetentionBudget
    from tychora.contracts.composition import PlannedRegion
    from tychora.contracts.evidence import Geometry
    from tychora.ingestion.vision import planned_vision_proxy
    from tychora.ports.vision import RegionInput, vision_context_identity

    budget = PdfRetentionBudget.for_extraction(parsed, budget)
    proxies, regions = [], []
    for evidence in parsed.ocr_pages:
        ctx.checkpoint()
        with owner.artifacts.open_verified(evidence.model_input_image) as stream:
            payload = stream.read(evidence.model_input_image.size_bytes + 1)
        region = RegionInput(
            source_native_ref=f"#/pages/{evidence.physical_page}",
            image=payload,
            image_ref=evidence.model_input_image,
            media_type="image/png",
            image_sha256=evidence.model_input_image.sha256,
            original_image_sha256=evidence.model_input_image.sha256,
            original_pixel_sha256=evidence.original_pixel_sha256,
            region_pixel_sha256=evidence.model_input_pixel_sha256,
            original_width=evidence.width,
            original_height=evidence.height,
            width=evidence.width,
            height=evidence.height,
            color_mode="RGB",
            original_pixel_transform="exif-transpose-rgb-v1",
            region_pixel_transform=evidence.region_pixel_transform,
            geometry=Geometry(
                domain="pixels", x0=0, y0=0, x1=evidence.width, y1=evidence.height
            ),
            context_sha256=vision_context_identity(()),
            task="ocr",
            dependency_ids=frozenset({parsed.registration.source.source_id}),
        )
        # Detector-zero pages retain pixels and evidence, but have no observation
        # to cite; constructing their region must not create an orphan receipt.
        task_schema_sha, prompt_identity = local_ocr_identities("local-ocr-v1")
        planned = PlannedRegion(
            **region.model_dump(exclude={"image", "source_context"}),
            model="docling-rapidocr:" + evidence.policy_sha256,
            prompt_schema_version="local-ocr-v1",
            task_schema_sha256=task_schema_sha,
            prompt_identity=prompt_identity,
        )
        regions.append(planned)
        if evidence.transcript.strip():
            observation = retain_ocr_page_observation(
                owner, region, evidence, ctx, budget=budget
            )
            proxies.append(planned_vision_proxy(region, observation))
    return tuple(proxies), tuple(regions)
