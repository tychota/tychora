"""Real vector storage contract."""

import math
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import tychora
from tychora.adapters.chroma import index as chroma_index
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.errors import TychoraError
from tychora.contracts.vectors import VectorBatch, VectorOwner


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


def test_only_granted_revisions_enter_vector_owners(
    vector_index, vector_family, execution
):
    vector_index.upsert_batch(
        vector_family.batch([("hidden", "u0", (1.0, 0.0))], revision="hidden-rev"),
        execution.ctx,
    )
    vector_index.upsert_batch(
        vector_family.batch([("visible", "u1", (0.8, 0.6))], revision="visible-rev"),
        execution.ctx,
    )
    result = vector_index.search(
        vector_family.view("visible-rev"),
        vector_family.query((1.0, 0.0)),
        1,
        execution.ctx,
    )
    assert [owner.key for owner in result.owners] == [("unit", "u1")]
    assert result.owners[0].window_ids == ("visible",)
    assert not result.truncated


def test_records_without_owner_metadata_are_never_returned(
    vector_index, vector_family, execution, monkeypatch
):
    vector_index.upsert_batch(
        vector_family.batch([("bare", "u0", (1.0, 0.0))]), execution.ctx
    )
    vector_index.upsert_batch(
        vector_family.batch([("owned", "u1", (0.8, 0.6))], revision="rev"),
        execution.ctx,
    )
    # Above the filter limit the store is queried unfiltered.
    monkeypatch.setattr(chroma_index, "REVISION_FILTER_LIMIT", 0)
    result = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 2, execution.ctx
    )
    assert [owner.key for owner in result.owners] == [("unit", "u1")]


def test_empty_eligibility_stays_empty(vector_index, vector_family, execution):
    vector_index.upsert_batch(
        vector_family.batch([("stored", "u1", (1.0, 0.0))], revision="rev"),
        execution.ctx,
    )
    result = vector_index.search(
        vector_family.view(), vector_family.query((1.0, 0.0)), 1, execution.ctx
    )
    assert result.owners == ()
    assert result.detail == {
        "returned": 0,
        "queries": 0,
        "rescored": 0,
        "exhausted": 1,
    }


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


def test_overfetch_cap_truncates_owner_pool(
    vector_index, vector_family, execution, monkeypatch
):
    vector_index.upsert_batch(
        vector_family.batch(
            [(f"r{i}", "same", (1.0, 0.0)) for i in range(20)]
            + [("unique", "other", (0.8, 0.6))],
            revision="rev",
        ),
        execution.ctx,
    )
    monkeypatch.setattr(chroma_index, "OVERFETCH_CAP", 8)
    result = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 2, execution.ctx
    )
    assert [owner.key for owner in result.owners] == [("unit", "same")]
    assert result.truncated
    assert result.detail["returned"] == 8
    assert result.detail["queries"] == 1


def test_overfetch_doublings_stop_and_truncate(
    vector_index, vector_family, execution, monkeypatch
):
    rows = [(f"same-{i}", "same", (1.0, 0.0)) for i in range(20)]
    rows.append(("other", "other", (0.8, 0.6)))
    vector_index.upsert_batch(vector_family.batch(rows, revision="rev"), execution.ctx)
    monkeypatch.setattr(chroma_index, "OVERFETCH_DOUBLINGS", 1)
    result = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 2, execution.ctx
    )
    # 8 then 16 records, all of one owner: the doublings ran out first.
    assert result.detail["queries"] == 2
    assert result.detail["returned"] == 16
    assert result.truncated
    monkeypatch.setattr(chroma_index, "OVERFETCH_DOUBLINGS", 2)
    deeper = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 2, execution.ctx
    )
    assert [owner.key[1] for owner in deeper.owners] == ["same", "other"]
    assert not deeper.truncated


