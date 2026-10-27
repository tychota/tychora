"""Real Chroma qualification with synthetic normalized vectors."""

import math

import chromadb
import pytest
from chromadb.config import Settings

from tychora.adapters.chroma.index import ChromaVectorIndex


def test_exact_tiny_inner_product_order(vector_index, vector_family, vector_execution):
    vector_index.upsert_batch(
        vector_family.batch(
            [
                ("east", "east", (1.0, 0.0)),
                ("diagonal", "diagonal", (0.8, 0.6)),
                ("north", "north", (0.0, 1.0)),
            ],
            revision="rev",
        ),
        vector_execution.ctx,
    )
    result = vector_index.search(
        vector_family.view("rev"),
        vector_family.query((1.0, 0.0)),
        3,
        vector_execution.ctx,
    )
    assert [owner.key for owner in result.owners] == [
        ("unit", "east"),
        ("unit", "diagonal"),
        ("unit", "north"),
    ]
    assert math.isclose(result.owners[1].score, -0.2, abs_tol=1e-6)
    assert not result.truncated


def test_owner_collapse_across_many_windows_reports_truncation(
    vector_index, vector_family, vector_execution
):
    rows = [
        (f"same-{i}", "same", (math.cos(i / 1000), math.sin(i / 1000)))
        for i in range(150)
    ]
    rows.extend([("second", "second", (0.8, 0.6)), ("third", "third", (0.0, 1.0))])
    vector_index.upsert_batch(
        vector_family.batch(rows, revision="rev"), vector_execution.ctx
    )
    result = vector_index.search(
        vector_family.view("rev"),
        vector_family.query((1.0, 0.0)),
        3,
        vector_execution.ctx,
    )
    # 12 -> 24 -> 48 nearest records all belong to one owner: the doublings
    # run out first and the page reports it rather than claiming the pool.
    assert [owner.key for owner in result.owners] == [("unit", "same")]
    assert len(result.owners[0].window_ids) == 48
    assert result.truncated
    assert result.detail["queries"] == 3


def test_short_filtered_page_is_not_exhaustion(
    vector_index, vector_family, vector_execution
):
    # 30 records of another revision and 2 of the viewed one: 32 stored
    # records exceed the 20 wanted, so the first page is filtered and short.
    other = [(f"o{i}", f"o{i}", (1.0, 0.0)) for i in range(30)]
    mine = [("m0", "m0", (0.8, 0.6)), ("m1", "m1", (0.6, 0.8))]
    vector_index.upsert_batch(
        vector_family.batch(other, revision="other"), vector_execution.ctx
    )
    vector_index.upsert_batch(
        vector_family.batch(mine, revision="mine"), vector_execution.ctx
    )
    result = vector_index.search(
        vector_family.view("mine"),
        vector_family.query((1.0, 0.0)),
        5,
        vector_execution.ctx,
    )
    # The short filtered page proved nothing, so the search doubled to 40,
    # which holds all 32 records unfiltered: that page proves exhaustion.
    assert result.detail["queries"] == 2
    assert result.detail["exhausted"] == 1
    assert [owner.key for owner in result.owners] == [("unit", "m0"), ("unit", "m1")]
    assert not result.truncated


def test_small_collection_is_exhausted_in_one_page(
    vector_index, vector_family, vector_execution
):
    rows = [("east", "east", (1.0, 0.0)), ("north", "north", (0.0, 1.0))]
    vector_index.upsert_batch(
        vector_family.batch(rows, revision="rev"), vector_execution.ctx
    )
    result = vector_index.search(
        vector_family.view("rev"),
        vector_family.query((1.0, 0.0)),
        5,
        vector_execution.ctx,
    )
    assert result.detail == {
        "returned": 2,
        "queries": 1,
        "rescored": 0,
        "exhausted": 1,
    }
    assert not result.truncated


def test_k_owners_are_ranked_per_unit_and_asset_kind(
    vector_index, vector_family, vector_execution
):
    rows = [
        ("unit-east", "east", (1.0, 0.0)),
        ("unit-mid", "mid", (0.8, 0.6)),
        ("asset-north", "north", (0.0, 1.0)),
        ("asset-west", "west", (-1.0, 0.0)),
    ]
    kinds = {record: "asset" for record, _, _ in rows if record.startswith("asset")}
    vector_index.upsert_batch(
        vector_family.batch(rows, revision="rev", kinds=kinds), vector_execution.ctx
    )
    result = vector_index.search(
        vector_family.view("rev"),
        vector_family.query((1.0, 0.0)),
        4,
        vector_execution.ctx,
    )
    assert [(owner.key, owner.rank) for owner in result.owners] == [
        (("unit", "east"), 1),
        (("unit", "mid"), 2),
        (("asset", "north"), 1),
        (("asset", "west"), 2),
    ]
    assert result.detail["queries"] == 1


def test_existing_family_rejects_changed_hnsw_construction_setting(
    tmp_path, vector_index, vector_family
):
    native = chromadb.PersistentClient(
        path=str(tmp_path / "altered"), settings=Settings(anonymized_telemetry=False)
    )
    native.create_collection(
        name="family_" + vector_family.spec.family_id,
        embedding_function=None,
        metadata=vector_index._collection.metadata,
        configuration={"hnsw": {"space": "ip", "ef_search": 200, "max_neighbors": 32}},
    )
    with pytest.raises(ValueError, match="configuration"):
        ChromaVectorIndex(tmp_path / "altered").create_family(vector_family.spec)
