"""Real vector storage contract."""

import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tychora.adapters.chroma import index as chroma_index
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.vectors import VectorBatch, WorkLimits


def record(
    record_id: str, values: tuple[float, float], space: str, *, input_byte: str = "a"
) -> EmbeddingRecord:
    raw = struct.pack("<ff", *values)
    return EmbeddingRecord.from_normalized_bytes(
        record_id=record_id,
        input_sha=input_byte * 64,
        space_id=space,
        stored=raw,
    )


def test_only_eligible_owners_enter_vector_candidates(
    vector_index, vector_family, execution
):
    vector_index.upsert_batch(
        vector_family.batch(
            [("hidden", "u0", (1.0, 0.0)), ("visible", "u1", (0.8, 0.6))]
        ),
        execution.ctx,
    )
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        {"visible": ("unit", "u1")},
        1,
        vector_family.limits,
        execution.ctx,
    )
    assert [candidate.key for candidate in page.candidates] == [("unit", "u1")]
    assert page.coverage_complete
    assert not page.truncated


def test_missing_expected_records_leave_incomplete_coverage(
    vector_index, vector_family, execution
):
    vector_index.upsert_batch(
        vector_family.batch([("present", "u1", (1.0, 0.0))]), execution.ctx
    )
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        {"present": ("unit", "u1"), "missing": ("unit", "u2")},
        2,
        vector_family.limits,
        execution.ctx,
    )
    assert [candidate.key for candidate in page.candidates] == [("unit", "u1")]
    assert page.missing_record_ids == ("missing",)
    assert not page.coverage_complete


def test_empty_eligibility_stays_empty(vector_index, vector_family, execution):
    vector_index.upsert_batch(
        vector_family.batch([("stored", "u1", (1.0, 0.0))]), execution.ctx
    )
    page = vector_index.search(
        vector_family.query((1.0, 0.0)), {}, 1, vector_family.limits, execution.ctx
    )
    assert page.candidates == ()
    assert page.exhausted
    assert page.returned_records == 0


def test_drop_records_invalidates_receipt(vector_index, vector_family, execution):
    receipt = vector_index.upsert_batch(
        vector_family.batch([("stored", "u1", (1.0, 0.0))]), execution.ctx
    )
    vector_index.drop_records(["stored"], execution.ctx)
    verification = vector_index.verify_batch(receipt)
    assert not verification.ready
    assert verification.missing_record_ids == ("stored",)


def test_immutable_retry_and_reopened_exact_bytes(
    vector_index, vector_family, execution, tmp_path
):
    batch = vector_family.batch([("a", "u1", (0.8, 0.6)), ("b", "u2", (0.0, -1.0))])
    receipt = vector_index.upsert_batch(batch, execution.ctx)
    assert vector_index.verify_batch(receipt).ready
    assert vector_index.upsert_batch(batch, execution.ctx) == receipt
    reopened = ChromaVectorIndex(tmp_path / "vectors")
    reopened.create_family(vector_family.spec)
    assert reopened.verify_batch(receipt).ready
    with pytest.raises(ValueError, match="immutable"):
        vector_index.upsert_batch(
            VectorBatch(
                family_id=batch.family_id,
                records=(record("a", (1.0, 0.0), batch.records[0].space_id),),
            ),
            execution.ctx,
        )


def test_receipt_rejects_tamper_and_backend_config_mismatch(
    vector_index, vector_family, execution, tmp_path
):
    from tychora.contracts.vectors import BatchReceipt

    receipt = vector_index.upsert_batch(
        vector_family.batch([("a", "u1", (1.0, 0.0))]), execution.ctx
    )
    changed = BatchReceipt.model_validate({**receipt.model_dump(), "digest": "0" * 64})
    assert not vector_index.verify_batch(changed).ready
    other = ChromaVectorIndex(tmp_path / "vectors")
    with pytest.raises(ValueError, match="configuration"):
        other.create_family(
            vector_family.spec.model_copy(update={"backend_space": "cosine"})
        )
    vector_index._collection.modify(metadata={"physical_space": "cosine"})
    with pytest.raises(ValueError, match="configuration"):
        ChromaVectorIndex(tmp_path / "vectors").create_family(vector_family.spec)


def test_shared_return_budget_truncates_owner_pool(
    vector_index, vector_family, execution
):
    vector_index.upsert_batch(
        vector_family.batch(
            [(f"r{i}", "same", (1.0, 0.0)) for i in range(20)]
            + [("unique", "other", (0.8, 0.6))]
        ),
        execution.ctx,
    )
    eligible = {f"r{i}": ("unit", "same") for i in range(20)}
    eligible["unique"] = ("unit", "other")
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        eligible,
        2,
        WorkLimits(max_returned_records=4),
        execution.ctx,
    )
    assert page.truncated
    assert page.returned_records <= 4


