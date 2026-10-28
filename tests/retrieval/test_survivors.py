"""Fused candidates survive only when their persisted dependency sets are readable."""

from types import SimpleNamespace

from tests.helpers.eligibility import revision_view
from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.contracts.candidates import CandidateKey
from tychora.ports.repositories import OwnerFact
from tychora.retrieval import survivors as survivors_module
from tychora.retrieval.admission import LanePolicy, admit
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.survivors import (
    SURVIVOR_LIMIT,
    check_survivors,
    load_survivors,
    survivor_ids,
    survivor_selection,
)


def _unit(unit_id, *, deps=None, citable=True):
    return OwnerFact(
        owner_id=unit_id,
        kind="unit",
        extraction_id="extraction",
        dependency_ids=frozenset(deps or {"revision", unit_id}),
        unit_id=unit_id,
        citable=citable,
    )


def _chunk(chunk_id, unit_id, *, deps=None):
    return OwnerFact(
        owner_id=chunk_id,
        kind="chunk",
        extraction_id="extraction",
        dependency_ids=frozenset(deps or {"revision", unit_id}),
        unit_id=unit_id,
        chunk_id=chunk_id,
        citable=True,
    )


def _candidate(key, windows=()):
    return FusedCandidate(
        key=key, rank=1, score=1.0, ranks={"lexical": 1}, matching_window_ids=windows
    )


def test_survivor_check_drops_ineligible_dependency():
    view = revision_view(excluded_units=("u-excluded",))
    facts = {
        f.owner_id: f
        for f in (
            _unit("u-ok"),
            _chunk("c-ok", "u-ok"),
            _chunk("c-overlap", "u-ok", deps={"revision", "u-ok", "u-excluded"}),
            _unit("u-bad", deps={"revision", "u-bad", "u-excluded"}),
        )
    }
    survivors = check_survivors(
        view,
        (
            _candidate(("unit", "u-ok"), ("c-ok", "c-overlap")),
            _candidate(("unit", "u-bad")),
        ),
        facts,
    )
    assert [c.key for c in survivors.candidates] == [("unit", "u-ok")]
    assert survivors.candidates[0].matching_window_ids == ("c-ok",)
    assert survivors.dropped == 1


def test_a_candidate_whose_every_window_fails_is_dropped():
    view = revision_view(excluded_units=("u-x",))
    facts = {
        f.owner_id: f
        for f in (_unit("u"), _chunk("c", "u", deps={"revision", "u", "u-x"}))
    }
    survivors = check_survivors(view, (_candidate(("unit", "u"), ("c",)),), facts)
    assert survivors.candidates == ()
    assert survivors.dropped == 1


def test_lanes_need_no_sql():
    view = revision_view(lane="authority")
    facts = {f.owner_id: f for f in (_unit("u"),)}
    survivors = check_survivors(view, (_candidate(("unit", "u")),), facts)
    assert survivors.lanes == {("unit", "u"): "authority"}


def test_a_candidate_of_an_ungranted_extraction_is_dropped():
    stray = OwnerFact(
        owner_id="u",
        kind="unit",
        extraction_id="other",
        dependency_ids=frozenset({"revision", "u"}),
        unit_id="u",
        citable=True,
    )
    survivors = check_survivors(
        revision_view(), (_candidate(("unit", "u")),), {"u": stray}
    )
    assert survivors.candidates == ()
    assert survivors.dropped == 1


def test_at_most_200_candidates_are_read():
    candidates = tuple(_candidate(("unit", f"u{i}"), (f"c{i}",)) for i in range(300))
    ids = survivor_ids(survivor_selection(candidates))
    assert SURVIVOR_LIMIT == 200
    assert len(ids) == 400
    assert "u199" in ids and "u200" not in ids


def test_candidates_beyond_the_cap_are_not_counted_as_dependency_drops():
    view = revision_view()
    candidates = tuple(_candidate(("unit", f"u{i}")) for i in range(SURVIVOR_LIMIT + 5))
    facts = {f"u{i}": _unit(f"u{i}") for i in range(SURVIVOR_LIMIT)}
    survivors = check_survivors(view, candidates, facts)
    assert len(survivors.candidates) == SURVIVOR_LIMIT
    assert survivors.dropped == 0
    assert survivors.beyond_limit == 5


