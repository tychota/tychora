"""Matched rerank text comes from exact canonical source ranges."""

from dataclasses import replace

import pytest

from tests.helpers.corpus import registry, seed_bundle
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.corpus import ChunkRow
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import prepare_extraction
from tychora.indexing.windows import window_unit
from tychora.ports.reranking import render_ranking_input
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import (
    RerankFacts,
    RerankMatch,
    matches_from_channels,
    ranking_input,
)


class CharacterCounter:
    model = "rerank-2.5"
    tokenizer_id = "synthetic-character-counter"

    def count(self, text: str) -> int:
        return len(text)


def test_rerank_representation_keeps_end_match():
    source = "unrelated beginning " * 90 + "END_MARKER exact answer"
    bundle = seed_bundle(source)
    unit = bundle.units[0]
    start = source.index("END_MARKER")
    prepared = prepare_extraction(bundle)
    indexed = next(
        window
        for window in window_unit(
            prepared.representation(unit.unit_id), CharacterCounter()
        )
        if window.char_start <= start < window.char_end
    )
    chunk = ChunkRow(
        chunk_id=indexed.chunk_id,
        unit_id=indexed.unit_id,
        extraction_id=indexed.extraction_id,
        ordinal_in_unit=0,
        char_start=indexed.char_start,
        char_end=indexed.char_end,
        context_group_key=indexed.context_group_key,
        chunker_fingerprint=indexed.chunker_fingerprint,
        token_count=indexed.token_count,
        text=indexed.text,
    )
    candidate = FusedCandidate(
        key=("unit", unit.unit_id),
        rank=1,
        score=1.0,
        ranks={"lexical": 1},
        matching_window_ids=(chunk.chunk_id,),
    )
    view = type(
        "View",
        (),
        {
            "revision_ids": frozenset({bundle.revision.revision_id}),
            "context_unit_ids": frozenset({unit.unit_id}),
            "structural_unit_ids": frozenset(),
            "asset_ids": frozenset(),
            "chunk_ids": frozenset({chunk.chunk_id}),
        },
    )()
    facts = RerankFacts(
        rows=bundle,
        prepared=prepared,
        matches=matches_from_channels(
            candidate,
            {
                "lexical": (
                    ChannelCandidate(
                        key=candidate.key,
                        rank=1,
                        matching_window_ids=(chunk.chunk_id,),
                    ),
                )
            },
            {chunk.chunk_id: chunk},
            prepared,
            view,
        ),
    )
    item = ranking_input(
        candidate, {unit.unit_id: unit}, view, CharacterCounter(), facts=facts
    )
    assert "END_MARKER" in item.text
    assert item.anchor_ranges[0].char_start > 0
    assert CharacterCounter().count(item.text) <= 1200


def _case(text: str, matches: tuple[RerankMatch, ...]):
    bundle = seed_bundle(text)
    unit = bundle.units[0]
    candidate = FusedCandidate(
        ("unit", unit.unit_id),
        1,
        1.0,
        {match.channel: match.rank for match in matches} or {"graph": 1},
        matching_window_ids=tuple(
            match.window_id for match in matches if match.window_id is not None
        ),
    )
    view = type(
        "View",
        (),
        {
            "revision_ids": frozenset({bundle.revision.revision_id}),
            "context_unit_ids": frozenset({unit.unit_id}),
            "structural_unit_ids": frozenset(),
            "asset_ids": frozenset(),
            "chunk_ids": frozenset(candidate.matching_window_ids),
        },
    )()
    return (
        bundle,
        candidate,
        view,
        RerankFacts(bundle, prepare_extraction(bundle), matches),
    )


def test_two_disjoint_matches_keep_strongest_first_and_both_anchors():
    text = "FIRST_MATCH " + "x" * 200 + " SECOND_MATCH"
    first = RerankMatch("first", "placeholder", 0, 11, "lexical", 4, 1.0)
    second = RerankMatch(
        "second", "placeholder", len(text) - 12, len(text), "exact", 1, 2.0
    )
    bundle, candidate, view, facts = _case(text, (first, second))
    unit_id = bundle.units[0].unit_id
    facts = replace(
        facts,
        matches=(replace(first, unit_id=unit_id), replace(second, unit_id=unit_id)),
    )
    item = ranking_input(
        candidate, {unit_id: bundle.units[0]}, view, CharacterCounter(), facts=facts
    )
    assert [span.text for span in item.spans] == ["SECOND_MATCH", "FIRST_MATCH"]
    assert [anchor.indexed_window_id for anchor in item.anchors] == ["second", "first"]
    assert item.omitted_match_count == 0


def test_same_window_from_two_channels_is_one_match_not_an_omission():
    text = "one exact matched window"
    first = RerankMatch("one", "placeholder", 0, len(text), "lexical", 2, 1.0)
    second = replace(first, channel="text", rank=1)
    bundle, candidate, view, facts = _case(text, (first, second))
    unit_id = bundle.units[0].unit_id
    facts = replace(
        facts,
        matches=(replace(first, unit_id=unit_id), replace(second, unit_id=unit_id)),
    )
    item = ranking_input(
        candidate, {unit_id: bundle.units[0]}, view, CharacterCounter(), facts=facts
    )
    assert len(item.anchors) == 1
    assert item.omitted_match_count == 0


def test_graph_only_without_support_uses_labelled_unit_fallback():
    bundle, candidate, view, facts = _case("Graph target body", ())
    item = ranking_input(
        candidate,
        {bundle.units[0].unit_id: bundle.units[0]},
        view,
        CharacterCounter(),
        facts=facts,
    )
    assert item.spans[0].text == "Graph target body"
    assert item.anchors[0].origin == "graph_unit_fallback"


