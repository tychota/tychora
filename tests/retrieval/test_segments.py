"""Final excerpts use exact retained source and ownership."""

from dataclasses import replace

from tests.helpers.corpus import seed_bundle
from tests.helpers.eligibility import revision_view, synthetic_graph_scope
from tests.ingestion.conftest import native_case  # noqa: F401
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import (
    CodeLocator,
    DocumentLocator,
    Locator,
    TextLocator,
)
from tychora.contracts.indexing import OwnedInterval
from tychora.indexing.representations import prepare_extraction
from tychora.ingestion.normalize import normalize
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import (
    RankedEvidence,
    RankedEvidenceItem,
    RerankFacts,
    RerankMatch,
)
from tychora.retrieval.segments import (
    OutputTokenCounter,
    _separate_fragments,
    assemble_segments,
)


class Counter:
    model = "output"
    tokenizer_id = "cl100k_base"

    def count(self, text):
        return len(text)


def locator_char_start(locator: Locator) -> int:
    assert isinstance(locator, (TextLocator, DocumentLocator, CodeLocator))
    return locator.char_start


def _sets(prepared, matches=()):
    """Each unit's persisted set from its ownership, and each window's from its unit."""
    sets = {
        rep.unit.unit_id: frozenset().union(
            *(interval.dependency_ids for interval in rep.ownership)
        )
        or frozenset({prepared.revision_id, rep.unit.unit_id})
        for rep in prepared.units
    }
    sets.update(
        {match.window_id: sets[match.unit_id] for match in matches if match.window_id}
    )
    return sets


def _facts(rows, prepared, matches):
    """Facts carrying the persisted sets of their units and windows."""
    return RerankFacts(rows, prepared, matches, dependencies=_sets(prepared, matches))


def _view(rows, units=(), readable=None):
    """A revision-level view of the rows' revision; units outside ``readable`` are excluded."""
    excluded = (
        ()
        if readable is None
        else tuple(unit.unit_id for unit in units if unit.unit_id not in readable)
    )
    return revision_view(
        {rows.revision.revision_id: rows.extraction.extraction_id},
        excluded_units=excluded,
    )


def config(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


def case(text, matches, *, kind="manual@1", source_key="manual.md"):
    bundle = seed_bundle(text, kind=kind, source_key=source_key)
    unit = bundle.units[0]
    prepared = prepare_extraction(bundle)
    matches = tuple(replace(match, unit_id=unit.unit_id) for match in matches)
    candidate = FusedCandidate(("unit", unit.unit_id), 1, 1.0, {"lexical": 1})
    item = RankedEvidenceItem(
        candidate, None, _facts(bundle, prepared, matches), "disabled"
    )
    return bundle, prepared, RankedEvidence((item,)), _view(bundle)


def _mapped_code_rows(bundle, units, file_texts):
    from tychora.adapters.code.syntax import source_maps
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.code_storage import CodeFileDescriptor
    from tychora.contracts.corpus import LocatorRow, RevisionArtifactRow
    from tychora.contracts.identity import digest

    mapped_units = tuple(
        unit.model_copy(
            update={
                "attributes": FrozenJsonMap(
                    {
                        **dict(unit.attributes),
                        "range_dependency_ids": [unit.unit_id],
                    }
                )
            }
        )
        for unit in units
    )
    descriptors = []
    for path, text in file_texts.items():
        encoded = text.encode()
        offsets, reverse, starts, lines = source_maps(encoded, text, "utf-8")
        descriptors.append(
            CodeFileDescriptor(
                relative_path=path,
                artifact_ref=bundle.revision.original_sha,
                capture_entry_digest="1" * 64,
                language="python",
                encoding="utf-8",
                coverage="parsed",
                adapter_fingerprint="2" * 64,
                byte_length=len(encoded),
                char_length=len(text),
                codepoint_to_byte=offsets,
                byte_to_codepoint=reverse,
                line_starts=starts,
                byte_to_line=lines,
            )
        )
    rows = bundle.model_copy(
        update={
            "units": mapped_units,
            "locators": tuple(
                LocatorRow(
                    locator_id=digest("locator-v1", unit.unit_id, index),
                    unit_id=unit.unit_id,
                    fragment_ordinal=index,
                    locator=locator,
                )
                for unit in mapped_units
                for index, locator in enumerate(unit.locators)
            ),
            "code_files": tuple(descriptors),
            "revision_artifacts": (
                *bundle.revision_artifacts,
                *(
                    RevisionArtifactRow(
                        revision_id=bundle.revision.revision_id,
                        role="source_file",
                        source_ref=descriptor.relative_path,
                        artifact_sha=descriptor.artifact_ref,
                    )
                    for descriptor in descriptors
                ),
            ),
        }
    )
    from tychora.indexing.representations import _bundle_index

    _bundle_index(rows)
    return rows


def test_sets_helper_matches_persisted_representation_facts():
    from tychora.indexing.representations import ChunkSpan, representation_facts

    bundle = seed_bundle("Seal checks are quarterly.\n\nRecords are kept.")
    prepared = prepare_extraction(bundle)
    persisted = representation_facts(
        bundle,
        prepared,
        (
            ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end)
            for c in bundle.chunks
        ),
    ).dependency_map()
    windows = tuple(
        RerankMatch(c.chunk_id, c.unit_id, c.char_start, c.char_end, "lexical", 1, 1.0)
        for c in bundle.chunks
    )
    sets = _sets(prepared, windows)
    assert bundle.chunks
    assert sets == {owner: persisted[owner] for owner in sets}


