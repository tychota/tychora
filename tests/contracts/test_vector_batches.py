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


def _with_header(header: bytes) -> bytes:
    return VECTOR_BATCH_MAGIC + struct.pack("<I", len(header)) + header


def _canonical(**override):
    header = {"dimension": 2, "family_id": "f" * 64, "records": []}
    header.update(override)
    return _with_header(
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    )


def _record(record_id, value_sha=None):
    return {
        "input_sha": "1" * 64,
        "record_id": record_id,
        "value_sha": value_sha or row_value_sha((0.0, 1.0)),
    }


_ROW = struct.pack("<2f", 0.0, 1.0)


def _crafted():
    bad_length = VECTOR_BATCH_MAGIC + struct.pack("<I", 10_000) + b"{}"
    dup = _canonical(records=[_record("a"), _record("a")]) + _ROW * 2
    return {
        "deep": _with_header(b"[" * 100_000),
        "nan": _with_header(b'{"dimension":NaN,"family_id":"x","records":[]}'),
        "infinity": _with_header(
            b'{"dimension":Infinity,"family_id":"x","records":[]}'
        ),
        "length_beyond_file": bad_length,
        "malformed": _with_header(b"{not json"),
        "non_canonical": _with_header(
            b'{ "dimension": 2, "family_id": "x", "records": [] }'
        ),
        "duplicate_ids": dup,
        "bool_dimension": _canonical(dimension=True),
        "float_dimension": _canonical(dimension=2.0),
        "str_dimension": _canonical(dimension="2"),
        "int_family": _canonical(family_id=7),
        "dict_records": _canonical(records={}),
    }


@pytest.mark.parametrize("name", sorted(_crafted()))
def test_crafted_header_is_a_typed_integrity_error(name):
    with pytest.raises(TychoraError) as error:
        decode_vector_batch(_crafted()[name])
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason == "canonical.row_invalid"
