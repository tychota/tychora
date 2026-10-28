"""Temporal selection over retained snapshot editions."""

from datetime import date
from hashlib import sha256

import pytest

from tychora.contracts.identity import canonical_bytes
from tychora.retrieval.selection import (
    RevisionFact,
    TemporalSelector,
    select_temporal_revisions,
)


def _revision(
    name: str,
    *,
    start: str | None = None,
    end: str | None = None,
    published: str | None = None,
    default: bool = False,
    edition: str = "1",
) -> RevisionFact:
    return RevisionFact(
        source_id="source",
        revision_id=name,
        extraction_id=name + "-extraction",
        collection_id="collection",
        kind_id="manual@1",
        access_rank=0,
        source_floor=0,
        collection_floor=0,
        allowed_uses=frozenset({"audit"}),
        review_state="accepted",
        withdrawn=False,
        published_on=published,
        effective_from=start,
        effective_until=end,
        is_default=default,
        edition_key=edition,
    )


def test_current_uses_declared_default_and_explicit_overrides_it():
    old = _revision("old", start="2022-01-01")
    current = _revision("current", start="2023-01-01", default=True)
    plain = select_temporal_revisions((old, current))
    assert (plain.selected, plain.warnings) == (frozenset({"current"}), ())
    explicit = select_temporal_revisions(
        (old, current), TemporalSelector(explicit_ids=("old",))
    )
    assert (explicit.selected, explicit.warnings) == (frozenset({"old"}), ())
    every = select_temporal_revisions((old, current), TemporalSelector(selector="all"))
    assert (every.selected, every.warnings) == (frozenset({"old", "current"}), ())


def test_as_of_filters_half_open_intervals_and_picks_latest_start():
    old = _revision("old", start="2022-01-01", end="2024-01-01")
    current = _revision("current", start="2024-01-01", default=True)
    assert select_temporal_revisions(
        (old, current), TemporalSelector(as_of=date(2023, 12, 31))
    ).selected == frozenset({"old"})
    assert select_temporal_revisions(
        (old, current), TemporalSelector(as_of=date(2024, 1, 1))
    ).selected == frozenset({"current"})


def test_cutoff_is_independent_of_effective_date():
    early = _revision("early", start="2020-01-01", published="2021-01-01")
    late = _revision("late", start="2022-01-01", published="2025-01-01", default=True)
    assert select_temporal_revisions(
        (early, late), TemporalSelector(as_of=date(2023, 1, 1), cutoff=date(2024, 1, 1))
    ).selected == frozenset({"early"})
    assert select_temporal_revisions(
        (early, late), TemporalSelector(cutoff=date(2024, 1, 1), selector="all")
    ).selected == frozenset({"early"})


def test_unknown_dates_supplement_known_winner_only_when_permissive():
    known = _revision("known", start="2022-01-01")
    unknown = _revision("unknown")
    assert select_temporal_revisions(
        (known, unknown), TemporalSelector(as_of=date(2023, 1, 1))
    ).selected == frozenset({"known"})
    assert select_temporal_revisions(
        (known, unknown),
        TemporalSelector(as_of=date(2023, 1, 1), unknown_dates="permissive"),
    ).selected == frozenset({"known", "unknown"})