def test_segment_keeps_winning_range_and_never_bridges_denied_gap(tmp_path):
    text = "left gap right"
    match = RerankMatch(None, "placeholder", 0, len(text), "lexical", 1, 1.0)
    bundle, prepared, ranked, view = case(text, (match,))
    unit = bundle.units[0]
    rep = prepared.units[0]
    ownership = (
        OwnedInterval(
            char_start=0,
            char_end=5,
            dependency_ids=frozenset(
                {unit.unit_id, bundle.revision.revision_id, "left"}
            ),
        ),
        OwnedInterval(
            char_start=5,
            char_end=9,
            dependency_ids=frozenset(
                {unit.unit_id, bundle.revision.revision_id, "gap"}
            ),
        ),
        OwnedInterval(
            char_start=9,
            char_end=len(text),
            dependency_ids=frozenset(
                {unit.unit_id, bundle.revision.revision_id, "right"}
            ),
        ),
    )
    ranked = RankedEvidence(
        (
            replace(
                ranked.items[0],
                facts=replace(
                    ranked.items[0].facts,
                    prepared=prepared.model_copy(
                        update={
                            "units": (rep.model_copy(update={"ownership": ownership}),)
                        }
                    ),
                ),
            ),
        )
    )
    view = view.model_copy(update={"excluded_unit_ids": frozenset({"gap"})})
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(result.segments) == 2
    for segment in result.segments:
        for excerpt in segment.excerpts:
            assert excerpt.text == unit.text[excerpt.char_start : excerpt.char_end]
    assert all(
        "gap" not in segment.context_only_unit_ids for segment in result.segments
    )
    assert all("gap" not in segment.text for segment in result.segments)


def test_distant_windows_and_end_match_keep_exact_offsets(tmp_path):
    text = "first" + "x" * 9990 + "LAST"
    matches = (
        RerankMatch(None, "placeholder", len(text) - 4, len(text), "lexical", 1, 1.0),
        RerankMatch(None, "placeholder", 0, 5, "lexical", 2, 1.0),
    )
    bundle, _, ranked, view = case(text, matches)
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(result.segments) == 2
    assert result.segments[0].excerpts[0].text == "LAST"
    assert result.segments[1].excerpts[0].text == "first"
    assert all(len(segment.text) <= 1200 for segment in result.segments)
    assert all(segment.citations for segment in result.segments)
    assert result.segments[0].source_id == bundle.source.source_id


def test_oversized_lookup_has_exact_truncation_and_fetch_reference(tmp_path):
    text = "A" * 3000
    _, _, ranked, view = case(text, ())
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.truncated and not segment.complete
    assert segment.omissions
    assert segment.citations[0].source_ref
    assert segment.text == text[: len(segment.text)]


def test_table_continuation_retains_two_physical_fragments(tmp_path):
    from tychora.contracts.evidence import DocumentLocator

    text = "rowArowB"
    bundle, prepared, ranked, view = case(
        text, (RerankMatch(None, "placeholder", 0, len(text), "lexical", 1, 1.0),)
    )
    unit = bundle.units[0]
    fragments = (
        DocumentLocator(
            artifact_ref=bundle.revision.original_sha,
            native_ref="table:1",
            char_start=0,
            char_end=4,
            physical_page=1,
        ),
        DocumentLocator(
            artifact_ref=bundle.revision.original_sha,
            native_ref="table:2",
            char_start=4,
            char_end=8,
            physical_page=2,
        ),
    )
    updated = unit.model_copy(update={"locators": fragments})
    prepared = prepared.model_copy(
        update={"units": (prepared.units[0].model_copy(update={"unit": updated}),)}
    )
    rows = bundle.model_copy(update={"units": (updated,)})
    ranked = RankedEvidence(
        (
            replace(
                ranked.items[0],
                facts=replace(ranked.items[0].facts, rows=rows, prepared=prepared),
            ),
        )
    )
    for policy in ("derived", "confirmed_only", "none"):
        cfg = config(tmp_path).model_copy(deep=True)
        cfg = cfg.model_copy(
            update={
                "values": cfg.values.model_copy(
                    update={
                        "retrieval": cfg.values.retrieval.model_copy(
                            update={"table_continuations": policy}
                        )
                    }
                )
            }
        )
        segments = assemble_segments(ranked, view, cfg, Counter()).segments
        assert len(segments) == 2
        assert {segment.citations[0].physical_page for segment in segments} == {1, 2}
        assert all(
            len(segment.citations) == 1 and len(segment.citations[0].locators) == 1
            for segment in segments
        )


def test_code_parent_and_child_original_ranges_do_not_duplicate(tmp_path):
    from hashlib import sha256

    from tychora.contracts.evidence import CodeLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    text = "class Box:\n    def run(self): pass\n"
    bundle = seed_bundle(text, kind="code_file@1")
    parent = bundle.units[0].model_copy(
        update={
            "unit_type": "symbol",
            "locators": (
                CodeLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="box.py",
                    relative_path="box.py",
                    char_start=0,
                    char_end=len(text),
                    byte_start=0,
                    byte_end=len(text.encode()),
                    line_start=1,
                    line_end=2,
                ),
            ),
        }
    )
    start = text.index("    def")
    child_text = text[start:]
    child = parent.model_copy(
        update={
            "unit_id": "child-method",
            "ordinal": 1,
            "parent_id": parent.unit_id,
            "text": child_text,
            "text_sha": sha256(child_text.encode()).hexdigest(),
            "locators": (
                CodeLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="box.py",
                    relative_path="box.py",
                    char_start=start,
                    char_end=len(text),
                    byte_start=start,
                    byte_end=len(text.encode()),
                    line_start=2,
                    line_end=2,
                ),
            ),
        }
    )
    owners = frozenset({bundle.revision.revision_id, parent.unit_id, child.unit_id})
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=(
            RepresentationUnit(
                unit=parent,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="code_file@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=start,
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, parent.unit_id}
                        ),
                    ),
                    OwnedInterval(
                        char_start=start, char_end=len(text), dependency_ids=owners
                    ),
                ),
            ),
            RepresentationUnit(
                unit=child,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="code_file@1",
                ownership=(
                    OwnedInterval(
                        char_start=0, char_end=len(child_text), dependency_ids=owners
                    ),
                ),
            ),
        ),
    )
    rows = _mapped_code_rows(bundle, (parent, child), {"box.py": text})
    candidates = (
        FusedCandidate(("unit", parent.unit_id), 1, 1.0, {"lexical": 1}),
        FusedCandidate(("unit", child.unit_id), 2, 0.5, {"lexical": 2}),
    )
    ranked = RankedEvidence(
        tuple(
            RankedEvidenceItem(
                candidate,
                None,
                _facts(
                    rows,
                    prepared,
                    (
                        RerankMatch(
                            None,
                            candidate.key[1],
                            0,
                            len(parent.text if index == 0 else child.text),
                            "lexical",
                            index + 1,
                            1.0,
                        ),
                    ),
                ),
                "disabled",
            )
            for index, candidate in enumerate(candidates)
        )
    )
    view = _view(bundle)
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(result.segments) == 1
    locator = result.segments[0].citations[0].locators[0]
    assert isinstance(locator, CodeLocator)
    assert locator.relative_path == "box.py"
    assert locator.char_start == 0 and locator.char_end == len(text)


