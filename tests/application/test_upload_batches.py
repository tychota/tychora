import pytest

from tests.providers.test_text import _group
from tychora.application.sources import upload_batches
from tychora.contracts.errors import TychoraError


def _groups(dependency_counts, requests):
    base = _group()
    return tuple(
        base.model_copy(
            update={
                "group_id": f"group-{index}",
                "window_ids": (f"window-{index}",),
                "dependency_ids": frozenset(f"unit-{index}-{n}" for n in range(count)),
                "request_index": request,
            }
        )
        for index, (count, request) in enumerate(zip(dependency_counts, requests))
    )


def test_small_document_stays_one_unchanged_batch():
    groups = _groups([3, 4, 5], [0, 0, 1])
    assert upload_batches(groups) == (groups,)


def test_large_document_splits_within_the_dependency_bound():
    groups = _groups([900] * 12, [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3])
    batches = upload_batches(groups)
    assert len(batches) > 1
    assert [g.group_id for b in batches for g in b] == [g.group_id for g in groups]
    for batch in batches:
        dependencies = frozenset().union(*(g.dependency_ids for g in batch))
        assert len(dependencies) <= 2048
        indices = [g.request_index for g in batch]
        assert indices == sorted(indices)
        assert set(indices) == set(range(indices[-1] + 1))


def test_shared_dependencies_count_once_toward_the_bound():
    base = _groups([1500, 1500], [0, 0])
    shared = tuple(
        g.model_copy(update={"dependency_ids": base[0].dependency_ids}) for g in base
    )
    assert upload_batches(shared) == (shared,)


def test_single_group_over_the_bound_fails_explicitly():
    with pytest.raises(TychoraError) as raised:
        upload_batches(_groups([10, 2049], [0, 0]))
    assert raised.value.code == "INVALID_REQUEST"
