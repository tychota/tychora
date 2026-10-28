"""Result cache entries are canonical JSON within the response bound; no pickle."""

import logging
import pickle

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.application import query as query_module
from tychora.application.query import QueryService
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.collections import CollectionCreate
from tychora.contracts.evidence import (
    AnchorRange,
    Coverage,
    DecisionRequirement,
    Geometry,
    ImageLocator,
    LabelledProxy,
    RelationChecks,
    RelationPath,
    StructuredRecord,
    VisualEvidence,
)
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.cached_selection import (
    CACHE_OVERHEAD_BYTES,
    CachedSelection,
    PreparedEvidence,
    Selection,
)

QUESTION = {"question": "pump seal"}
PUMP = "# Pump\n\nOperators verify the pump seal outcome."


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


def _record_puts(monkeypatch, keys=None) -> list[bytes]:
    stored: list[bytes] = []
    original = QueryCaches.put_result

    def recording(self, key, value):
        stored.append(value)
        if keys is not None:
            keys.append((self, key))
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
    assert _comparable(again) == _comparable(first)


def _comparable(result) -> dict:
    """The response without what differs per run: its id and timing trace."""
    return result.model_dump(mode="json", exclude={"run_id", "trace"})


def test_policy_write_between_identical_queries_misses(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", PUMP)
        kb.search(QUESTION)
        kb.search(QUESTION)
        assert len(stored) == 1
        kb._workspace.bridge.call(
            kb._workspace.services.collections.create(
                CollectionCreate(title="Second", slug="second"),
                kb.scope,
                ExecutionContext(),
                managed_owner_authorized=True,
            )
        )
        kb.search(QUESTION)
    assert len(stored) == 2


def _entry(order: int = 1) -> CachedSelection:
    """A populated entry; ``order`` flips the insertion order of every frozenset."""

    def ids(*values: str) -> frozenset[str]:
        return frozenset(values[::order])

    locator = ImageLocator(
        artifact_ref="artifact",
        native_ref="#/pictures/0",
        physical_page=2,
        geometry=Geometry(domain="normalized", x0=0.1, y0=0.25, x1=0.75, y1=0.9),
    )
    relation = RelationPath(
        unit_ids=("u1", "u2"),
        assertion_ids=("a1",),
        predicates=("requires",),
        support_unit_ids=ids("u3", "u1", "u2"),
        provenance=FrozenJsonMap({"confidence": 0.875, "path": ["a1"]}),
    )
    return CachedSelection.of(
        Selection(
            segments=(),
            reservation_priority=("standard",),
            omitted_anchors=(
                AnchorRange(unit_id="u1", origin="lexical", char_start=0, char_end=4),
            ),
            lane_coverage=("standard",),
            text_budget=5400,
            visual_reserved_tokens=600,
        ),
        PreparedEvidence(
            records=(
                StructuredRecord(
                    record_id="r1",
                    unit_id="u1",
                    record_type="table_row",
                    schema_id="schema",
                    value=FrozenJsonMap({"pressure": 2.5, "nested": {"ratio": 0.3}}),
                    dependencies=ids("u9", "u1", "u5"),
                ),
            ),
            checks=RelationChecks(status="complete", assertions=(relation,)),
            relations=(relation,),
            visuals=(
                VisualEvidence(
                    asset_id="asset",
                    source_id="source",
                    revision_id="revision",
                    asset_ref="artifact",
                    locator=locator,
                    proxies=(
                        LabelledProxy(
                            proxy_id="p1",
                            label="caption",
                            text="A pump",
                            origin="derived",
                            dependencies=ids("u7", "u2", "u4"),
                            review_state="accepted",
                        ),
                    ),
                ),
            ),
            coverage=Coverage(status="complete", completed_channels=("lexical",)),
            warnings=("edition warning",),
            decisions=(DecisionRequirement(requirement_id="d1", reason="pick one"),),
            candidate_count=3,
        ),
    )


def test_cached_selection_round_trips_without_pickle(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pickle used for a cached result")

    monkeypatch.setattr(pickle, "loads", forbidden)
    entry = _entry()
    raw = entry.to_bytes()
    assert raw.startswith(b"{")
    assert CachedSelection.from_bytes(raw) == entry
    caches = QueryCaches()
    caches.put_result("key", raw)
    assert caches.get_result("key") == raw


def test_equal_entries_from_differently_ordered_sets_have_identical_bytes():
    forward, backward = _entry(1), _entry(-1)
    assert forward == backward
    assert forward.to_bytes() == backward.to_bytes()
    assert b'"dependencies":["u1","u5","u9"]' in forward.to_bytes()


def test_of_a_cached_selection_takes_only_its_selection():
    entry = _entry()
    evidence = PreparedEvidence(
        **{name: getattr(entry, name) for name in PreparedEvidence.model_fields}
    ).model_copy(update={"candidate_count": 9})
    rebuilt = CachedSelection.of(entry, evidence)
    assert rebuilt == CachedSelection.of(entry.selection(), evidence)
    assert rebuilt.candidate_count == 9


def test_corrupt_entry_is_a_logged_miss_and_recomputed(tmp_path, monkeypatch, caplog):
    keys: list[tuple[QueryCaches, object]] = []
    stored = _record_puts(monkeypatch, keys)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", PUMP)
        first = kb.search(QUESTION)
        caches, key = keys[0]
        caches.put_result(key, b"\x00not a cached selection")
        with caplog.at_level(logging.WARNING, logger="tychora.application.query"):
            fresh = kb.search(QUESTION)
        again = kb.search(QUESTION)
    assert _comparable(fresh) == _comparable(first)
    assert _comparable(again) == _comparable(first)
    # The miss stores a fresh entry; the next query hits it.
    assert len(stored) == 3
    assert stored[2] == stored[0]
    (warning,) = [r for r in caplog.records if "cached result" in r.getMessage()]
    assert warning.levelno == logging.WARNING
    assert len(warning.args[0]) == 32


def test_entry_over_the_bound_is_served_uncached(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", PUMP)
        limit = kb._workspace.owner.config.values.limits.max_response_bytes
        monkeypatch.setattr(query_module, "CACHE_OVERHEAD_BYTES", -limit)
        first = kb.search(QUESTION)
        again = kb.search(QUESTION)
    assert first.segments
    assert _comparable(again) == _comparable(first)
    assert stored == []


def _coverage_of_prepare(monkeypatch, coverage: Coverage) -> None:
    original = QueryService._prepare

    async def degraded(self, *args):
        prepared, epoch = await original(self, *args)
        return prepared.model_copy(update={"coverage": coverage}), epoch

    monkeypatch.setattr(QueryService, "_prepare", degraded)


def test_failure_degraded_result_is_not_cached(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    _coverage_of_prepare(
        monkeypatch,
        Coverage(
            status="partial",
            completed_channels=("lexical",),
            unavailable_channels=("text", "rerank"),
        ),
    )
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", PUMP)
        first = kb.search(QUESTION)
        kb.search(QUESTION)
    assert first.segments
    assert first.coverage.unavailable_channels == ("text", "rerank")
    assert stored == []


def test_limit_truncated_partial_result_is_cached(tmp_path, monkeypatch):
    stored = _record_puts(monkeypatch)
    _coverage_of_prepare(
        monkeypatch, Coverage(status="partial", completed_channels=("lexical",))
    )
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pump.md", PUMP)
        kb.search(QUESTION)
        kb.search(QUESTION)
    assert len(stored) == 1


def test_put_result_refuses_non_bytes():
    with pytest.raises(TypeError):
        QueryCaches().put_result("key", object())  # ty: ignore[invalid-argument-type]