def test_revision_filter_applies_up_to_its_limit(
    vector_index, vector_family, execution, monkeypatch
):
    rows = [(f"r{i}", f"u{i}", (1.0, 0.0)) for i in range(10)]
    vector_index.upsert_batch(vector_family.batch(rows, revision="rev"), execution.ctx)
    collection, _ = vector_index._require()
    native_query = collection.query
    filters = []

    def spy(*args, **kwargs):
        filters.append(kwargs["where"])
        return native_query(*args, **kwargs)

    monkeypatch.setattr(collection, "query", spy)
    query = vector_family.query((1.0, 0.0))
    vector_index.search(vector_family.view("rev"), query, 1, execution.ctx)
    monkeypatch.setattr(chroma_index, "REVISION_FILTER_LIMIT", 0)
    vector_index.search(vector_family.view("rev"), query, 1, execution.ctx)
    # Once the 10 stored records fit in the page, it is unfiltered and exact.
    monkeypatch.setattr(chroma_index, "REVISION_FILTER_LIMIT", 512)
    full = vector_index.search(vector_family.view("rev"), query, 3, execution.ctx)
    assert filters == [{"revision_id": {"$in": ["rev"]}}, None, None]
    assert full.detail["exhausted"] == 1


def test_owner_metadata_without_a_known_kind_is_refused(
    vector_index, vector_family, execution
):
    vector_index.upsert_batch(
        vector_family.batch([("a", "u1", (1.0, 0.0))], revision="rev"), execution.ctx
    )
    collection, _ = vector_index._require()
    collection.update(
        ids=["a"],
        metadatas=[
            {
                "input_sha": "a" * 64,
                "value_sha": "b" * 64,
                "revision_id": "rev",
                "owner_kind": "chunk",
                "owner_id": "u1",
            }
        ],
    )
    with pytest.raises(TychoraError) as refused:
        vector_index.search(
            vector_family.view("rev"),
            vector_family.query((1.0, 0.0)),
            1,
            execution.ctx,
        )
    assert refused.value.code == "INTEGRITY_ERROR"
    assert refused.value.reason == "canonical.binding_mismatch"


def test_cancelled_search_does_not_query_store(vector_index, vector_family, execution):
    vector_index.upsert_batch(
        vector_family.batch([("a", "u1", (1.0, 0.0))], revision="rev"), execution.ctx
    )
    execution.ctx.cancelled.set()
    with pytest.raises(TychoraError, match="cancelled"):
        vector_index.search(
            vector_family.view("rev"),
            vector_family.query((1.0, 0.0)),
            1,
            execution.ctx,
        )


def test_cancellation_stops_overfetch_between_native_queries(
    vector_index, vector_family, execution, monkeypatch
):
    rows = [(f"same-{i}", "same", (1.0, 0.0)) for i in range(40)]
    vector_index.upsert_batch(vector_family.batch(rows, revision="rev"), execution.ctx)
    collection, _ = vector_index._require()
    native_query = collection.query
    calls = 0

    def cancel_after_first_query(*args, **kwargs):
        nonlocal calls
        result = native_query(*args, **kwargs)
        calls += 1
        execution.ctx.cancelled.set()
        return result

    monkeypatch.setattr(collection, "query", cancel_after_first_query)
    with pytest.raises(TychoraError, match="cancelled"):
        vector_index.search(
            vector_family.view("rev"),
            vector_family.query((1.0, 0.0)),
            2,
            execution.ctx,
        )
    assert calls == 1


def test_search_reads_no_presence(vector_index, vector_family, execution, monkeypatch):
    vector_index.upsert_batch(
        vector_family.batch([("a", "u1", (1.0, 0.0))], revision="rev"), execution.ctx
    )
    collection, _ = vector_index._require()

    def refuse(*args, **kwargs):
        raise AssertionError("search fetched records by ID")

    monkeypatch.setattr(collection, "get", refuse)
    result = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 1, execution.ctx
    )
    assert [owner.key for owner in result.owners] == [("unit", "u1")]


