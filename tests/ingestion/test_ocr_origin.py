from types import SimpleNamespace

import numpy as np
import pytest

from tests.ingestion import test_docling_mapping as mapping_fixtures
from tychora.contracts.errors import TychoraError


class DetectorPostprocess:
    box_thresh = 0.5

    def __call__(self, count):
        return np.zeros((count, 4, 2)), [1.0] * count


class Reader:
    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = 0
        self.text_det = SimpleNamespace(postprocess_op=DetectorPostprocess())

    def detect_and_crop(self):
        if self.scenario == "preprocess":
            raise ValueError("empty preprocessing")
        boxes, scores = self.text_det.postprocess_op(
            0 if self.scenario == "zero" else 1
        )
        if len(boxes) == 0:
            raise ValueError("expected detector empty")
        return [], SimpleNamespace(boxes=boxes, scores=scores)

    def cls_and_rotate(self):
        if self.scenario == "classifier":
            raise ValueError("classifier failed")
        return [], SimpleNamespace()

    def recognize_txt(self):
        if self.scenario == "recognition":
            raise ValueError("recognition failed")
        return SimpleNamespace(txts=("hello",), scores=(0.9,))

    def __call__(self, array, **kwargs):
        self.calls += 1
        try:
            self.detect_and_crop()
            self.cls_and_rotate()
            self.recognize_txt()
        except ValueError:
            return SimpleNamespace(boxes=None, txts=None, scores=None)
        if self.scenario == "filtered":
            return SimpleNamespace(boxes=None, txts=None, scores=None)
        return SimpleNamespace(
            boxes=np.zeros((1, 4, 2)), txts=("hello",), scores=(0.9,)
        )


@pytest.mark.parametrize(
    "scenario",
    ["zero", "success", "preprocess", "classifier", "recognition", "filtered"],
)
def test_same_invocation_detector_evidence_distinguishes_empty_output(scenario):
    from tychora.adapters.docling.ocr_evidence import CapturingReader

    reader = Reader(scenario)
    capture = CapturingReader(reader)
    reader.text_det.postprocess_op.box_thresh = 0.7
    assert capture.postprocess.delegate.box_thresh == 0.7
    pixels = np.zeros((30, 20, 3), dtype=np.uint8)
    if scenario in ("zero", "success"):
        result = capture(pixels)
        assert capture.record["detected_boxes"] == (0 if scenario == "zero" else 1)
        assert capture.record["diagnostic"] == (
            "no-text-detected" if scenario == "zero" else "transcribed"
        )
        assert capture.record["width"] == 20
        assert capture.record["height"] == 30
        assert (result.boxes is None) == (scenario == "zero")
    else:
        with pytest.raises(TychoraError) as error:
            capture(pixels)
        assert error.value.code == "INDEX_INCOMPLETE"
    assert reader.calls == 1


def test_pdf_coverage_distinguishes_detector_gaps_from_known_blank_pages():
    from tychora.contracts.composition import PdfCoverage, VisionCapabilityDiagnostics

    coverage = PdfCoverage(
        page_count=3,
        native_pages=(2,),
        ocr_pages=(1,),
        blank_pages=(3,),
        no_text_detected_pages=(1,),
    )
    diagnostic = VisionCapabilityDiagnostics(gaps=(), pdf_coverage=coverage)
    assert diagnostic.pdf_coverage is not None
    assert diagnostic.pdf_coverage.no_text_detected_pages == (1,)
    assert diagnostic.pdf_coverage.blank_pages == (3,)
    with pytest.raises(ValueError):
        PdfCoverage(
            page_count=3,
            native_pages=(2,),
            ocr_pages=(1,),
            blank_pages=(3,),
            no_text_detected_pages=(3,),
        )


