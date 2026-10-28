"""Result cache entries are canonical JSON within the response bound; no pickle."""

import pickle

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.contracts.collections import CollectionCreate
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.cached_selection import CACHE_OVERHEAD_BYTES, CachedSelection


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def _record_puts(monkeypatch) -> list[bytes]:
    stored: list[bytes] = []
    original = QueryCaches.put_result

    def recording(self, key, value):
        stored.append(value)
        return original(self, key, value)

    monkeypatch.setattr(QueryCaches, "put_result", recording)
    return stored


def test_cache_entry_under_bound_for_300_page_result(tmp_path, monkeypatch):
    pages = "\n\n".join(
        f"# Page {n}\n\nThe pump seal inspection outcome is recorded on page {n}. "
        + "Operators verify the seal and record the verification outcome. " * 30
        for n in range(300)
    )
    stored = _record_puts(monkeypatch)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "book.md", pages)
        first = kb.search({"question": "verification outcome"})
        again = kb.search({"question": "verification outcome"})
        limit = kb._workspace.owner.config.values.limits.max_response_bytes
    assert len(stored) == 1
    assert len(stored[0]) <= limit + CACHE_OVERHEAD_BYTES
    assert [s.segment_id for s in again.segments] == [
        s.segment_id for s in first.segments
    ]


def test_policy_write_between_identical_queries_misses(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", "# Pump\n\nOperators verify the pump seal outcome.")
        kb.search({"question": "pump seal"})
        kb.search({"question": "pump seal"})
        assert len(stored) == 1
        kb._workspace.bridge.call(
            kb._workspace.services.collections.create(
                CollectionCreate(title="Second", slug="second"),
                kb.scope,
                ExecutionContext(),
                managed_owner_authorized=True,
            )
        )
        kb.search({"question": "pump seal"})
    assert len(stored) == 2


def test_cached_selection_round_trips_without_pickle(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pickle used for a cached result")

    monkeypatch.setattr(pickle, "loads", forbidden)
    from tychora.contracts.evidence import Coverage, RelationChecks

    entry = CachedSelection(
        segments=(),
        reservation_priority=("standard",),
        omitted_anchors=(),
        lane_coverage=("standard",),
        text_budget=5400,
        visual_reserved_tokens=600,
        records=(),
        checks=RelationChecks(),
        relations=(),
        visuals=(),
        coverage=Coverage(status="complete", completed_channels=("lexical",)),
        warnings=("edition warning",),
        decisions=(),
        candidate_count=3,
    )
    raw = entry.to_bytes()
    assert raw.startswith(b"{")
    assert CachedSelection.from_bytes(raw) == entry
    caches = QueryCaches()
    caches.put_result("key", raw)
    assert caches.get_result("key") == raw


def test_put_result_refuses_non_bytes():
    with pytest.raises(TypeError):
        QueryCaches().put_result("key", object())  # ty: ignore[invalid-argument-type]
