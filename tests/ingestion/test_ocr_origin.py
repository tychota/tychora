from types import SimpleNamespace

import numpy as np
import pytest

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
