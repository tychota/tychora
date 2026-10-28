# tests/retrieval/test_eligibility_cache.py
"""Compiled views are cached by snapshot, epoch, scope, policy and request digests."""

import pytest

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.retrieval.cache import ELIGIBILITY_CACHE_SIZE, EligibilityCache
from tychora.retrieval.compiler import (
    CompiledEligibility,
    EligibilityKey,
    eligible_cache_key,
)


def _compiled(epoch=0, request="request"):
    return CompiledEligibility(
        EligibilityView(
            snapshot_id="snapshot",
            policy_epoch=epoch,
            scope_digest=digest("scope"),
            eligibility_policy_digest=digest("policy"),
            eligibility_request_digest=digest(request),
            revisions={},
        ),
        warnings=("edition warning",),
    )


def test_hit_returns_the_cached_entry_without_compiling():
    cache = EligibilityCache()
    compiled = _compiled()
    key = eligible_cache_key(compiled.view)
    assert cache.get_or_compile(key, lambda: compiled) is compiled
    assert cache.get_or_compile(key, lambda: pytest.fail("compiled twice")) is compiled


def test_a_view_that_does_not_answer_its_key_is_refused():
    cache = EligibilityCache()
    key = EligibilityKey(
        "snapshot", 1, digest("scope"), digest("policy"), digest("request")
    )
    with pytest.raises(TychoraError) as error:
        cache.get_or_compile(key, lambda: _compiled(epoch=0))
    assert error.value.reason == "invariant.unexpected"


def test_least_recent_entry_is_evicted():
    cache = EligibilityCache(capacity=1)
    first, second = _compiled(request="a"), _compiled(request="b")
    cache.get_or_compile(eligible_cache_key(first.view), lambda: first)
    cache.get_or_compile(eligible_cache_key(second.view), lambda: second)
    fresh = _compiled(request="a")
    assert cache.get_or_compile(eligible_cache_key(first.view), lambda: fresh) is fresh
    assert ELIGIBILITY_CACHE_SIZE == 64
    assert cache.purge() == 1


def test_graph_scope_is_built_once_per_key_and_purged_with_views():
    cache = EligibilityCache(capacity=1)
    first, second = (eligible_cache_key(_compiled(request=r).view) for r in "ab")
    builds = []

    def build():
        builds.append(1)

    assert cache.graph_scope(first, build) is None
    assert cache.graph_scope(first, build) is None
    assert len(builds) == 1
    cache.graph_scope(second, build)
    cache.graph_scope(first, build)
    assert len(builds) == 3
    cache.purge()
    cache.graph_scope(first, build)
    assert len(builds) == 4


def test_storing_a_newer_epoch_evicts_every_older_epoch_and_its_graph():
    cache = EligibilityCache()
    old = [_compiled(epoch=1, request=r) for r in "ab"]
    for compiled in old:
        key = eligible_cache_key(compiled.view)
        cache.get_or_compile(key, lambda compiled=compiled: compiled)
        cache.graph_scope(key, lambda: None)
    assert (len(cache._entries), len(cache._graphs)) == (2, 2)
    same = _compiled(epoch=2, request="a")
    cache.get_or_compile(eligible_cache_key(same.view), lambda: same)
    # Only the epoch-2 view is left; the epoch-1 views and graphs are gone.
    assert list(cache._entries) == [eligible_cache_key(same.view)]
    assert len(cache._graphs) == 0
    builds = []
    cache.graph_scope(eligible_cache_key(old[0].view), lambda: builds.append(1))
    assert builds == [1]
    # A graph of the superseded epoch is built for its caller but not kept.
    assert len(cache._graphs) == 0


def test_a_stale_epoch_compiled_late_is_returned_but_not_kept():
    cache = EligibilityCache()
    newer = _compiled(epoch=3)
    cache.get_or_compile(eligible_cache_key(newer.view), lambda: newer)
    stale = _compiled(epoch=2)
    key = eligible_cache_key(stale.view)
    assert cache.get_or_compile(key, lambda: stale) is stale
    again = _compiled(epoch=2)
    assert cache.get_or_compile(key, lambda: again) is again
    cache.graph_scope(key, lambda: None)
    assert list(cache._entries) == [eligible_cache_key(newer.view)]
    assert len(cache._graphs) == 0