def test_load_survivors_reads_proxy_window_units_and_asset_sources_once_more():
    asset = OwnerFact(
        owner_id="a",
        kind="asset",
        extraction_id="extraction",
        dependency_ids=frozenset({"revision", "a", "u"}),
        source_owner_ids=frozenset({"u"}),
    )
    proxy_window = OwnerFact(
        owner_id="c",
        kind="chunk",
        extraction_id="extraction",
        dependency_ids=frozenset({"revision", "p"}),
        unit_id="p",
        chunk_id="c",
        asset_id="a",
        proxy=True,
    )
    proxy = OwnerFact(
        owner_id="p",
        kind="unit",
        extraction_id="extraction",
        dependency_ids=frozenset({"revision", "p"}),
        unit_id="p",
        asset_id="a",
        proxy=True,
    )
    known = {f.owner_id: f for f in (asset, proxy_window, proxy, _unit("u"))}
    reads = []

    def owner_facts(ids):
        reads.append(ids)
        return tuple(known[i] for i in ids if i in known)

    tx = SimpleNamespace(eligibility=SimpleNamespace(owner_facts=owner_facts))
    view = revision_view(unit_filter=("u", "p"))
    survivors = load_survivors(
        tx, view, (_candidate(("asset", "a"), ("c",)), _candidate(("asset", "a")))
    )
    assert reads == [("a", "c"), ("p", "u")]
    assert [c.matching_window_ids for c in survivors.candidates] == [("c",), ()]
    assert survivors.dropped == 0


def _ranked(count):
    """``count`` fused unit candidates in descending score order."""
    return tuple(
        FusedCandidate(
            key=("unit", f"u{i}"),
            rank=i + 1,
            score=1.0 / (i + 1),
            ranks={"lexical": i + 1},
        )
        for i in range(count)
    )


def test_the_bound_reads_reserved_exact_and_graph_keys_first():
    candidates = _ranked(260)
    exact: tuple[CandidateKey, ...] = (("unit", "u230"),)
    graph: dict[str, tuple[CandidateKey, ...]] = {
        "structural": (("unit", "u240"), ("unit", "u-absent")),
        "semantic": (),
    }
    selected = survivor_selection(candidates, exact, graph)
    keys = [item.key for item in selected]
    assert len(keys) == SURVIVOR_LIMIT
    assert ("unit", "u230") in keys and ("unit", "u240") in keys
    # The rest is filled by fused rank, and fused order is kept.
    assert keys[:198] == [("unit", f"u{i}") for i in range(198)]
    assert ("unit", "u198") not in keys


def test_reservations_are_capped_at_admissions_places():
    candidates = _ranked(300)
    exact: tuple[CandidateKey, ...] = tuple(("unit", f"u{i}") for i in range(250, 290))
    graph: dict[str, tuple[CandidateKey, ...]] = {
        "semantic": tuple(("unit", f"u{i}") for i in range(200, 215))
    }
    keys = {item.key for item in survivor_selection(candidates, exact, graph)}
    assert len(keys & set(exact)) == 32
    assert len(keys & set(graph["semantic"])) == 8


def test_a_graph_only_key_past_the_bound_is_admitted_and_the_cut_reported():
    candidates = _ranked(SURVIVOR_LIMIT + 50)
    graph_key: CandidateKey = ("unit", f"u{SURVIVOR_LIMIT + 40}")
    graph: dict[str, tuple[CandidateKey, ...]] = {
        "structural": (graph_key,),
        "semantic": (),
    }
    facts = {f"u{i}": _unit(f"u{i}") for i in range(SURVIVOR_LIMIT + 50)}
    selected = survivor_selection(candidates, (), graph)
    survivors = check_survivors(revision_view(), selected, facts, total=len(candidates))
    assert survivors.beyond_limit == 50
    admission = admit(
        survivors.candidates,
        (),
        graph,
        LanePolicy(),
        lanes=survivors.lanes,
    )
    assert graph_key in {item.key for item in admission.selected}
    assert admission.reservation_coverage["graph"] == 1


def test_a_query_reports_the_survivor_cut_as_a_truncation(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for name in ("a", "b", "c"):
            job = kb.sources.add(
                {
                    "type": "text",
                    "source_key": f"{name}.md",
                    "collection_id": "default",
                    "kind": "document@1",
                    "format": "markdown",
                    "text": f"# {name}\n\nThe verification outcome {name}.\n",
                    "metadata": {"title": name},
                }
            )
            assert kb.jobs.wait(job.id).state == "succeeded"
        whole = kb.search({"question": "verification outcome"})
        assert "survivors" not in whole.coverage.truncated_channels
        monkeypatch.setattr(survivors_module, "SURVIVOR_LIMIT", 1)
        cut = kb.search({"question": "verification outcome", "limit": 3})
    assert "survivors" in cut.coverage.truncated_channels
    assert cut.coverage.status == "complete"
