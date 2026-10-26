"""Rebuild one batch's contents from SQL rows and its input manifest for native recovery.

Every row is checked against the batch's receipt and input manifest, so drift
in SQL is refused rather than served. Lexical records carry their chunk IDs
only: their text stays in the FTS index.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

from tychora.adapters.files.references import MAX_MANIFEST_BYTES
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import (
    GraphBatchReceipt,
    GraphManifest,
    graph_manifest_digest,
)
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.publication import (
    BatchReceiptVariant,
    LexicalBatchReceipt,
    LexicalRecord,
    RetainedBatch,
    SnapshotView,
    VectorRecordBinding,
    receipt_count,
)
from tychora.contracts.snapshots import SlotFamily
from tychora.contracts.vectors import BatchReceipt
from tychora.ports.artifacts import Artifacts
from tychora.ports.repositories import BatchRow

from .repositories import SqliteBatchRepository

_RECEIPTS: dict[str, type[BatchReceiptVariant]] = {
    "lexical": LexicalBatchReceipt,
    "text": BatchReceipt,
    "image": BatchReceipt,
    "graph": GraphBatchReceipt,
}


def _invalid(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


def _differs(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.binding_mismatch")


def _input(artifacts: Artifacts, ref: ArtifactRef) -> dict[str, Any]:
    if ref.size_bytes > MAX_MANIFEST_BYTES:
        raise _invalid("Batch input manifest exceeds its byte limit")
    with artifacts.open_verified(ref) as stream:
        raw = stream.read(ref.size_bytes + 1)
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise _invalid("Batch input manifest is malformed") from None
    if (
        not isinstance(document, dict)
        or canonical_bytes(document) != raw
        or document.get("type") != "index_input"
        or document.get("schema_version") != "1"
        or not isinstance(document.get("data"), dict)
        or not isinstance(document["data"].get("entries"), list)
    ):
        raise _invalid("Batch input manifest is not a canonical index input")
    return document["data"]


def _receipt(db: sqlite3.Connection, item: SlotFamily, row: BatchRow):
    stored = db.execute(
        "SELECT receipt_json FROM index_batches WHERE batch_id=?", (row.batch_id,)
    ).fetchone()
    try:
        receipt = _RECEIPTS[item.slot].model_validate_json(stored[0])
    except (KeyError, TypeError, ValueError):
        raise _invalid("Index batch receipt is invalid") from None
    if receipt.family_id != row.family_id or receipt_count(receipt) != (
        row.expected_count
    ):
        raise _differs("Index batch SQL differs from its receipt")
    return receipt


def _lexical(
    db: sqlite3.Connection, batch_id: str, entries: list[str]
) -> tuple[LexicalRecord, ...]:
    """Return the batch's lexical records by chunk ID, in input order."""
    stored = [
        row[0]
        for row in db.execute(
            "SELECT chunk_id FROM lexical_records WHERE batch_id=?", (batch_id,)
        )
    ]
    if set(stored) != set(entries) or len(stored) != len(entries):
        raise _differs("Lexical SQL differs from its batch input")
    return tuple(LexicalRecord(chunk_id=chunk_id) for chunk_id in entries)


def _complete_groups(representations: list, entries: list) -> bool:
    """True when every entry has one group with sorted, non-empty dependencies."""
    try:
        return [item["record_id"] for item in representations] == entries and all(
            isinstance(item["group_id"], str)
            and item["dependency_ids"]
            and item["dependency_ids"] == sorted(item["dependency_ids"])
            for item in representations
        )
    except (KeyError, TypeError):
        return False


