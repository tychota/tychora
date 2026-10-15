"""Versioned SDK spans and final ownership, replayed from source-bound cells.

Raw spans belong to SDK assembly steps, not a single final string. Legacy mapping
deliberately does not enter this module. Native and OCR evidence stay disjoint.
"""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from tychora.contracts.identity import canonical_bytes

from .ocr_lineage import MAX_ENTRIES, merge_lineage, replay_text, segment_text

VERSION = "docling-assembly@1"


@dataclass(frozen=True)
class CoordinateProof:
    evidence: dict[int, Any]
    records: tuple[Any, ...]
    native_pages: frozenset[int]


def coordinate_proof(version, evidence, records, native=None):
    try:
        return _coordinate_proof(version, evidence, records, native)
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("Invalid PDF coordinate assembly proof") from exc


def _coordinate_proof(version, evidence, records, native):
    if version is None:
        if native is not None:
            raise ValueError("Legacy PDF cannot carry native coordinate proof")
        return None
    if version != VERSION or records is None:
        raise ValueError("Missing or unsupported PDF coordinate proof")
    pages = dict(evidence)
    native_pages = set()
    native_records = ()
    if native is not None:
        if set(native) != {"pages", "lineage"}:
            raise ValueError("Invalid native assembly proof")
        if len(canonical_bytes(native)) > 268435456:
            raise ValueError("Native assembly proof exceeds capacity")
        native_records = native["lineage"]
        for page in native["pages"]:
            number = page["physical_page"]
            if (
                set(page) != {"physical_page", "cells"}
                or type(number) is not int
                or number < 1
                or number in pages
                or len(page["cells"]) > MAX_ENTRIES
            ):
                raise ValueError("Invalid native assembly page")
            for index, cell in enumerate(page["cells"]):
                if cell.get("index") != index or cell.get("from_ocr") is not False:
                    raise ValueError("Native assembly requires native cells")
            pages[number] = SimpleNamespace(cells=page["cells"])
            native_pages.add(number)
    combined = tuple(records) + tuple(native_records)
    if len(combined) > MAX_ENTRIES or len(canonical_bytes(combined)) > 268435456:
        raise ValueError("PDF assembly proof exceeds capacity")
    return CoordinateProof(pages, combined, frozenset(native_pages))


def project_document(document, proof):
    """Verify complete text coverage and exact fields/spans before deriving ranges."""
    try:
        return _project_document(document, proof)
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("Invalid PDF coordinate assembly proof") from exc


def _project_document(document, proof):
    if proof is None:
        return {}
    records = {record["native_ref"]: record for record in proof.records}
    if len(records) != len(proof.records):
        raise ValueError("Repeated assembly record")
    objects = {
        item["self_ref"]: item
        for domain in ("texts", "tables", "pictures", "key_value_items", "form_items")
        for item in document.get(domain, ())
    }
    if not records.keys() <= objects.keys():
        raise ValueError("Assembly record references missing item")
    projected = {}
    for ref, item in objects.items():
        record = records.get(ref)
        if not isinstance(item.get("text"), str):
            if record is not None:
                if (
                    set(record) != {"native_ref", "kind", "cells"}
                    or record["kind"] != "table"
                    or item["label"] not in ("table", "document_index")
                ):
                    raise ValueError("Unexpected nontext assembly record")
                cells = item["data"]["table_cells"]
                segments = record["cells"]
                if len(cells) != len(segments) or any(
                    cell["text"]
                    != segment_text(
                        segment,
                        proof.evidence,
                        from_ocr=segment["page"] not in proof.native_pages,
                    )
                    for cell, segment in zip(cells, segments, strict=True)
                ):
                    raise ValueError("Table assembly differs from captured cells")
                if segments and {s["page"] for s in segments} != {
                    p["page_no"] for p in item.get("prov", ())
                }:
                    raise ValueError(
                        "Table assembly page differs from retained provenance"
                    )
            continue
        if record is None or record["kind"] != "text":
            raise ValueError("Missing source-bound text assembly")
        projection = (
            "list"
            if item["label"] == "list_item"
            else ("formula" if item["label"] == "formula" else "text")
        )
        segments = record["segments"]
        pages = tuple(segment["page"] for segment in segments)
        is_native = bool(pages) and all(p in proof.native_pages for p in pages)
        if not is_native and any(p in proof.native_pages for p in pages):
            raise ValueError("Mixed-origin text assembly")
        fields, spans, intervals = replay_text(
            record, proof.evidence, from_ocr=not is_native
        )
        provenance = item.get("prov", ())
        if (
            projection != record["projection"]
            or any(item.get(key) != value for key, value in fields.items())
            or pages != tuple(p["page_no"] for p in provenance)
            or spans != tuple(tuple(p["charspan"]) for p in provenance)
            or any(
                not 0 <= start <= end <= len(fields["text"]) for start, end in intervals
            )
        ):
            raise ValueError("PDF text assembly coordinates differ from retained proof")
        projected[ref] = intervals
    if any(
        record["kind"] == "text" and ref not in projected
        for ref, record in records.items()
    ):
        raise ValueError("Unexpected text assembly record")
    return projected


def merge_coordinate_proofs(exports, proofs):
    if not proofs:
        return None
    if len(exports) != len(proofs) or any(proof is None for proof in proofs):
        raise ValueError("Coordinate batch coverage differs")
    evidence: dict[int, Any] = {}
    native = set()
    for proof in proofs:
        if evidence.keys() & proof.evidence.keys():
            raise ValueError("Repeated coordinate evidence page")
        evidence.update(proof.evidence)
        native.update(proof.native_pages)
    return CoordinateProof(
        evidence,
        tuple(merge_lineage(exports, [p.records for p in proofs])),
        frozenset(native),
    )


def batch_coordinate_proof(policy, pages, records, native):
    return coordinate_proof(
        policy.text_coordinates,
        {page["physical_page"]: SimpleNamespace(cells=page["cells"]) for page in pages},
        records,
        native,
    )


def merge_native_evidence(exports, batches):
    if len(exports) != len(batches):
        raise ValueError("Native assembly batch coverage differs")
    pages = [page for batch in batches for page in batch["pages"]]
    if len({page["physical_page"] for page in pages}) != len(pages):
        raise ValueError("Repeated native assembly page")
    return {
        "pages": pages,
        "lineage": merge_lineage(exports, [batch["lineage"] for batch in batches]),
    }