def test_continuation_context_requires_current_eligible_graph_path(tmp_path):
    from hashlib import sha256

    from tychora.contracts.evidence import DocumentLocator
    from tychora.contracts.graph import GraphPath
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    bundle = seed_bundle("first row", kind="manual@1")
    first = bundle.units[0].model_copy(
        update={
            "unit_type": "table",
            "locators": (
                DocumentLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="fragment:1",
                    char_start=0,
                    char_end=9,
                    physical_page=1,
                ),
            ),
        }
    )
    second_text = "next row"
    second = first.model_copy(
        update={
            "unit_id": "second-fragment",
            "ordinal": 1,
            "text": second_text,
            "text_sha": sha256(second_text.encode()).hexdigest(),
            "locators": (
                DocumentLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="fragment:2",
                    char_start=0,
                    char_end=8,
                    physical_page=2,
                ),
            ),
        }
    )
    units = (first, second)
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="manual@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=len(unit.text),
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    rows = bundle.model_copy(update={"units": units})
    candidate = FusedCandidate(("unit", first.unit_id), 1, 1.0, {"lexical": 1})
    ranked = RankedEvidence(
        (
            RankedEvidenceItem(
                candidate,
                None,
                _facts(
                    rows,
                    prepared,
                    (
                        RerankMatch(
                            None, first.unit_id, 0, len(first.text), "lexical", 1, 1.0
                        ),
                    ),
                ),
                "disabled",
            ),
        )
    )
    view = _view(bundle)
    graph_view = synthetic_graph_scope(
        context=(first.unit_id, second.unit_id),
        revisions=(bundle.revision.revision_id,),
        generation_id="pinned-generation",
    )
    path = GraphPath(
        assertion_ids=("a" * 64,),
        required_support_ids=frozenset({first.unit_id, second.unit_id}),
        predicate="continues_table@1",
        status="recorded",
        confidence=0.9,
        source_unit_id=first.unit_id,
        target_unit_id=second.unit_id,
    )
    base = config(tmp_path)
    graph = base.values.retrieval.graph.model_copy(
        update={
            "semantic": True,
            "predicates": ("continues_table@1",),
            "statuses": ("recorded", "confirmed"),
        }
    )
    for policy, status, expected in (
        ("derived", "recorded", True),
        ("confirmed_only", "recorded", False),
        ("confirmed_only", "confirmed", True),
        ("none", "confirmed", False),
        ("derived", "proposed", False),
        ("derived", "rejected", False),
    ):
        configured = base.model_copy(
            update={
                "values": base.values.model_copy(
                    update={
                        "retrieval": base.values.retrieval.model_copy(
                            update={"table_continuations": policy, "graph": graph}
                        )
                    }
                )
            }
        )
        result = assemble_segments(
            ranked,
            view,
            configured,
            Counter(),
            graph_view=graph_view,
            continuation_paths=(path.model_copy(update={"status": status}),),
        )
        segment = result.segments[0]
        assert (second.unit_id in segment.context_only_unit_ids) is expected
        assert segment.citations[0].physical_page == 1
        if expected:
            assert segment.context_excerpts[0].text == second.text
            assert segment.citations[1].physical_page == 2
        else:
            assert len(segment.citations) == 1
    graph_view = graph_view.model_copy(
        update={"blocked_assertion_ids": frozenset(path.assertion_ids)}
    )
    configured = base.model_copy(
        update={
            "values": base.values.model_copy(
                update={
                    "retrieval": base.values.retrieval.model_copy(
                        update={"table_continuations": "derived", "graph": graph}
                    )
                }
            )
        }
    )
    assert (
        assemble_segments(
            ranked,
            view,
            configured,
            Counter(),
            graph_view=graph_view,
            continuation_paths=(path,),
        )
        .segments[0]
        .context_excerpts
        == ()
    )


def test_table_views_of_one_fragment_are_not_continuations():
    bundle = seed_bundle("Pump A", kind="manual@1")
    table = bundle.units[0].model_copy(
        update={
            "unit_type": "table",
            "boundary_key": "table:#/tables/0",
            "attributes": FrozenJsonMap({"source_refs": ("#/tables/0",)}),
        }
    )
    row = table.model_copy(update={"unit_type": "table_row"})
    other = table.model_copy(
        update={"attributes": FrozenJsonMap({"source_refs": ("#/tables/1",)})}
    )
    assert not _separate_fragments(table, row)
    assert _separate_fragments(table, other)
    assert _separate_fragments(bundle.units[0], bundle.units[0])
    assert not _separate_fragments(table, bundle.units[0])


def test_owned_match_expands_to_paragraph_boundary_without_changing_source(tmp_path):
    text = "Earlier paragraph.\nTarget sentence continues.\nLater paragraph."
    start = text.index("Target")
    bundle, _, ranked, view = case(
        text, (RerankMatch(None, "placeholder", start, start + 6, "lexical", 1, 1.0),)
    )
    segment = assemble_segments(ranked, view, config(tmp_path), Counter()).segments[0]
    excerpt = segment.excerpts[0]
    assert excerpt.text == "Target sentence continues.\n"
    assert excerpt.text == bundle.units[0].text[excerpt.char_start : excerpt.char_end]
    assert segment.anchors[0].char_start == start


