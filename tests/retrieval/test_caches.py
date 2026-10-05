"""Memory cache boundaries for query embeddings and result preparation."""

from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.retrieval.cache import QueryCaches


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def test_filter_change_reuses_embedding_but_not_result() -> None:
    clock = Clock()
    caches = QueryCaches(clock=clock)
    vector_key = ("exact input", "workspace", "principal", "credential")
    alpha = ("snapshot", "policy", "alpha")
    beta = ("snapshot", "policy", "beta")
    caches.put_embedding(vector_key, b"vector")
    caches.put_result(alpha, b"alpha")

    assert caches.get_embedding(vector_key) == b"vector"
    assert caches.get_result(beta) is None
    assert caches.get_result(alpha) == b"alpha"


def test_result_only_purge_preserves_query_embedding_for_uncached_retrieval() -> None:
    caches = QueryCaches(clock=Clock())
    caches.put_embedding("query", b"vector")
    caches.put_result("result", b"evidence")
    assert caches.purge_results() == 1
    assert caches.get_result("result") is None
    assert caches.get_embedding("query") == b"vector"


def test_ttl_uses_monotonic_boundary_and_lru_evicts_oldest() -> None:
    clock = Clock()
    caches = QueryCaches(clock=clock, embedding_capacity=2, result_capacity=2)
    caches.put_embedding("a", b"a")
    caches.put_embedding("b", b"b")
    assert caches.get_embedding("a") == b"a"
    caches.put_embedding("c", b"c")
    assert caches.get_embedding("b") is None
    clock.now = 300.0
    assert caches.get_embedding("a") is None
    assert caches.get_embedding("c") is None


def test_shared_byte_ceiling_bypasses_oversized_entry_and_purge() -> None:
    caches = QueryCaches(clock=Clock(), max_bytes=300)
    caches.put_embedding("a", b"1234")
    caches.put_result("large", b"x" * 100)
    assert caches.get_result("large") is None
    caches.put_result("small", b"abc")
    assert caches.get_embedding("a") is None
    assert caches.get_result("small") == b"abc"
    assert caches.purge() == 1
    assert caches.get_result("small") is None


def test_provider_runtime_shares_query_cache_memory_ceiling() -> None:
    caches = QueryCaches(clock=Clock(), max_bytes=300)
    runtime = VoyageRuntime(
        lambda: None,
        workspace_id="workspace",
        credential_binding_id="credential",
        query_caches=caches,
    )
    runtime.cache_query(("query",), b"1234")
    caches.put_result("result", b"abc")
    assert runtime.cached_query(("query",)) is None
    assert caches.get_result("result") == b"abc"


def test_nested_prepared_result_is_serialized_and_all_retained_bytes_are_bounded() -> (
    None
):
    caches = QueryCaches(clock=Clock(), max_bytes=4096)
    large_key = ("query" * 2000, "scope")
    nested = {"segments": [{"id": str(i), "parts": [i, i + 1]} for i in range(60)]}
    caches.put_result(large_key, nested)
    nested["segments"].clear()
    stored = caches.get_result(large_key)
    assert stored is not None
    assert len(stored["segments"]) == 60
    assert caches.retained_bytes <= 4096
    assert all(len(key) == 64 for key in caches._entries[1])
    assert all(isinstance(entry.value, bytes) for entry in caches._entries[1].values())
