"""Content digests for immutable index batch receipts."""

from hashlib import sha256

from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.indexing import IndexRecordDigest


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