def test_lane_reservation_and_visual_space_under_tight_budget(tmp_path):
    _, _, standard, standard_view = case("Standard", ())
    _, _, authority, authority_view = case(
        "Authority", (), kind="law@1", source_key="law.md"
    )
    ranked = RankedEvidence((*standard.items, *authority.items))
    view = standard_view.model_copy(
        update={"revisions": {**standard_view.revisions, **authority_view.revisions}}
    )
    base = config(tmp_path)
    values = base.values.model_copy(
        update={
            "selection": base.values.selection.model_copy(
                update={"lane_reservation": "one_per_available_lane"}
            ),
            "output": base.values.output.model_copy(
                update={"lane_order": ("authority", "standard")}
            ),
            "limits": base.values.limits.model_copy(
                update={"max_tokens": 30, "limit": 2}
            ),
        }
    )
    selected = assemble_segments(
        ranked, view, base.model_copy(update={"values": values}), Counter()
    )
    assert selected.visual_reserved_tokens == 3
    assert selected.text_budget == 27
    assert [segment.lane for segment in selected.segments] == ["authority", "standard"]
    assert selected.lane_coverage == ("authority", "standard")


def test_same_boundary_neighbors_require_complete_current_eligibility(tmp_path):
    from hashlib import sha256

    from tychora.contracts.evidence import TextLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    bundle = seed_bundle("one|two|three")
    original = bundle.units[0]
    pieces = (
        ("left-neighbor", 0, 3, 0),
        ("middle-match", 4, 7, 1),
        ("right-neighbor", 8, 13, 2),
    )
    units = tuple(
        original.model_copy(
            update={
                "unit_id": uid,
                "ordinal": ordinal,
                "text": original.text[start:end],
                "text_sha": sha256(original.text[start:end].encode()).hexdigest(),
                "locators": (
                    TextLocator(
                        artifact_ref=bundle.revision.original_sha,
                        native_ref="manual.md",
                        char_start=start,
                        char_end=end,
                    ),
                ),
            }
        )
        for uid, start, end, ordinal in pieces
    )
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="manual@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=len(unit.text),
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    rows = bundle.model_copy(update={"units": units})
    candidate = FusedCandidate(("unit", units[1].unit_id), 1, 1.0, {"lexical": 1})
    ranked = RankedEvidence(
        (
            RankedEvidenceItem(
                candidate,
                None,
                _facts(
                    rows,
                    prepared,
                    (RerankMatch(None, units[1].unit_id, 0, 3, "lexical", 1, 1.0),),
                ),
                "disabled",
            ),
        )
    )
    view = _view(bundle)
    segment = assemble_segments(ranked, view, config(tmp_path), Counter()).segments[0]
    assert segment.context_only_unit_ids == (units[0].unit_id, units[2].unit_id)
    assert [
        locator_char_start(citation.locators[0]) for citation in segment.citations
    ] == [
        4,
        0,
        8,
    ]
    view = _view(rows, units, {units[1].unit_id, units[2].unit_id})
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    segment = result.segments[0]
    assert segment.context_only_unit_ids == (units[2].unit_id,)
    assert all(
        units[0].unit_id not in omission.unit_ids for omission in segment.omissions
    )
    assert all(citation.unit_id != units[0].unit_id for citation in segment.citations)
    assert all(anchor.unit_id != units[0].unit_id for anchor in result.omitted_anchors)


def test_one_eligible_ordinal_gap_can_join_matches_but_denied_gap_cannot(tmp_path):
    from hashlib import sha256

    from tychora.contracts.evidence import TextLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    bundle = seed_bundle("left|gap|right")
    original = bundle.units[0]
    spans = (("left-match", 0, 4, 0), ("gap-unit", 5, 8, 1), ("right-match", 9, 14, 2))
    units = tuple(
        original.model_copy(
            update={
                "unit_id": uid,
                "ordinal": ordinal,
                "text": original.text[start:end],
                "text_sha": sha256(original.text[start:end].encode()).hexdigest(),
                "locators": (
                    TextLocator(
                        artifact_ref=bundle.revision.original_sha,
                        native_ref="manual.md",
                        char_start=start,
                        char_end=end,
                    ),
                ),
            }
        )
        for uid, start, end, ordinal in spans
    )
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="manual@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=len(unit.text),
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    rows = bundle.model_copy(update={"units": units})
    ranked = RankedEvidence(
        tuple(
            RankedEvidenceItem(
                FusedCandidate(
                    ("unit", unit.unit_id), order, 1 / order, {"lexical": order}
                ),
                None,
                _facts(
                    rows,
                    prepared,
                    (
                        RerankMatch(
                            None, unit.unit_id, 0, len(unit.text), "lexical", order, 1.0
                        ),
                    ),
                ),
                "disabled",
            )
            for order, unit in enumerate((units[0], units[2]), 1)
        )
    )
    view = _view(bundle)
    joined = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(joined.segments) == 1
    assert joined.segments[0].matched_unit_ids == (units[0].unit_id, units[2].unit_id)
    assert joined.segments[0].context_only_unit_ids == (units[1].unit_id,)
    view = _view(rows, units, {units[0].unit_id, units[2].unit_id})
    split = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert len(split.segments) == 2
    assert all(
        units[1].unit_id not in segment.context_only_unit_ids
        for segment in split.segments
    )