def test_tied_latest_editions_warn_and_keep_both():
    first = _revision("first", start="2022-01-01")
    second = _revision("second", start="2022-01-01", default=True)
    result = select_temporal_revisions(
        (first, second), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert result.selected == frozenset({"first", "second"})
    assert result.warnings == ("Retained editions tie for source source",)


def test_explicit_revision_still_obeys_date_constraints():
    old = _revision("old", start="2020-01-01", end="2021-01-01")
    assert (
        select_temporal_revisions(
            (old,), TemporalSelector(explicit_ids=("old",), as_of=date(2022, 1, 1))
        ).selected
        == frozenset()
    )


def test_compiled_attempt_exposes_overlap_warning(tmp_path):
    from typing import Any, cast

    from tests.helpers.eligibility import member_row, members_transaction
    from tests.typed_json import json_object, json_string
    from tychora.configuration.registry import Registry
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.identity import digest
    from tychora.contracts.publication import SnapshotMember, SnapshotView
    from tychora.contracts.requests import QueryRequest, RequestScope
    from tychora.retrieval.compiler import EligibilityCompiler, EligibilityInputs

    workspace = tmp_path / "workspace.toml"
    workspace.write_text(
        'schema_version = 1\n[profiles.test]\nbuiltin = "documents@1"\n'
    )
    config = resolve_config(workspace, "test", {})
    registry = Registry.from_config(config)
    envelope = json_object(registry.historical_definitions(), "manual@1")
    definition_hash = json_string(envelope["envelope_digest"])
    snapshot = SnapshotView(
        snapshot_id="snapshot",
        workspace_id="workspace",
        membership_sha=digest("membership"),
        config_sha=sha256(
            canonical_bytes(
                {
                    "type": "registry",
                    "schema_version": "1",
                    "data": config.model_dump(mode="json"),
                }
            )
        ).hexdigest(),
        member_count=2,
        page_ids=(),
        families=(),
    )

    rows = tuple(
        member_row(
            name,
            definition_hash,
            is_default=False,
            effective_from="2023-01-01",
            edition_key="A",
        )
        for name in ("one", "two")
    )
    tx = members_transaction(rows, {"manual@1": envelope})
    scope = RequestScope(
        principal_id="principal",
        collection_ids=frozenset({"collection"}),
        uses=frozenset({"audit"}),
    )
    compiler = EligibilityCompiler(registry, cast(Any, None))
    compiler.mark_compatible(snapshot, config, config)
    compiled = compiler.compile(
        tx,
        EligibilityInputs(
            snapshot,
            scope,
            QueryRequest.from_mapping(
                {"question": "Why?", "mode": "audit", "as_of": "2024-01-01"}
            ),
            config,
        ),
        members=(
            SnapshotMember(
                source_id="source", revision_id="one", extraction_id="one-extraction"
            ),
            SnapshotMember(
                source_id="source", revision_id="two", extraction_id="two-extraction"
            ),
        ),
    )
    assert [(d.reason, d.revision_ids) for d in compiled.decisions] == [
        ("tie", ("one", "two"))
    ]
    assert compiled.warnings == ("Retained editions tie for source source",)
    assert set(compiled.view.revisions) == {"one", "two"}


def test_unique_winner_yields_no_decision():
    old = _revision("old", start="2020-01-01", edition="1")
    new = _revision("new", start="2022-01-01", edition="2", default=True)
    result = select_temporal_revisions(
        (old, new), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert result.selected == {"new"}
    assert result.decisions == ()
    cutoff = select_temporal_revisions(
        (
            _revision("a", published="2020-01-01"),
            _revision("b", published="2021-01-01"),
        ),
        TemporalSelector(cutoff=date(2024, 1, 1)),
    )
    assert cutoff.selected == {"b"} and cutoff.decisions == ()


def test_effective_versus_publication_ordering():
    early_effect_late_publish = _revision(
        "x", start="2021-01-01", published="2023-06-01", edition="2"
    )
    late_effect_early_publish = _revision(
        "y", start="2022-01-01", published="2022-02-01", edition="1"
    )
    pair = (early_effect_late_publish, late_effect_early_publish)
    assert select_temporal_revisions(
        pair, TemporalSelector(as_of=date(2024, 1, 1))
    ).selected == {"y"}
    assert select_temporal_revisions(
        pair, TemporalSelector(cutoff=date(2024, 1, 1))
    ).selected == {"x"}


def test_tie_and_overlap_yield_one_decision():
    first = _revision("first", start="2022-01-01", edition="A")
    second = _revision("second", start="2022-01-01", edition="A")
    tie = select_temporal_revisions(
        (first, second), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert tie.selected == {"first", "second"}
    assert [(d.reason, d.revision_ids) for d in tie.decisions] == [
        ("tie", ("first", "second"))
    ]
    older = _revision("older", start="2020-01-01", end="2024-01-01", edition="1")
    newer = _revision("newer", start="2022-01-01", edition="2")
    overlap = select_temporal_revisions(
        (older, newer), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert overlap.selected == {"newer"}
    assert [(d.reason, d.revision_ids) for d in overlap.decisions] == [
        ("overlap", ("newer", "older"))
    ]
    superseded = _revision("superseded", start="2020-01-01", edition="1")
    quiet = select_temporal_revisions(
        (superseded, newer), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert quiet.selected == {"newer"} and quiet.decisions == ()


def test_undated_candidates_are_separate():
    known = _revision("known", start="2022-01-01")
    unknown = _revision("unknown", edition="2")
    result = select_temporal_revisions(
        (known, unknown),
        TemporalSelector(as_of=date(2023, 1, 1), unknown_dates="permissive"),
    )
    assert result.selected == {"known", "unknown"}
    assert result.undated == {"unknown"}
    assert [(d.reason, d.revision_ids) for d in result.decisions] == [
        ("undated", ("unknown",))
    ]
    strict = select_temporal_revisions(
        (known, unknown), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert strict.selected == {"known"} and strict.undated == frozenset()


def test_applicable_selector_lists_editions_in_force():
    older = _revision("older", start="2020-01-01", edition="1")
    newer = _revision("newer", start="2022-01-01", edition="2")
    expired = _revision("expired", start="2019-01-01", end="2021-01-01", edition="0")
    result = select_temporal_revisions(
        (older, newer, expired),
        TemporalSelector(selector="applicable", as_of=date(2023, 1, 1)),
    )
    assert result.selected == {"older", "newer"}
    assert [(d.reason, d.revision_ids) for d in result.decisions] == [
        ("applicable", ("newer", "older"))
    ]


def test_applicable_selector_requires_as_of():
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError) as error:
        select_temporal_revisions(
            (_revision("a", start="2020-01-01"),),
            TemporalSelector(selector="applicable"),
        )
    assert error.value.reason == "edition.as_of_required"


def test_edition_decisions_become_decision_requirements():
    from tychora.application.query import _edition_decisions
    from tychora.contracts.identity import digest
    from tychora.retrieval.selection import EditionDecision

    decision = EditionDecision("overlap", "source", ("newer", "older"))
    (requirement,) = _edition_decisions((decision,))
    assert requirement.requirement_id == digest(
        "edition-decision-v1", "overlap", "source", ["newer", "older"]
    )
    assert requirement.reason == "Overlapping retained editions for source source"
    assert requirement.revision_ids == ("newer", "older")
