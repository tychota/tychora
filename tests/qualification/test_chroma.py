"""Real Chroma qualification with synthetic normalized vectors."""

import math

from tychora.contracts.vectors import WorkLimits


def test_exact_tiny_inner_product_order(vector_index, vector_family, execution):
    vector_index.upsert_batch(
        vector_family.batch(
            [
                ("east", "east", (1.0, 0.0)),
                ("diagonal", "diagonal", (0.8, 0.6)),
                ("north", "north", (0.0, 1.0)),
            ]
        ),
        execution.ctx,
    )
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        {name: ("unit", name) for name in ("east", "diagonal", "north")},
        3,
        WorkLimits(),
        execution.ctx,
    )
    assert [candidate.key for candidate in page.candidates] == [
        ("unit", "east"),
        ("unit", "diagonal"),
        ("unit", "north"),
    ]
    assert math.isclose(page.candidates[1].distance, 0.2, abs_tol=1e-6)
    assert page.exhausted


def test_owner_collapse_across_many_windows(vector_index, vector_family, execution):
    rows = [
        (f"same-{i}", "same", (math.cos(i / 1000), math.sin(i / 1000)))
        for i in range(150)
    ]
    rows.extend([("second", "second", (0.8, 0.6)), ("third", "third", (0.0, 1.0))])
    vector_index.upsert_batch(vector_family.batch(rows), execution.ctx)
    eligible = {record_id: ("unit", owner_id) for record_id, owner_id, _ in rows}
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        eligible,
        3,
        WorkLimits(),
        execution.ctx,
    )
    assert [candidate.key for candidate in page.candidates] == [
        ("unit", "same"),
        ("unit", "second"),
        ("unit", "third"),
    ]
    assert len(page.candidates[0].record_ids) >= 1
    assert not page.truncated


def test_ann_short_result_is_not_reported_as_exhausted(
    vector_index, vector_family, execution
):
    rows = [(f"same-{i}", "same", (1.0, 0.0)) for i in range(150)]
    rows.extend([("second", "second", (0.8, 0.6)), ("third", "third", (0.0, 1.0))])
    vector_index.upsert_batch(vector_family.batch(rows), execution.ctx)
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        {record_id: ("unit", owner_id) for record_id, owner_id, _ in rows},
        3,
        WorkLimits(),
        execution.ctx,
    )
    if len(page.candidates) < 3:
        assert page.truncated
        assert not page.exhausted


def test_k_owners_span_unit_and_asset_partitions(
    vector_index, vector_family, execution
):
    rows = [
        ("unit-east", "east", (1.0, 0.0)),
        ("unit-mid", "mid", (0.8, 0.6)),
        ("asset-north", "north", (0.0, 1.0)),
        ("asset-west", "west", (-1.0, 0.0)),
    ]
    vector_index.upsert_batch(vector_family.batch(rows), execution.ctx)
    eligible = {
        record_id: (("asset" if record_id.startswith("asset") else "unit"), owner)
        for record_id, owner, _ in rows
    }
    page = vector_index.search(
        vector_family.query((1.0, 0.0)),
        eligible,
        4,
        WorkLimits(),
        execution.ctx,
    )
    assert [candidate.key for candidate in page.candidates] == [
        ("unit", "east"),
        ("unit", "mid"),
        ("asset", "north"),
        ("asset", "west"),
    ]
    assert page.queried_partitions == 2