def test_code_import_context_is_separate_bounded_and_owner_checked(tmp_path):
    from hashlib import sha256

    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.corpus import StructuredRecordRow
    from tychora.contracts.evidence import CodeLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    source = "import os\n\ndef run(): return 1\n"
    bundle = seed_bundle(source, kind="code_file@1")
    start = source.index("def run")
    units = []
    for uid, left, right, ordinal in (
        ("import-unit", 0, 10, 0),
        ("function-unit", start, len(source), 1),
    ):
        text = source[left:right]
        unit = bundle.units[0].model_copy(
            update={
                "unit_id": uid,
                "ordinal": ordinal,
                "unit_type": "symbol",
                "text": text,
                "text_sha": sha256(text.encode()).hexdigest(),
                "locators": (
                    CodeLocator(
                        artifact_ref=bundle.revision.original_sha,
                        native_ref="demo.py",
                        relative_path="demo.py",
                        char_start=left,
                        char_end=right,
                        byte_start=left,
                        byte_end=right,
                        line_start=1 if ordinal == 0 else 3,
                        line_end=1 if ordinal == 0 else 3,
                    ),
                ),
            }
        )
        units.append(unit)
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="code_file@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=len(unit.text),
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    record = StructuredRecordRow(
        unit_id=units[1].unit_id,
        record_type="code_symbol",
        schema_hash="a" * 64,
        data_json=FrozenJsonMap(
            {
                "context_spans": (
                    {
                        "source_unit_id": units[0].unit_id,
                        "char_start": 0,
                        "char_end": len(units[0].text),
                        "reason": "referenced_import",
                    },
                ),
            }
        ),
    )
    rows = _mapped_code_rows(bundle, tuple(units), {"demo.py": source}).model_copy(
        update={"records": (record,)}
    )
    candidate = FusedCandidate(("unit", units[1].unit_id), 1, 1.0, {"lexical": 1})
    ranked = RankedEvidence(
        (
            RankedEvidenceItem(
                candidate,
                None,
                _facts(
                    rows,
                    prepared,
                    (
                        RerankMatch(
                            None,
                            units[1].unit_id,
                            0,
                            len(units[1].text),
                            "lexical",
                            1,
                            1.0,
                        ),
                    ),
                ),
                "disabled",
            ),
        )
    )
    view = _view(bundle)
    segment = assemble_segments(ranked, view, config(tmp_path), Counter()).segments[0]
    assert segment.text == units[1].text
    assert segment.context_excerpts[0].text == units[0].text
    assert (
        sum(Counter().count(excerpt.text) for excerpt in segment.context_excerpts)
        <= 160
    )
    assert locator_char_start(segment.citations[1].locators[0]) == 0
    view = _view(rows, units, {units[1].unit_id})
    assert (
        assemble_segments(ranked, view, config(tmp_path), Counter())
        .segments[0]
        .context_excerpts
        == ()
    )


def test_verified_output_counter_counts_literal_source_tokens():
    import os
    from pathlib import Path

    import pytest

    root = os.environ.get("TYCHORA_TEST_MODEL_CACHE")
    if root is None:
        pytest.skip("requires explicitly provisioned local output tokenizer")
    counter = OutputTokenCounter(Path(root))
    assert counter.tokenizer_id == "cl100k_base"
    assert counter.count("Café 🧬 <|endoftext|>") > 0


def test_historical_role_comes_from_validated_selected_attempt(tmp_path):
    import json
    from typing import Any, cast

    import pytest

    from tests.helpers.eligibility import member_row, members_transaction
    from tests.retrieval.test_eligibility import _request, _snapshot
    from tychora.configuration.registry import Registry
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.errors import TychoraError
    from tychora.contracts.identity import canonical_bytes, digest
    from tychora.contracts.requests import RequestScope
    from tychora.retrieval.compiler import EligibilityCompiler, EligibilityInputs

    base = config(tmp_path)
    registry_data = json.loads(canonical_bytes(base.registry))
    registry_data["compatibility"]["kind_lanes"] = {"manual@0": "standard"}
    cfg = base.model_copy(
        update={
            "registry": FrozenJsonMap(registry_data),
            "registry_digest": digest("registry-v1", registry_data),
        }
    )
    envelope = json.loads(
        canonical_bytes(Registry.from_config(base).historical_definitions()["manual@1"])
    )
    original_role = envelope["kind"]["role"]
    envelope["kind"]["id"] = "manual@0"
    envelope["kind_digest"] = digest("kind-v1", envelope["kind"])
    envelope["envelope_digest"] = digest(
        "historical-kind-v1",
        {key: value for key, value in envelope.items() if key != "envelope_digest"},
    )
    bundle, prepared, ranked, _ = case("Historical text", ())
    tx = members_transaction(
        (
            member_row(
                bundle.revision.revision_id,
                envelope["envelope_digest"],
                source_id=bundle.source.source_id,
                extraction_id=bundle.extraction.extraction_id,
                kind_id="manual@0",
            ),
        ),
        {"manual@0": envelope},
    )
    scope = RequestScope(
        principal_id="principal",
        collection_ids=frozenset({"collection"}),
        access_ceiling=2,
        uses=frozenset({"audit"}),
        capabilities=frozenset({"search"}),
    )
    snapshot = _snapshot(cfg, chunks=())
    compiler = EligibilityCompiler(Registry.from_config(cfg), cast(Any, None))
    compiler.mark_compatible(snapshot, cfg, cfg)
    attempt = compiler.compile(tx, EligibilityInputs(snapshot, scope, _request(), cfg))
    assert attempt.historical_kind_roles == {"manual@0": original_role}
    assert (
        attempt.historical_kind_envelopes["manual@0"]["envelope_digest"]
        == envelope["envelope_digest"]
    )
    rows = bundle.model_copy(
        update={"revision": bundle.revision.model_copy(update={"kind_id": "manual@0"})}
    )
    prepared = prepared.model_copy(
        update={
            "units": (prepared.units[0].model_copy(update={"kind_id": "manual@0"}),)
        }
    )
    ranked = RankedEvidence(
        (
            replace(
                ranked.items[0],
                facts=replace(ranked.items[0].facts, rows=rows, prepared=prepared),
            ),
        )
    )
    segment = assemble_segments(
        ranked,
        _view(bundle),
        cfg,
        Counter(),
        historical_kind_roles=attempt.historical_kind_roles,
    ).segments[0]
    assert (
        segment.kind == "manual@0"
        and segment.lane == "standard"
        and segment.role == original_role
    )
    with pytest.raises(TychoraError) as missing:
        assemble_segments(ranked, _view(bundle), cfg, Counter())
    assert missing.value.code == "PROFILE_INCOMPATIBLE"