def test_later_adaptive_request_reports_budget_clip(
    vector_index, vector_family, execution
):
    rows = [(f"same-{i}", "same", (1.0, 0.0)) for i in range(4)]
    rows.append(("other", "other", (0.8, 0.6)))
    rows.extend((f"worse-{i}", f"worse-{i}", (0.0, 1.0)) for i in range(16))
    vector_index.upsert_batch(vector_family.batch(rows), execution.ctx)
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        {record_id: ("unit", owner) for record_id, owner, _ in rows},
        2,
        WorkLimits(max_returned_records=10),
        execution.ctx,
    )
    assert page.truncated
    assert page.returned_records == 10


def test_scope_above_partition_cap_is_rejected(vector_index, vector_family, execution):
    eligible = {str(i): ("unit", str(i)) for i in range(320_001)}
    with pytest.raises(ValueError, match="partition"):
        vector_index.search(
            vector_family.query((1.0, 0.0)),
            eligible,
            1,
            vector_family.limits,
            execution.ctx,
        )


def test_cancelled_search_does_not_query_store(vector_index, vector_family, execution):
    vector_index.upsert_batch(
        vector_family.batch([("a", "u1", (1.0, 0.0))]), execution.ctx
    )
    execution.ctx.cancelled.set()
    with pytest.raises(TychoraError, match="cancelled"):
        vector_index.search(
            vector_family.query((1.0, 0.0)),
            {"a": ("unit", "u1")},
            1,
            vector_family.limits,
            execution.ctx,
        )


def test_cancellation_stops_presence_scan_between_native_reads(
    vector_index, vector_family, execution, monkeypatch
):
    vector_index.upsert_batch(
        vector_family.batch([("present", "u1", (1.0, 0.0))]), execution.ctx
    )
    eligible = {"present": ("unit", "u1")}
    eligible.update({f"missing-{i}": ("unit", f"u{i}") for i in range(5_000)})
    native_get = vector_index._collection.get
    calls = 0

    def cancel_after_first_read(*args, **kwargs):
        nonlocal calls
        result = native_get(*args, **kwargs)
        calls += 1
        if calls == 1:
            execution.ctx.cancelled.set()
        return result

    monkeypatch.setattr(vector_index._collection, "get", cancel_after_first_read)
    with pytest.raises(TychoraError, match="cancelled"):
        vector_index.search(
            vector_family.query((1.0, 0.0)),
            eligible,
            1,
            vector_family.limits,
            execution.ctx,
        )
    assert calls == 1


def test_work_deadline_stops_presence_scan_without_claiming_coverage(
    vector_index, vector_family, execution, monkeypatch
):
    vector_index.upsert_batch(
        vector_family.batch([("present", "u1", (1.0, 0.0))]), execution.ctx
    )
    eligible = {"present": ("unit", "u1")}
    eligible.update({f"missing-{i}": ("unit", f"u{i}") for i in range(5_000)})
    native_get = vector_index._collection.get
    calls = 0
    clock = [0.0]
    monkeypatch.setattr(
        chroma_index, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def expire_after_first_read(*args, **kwargs):
        nonlocal calls
        result = native_get(*args, **kwargs)
        calls += 1
        if calls == 1:
            clock[0] = 2.0
        return result

    monkeypatch.setattr(vector_index._collection, "get", expire_after_first_read)
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        eligible,
        1,
        WorkLimits(deadline=1.0),
        execution.ctx,
    )
    assert calls == 1
    assert page.candidates == ()
    assert page.truncated
    assert not page.coverage_complete
    assert not page.exhausted


def test_writer_exit_then_reader_verifies_varied_float32_bytes(tmp_path, vector_family):
    script = """
import math, os, struct, sys
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import VectorBatch
from tychora.contracts.identity import space_id
from tychora.ports.execution import ExecutionContext
spec = FamilySpec.model_validate_json(sys.argv[2])
space = space_id(spec.modality, spec.provider, spec.model, spec.dimensions,
                 spec.metric, spec.representation_fingerprint)
records = []
for i in range(1200):
    angle = i * 0.017
    values = (-0.0, 1.0) if i == 0 else (math.cos(angle), math.sin(angle))
    records.append(EmbeddingRecord.from_normalized_bytes(
        record_id=f"record-{i}", input_sha="a" * 64, space_id=space,
        stored=struct.pack("<ff", *values)))
index = ChromaVectorIndex(sys.argv[1])
index.create_family(spec)
receipt = index.upsert_batch(VectorBatch(family_id=spec.family_id, records=tuple(records)), ExecutionContext())
print(receipt.model_dump_json())
sys.stdout.flush()
os._exit(0)
"""
    path = tmp_path / "writer-exit"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), vector_family.spec.model_dump_json()],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    from tychora.contracts.vectors import BatchReceipt

    receipt = BatchReceipt.model_validate_json(completed.stdout)
    reader = ChromaVectorIndex(path)
    reader.create_family(vector_family.spec)
    assert reader.verify_batch(receipt).ready
    raw = reader._present(["record-0"], embeddings=True)["record-0"][2]
    assert raw == struct.pack("<ff", -0.0, 1.0)
