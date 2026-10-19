"""Encode and verify one vector batch artifact (`TYVB0001`)."""

import json
import struct
from collections.abc import Sequence
from hashlib import sha256
from typing import Any

from tychora.contracts.base import Contract, Identifier, PositiveInt, Sha256
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes

VECTOR_BATCH_MAGIC = b"TYVB0001"
VECTOR_BATCH_MEDIA_TYPE = "application/vnd.tychora.vector-batch.v1"
_HEADER_LENGTH = struct.Struct("<I")
_PREFIX_BYTES = len(VECTOR_BATCH_MAGIC) + _HEADER_LENGTH.size
_MAX_HEADER_BYTES = 64 * 1024 * 1024


class VectorBatchRow(Contract):
    """One vector row: its record, the input it embeds and its values."""

    record_id: Identifier
    input_sha: Sha256
    values: tuple[float, ...]


class DecodedVectorBatch(Contract):
    """A verified vector batch: family, dimension and rows in record order."""

    family_id: Sha256
    dimension: PositiveInt
    rows: tuple[VectorBatchRow, ...]


def vector_value_sha(stored_float32: bytes) -> str:
    """Hash the exact normalized little-endian float32 bytes accepted by storage."""
    if not stored_float32 or len(stored_float32) % 4:
        raise ValueError("Stored vector bytes must contain complete float32 values")
    return sha256(stored_float32).hexdigest()


def _pack(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def row_value_sha(values: Sequence[float]) -> str:
    """Hash one row's little-endian float32 bytes, as receipts do."""
    return vector_value_sha(_pack(values))


def encode_vector_batch(
    family_id: str, dimension: int, rows: Sequence[VectorBatchRow]
) -> bytes:
    """Return the artifact bytes: magic, header length, header, float32 rows."""
    ordered = sorted(rows, key=lambda row: row.record_id)
    if any(len(row.values) != dimension for row in ordered):
        raise ValueError("Vector row length differs from the family dimension")
    header = canonical_bytes(
        {
            "family_id": family_id,
            "dimension": dimension,
            "records": [
                {
                    "record_id": row.record_id,
                    "input_sha": row.input_sha,
                    "value_sha": row_value_sha(row.values),
                }
                for row in ordered
            ],
        }
    )
    body = b"".join(_pack(row.values) for row in ordered)
    return VECTOR_BATCH_MAGIC + _HEADER_LENGTH.pack(len(header)) + header + body


def _invalid(message: str) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason="canonical.row_invalid")


def _header(raw: bytes) -> tuple[dict[str, Any], int]:
    if raw[: len(VECTOR_BATCH_MAGIC)] != VECTOR_BATCH_MAGIC or len(raw) < _PREFIX_BYTES:
        raise _invalid("Vector batch magic differs")
    (length,) = _HEADER_LENGTH.unpack(raw[len(VECTOR_BATCH_MAGIC) : _PREFIX_BYTES])
    end = _PREFIX_BYTES + length
    if length > _MAX_HEADER_BYTES or end > len(raw):
        raise _invalid("Vector batch header length is invalid")
    encoded = raw[_PREFIX_BYTES:end]
    try:
        header = json.loads(encoded)
    except ValueError:
        raise _invalid("Vector batch header is malformed") from None
    if not isinstance(header, dict) or canonical_bytes(header) != encoded:
        raise _invalid("Vector batch header is not canonical")
    return header, end


def _row(record: Any, chunk: bytes, dimension: int) -> VectorBatchRow:
    if not isinstance(record, dict) or vector_value_sha(chunk) != record.get(
        "value_sha"
    ):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Vector batch row differs from its digest",
            reason="payload.hash_mismatch",
        )
    try:
        return VectorBatchRow(
            record_id=record["record_id"],
            input_sha=record["input_sha"],
            values=struct.unpack(f"<{dimension}f", chunk),
        )
    except (KeyError, ValueError):
        raise _invalid("Vector batch record fields are invalid") from None


def decode_vector_batch(raw: bytes) -> DecodedVectorBatch:
    """Decode and verify every row against its recorded value digest."""
    header, offset = _header(raw)
    try:
        dimension = int(header["dimension"])
        records = list(header["records"])
        family_id = str(header["family_id"])
    except (KeyError, TypeError, ValueError):
        raise _invalid("Vector batch header fields are missing") from None
    width = 4 * dimension
    if dimension < 1 or len(raw) - offset != width * len(records):
        raise _invalid("Vector batch body length differs from its header")
    rows = tuple(
        _row(
            record,
            raw[offset + index * width : offset + (index + 1) * width],
            dimension,
        )
        for index, record in enumerate(records)
    )
    if [row.record_id for row in rows] != sorted(row.record_id for row in rows):
        raise _invalid("Vector batch records are not sorted")
    try:
        return DecodedVectorBatch(family_id=family_id, dimension=dimension, rows=rows)
    except ValueError:
        raise _invalid("Vector batch family is invalid") from None