def test_legacy_vision_diagnostics_omit_pdf_fields():
    from tychora.contracts.composition import VisionCapabilityDiagnostics
    from tychora.contracts.identity import canonical_bytes

    legacy = {
        "type": "document_vision_capability",
        "schema_version": "1",
        "gaps": [{"selection_sha256": "a" * 64, "reason": "provider_unavailable"}],
    }
    assert (
        VisionCapabilityDiagnostics.model_validate_json(
            canonical_bytes(legacy)
        ).model_dump(mode="json")
        == legacy
    )


def pdf_ocr_case(tmp_path, *, transcript, allowance=32):
    """Real CAS, exact tiny PNG, and the retained refs consumed by the planner."""
    from io import BytesIO

    from PIL import Image

    from tychora.adapters.files.cas import LocalArtifacts
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.documents import OcrPageEvidence, image_pixel_identity

    artifacts = LocalArtifacts(tmp_path / "cas")
    original = artifacts.put_stream(
        BytesIO(b"synthetic original PDF"), "application/pdf"
    )
    image = Image.new("RGB", (1, 1), "white")
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    raster = artifacts.put_stream(BytesIO(encoded.getvalue()), "image/png")
    metadata = [
        artifacts.put_stream(BytesIO(payload), "application/json")
        for payload in (b'{"raw":1}', b'{"manifest":1}', b"[]", b'{"bindings":[]}')
    ]
    refs = (original, raster, *metadata)
    page = OcrPageEvidence(
        physical_page=1,
        model_input_image=raster,
        model_input_pixel_sha256=image_pixel_identity(
            image.tobytes(), 1, 1, "RGB", "exif-transpose-rgb-v1:crop:0,0,1,1"
        ),
        original_pixel_sha256=image_pixel_identity(
            image.tobytes(), 1, 1, "RGB", "exif-transpose-rgb-v1"
        ),
        original_sha256=original.sha256,
        width=1,
        height=1,
        region_pixel_transform="exif-transpose-rgb-v1:crop:0,0,1,1",
        renderer=FrozenJsonMap({"backend": "pdfium"}),
        channel_interpretation="pdfium-rgb-array-rapidocr-ndarray-no-channel-swap",
        policy_sha256="a" * 64,
        model_identity="b" * 64,
        runtime_inventory=FrozenJsonMap({}),
        cells=(FrozenJsonMap({"text": transcript, "from_ocr": True}),)
        if transcript
        else (),
        transcript=transcript,
        detected_boxes=1 if transcript else 0,
        diagnostic="transcribed" if transcript else "no-text-detected",
    )
    parsed = SimpleNamespace(
        original_ref=original,
        docling_json_ref=metadata[0],
        raw_document_ref=metadata[1],
        ocr_evidence_ref=metadata[2],
        image_bindings_ref=metadata[3],
        assets=(SimpleNamespace(artifact_ref=raster),),
        pdf_processing=FrozenJsonMap(
            {
                "limits": {
                    "max_retained_bytes": sum(ref.size_bytes for ref in refs)
                    + allowance
                }
            }
        ),
        ocr_pages=(page,),
        registration=SimpleNamespace(source=SimpleNamespace(source_id="c" * 64)),
    )
    return SimpleNamespace(artifacts=artifacts), parsed


def test_over_budget_pdf_receipt_is_rejected_before_cas_write(tmp_path, monkeypatch):
    from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
    from tychora.ports.execution import ExecutionContext

    owner, parsed = pdf_ocr_case(tmp_path, transcript="Synthetic text")
    writes = []
    put = owner.artifacts.put_stream

    def observed_put(stream, media_type):
        writes.append(media_type)
        return put(stream, media_type)

    monkeypatch.setattr(owner.artifacts, "put_stream", observed_put)
    with pytest.raises(TychoraError) as error:
        plan_pdf_ocr(owner, parsed, ExecutionContext())
    assert error.value.code == "LIMIT_EXCEEDED"
    assert not error.value.retryable
    assert writes == []


