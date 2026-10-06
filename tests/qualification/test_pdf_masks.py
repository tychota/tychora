"""Synthetic PDF metadata bounds for separately referenced image masks."""

import zlib

import pytest

from tychora.adapters.docling.parser import MAX_IMAGE_PIXELS, _check_pdf_image_capacity
from tychora.contracts.errors import TychoraError


def _stream(dictionary: bytes, payload: bytes) -> bytes:
    return (
        b"<< "
        + dictionary
        + b" /Length "
        + str(len(payload)).encode()
        + b" >>\nstream\n"
        + payload
        + b"\nendstream"
    )


def _masked_pdf(
    path, *, kind: str, width: int, height: int, mask_ref: bytes | None = None
):
    if kind == "soft":
        mask_data = zlib.compress(bytes(width * height), 9)
        mask_dict = (
            f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode"
        ).encode()
        mask_ref = mask_ref or b"/SMask 9 0 R"
    else:
        mask_data = zlib.compress(bytes((width * height + 7) // 8), 9)
        mask_dict = (
            f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/ImageMask true /BitsPerComponent 1 /Filter /FlateDecode"
        ).encode()
        mask_ref = mask_ref or b"/Mask 9 0 R"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /XObject << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: _stream(b"", b"q /F1 Do Q"),
        5: _stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 200 200] /Resources << /XObject << /F2 7 0 R >> >>",
            b"q /F2 Do Q",
        ),
        6: _stream(
            b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            + mask_ref,
            zlib.compress(b"\xff\xff\xff"),
        ),
        7: _stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 200 200] /Resources << /XObject << /Im1 6 0 R >> >>",
            b"q /Im1 Do Q",
        ),
        8: b"<< /Producer (Synthetic mask fixture) >>",
        9: _stream(mask_dict, mask_data),
    }
    data = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, 10):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode())
        data.extend(objects[number])
        data.extend(b"\nendobj\n")
    start_xref = len(data)
    data.extend(b"xref\n0 10\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(b"trailer\n<< /Size 10 /Root 1 0 R /Info 8 0 R >>\nstartxref\n")
    data.extend(str(start_xref).encode())
    path.write_bytes(data + b"\n%%EOF\n")


@pytest.mark.parametrize("kind", ["soft", "explicit"])
def test_oversized_referenced_mask_is_rejected_before_decode(tmp_path, kind):
    path = tmp_path / f"{kind}.pdf"
    _masked_pdf(path, kind=kind, width=6500, height=6500)
    assert 6500 * 6500 > MAX_IMAGE_PIXELS
    with pytest.raises(TychoraError, match="pixel limit") as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("kind", ["soft", "explicit"])
def test_small_referenced_mask_remains_accepted(tmp_path, kind):
    path = tmp_path / f"small-{kind}.pdf"
    _masked_pdf(path, kind=kind, width=10, height=10)
    _check_pdf_image_capacity(path)


def test_malformed_mask_reference_fails_closed(tmp_path):
    path = tmp_path / "missing-mask.pdf"
    _masked_pdf(path, kind="soft", width=10, height=10, mask_ref=b"/SMask 40 0 R")
    with pytest.raises(TychoraError) as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "INVALID_REQUEST"


def test_cyclic_mask_reference_fails_closed(tmp_path):
    path = tmp_path / "cyclic-mask.pdf"
    _masked_pdf(path, kind="soft", width=10, height=10, mask_ref=b"/SMask 6 0 R")
    with pytest.raises(TychoraError) as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "INVALID_REQUEST"


def test_mask_metadata_walk_has_finite_work_budget(tmp_path, monkeypatch):
    from tychora.adapters.docling import parser

    path = tmp_path / "bounded-mask.pdf"
    _masked_pdf(path, kind="soft", width=10, height=10)
    monkeypatch.setattr(parser, "MAX_ITEMS", 3)
    with pytest.raises(TychoraError, match="mask metadata") as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "INVALID_REQUEST"


def test_missing_documents_mask_inspector_is_actionable(tmp_path, monkeypatch):
    import sys

    path = tmp_path / "small-mask.pdf"
    _masked_pdf(path, kind="soft", width=10, height=10)
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(TychoraError, match="documents extra") as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "CAPABILITY_MISSING"


def test_nested_form_metadata_depth_is_bounded(tmp_path):
    depth = 130
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /XObject << /F 5 0 R >> >> /Contents 4 0 R >>",
        4: _stream(b"", b"q /F Do Q"),
    }
    for number in range(5, 5 + depth):
        successor = number + 1
        objects[number] = _stream(
            f"/Type /XObject /Subtype /Form /BBox [0 0 200 200] /Resources << /XObject << /F {successor} 0 R >> >>".encode(),
            b"q /F Do Q",
        )
    objects[5 + depth] = _stream(
        b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode",
        zlib.compress(b"\xff\xff\xff"),
    )
    data = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, 6 + depth):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode())
        data.extend(objects[number])
        data.extend(b"\nendobj\n")
    start_xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n".encode()
    )
    path = tmp_path / "deep-forms.pdf"
    path.write_bytes(data)
    with pytest.raises(TychoraError) as failure:
        _check_pdf_image_capacity(path)
    assert failure.value.code == "INVALID_REQUEST"
