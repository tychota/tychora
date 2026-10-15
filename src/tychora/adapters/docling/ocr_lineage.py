"""Bounded exact replay of the pinned Docling OCR assembly operations.

No SDK/model dependency: retained artifacts replay this recipe independently.
The character and list-marker rules mirror Docling 2.132.0 (MIT licensed).
"""

import re

from tychora.contracts.identity import canonical_bytes

VERSION = "ordered-ocr-cells@1"
MAX_ENTRIES = 100_000
_LIGATURES = dict(
    zip(
        "ﬀﬁﬂﬃﬄﬅﬆĲĳ\uf0a0",
        ("ff", "fi", "fl", "ffi", "ffl", "st", "st", "IJ", "ij", ""),
        strict=True,
    )
)
_LIGATURE_RE = re.compile(r"([\ufb00-\ufb06]|\u0132|\u0133|\uf0a0)( (?=\w))?")
_BULLETS = (
    r"[\u2022\u2023\u25E6\u2043\u204C\u204D\u2219\u25AA\u25AB\u25CF\u25CB]",
    r"[-*+•·‣⁃]",
    r"[►▶▸‣➤➢]",
    r"[✓✔✗✘]",
)
_NUMBERS = (
    r"\d+(?:\.\d+)+\.?",
    r"\d+\.?[a-zA-Z]\.",
    r"\d+\.?[a-zA-Z]\)",
    r"\(\d+\.?[a-zA-Z]\)",
    r"\d+\.",
    r"\d+\)",
    r"\(\d+\)",
    r"\[\d+\]",
    r"[ivxlcdm]+\.",
    r"[IVXLCDM]+\.",
    r"[a-z]\.",
    r"[A-Z]\.",
    r"[a-z]\)",
    r"[A-Z]\)",
)


def fail():
    raise ValueError("Invalid ordered OCR cell lineage")


def sanitize_text(lines):
    """Exact qualified page-assembly normalization, including line boundaries."""
    lines = list(lines)
    for index, line in enumerate(lines[1:]):
        previous = lines[index]
        if previous.endswith("-"):
            before = re.findall(r"\b[\w]+\b", previous)
            after = re.findall(r"\b[\w]+\b", line)
            attached = len(previous) > 1 and previous[-2].isalnum()
            if (
                attached
                and before
                and after
                and before[-1].isalnum()
                and after[0].isalnum()
            ):
                lines[index] = previous[:-1]
            elif not attached:
                lines[index] += " "
        else:
            lines[index] += " "
    text = "".join(lines).translate(
        str.maketrans({"⁄": "/", "’": "'", "‘": "'", "“": '"', "”": '"', "•": "·"})
    )
    return _LIGATURE_RE.sub(
        lambda match: (
            _LIGATURES[match[1]]
            + ("" if "\ufb00" <= match[1] <= "\ufb06" else (match[2] or ""))
        ),
        text,
    ).strip()


def segment_text(segment, evidence):
    if set(segment) != {"page", "cells", "join"}:
        fail()
    page = segment["page"]
    ids = segment["cells"]
    if (
        type(page) is not int
        or page not in evidence
        or len(ids) > MAX_ENTRIES
        or len(set(ids)) != len(ids)
    ):
        fail()
    cells = evidence[page].cells
    texts = []
    for index in ids:
        if type(index) is not int or not 0 <= index < len(cells):
            fail()
        cell = cells[index]
        if cell.get("index") != index or cell.get("from_ocr") is not True:
            fail()
        texts.append(cell["text"])
    join = segment["join"]
    if join == "table":
        return " ".join(texts)
    lines = [text.replace("\x02", "-").strip() for text in texts if text.strip()]
    if join == "sanitize":
        return sanitize_text(lines)
    if join == "plain":
        return " ".join(lines)
    fail()


