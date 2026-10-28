"""Temporal selection over retained snapshot editions."""

from datetime import date
from hashlib import sha256

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
    )


def test_current_uses_declared_default_and_explicit_overrides_it():
    old = _revision("old", start="2022-01-01")
    current = _revision("current", start="2023-01-01", default=True)
    assert select_temporal_revisions((old, current)) == (frozenset({"current"}), ())
    assert select_temporal_revisions(
        (old, current), TemporalSelector(explicit_ids=("old",))
    ) == (
        frozenset({"old"}),
        (),
    )
    assert select_temporal_revisions(
        (old, current), TemporalSelector(selector="all")
    ) == (
        frozenset({"old", "current"}),
        (),
    )


def test_as_of_filters_half_open_intervals_and_picks_latest_start():
    old = _revision("old", start="2022-01-01", end="2024-01-01")
    current = _revision("current", start="2024-01-01", default=True)
    assert select_temporal_revisions(
        (old, current), TemporalSelector(as_of=date(2023, 12, 31))
    )[0] == frozenset({"old"})
    assert select_temporal_revisions(
        (old, current), TemporalSelector(as_of=date(2024, 1, 1))
    )[0] == frozenset({"current"})


def test_cutoff_is_independent_of_effective_date():
    early = _revision("early", start="2020-01-01", published="2021-01-01")
    late = _revision("late", start="2022-01-01", published="2025-01-01", default=True)
    assert select_temporal_revisions(
        (early, late), TemporalSelector(as_of=date(2023, 1, 1), cutoff=date(2024, 1, 1))
    )[0] == frozenset({"early"})
    assert select_temporal_revisions(
        (early, late), TemporalSelector(cutoff=date(2024, 1, 1), selector="all")
    )[0] == frozenset({"early"})


def test_unknown_dates_supplement_known_winner_only_when_permissive():
    known = _revision("known", start="2022-01-01")
    unknown = _revision("unknown")
    assert select_temporal_revisions(
        (known, unknown), TemporalSelector(as_of=date(2023, 1, 1))
    )[0] == frozenset({"known"})
    assert select_temporal_revisions(
        (known, unknown),
        TemporalSelector(as_of=date(2023, 1, 1), unknown_dates="permissive"),
    )[0] == frozenset({"known", "unknown"})


def test_tied_latest_editions_warn_and_keep_both():
    first = _revision("first", start="2022-01-01")
    second = _revision("second", start="2022-01-01", default=True)
    ids, warnings = select_temporal_revisions(
        (first, second), TemporalSelector(as_of=date(2023, 1, 1))
    )
    assert ids == frozenset({"first", "second"})
    assert warnings


def test_explicit_revision_still_obeys_date_constraints():
    old = _revision("old", start="2020-01-01", end="2021-01-01")
    assert (
        select_temporal_revisions(
            (old,), TemporalSelector(explicit_ids=("old",), as_of=date(2022, 1, 1))
        )[0]
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
        member_row(name, definition_hash, is_default=False, effective_from="2023-01-01")
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
    assert compiled.warnings
    assert set(compiled.view.revisions) == {"one", "two"}