def test_upsert_adds_one_partition_per_native_call(
    vector_index, vector_family, execution, monkeypatch
):
    rows = [(f"r{i}", "u", (1.0, 0.0)) for i in range(5)]
    collection, _ = vector_index._require()
    native_add = collection.add
    sizes = []

    def spy(*args, **kwargs):
        sizes.append(len(kwargs["ids"]))
        return native_add(*args, **kwargs)

    monkeypatch.setattr(collection, "add", spy)
    monkeypatch.setattr(chroma_index, "partition_size", lambda dimension: 2)
    vector_index.upsert_batch(vector_family.batch(rows), execution.ctx)
    assert sizes == [2, 2, 1]


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
    # The writer imports the same tychora as this test, also when that copy is
    # not installed, as mutmut's mutants/src is not.
    source_root = str(Path(tychora.__file__).resolve().parents[1])
    inherited = os.environ.get("PYTHONPATH")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), vector_family.spec.model_dump_json()],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, (source_root, inherited))),
        },
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


@pytest.mark.parametrize("strong_kind", ["unit", "asset"])
def test_owner_pools_are_ranked_per_kind(
    vector_index, vector_family, execution, strong_kind
):
    weak_kind = "asset" if strong_kind == "unit" else "unit"
    # Distinct directions keep the approximate index exact at this size.
    strong = [
        (
            f"strong-{i:03}",
            f"strong-owner-{i:03}",
            (math.cos(i / 1000), math.sin(i / 1000)),
        )
        for i in range(81)
    ]
    weak = [("weak", "weak-owner", (0.6, 0.8))]
    kinds = {record: strong_kind for record, _, _ in strong} | {"weak": weak_kind}
    vector_index.upsert_batch(
        vector_family.batch(strong + weak, revision="rev", kinds=kinds),
        execution.ctx,
    )
    result = vector_index.search(
        vector_family.view("rev"), vector_family.query((1.0, 0.0)), 80, execution.ctx
    )
    # The page returns every owner; the per-kind cut is the caller's.
    assert len([o for o in result.owners if o.key[0] == strong_kind]) == 81
    assert (weak_kind, "weak-owner") in [o.key for o in result.owners]
    assert [o.rank for o in result.owners if o.key[0] == weak_kind] == [1]
    assert result.detail["returned"] == 82
    assert not result.truncated


def _owner(record_id: str) -> VectorOwner:
    return VectorOwner(
        record_id=record_id,
        revision_id="r" * 64,
        extraction_id="x" * 64,
        owner_kind="unit",
        owner_id="u" * 64,
    )


def test_upsert_writes_owner_metadata(vector_index, vector_family, execution):
    index = vector_index
    batch = vector_family.batch([("record", "u0", (1.0, 0.0))])
    owner = _owner(batch.records[0].record_id)
    index.upsert_batch(batch.model_copy(update={"owners": (owner,)}), execution.ctx)
    collection, _ = index._require()
    stored = collection.get(ids=[owner.record_id], include=["metadatas"])["metadatas"][
        0
    ]
    assert stored["revision_id"] == "r" * 64
    assert stored["extraction_id"] == "x" * 64
    assert stored["owner_kind"] == "unit"
    assert stored["owner_id"] == "u" * 64


def test_upsert_without_owners_keeps_the_digest_only_metadata(
    vector_index, vector_family, execution
):
    batch = vector_family.batch([("record", "u0", (1.0, 0.0))])
    vector_index.upsert_batch(batch, execution.ctx)
    collection, _ = vector_index._require()
    stored = collection.get(ids=["record"], include=["metadatas"])["metadatas"][0]
    assert set(stored) == {"input_sha", "value_sha"}


def test_owner_metadata_does_not_change_the_receipt(
    vector_index, vector_family, execution
):
    batch = vector_family.batch([("record", "u0", (1.0, 0.0))])
    owned = batch.model_copy(update={"owners": (_owner("record"),)})
    assert vector_index.upsert_batch(owned, execution.ctx).digest == (
        vector_index.upsert_batch(batch, execution.ctx).digest
    )


@pytest.mark.parametrize(
    "owner_ids", [["other"], ["record", "record"], ["record", "extra"]]
)
def test_owners_must_cover_each_record_once(vector_family, owner_ids):
    batch = vector_family.batch([("record", "u0", (1.0, 0.0))])
    with pytest.raises(ValueError, match="Vector owners must cover each record once"):
        VectorBatch(
            family_id=batch.family_id,
            records=batch.records,
            owners=tuple(_owner(i) for i in owner_ids),
        )