def test_detector_zero_keeps_pixel_region_without_orphan_receipt(tmp_path, monkeypatch):
    from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
    from tychora.ports.execution import ExecutionContext

    owner, parsed = pdf_ocr_case(tmp_path, transcript="")
    writes = []
    put = owner.artifacts.put_stream

    def observed_put(stream, media_type):
        writes.append(media_type)
        return put(stream, media_type)

    monkeypatch.setattr(owner.artifacts, "put_stream", observed_put)
    proxies, regions = plan_pdf_ocr(owner, parsed, ExecutionContext())
    assert proxies == ()
    assert len(regions) == 1
    assert regions[0].image_ref == parsed.ocr_pages[0].model_input_image
    assert parsed.ocr_pages[0].diagnostic == "no-text-detected"
    assert writes == []


# Reuse the synthetic retained-parser fixture; these checks load no OCR model.
document_kind = mapping_fixtures.document_kind
execution = mapping_fixtures.execution
retained_case = mapping_fixtures.retained_case
rich_capture = mapping_fixtures.rich_capture


@pytest.mark.parametrize("execution", ["pdf"], indirect=True)
def test_selected_crop_budget_denies_before_cas(retained_case, monkeypatch):
    from tychora.application.vision_selection import derive_selected_region
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.requests import VisionCrop, VisionSelection

    parsed, execution, _ = retained_case
    selected = VisionSelection(
        captured_original_sha256=parsed.original_ref.sha256,
        source_native_ref=parsed.assets[0].native_ref,
        geometry=VisionCrop(domain="pixels", x0=0, y0=0, x1=2, y1=2),
        task="ocr",
        task_schema=FrozenJsonMap({"type": "null"}),
    )
    writes = []
    put = execution.owner.artifacts.put_stream

    def observed_put(stream, media_type):
        writes.append(media_type)
        return put(stream, media_type)

    def deny(payload, media_type):
        assert payload and media_type == "image/png"
        raise TychoraError("LIMIT_EXCEEDED", "Synthetic exhausted PDF budget")

    monkeypatch.setattr(execution.owner.artifacts, "put_stream", observed_put)
    with pytest.raises(TychoraError) as error:
        execution.owner.run_blocking_sync(
            lambda: derive_selected_region(
                execution.owner, parsed, selected, before_retain=deny
            ),
            execution.ctx,
        )
    assert error.value.code == "LIMIT_EXCEEDED"
    assert writes == []


@pytest.mark.parametrize("kind", ["local", "remote"])
def test_selected_receipt_budget_denies_before_cas(tmp_path, monkeypatch, kind):
    from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
    from tychora.adapters.vision.local import retain_local_ocr_observation
    from tychora.application.vision_selection import retain_remote_observation
    from tychora.ingestion.composition import _region_input
    from tychora.ports.execution import ExecutionContext

    owner, parsed = pdf_ocr_case(tmp_path, transcript="Synthetic", allowance=10000)
    _, regions = plan_pdf_ocr(owner, parsed, ExecutionContext())
    with owner.artifacts.open_verified(regions[0].image_ref) as stream:
        region = _region_input(regions[0], stream.read())
    writes = []
    put = owner.artifacts.put_stream

    def observed_put(stream, media_type):
        writes.append(media_type)
        return put(stream, media_type)

    def deny(payload, media_type):
        assert payload and media_type == "application/json"
        raise TychoraError("LIMIT_EXCEEDED", "Synthetic exhausted PDF budget")

    monkeypatch.setattr(owner.artifacts, "put_stream", observed_put)
    with pytest.raises(TychoraError) as error:
        if kind == "local":
            retain_local_ocr_observation(
                owner,
                region,
                {"literal_transcription": "Synthetic"},
                model="synthetic",
                prompt_schema_version="local-ocr-v1",
                before_retain=deny,
            )
        else:
            from tychora.ports.vision import DerivedObservation, vision_cache_key

            observation = DerivedObservation(
                cache_key=vision_cache_key(
                    region.image_sha256,
                    "ocr",
                    "synthetic",
                    regions[0].prompt_identity,
                    region.context_sha256,
                ),
                task="ocr",
                image_sha256=region.image_sha256,
                original_image_sha256=region.original_image_sha256,
                original_pixel_sha256=region.original_pixel_sha256,
                region_pixel_sha256=region.region_pixel_sha256,
                context_sha256=region.context_sha256,
                literal_transcription="Synthetic",
                interpretation=None,
                uncertainties=(),
                model="synthetic",
                prompt_schema_version=regions[0].prompt_schema_version,
                task_schema_sha256=regions[0].task_schema_sha256,
                prompt_identity=regions[0].prompt_identity,
            )
            retain_remote_observation(owner, region, observation, before_retain=deny)
    assert error.value.code == "LIMIT_EXCEEDED"
    assert writes == []


