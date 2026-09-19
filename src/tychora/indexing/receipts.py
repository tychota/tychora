"""Content digests for immutable index batch receipts."""

from hashlib import sha256

from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.indexing import IndexRecordDigest


def vector_value_sha(stored_float32: bytes) -> str:
    """Hash the exact normalized little-endian float32 bytes accepted by storage."""
    if not stored_float32 or len(stored_float32) % 4:
        raise ValueError("Stored vector bytes must contain complete float32 values")
    return sha256(stored_float32).hexdigest()


def graph_value_sha(record: JsonValue) -> str:
    """Hash one complete canonical graph record without lossy normalization."""
    return sha256(canonical_bytes(record)).hexdigest()


def receipt_digest(records: tuple[IndexRecordDigest, ...]) -> str:
    """Hash complete record/input/value triples in stable record order."""
    if len({record.record_id for record in records}) != len(records):
        raise ValueError("Duplicate index record identity")
    triples = sorted(
        (record.record_id, record.input_sha, record.value_sha) for record in records
    )
    return digest("receipt-v1", triples)