def test_caller_total_budget_preserves_a_bounded_winning_subrange(tmp_path):
    bundle, _, ranked, view = case("W" * 200, ())
    selection = assemble_segments(
        ranked, view, config(tmp_path), Counter(), total_tokens=20
    )
    assert selection.visual_reserved_tokens == 2
    assert selection.text_budget == 18
    assert len(selection.segments) == 1
    assert selection.segments[0].text == bundle.units[0].text[:18]
    assert selection.segments[0].truncated
    source = "earlier " * 20 + "WINNER at end"
    offset = source.index("WINNER")
    _, _, end_ranked, end_view = case(
        source,
        (RerankMatch(None, "placeholder", offset, len(source), "lexical", 1, 1.0),),
    )
    end_selection = assemble_segments(
        end_ranked, end_view, config(tmp_path), Counter(), total_tokens=20
    )
    assert end_selection.segments[0].text.startswith("WINNER")


def test_code_diversity_counts_each_file_not_entire_repository(tmp_path):
    from hashlib import sha256

    from tychora.contracts.evidence import CodeLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    bundle = seed_bundle("ABCD", kind="code_file@1")
    root = bundle.units[0]
    units = tuple(
        root.model_copy(
            update={
                "unit_id": f"symbol-{index}",
                "ordinal": index,
                "unit_type": "symbol",
                "text": letter,
                "text_sha": sha256(letter.encode()).hexdigest(),
                "locators": (
                    CodeLocator(
                        artifact_ref=bundle.revision.original_sha,
                        native_ref=path,
                        relative_path=path,
                        char_start=index,
                        char_end=index + 1,
                        byte_start=index,
                        byte_end=index + 1,
                        line_start=1,
                        line_end=1,
                    ),
                ),
            }
        )
        for index, (letter, path) in enumerate(
            (("A", "a.py"), ("B", "a.py"), ("C", "a.py"), ("D", "b.py"))
        )
    )
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="code_file@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=1,
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    rows = _mapped_code_rows(bundle, units, {"a.py": "ABCD", "b.py": "ABCD"})
    ranked = RankedEvidence(
        tuple(
            RankedEvidenceItem(
                FusedCandidate(
                    ("unit", unit.unit_id), index, 1 / index, {"lexical": index}
                ),
                None,
                _facts(
                    rows,
                    prepared,
                    (RerankMatch(None, unit.unit_id, 0, 1, "lexical", index, 1.0),),
                ),
                "disabled",
            )
            for index, unit in enumerate(units, 1)
        )
    )
    view = _view(bundle)
    result = assemble_segments(ranked, view, config(tmp_path), Counter())
    assert [segment.excerpts[0].text for segment in result.segments] == ["A", "B", "D"]
    assert result.omitted_anchors[0].unit_id == units[2].unit_id


def test_clipped_code_citation_uses_retained_encoding_and_cr_line_map(tmp_path):
    from hashlib import sha256

    from tests.helpers.corpus import artifact
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.corpus import LocatorRow, RevisionArtifactRow
    from tychora.contracts.evidence import CodeLocator
    from tychora.contracts.identity import digest
    from tychora.retrieval.segments import _citation

    cases = (
        ("latin-1", "café\rnext\rfin", 0, 6, 10),
        ("utf-8-sig", "café\rnext\rfin", 3, 6, 10),
    )
    for encoding, text, preamble, start, end in cases:
        bundle = seed_bundle(text, kind="code_file@1")
        original = artifact(text.encode(encoding), "text/plain")
        path = "src/é.py"
        empty_sha = sha256(b"").hexdigest()
        container = bundle.units[0].model_copy(
            update={
                "unit_id": "file-container",
                "unit_type": "code_file",
                "native_ref": f"{path}#file",
                "boundary_key": f"file:{path}",
                "text": "",
                "text_sha": empty_sha,
                "citable": False,
                "locators": (
                    CodeLocator(
                        artifact_ref=original.sha256,
                        native_ref=path,
                        relative_path=path,
                        char_start=0,
                        char_end=0,
                        byte_start=preamble,
                        byte_end=preamble,
                        line_start=1,
                        line_end=1,
                    ),
                ),
                "attributes": FrozenJsonMap(
                    {
                        "relative_path": path,
                        "language": "python",
                        "encoding": encoding,
                        "coverage": "text_fallback",
                        "artifact_ref": original.sha256,
                        "adapter_fingerprint": "1" * 64,
                    }
                ),
            }
        )
        source = bundle.units[0].model_copy(
            update={
                "parent_id": container.unit_id,
                "ordinal": 1,
                "unit_type": "code",
                "boundary_key": f"file:{path}",
                "locators": (
                    CodeLocator(
                        artifact_ref=original.sha256,
                        native_ref=path,
                        relative_path=path,
                        char_start=0,
                        char_end=len(text),
                        byte_start=preamble,
                        byte_end=len(text.encode(encoding)),
                        line_start=1,
                        line_end=3,
                    ),
                ),
                "attributes": FrozenJsonMap(
                    {
                        "relative_path": path,
                        "language": "python",
                        "range_dependency_ids": [bundle.units[0].unit_id],
                    }
                ),
            }
        )
        rows = bundle.model_copy(
            update={
                "units": (container, source),
                "locators": tuple(
                    LocatorRow(
                        locator_id=digest("locator-v1", unit.unit_id, 0),
                        unit_id=unit.unit_id,
                        fragment_ordinal=0,
                        locator=unit.locators[0],
                    )
                    for unit in (container, source)
                ),
                "artifacts": (*bundle.artifacts, original),
                "revision_artifacts": (
                    *bundle.revision_artifacts,
                    RevisionArtifactRow(
                        revision_id=bundle.revision.revision_id,
                        role="source_file",
                        source_ref=path,
                        artifact_sha=original.sha256,
                    ),
                ),
            }
        )
        prepared = prepare_extraction(rows)
        item = RankedEvidenceItem(
            FusedCandidate(("unit", source.unit_id), 1, 1.0, {"lexical": 1}),
            None,
            _facts(rows, prepared, ()),
            "disabled",
        )
        clipped = _citation(item, source.unit_id, start, end).locators[0]
        assert isinstance(clipped, CodeLocator)
        assert clipped.char_start == start and clipped.char_end == end
        assert clipped.byte_start == len(text[:start].encode(encoding))
        assert clipped.byte_end == len(text[:end].encode(encoding))
        assert clipped.line_start == 2
        assert clipped.line_end == 2


