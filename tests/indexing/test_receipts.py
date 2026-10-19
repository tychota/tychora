import struct

import pytest
from pydantic import ValidationError

from tychora.contracts.indexing import FamilySpec, IndexRecordDigest
from tychora.contracts.vector_batches import vector_value_sha
from tychora.indexing.receipts import graph_value_sha, receipt_digest


def test_receipt_digest_sorts_complete_record_triples() -> None:
    records = (
        IndexRecordDigest(record_id="record-b", input_sha="c" * 64, value_sha="d" * 64),
        IndexRecordDigest(record_id="record-a", input_sha="a" * 64, value_sha="b" * 64),
    )

    assert receipt_digest(records) == (
        "e65a235a7b8291752178840e6c5783af1d93024f84fee84bc646974915894fd1"
    )
    assert receipt_digest(tuple(reversed(records))) == receipt_digest(records)


def test_receipt_digest_changes_with_input_or_value_identity() -> None:
    original = (
        IndexRecordDigest(record_id="record", input_sha="a" * 64, value_sha="b" * 64),
    )

    assert receipt_digest(original) != receipt_digest(
        (IndexRecordDigest(record_id="record", input_sha="c" * 64, value_sha="b" * 64),)
    )
    assert receipt_digest(original) != receipt_digest(
        (IndexRecordDigest(record_id="record", input_sha="a" * 64, value_sha="d" * 64),)
    )
    with pytest.raises(ValueError, match="Duplicate index record"):
        receipt_digest((*original, *original))


def test_family_identity_reuses_job_nonce_but_distinguishes_fresh_family() -> None:
    from tychora.contracts.identity import family_id

    first_nonce = "00000000-0000-4000-8000-000000000001"
    retry = family_id("workspace-a", "config-a", first_nonce)

    assert retry == "c2dc8b00f26437685e9051f5a36d64817d01b5be12baa0de8034aff45553f4a3"
    assert family_id("workspace-a", "config-a", first_nonce) == retry
    assert (
        family_id("workspace-a", "config-a", "00000000-0000-4000-8000-000000000002")
        != retry
    )


def test_value_hashes_use_exact_stored_float32_bytes_and_canonical_graph_record() -> (
    None
):
    normalized_float32 = struct.pack("<ff", 0.6, 0.8)
    graph_record = {
        "assertion_id": "a",
        "predicate": "contains@1",
        "supports": ["u1", "u2"],
    }

    assert vector_value_sha(normalized_float32) == (
        "9d2c143dc5e2c8df311211d38cb9ed78357a179827854314aec553e5da319145"
    )
    assert graph_value_sha(graph_record) == (
        "29de4aee7858aa40fcc58b20b0a1d18c64a105db9932479ba2fa85c3dbe83e09"
    )
    assert vector_value_sha(struct.pack(">ff", 0.6, 0.8)) != vector_value_sha(
        normalized_float32
    )


def test_family_spec_binds_explicit_metric_config_and_creation_uuid() -> None:
    from tychora.contracts.identity import family_id

    creation_uuid = "00000000-0000-4000-8000-000000000001"
    expected = family_id("workspace-a", "a" * 64, creation_uuid)
    spec = FamilySpec(
        family_id=expected,
        workspace_id="workspace-a",
        config_sha="a" * 64,
        creation_uuid=creation_uuid,
        modality="text",
        provider="voyage",
        model="voyage-context-4",
        dimensions=1024,
        metric="cosine",
        normalization="unit-l2",
        representation_fingerprint="b" * 64,
        backend_namespace="text-family-a",
    )

    assert spec.family_id == expected
    with pytest.raises(ValidationError, match="Family identity"):
        FamilySpec.model_validate({**spec.model_dump(), "family_id": "c" * 64})
