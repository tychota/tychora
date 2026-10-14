"""All-page structural census, using generated PDFs only."""

import io

import pytest
from pypdf import PdfReader, PdfWriter

from tests.transports.test_pdf_intake import make_pdf
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError


def analyse(path, **limits):
    from tychora.ingestion.pdf_analysis import analyse_pdf

    return analyse_pdf(path, FrozenJsonMap(limits))


def test_census_counts_every_native_image_and_blank_page(tmp_path):
    writer = PdfWriter()
    for scanned, blank in [(False, False), (True, False), (False, True), (True, False)]:
        writer.add_page(
            PdfReader(io.BytesIO(make_pdf(tmp_path, scanned, blank))).pages[0]
        )
    path = tmp_path / "synthetic.pdf"
    writer.write(path)
    result = analyse(path)
    assert result["page_count"] == 4
    assert result["native_text_pages"] == (1,)
    assert result["image_only_pages"] == (2, 4)
    assert result["blank_pages"] == (3,)
    assert result["unknown_pages"] == ()
    assert result["requires_ocr"] is True
    assert "Synthetic local document" not in repr(result)


@pytest.mark.parametrize("encrypted", [False, True])
def test_invalid_and_encrypted_documents_are_rejected(tmp_path, encrypted):
    path = tmp_path / "invalid.pdf"
    if encrypted:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("synthetic-password")
        writer.write(path)
    else:
        path.write_bytes(b"%PDF-invalid synthetic")
    with pytest.raises(TychoraError) as error:
        analyse(path)
    assert error.value.code == "INVALID_REQUEST"


def test_page_and_pixel_limits_are_enforced(tmp_path):
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(make_pdf(tmp_path, scanned=True))
    with pytest.raises(TychoraError) as error:
        analyse(path, max_image_pixels=9999)
    assert error.value.code == "LIMIT_EXCEEDED"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.write(path)
    with pytest.raises(TychoraError) as error:
        analyse(path, max_pages=1)
    assert error.value.code == "LIMIT_EXCEEDED"