def _groups(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a text batch's group representations by record, all present and paired."""
    representations = data.get("representations")
    if not isinstance(representations, list) or not _complete_groups(
        representations, data["entries"]
    ):
        raise TychoraError(
            "INDEX_INCOMPLETE",
            "Text batch needs rebuilt group ownership",
            reason="batch.missing",
        )
    return {item["record_id"]: item for item in representations}


def _vectors(
    db: sqlite3.Connection,
    item: SlotFamily,
    batch_id: str,
    receipt: BatchReceipt,
    data: dict[str, Any],
) -> tuple[VectorRecordBinding, ...]:
    sealed = {record.record_id: record for record in receipt.records}
    groups = _groups(data) if item.slot == "text" else {}
    rows = db.execute(
        "SELECT record_id,chunk_id,asset_id,input_sha,row_index,group_id FROM vector_records "
        "WHERE batch_id=? ORDER BY record_id",
        (batch_id,),
    ).fetchall()
    if [row[0] for row in rows] != sorted(sealed) or sorted(data["entries"]) != sorted(
        sealed
    ):
        raise _differs("Vector SQL differs from its batch receipt")
    bindings = []
    for record_id, chunk_id, asset_id, input_sha, row_index, group_id in rows:
        group = groups.get(record_id, {})
        if input_sha != sealed[record_id].input_sha or group_id != group.get(
            "group_id"
        ):
            raise _differs("Vector SQL differs from its batch receipt")
        bindings.append(
            VectorRecordBinding(
                record_id=record_id,
                input_sha=input_sha,
                value_sha=sealed[record_id].value_sha,
                chunk_id=chunk_id,
                asset_id=asset_id,
                row_index=row_index,
                group_id=group.get("group_id"),
                dependency_ids=frozenset(group.get("dependency_ids", ())),
            )
        )
    return tuple(bindings)


def _graph(receipt: GraphBatchReceipt, data: dict[str, Any]) -> GraphManifest:
    try:
        manifest = GraphManifest.model_validate_json(
            canonical_bytes(data["graph_manifest"])
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid("Graph batch input has no generation manifest") from None
    if receipt.manifest_digest != graph_manifest_digest(manifest) or data[
        "entries"
    ] != sorted(manifest.evidence_ids):
        raise _differs("Graph batch input differs from its receipt")
    return manifest


def _batch(
    db: sqlite3.Connection, artifacts: Artifacts, item: SlotFamily, row: BatchRow
) -> RetainedBatch:
    receipt = _receipt(db, item, row)
    data = _input(artifacts, row.input_manifest)
    try:
        return RetainedBatch(
            batch_id=row.batch_id,
            family_id=row.family_id,
            extraction_id=row.extraction_id,
            input_manifest=row.input_manifest,
            expected_count=row.expected_count,
            receipt=receipt,
            vectors=row.vectors,
            graph_manifest=_graph(receipt, data)
            if isinstance(receipt, GraphBatchReceipt)
            else None,
            lexical_records=_lexical(db, row.batch_id, list(data["entries"]))
            if isinstance(receipt, LexicalBatchReceipt)
            else (),
            vector_records=_vectors(db, item, row.batch_id, receipt, data)
            if isinstance(receipt, BatchReceipt)
            else (),
        )
    except ValueError:
        raise _differs("Retained batch rows differ from their artifact") from None


def iter_retained_batches(
    db: sqlite3.Connection,
    artifacts: Artifacts,
    view: SnapshotView,
    *,
    slots: frozenset[str] | None = None,
    before_each: Callable[[], None] | None = None,
) -> Iterator[tuple[SlotFamily, RetainedBatch]]:
    """Yield the batches of ``view`` one at a time, each read fresh from SQL.

    Nothing is cached and only the batch being yielded is held, so a caller
    that drops it keeps memory bounded by one batch. ``before_each`` runs
    before each batch is read, so a caller can stop between batches.
    """
    rows = SqliteBatchRepository(db, writable=False)
    for item in view.families:
        if slots is not None and item.slot not in slots:
            continue
        for row in rows.for_snapshot(view.snapshot_id, item.slot):
            if before_each is not None:
                before_each()
            yield item, _batch(db, artifacts, item, row)