def test_clipped_code_citation_uses_validated_capture_descriptor_map():
    from tychora.contracts.evidence import CodeLocator
    from tychora.retrieval.segments import _citation

    text = "café\rnext\rfin"
    bundle = seed_bundle(text, kind="code_file@1")
    source = bundle.units[0].model_copy(
        update={
            "unit_type": "symbol",
            "locators": (
                CodeLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="x.py",
                    relative_path="x.py",
                    char_start=0,
                    char_end=len(text),
                    byte_start=0,
                    byte_end=len(text.encode()),
                    line_start=1,
                    line_end=3,
                ),
            ),
        }
    )
    rows = _mapped_code_rows(bundle, (source,), {"x.py": text})
    prepared = prepare_extraction(rows)
    item = RankedEvidenceItem(
        FusedCandidate(("unit", source.unit_id), 1, 1.0, {"lexical": 1}),
        None,
        _facts(rows, prepared, ()),
        "disabled",
    )
    clipped = _citation(item, source.unit_id, 6, 10).locators[0]
    assert isinstance(clipped, CodeLocator)
    assert clipped.byte_start == len(text[:6].encode())
    assert clipped.byte_end == len(text[:10].encode())
    assert clipped.line_start == clipped.line_end == 2


def test_empty_range_on_textless_picture_locator_yields_no_fragment():
    # Pictures retain zero-width document locators; an empty anchor range has
    # nothing to excerpt and must not be reported as an integrity failure.
    import pytest

    from tychora.contracts.errors import TychoraError
    from tychora.contracts.evidence import DocumentLocator
    from tychora.retrieval.segments import _physical_ranges

    text = "caption"
    bundle, prepared, ranked, _view = case(
        text, (RerankMatch(None, "placeholder", 0, len(text), "lexical", 1, 1.0),)
    )
    unit = bundle.units[0].model_copy(
        update={
            "locators": (
                DocumentLocator(
                    artifact_ref=bundle.revision.original_sha,
                    native_ref="#/pictures/0",
                    char_start=0,
                    char_end=0,
                    physical_page=3,
                ),
            )
        }
    )
    prepared = prepared.model_copy(
        update={"units": (prepared.units[0].model_copy(update={"unit": unit}),)}
    )
    item = replace(
        ranked.items[0], facts=replace(ranked.items[0].facts, prepared=prepared)
    )
    assert _physical_ranges(item, unit.unit_id, 0, 0) == ()
    with pytest.raises(TychoraError, match="no physical fragment"):
        _physical_ranges(item, unit.unit_id, 0, len(text))


def test_separator_between_table_cell_fragments_yields_no_fragment():
    import pytest

    from tychora.contracts.errors import TychoraError
    from tychora.contracts.evidence import DocumentLocator
    from tychora.retrieval.segments import _physical_ranges

    text = "rowA\trowB"
    bundle, prepared, ranked, _view = case(
        text, (RerankMatch(None, "placeholder", 0, len(text), "lexical", 1, 1.0),)
    )
    cells = tuple(
        DocumentLocator(
            artifact_ref=bundle.revision.original_sha,
            native_ref=f"#/tables/0:{index}",
            char_start=start,
            char_end=end,
            physical_page=1,
        )
        for index, (start, end) in enumerate(((0, 4), (5, 9)))
    )
    unit = bundle.units[0].model_copy(update={"locators": cells})
    prepared = prepared.model_copy(
        update={"units": (prepared.units[0].model_copy(update={"unit": unit}),)}
    )
    item = replace(
        ranked.items[0], facts=replace(ranked.items[0].facts, prepared=prepared)
    )
    assert _physical_ranges(item, unit.unit_id, 4, 5) == ()
    assert _physical_ranges(item, unit.unit_id, 3, 6) == ((3, 4), (5, 6))
    gapped = unit.model_copy(update={"locators": cells[:1]})
    prepared = prepared.model_copy(
        update={"units": (prepared.units[0].model_copy(update={"unit": gapped}),)}
    )
    item = replace(item, facts=replace(item.facts, prepared=prepared))
    with pytest.raises(TychoraError, match="no physical fragment"):
        _physical_ranges(item, unit.unit_id, 5, 9)


def _document_case(spans):
    """Docling-shaped units: per-paragraph refs, one section key per heading path.

    A span may carry a fourth item, its boundary key, to model articles or
    sibling sections that share a heading path.
    """
    from hashlib import sha256

    from tychora.contracts.evidence import DocumentLocator
    from tychora.contracts.indexing import PreparedExtraction, RepresentationUnit

    text = "|".join(span[1] for span in spans)
    bundle = seed_bundle(text)
    original = bundle.units[0]
    units = []
    cursor = 0
    for ordinal, (uid, piece, heading, *key) in enumerate(spans):
        units.append(
            original.model_copy(
                update={
                    "unit_id": uid,
                    "ordinal": ordinal,
                    "native_ref": f"#/texts/{ordinal}",
                    "boundary_key": key[0] if key else "section:" + "/".join(heading),
                    "heading_path": heading,
                    "text": piece,
                    "text_sha": sha256(piece.encode()).hexdigest(),
                    "locators": (
                        DocumentLocator(
                            artifact_ref=bundle.revision.original_sha,
                            native_ref=f"#/texts/{ordinal}",
                            char_start=0,
                            char_end=len(piece),
                            physical_page=ordinal + 1,
                        ),
                    ),
                }
            )
        )
        cursor += len(piece) + 1
    units = tuple(units)
    prepared = PreparedExtraction(
        extraction_id=bundle.extraction.extraction_id,
        revision_id=bundle.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=bundle.revision.revision_id,
                title=bundle.revision.title,
                kind_id="manual@1",
                ownership=(
                    OwnedInterval(
                        char_start=0,
                        char_end=len(unit.text),
                        dependency_ids=frozenset(
                            {bundle.revision.revision_id, unit.unit_id}
                        ),
                    ),
                ),
            )
            for unit in units
        ),
    )
    rows = bundle.model_copy(update={"units": units})
    view = _view(bundle)
    return units, rows, prepared, view