def text_fields(record, evidence):
    if set(record) != {"native_ref", "kind", "projection", "segments"}:
        fail()
    segments = record["segments"]
    if not 0 < len(segments) <= MAX_ENTRIES:
        fail()
    original = segment_text(segments[0], evidence)
    text = original
    projection = record["projection"]
    fields = {}
    if projection == "formula":
        text = ""
    elif projection == "list":
        fields = {"marker": "", "enumerated": False}
        for enumerated, patterns in ((False, _BULLETS), (True, _NUMBERS)):
            match = next(
                (
                    m
                    for pattern in patterns
                    if (m := re.match(f"^({pattern})" + r"\s(.+)", original, re.DOTALL))
                ),
                None,
            )
            if match:
                fields = {"marker": match[1], "enumerated": enumerated}
                text = match[2]
                break
    elif projection != "text":
        fail()
    for segment in segments[1:]:
        continuation = segment_text(segment, evidence)
        stripped = continuation.lstrip()
        if text.endswith("\u00ad") or (
            text.endswith("-") and stripped and stripped[0].islower()
        ):
            text = text[:-1] + continuation
            original = original[:-1] + continuation
        else:
            text += " " + continuation
            original += " " + continuation
    return {**fields, "text": text, "orig": original}


def validate_lineage(units, evidence, records):
    """Every OCR textual output is covered once by exact bound-cell replay."""
    try:
        if len(records) > MAX_ENTRIES or len(canonical_bytes(records)) > 268435456:
            fail()
        by_ref = {record["native_ref"]: record for record in records}
        if len(by_ref) != len(records):
            fail()
        expected = set()
        for unit in units:
            raw = unit.raw_item
            if unit.text_origin != "ocr":
                continue
            if not unit.native_ref.startswith(("#/texts/", "#/tables/", "#/pictures/")):
                fail()
            if raw.get("annotations") or raw.get("graph"):
                fail()
            textual = (
                "text" in raw
                or "orig" in raw
                or raw.get("label") in ("table", "document_index")
            )
            if not textual:
                continue
            expected.add(unit.native_ref)
            record = by_ref[unit.native_ref]
            pages = {p.physical_page for p in unit.provenance}
            if record["kind"] == "text":
                fields = text_fields(record, evidence)
                projection = (
                    "list"
                    if raw["label"] == "list_item"
                    else "formula"
                    if raw["label"] == "formula"
                    else "text"
                )
                if record["projection"] != projection or any(
                    raw.get(key) != value for key, value in fields.items()
                ):
                    fail()
                segments = record["segments"]
                if tuple(segment["page"] for segment in segments) != tuple(
                    p.physical_page for p in unit.provenance
                ):
                    fail()
            elif record["kind"] == "table":
                if set(record) != {"native_ref", "kind", "cells"} or raw.get(
                    "label"
                ) not in ("table", "document_index"):
                    fail()
                cells = raw["data"]["table_cells"]
                segments = record["cells"]
                if len(cells) != len(segments) or any(
                    cell["text"] != segment_text(segment, evidence)
                    for cell, segment in zip(cells, segments, strict=True)
                ):
                    fail()
                if raw.get("text", "") or raw.get("orig", ""):
                    fail()
            else:
                fail()
            if segments and {segment["page"] for segment in segments} != pages:
                fail()
        if set(by_ref) != expected:
            fail()
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("Invalid ordered OCR cell lineage") from exc


def validate_parser_lineage(units, evidence, records):
    """Only proof violations become typed integrity failures at extraction."""
    from tychora.contracts.errors import TychoraError

    try:
        if records is None:
            fail()
        validate_lineage(units, evidence, records)
    except ValueError as exc:
        raise TychoraError("INTEGRITY_ERROR", "Invalid PDF OCR lineage proof") from exc


def evidence_document(pages, lineage, version):
    serialized = [page.model_dump(mode="json") for page in pages]
    if version is None:
        return serialized
    if version != VERSION:
        fail()
    return {"version": VERSION, "pages": serialized, "lineage": lineage}


def merge_lineage(exports, proofs):
    from .pdf_batches import DOMAINS

    offsets = dict.fromkeys(DOMAINS, 0)
    result = []
    for export, records in zip(exports, proofs, strict=True):
        for record in records:
            reference = record["native_ref"]
            if not isinstance(reference, str):
                fail()
            parts = reference.split("/")
            if (
                len(parts) != 3
                or parts[0] != "#"
                or parts[1] not in DOMAINS
                or re.fullmatch(r"0|[1-9][0-9]*", parts[2]) is None
            ):
                fail()
            _, domain, number = parts
            index = int(number)
            if (
                index >= len(export.get(domain, ()))
                or export[domain][index]["self_ref"] != reference
            ):
                fail()
            result.append(
                {**record, "native_ref": f"#/{domain}/{offsets[domain] + index}"}
            )
        for domain in DOMAINS:
            offsets[domain] += len(export.get(domain, ()))
    return result
