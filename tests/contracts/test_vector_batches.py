"""One artifact per vector batch, verified row by row."""

import json
import struct
from hashlib import sha256

import pytest

from tychora.contracts.errors import TychoraError
from tychora.contracts.vector_batches import (
    VECTOR_BATCH_MAGIC,
    VectorBatchRow,
    decode_vector_batch,
    encode_vector_batch,
    row_value_sha,
    vector_value_sha,
)


def _rows():
    return (
        VectorBatchRow(record_id="b" * 64, input_sha="1" * 64, values=(0.0, 1.0)),
        VectorBatchRow(record_id="a" * 64, input_sha="2" * 64, values=(1.0, 0.0)),
    )


def test_layout_is_magic_header_then_rows_in_record_order():
    raw = encode_vector_batch("f" * 64, 2, _rows())
    assert raw[:8] == VECTOR_BATCH_MAGIC
    (length,) = struct.unpack("<I", raw[8:12])
    header = json.loads(raw[12 : 12 + length])
    assert header == {
        "dimension": 2,
        "family_id": "f" * 64,
        "records": [
            {
                "input_sha": "2" * 64,
                "record_id": "a" * 64,
                "value_sha": row_value_sha((1.0, 0.0)),
            },
            {
                "input_sha": "1" * 64,
                "record_id": "b" * 64,
                "value_sha": row_value_sha((0.0, 1.0)),
            },
        ],
    }
    body = raw[12 + length :]
    assert struct.unpack("<4f", body) == (1.0, 0.0, 0.0, 1.0)


def test_round_trip_preserves_rows():
    decoded = decode_vector_batch(encode_vector_batch("f" * 64, 2, _rows()))
    assert decoded.family_id == "f" * 64
    assert [row.record_id for row in decoded.rows] == ["a" * 64, "b" * 64]
    assert decoded.rows[0].values == (1.0, 0.0)


def test_value_sha_is_one_row_float32_bytes():
    assert row_value_sha((1.0, 0.0)) == sha256(struct.pack("<2f", 1.0, 0.0)).hexdigest()


@pytest.mark.parametrize("damage", ["magic", "row", "truncate", "dimension"])
def test_damaged_batch_is_an_integrity_error(damage):
    raw = bytearray(encode_vector_batch("f" * 64, 2, _rows()))
    if damage == "magic":
        raw[0:8] = b"TYVB0002"
    elif damage == "row":
        raw[-1] ^= 0x01
    elif damage == "truncate":
        raw = raw[:-4]
    else:
        raw = bytearray(
            encode_vector_batch("f" * 64, 2, _rows()).replace(
                b'"dimension":2', b'"dimension":3'
            )
        )
    with pytest.raises(TychoraError) as error:
        decode_vector_batch(bytes(raw))
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason in {"payload.hash_mismatch", "canonical.row_invalid"}


def test_rows_must_match_dimension():
    with pytest.raises(ValueError, match="dimension"):
        encode_vector_batch("f" * 64, 3, _rows())


def test_empty_batch_encodes_header_only():
    raw = encode_vector_batch("f" * 64, 2, ())
    assert decode_vector_batch(raw).rows == ()


def test_vector_value_sha_rejects_partial_floats():
    with pytest.raises(ValueError, match="float32"):
        vector_value_sha(b"abc")
