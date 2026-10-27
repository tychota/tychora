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