def _ranked(rows, prepared, matched_units):
    return RankedEvidence(
        tuple(
            RankedEvidenceItem(
                FusedCandidate(
                    ("unit", unit.unit_id), order, 1 / order, {"lexical": order}
                ),
                None,
                _facts(
                    rows,
                    prepared,
                    (
                        RerankMatch(
                            None, unit.unit_id, 0, len(unit.text), "lexical", order, 1.0
                        ),
                    ),
                ),
                "disabled",
            )
            for order, unit in enumerate(matched_units, 1)
        )
    )


def test_document_neighbors_come_from_the_same_heading_section(tmp_path):
    section, other = ("Pumps", "Seals"), ("Pumps", "Impellers")
    units, rows, prepared, view = _document_case(
        (
            ("before", "Check the housing.", section),
            ("match", "Inspect seals quarterly.", section),
            ("after", "Record the outcome.", section),
            ("next-section", "Replace worn impellers.", other),
        )
    )
    segment = assemble_segments(
        _ranked(rows, prepared, (units[1],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == (units[0].unit_id, units[2].unit_id)
    # Neighbour citations keep their physical pages.
    pages = {}
    for citation in segment.citations:
        locator = citation.locators[0]
        assert isinstance(locator, DocumentLocator)
        pages[citation.unit_id] = locator.physical_page
    assert pages == {units[1].unit_id: 2, units[0].unit_id: 1, units[2].unit_id: 3}

    edge = assemble_segments(
        _ranked(rows, prepared, (units[2],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert edge.context_only_unit_ids == (units[1].unit_id,)


def test_document_matches_join_across_an_eligible_gap_in_one_section(tmp_path):
    section, other = ("Pumps",), ("Valves",)
    units, rows, prepared, view = _document_case(
        (
            ("left", "Seal checks are quarterly.", section),
            ("gap", "Use the approved form.", section),
            ("right", "Seal records are kept.", section),
            ("elsewhere", "Seal valves yearly.", other),
        )
    )
    joined = assemble_segments(
        _ranked(rows, prepared, (units[0], units[2])), view, config(tmp_path), Counter()
    )
    assert len(joined.segments) == 1
    assert joined.segments[0].matched_unit_ids == (units[0].unit_id, units[2].unit_id)

    split = assemble_segments(
        _ranked(rows, prepared, (units[2], units[3])), view, config(tmp_path), Counter()
    )
    assert len(split.segments) == 2


def test_consecutive_articles_under_one_heading_are_not_neighbours(tmp_path):
    heading = ("Regulation", "Title I")
    units, rows, prepared, view = _document_case(
        (
            ("article-1", "Article 1 applies to pumps.", heading, "article:#/a/1"),
            ("article-2", "Article 2 applies to seals.", heading, "article:#/a/2"),
            ("article-3", "Article 3 applies to valves.", heading, "article:#/a/3"),
        )
    )
    segment = assemble_segments(
        _ranked(rows, prepared, (units[1],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == ()
    joined = assemble_segments(
        _ranked(rows, prepared, (units[0], units[2])), view, config(tmp_path), Counter()
    )
    assert len(joined.segments) == 2


def test_sibling_sections_with_identical_headings_are_not_neighbours(tmp_path):
    heading = ("Manual", "Notes")
    units, rows, prepared, view = _document_case(
        (
            ("first-notes", "Check the housing.", heading, "section:#/texts/0"),
            ("second-notes", "Inspect seals quarterly.", heading, "section:#/texts/2"),
        )
    )
    segment = assemble_segments(
        _ranked(rows, prepared, (units[1],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == ()


def test_document_tables_never_join_as_plain_neighbors(tmp_path):
    section = ("Results",)
    units, rows, prepared, view = _document_case(
        (
            ("intro", "Measured values follow.", section),
            ("table", "Pump\tHours", section),
        )
    )
    units = (units[0], units[1].model_copy(update={"unit_type": "table"}))
    prepared = prepared.model_copy(
        update={
            "units": tuple(
                rep.model_copy(update={"unit": unit})
                for rep, unit in zip(prepared.units, units)
            )
        }
    )
    rows = rows.model_copy(update={"units": units})
    segment = assemble_segments(
        _ranked(rows, prepared, (units[0],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == ()


def test_lane_and_role_read_the_frozen_registry_without_dumping(tmp_path, monkeypatch):
    from tychora.configuration.models import ResolvedConfig
    from tychora.retrieval import segments

    resolved = config(tmp_path)

    def forbidden(self, *args, **kwargs):
        raise AssertionError("lane lookup must not dump the configuration")

    monkeypatch.setattr(ResolvedConfig, "model_dump", forbidden)
    assert segments._lane(resolved, "manual@1")
    assert segments._role(resolved, "manual@1", {})


def test_neighbour_context_attaches_within_native_section(native_case, tmp_path):  # noqa: F811
    text = (
        "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
        "Record the outcome.\n\n# Valves\n\nReplace worn valves.\n"
    )
    parsed, _, registry = native_case(text)
    rows = normalize(parsed, parsed.registration.revision.revision_id, registry)
    prepared = prepare_extraction(rows)
    units = rows.units
    view = _view(rows)
    segment = assemble_segments(
        _ranked(rows, prepared, (units[2],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert segment.context_only_unit_ids == (units[1].unit_id, units[3].unit_id)
    edge = assemble_segments(
        _ranked(rows, prepared, (units[3],)), view, config(tmp_path), Counter()
    ).segments[0]
    assert edge.context_only_unit_ids == (units[2].unit_id,)