def test_live_pdf_budget_counts_prior_outputs_and_deduplicates_repeated_refs(
    tmp_path, monkeypatch
):
    from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
    from tychora.application.pdf_processing import PdfRetentionBudget
    from tychora.contracts.base import FrozenJsonMap
    from tychora.ports.execution import ExecutionContext

    owner, parsed = pdf_ocr_case(tmp_path, transcript="Synthetic", allowance=10000)
    # Measure the actual serializer once, then admit exactly one receipt and repeat
    # it under the same budget; physical content must count once.
    proxies, _ = plan_pdf_ocr(owner, parsed, ExecutionContext())
    receipt = proxies[0].derivation
    seed = PdfRetentionBudget.for_extraction(parsed).retained_bytes
    parsed.pdf_processing = FrozenJsonMap(
        {"limits": {"max_retained_bytes": seed + receipt.size_bytes}}
    )
    budget = PdfRetentionBudget.for_extraction(parsed)
    plan_pdf_ocr(owner, parsed, ExecutionContext(), budget=budget)
    plan_pdf_ocr(owner, parsed, ExecutionContext(), budget=budget)
    assert budget.retained_bytes == seed + receipt.size_bytes
    budget.reserve_refs((receipt, receipt))
    assert budget.retained_bytes == seed + receipt.size_bytes
    # A previous admitted output absent from this plan must still consume room.
    budget = PdfRetentionBudget.for_extraction(parsed)
    budget.reserve_payload(b"prior output", "application/json")
    writes = []
    put = owner.artifacts.put_stream

    def observed_put(stream, media_type):
        writes.append(media_type)
        return put(stream, media_type)

    monkeypatch.setattr(owner.artifacts, "put_stream", observed_put)
    with pytest.raises(TychoraError) as error:
        plan_pdf_ocr(owner, parsed, ExecutionContext(), budget=budget)
    assert error.value.code == "LIMIT_EXCEEDED"
    assert writes == []


def test_legacy_composition_limit_and_conflicting_identity_remain_distinct(
    retained_case,
):
    from tychora.contracts.artifacts import ArtifactRef
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.composition import DocumentCompositionPlan
    from tychora.ingestion.composition import _input_references

    ref = ArtifactRef(
        sha256="a" * 64,
        storage_key="a" * 64,
        size_bytes=268435457,
        media_type="image/png",
    )
    plan = DocumentCompositionPlan(base_normalizer_fingerprint="b" * 64)
    original, _, _ = retained_case
    asset = original.assets[0].model_copy(update={"artifact_ref": ref})
    parsed = original.model_copy(update={"pdf_processing": None, "assets": (asset,)})
    with pytest.raises(ValueError, match="aggregate capacity"):
        _input_references(parsed, plan)
    parsed = parsed.model_copy(
        update={
            "pdf_processing": FrozenJsonMap({"mode": "native"}),
            "assets": (
                asset,
                asset.model_copy(
                    update={"artifact_ref": ref.model_copy(update={"size_bytes": 1})}
                ),
            ),
        }
    )
    with pytest.raises(ValueError, match="Conflicting"):
        _input_references(parsed, plan)