def test_denied_match_dependency_fails_entire_representation():
    text = "good and denied"
    match = RerankMatch("window", "placeholder", 0, len(text), "lexical", 1, 1.0)
    bundle, candidate, view, facts = _case(text, (match,))
    unit_id = bundle.units[0].unit_id
    facts = replace(facts, matches=(replace(match, unit_id=unit_id),))
    view.context_unit_ids = frozenset()
    with pytest.raises(TychoraError) as error:
        ranking_input(
            candidate,
            {unit_id: bundle.units[0]},
            view,
            CharacterCounter(),
            facts=facts,
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


def test_oversize_window_records_trimmed_source_subrange():
    text = "A" * 1500 + " TAIL_MARKER"
    match = RerankMatch("large", "placeholder", 0, len(text), "text", 1, 1.0)
    bundle, candidate, view, facts = _case(text, (match,))
    unit_id = bundle.units[0].unit_id
    facts = replace(facts, matches=(replace(match, unit_id=unit_id),))
    item = ranking_input(
        candidate,
        {unit_id: bundle.units[0]},
        view,
        CharacterCounter(),
        facts=facts,
    )
    assert item.spans[0].char_end < len(text)
    assert item.truncations[0].window_id == "large"
    assert item.truncations[0].original_char_end == len(text)
    assert item.anchors[0].char_end == item.spans[0].char_end


def test_channel_matches_use_retained_chunk_ranges_including_nonwinning_family():
    bundle = seed_bundle("exact retained chunk")
    unit = bundle.units[0]
    chunk = bundle.chunks[0]
    candidate = FusedCandidate(("unit", unit.unit_id), 1, 1.0, {"text": 1})
    rows = {
        "text:one": (
            ChannelCandidate(
                key=candidate.key, rank=3, matching_window_ids=(chunk.chunk_id,)
            ),
        ),
        "text:two": (
            ChannelCandidate(
                key=candidate.key, rank=2, matching_window_ids=(chunk.chunk_id,)
            ),
        ),
    }
    view = type("View", (), {"chunk_ids": frozenset({chunk.chunk_id})})()
    matches = matches_from_channels(
        candidate,
        rows,
        {chunk.chunk_id: chunk},
        prepare_extraction(bundle),
        view,
    )
    assert [(hit.channel, hit.rank) for hit in matches] == [
        ("text:one", 3),
        ("text:two", 2),
    ]
    assert all(hit.char_start == chunk.char_start for hit in matches)
    forged = chunk.model_copy(update={"text": "forged"})
    with pytest.raises(TychoraError) as error:
        matches_from_channels(
            candidate,
            rows,
            {chunk.chunk_id: forged},
            prepare_extraction(bundle),
            view,
        )
    assert error.value.code == "INTEGRITY_ERROR"


def test_graph_support_must_be_on_target_unit():
    bundle, candidate, view, facts = _case("Graph target body", ())
    unit_id = bundle.units[0].unit_id
    support = RerankMatch(None, unit_id, 6, 12, "graph", 1, 0.35)
    item = ranking_input(
        candidate,
        {unit_id: bundle.units[0]},
        view,
        CharacterCounter(),
        facts=replace(facts, graph_support=support),
    )
    assert item.spans[0].text == "target"
    with pytest.raises(TychoraError) as error:
        ranking_input(
            candidate,
            {unit_id: bundle.units[0]},
            view,
            CharacterCounter(),
            facts=replace(facts, graph_support=replace(support, unit_id="other")),
        )
    assert error.value.code == "FORBIDDEN_SCOPE"


def test_code_parent_context_is_exact_and_dropped_before_winner(tmp_path):
    from tests.code.test_structure import _registration
    from tychora.adapters.code.normalize import normalize_code, retain_code_extraction

    artifacts, registration, parsed = _registration(tmp_path)
    bundle = normalize_code(
        retain_code_extraction(registration, parsed, registry(), artifacts),
        registry(),
    )
    child_record = next(
        row for row in bundle.records if row.data_json.get("name") == "child"
    )
    child = next(unit for unit in bundle.units if unit.unit_id == child_record.unit_id)
    parent_context = next(
        span
        for span in child_record.data_json["context_spans"]
        if span["reason"] == "parent_declaration"
    )
    parent = next(
        unit
        for unit in bundle.units
        if unit.unit_id == parent_context["source_unit_id"]
    )
    match = RerankMatch(None, child.unit_id, 0, len(child.text), "text", 1, 1.0)
    candidate = FusedCandidate(("unit", child.unit_id), 1, 1.0, {"text": 1})
    view = type(
        "View",
        (),
        {
            "revision_ids": frozenset({bundle.revision.revision_id}),
            "context_unit_ids": frozenset(unit.unit_id for unit in bundle.units),
            "structural_unit_ids": frozenset(),
            "asset_ids": frozenset(),
            "chunk_ids": frozenset(),
        },
    )()
    facts = RerankFacts(bundle, prepare_extraction(bundle), (match,))
    units = {unit.unit_id: unit for unit in bundle.units}
    full = ranking_input(candidate, units, view, CharacterCounter(), facts=facts)
    assert any(
        span.role == "context" and span.unit_id == parent.unit_id for span in full.spans
    )
    assert full.spans[0].text == child.text
    tight = ranking_input(
        candidate,
        units,
        view,
        CharacterCounter(),
        budget=CharacterCounter().count(
            render_ranking_input(
                bundle.revision.kind_id,
                bundle.revision.title,
                child.heading_path,
                (full.spans[0],),
            )
        ),
        facts=facts,
    )
    assert all(span.role == "match" for span in tight.spans)
    assert tight.spans[0].text == child.text
