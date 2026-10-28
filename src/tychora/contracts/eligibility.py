"""Revision-level eligibility: what one pinned query may read, without its units."""

import json
from collections.abc import Iterable, Mapping
from functools import cached_property

from pydantic import Field

from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt, Sha256
from .graph import GraphBatchReceipt


def json_array(ids: Iterable[str]) -> str:
    """One JSON array of ``ids`` in a stable order, for a ``json_each`` binding."""
    return json.dumps(sorted(ids))


class RevisionGrant(Contract):
    """One eligible revision: its selected extraction, kind, lane and collection."""

    extraction_id: Identifier
    source_id: Identifier
    kind_id: NonEmptyStr
    lane: NonEmptyStr
    collection_id: Identifier


class GraphBinding(Contract):
    """The graph a view reads and whether every snapshot member is eligible."""

    family_id: Sha256
    all_eligible: bool
    receipt: GraphBatchReceipt


class EligibilityView(Contract):
    """What one pinned query may read: O(revisions + exclusions), never O(units).

    A unit, chunk, asset or vector record is readable when its persisted
    dependency set is (``allows``); candidate rules add citability and the
    candidate filter on top.
    """

    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    scope_digest: Sha256
    eligibility_policy_digest: Sha256
    eligibility_request_digest: Sha256
    revisions: Mapping[Identifier, RevisionGrant]
    excluded_unit_ids: frozenset[Identifier] = frozenset()
    excluded_asset_ids: frozenset[Identifier] = frozenset()
    unit_filter: frozenset[Identifier] | None = None
    selected_batches: Mapping[NonEmptyStr, tuple[Identifier, ...]] = Field(
        default_factory=dict
    )
    # The extractions each slot's selected batches serve, from the same rows.
    batch_extractions: Mapping[NonEmptyStr, frozenset[Identifier]] = Field(
        default_factory=dict
    )
    graph: GraphBinding | None = None
    all_members_eligible: bool = False

    def allows(self, dependency_ids: Iterable[str]) -> bool:
        """Whether a persisted dependency set is readable under this view.

        A set names exactly one revision, its owner's, plus units and assets of
        that revision's extraction; exclusions are closed under dependency. The
        set is readable when its revision is eligible and it names no excluded
        unit or asset.
        """
        revision_seen = False
        for item in dependency_ids:
            if item in self.revisions:
                revision_seen = True
            elif item in self.excluded_unit_ids or item in self.excluded_asset_ids:
                return False
        return revision_seen

    @cached_property
    def revision_of_extraction(self) -> Mapping[str, str]:
        """The eligible revision of each selected extraction."""
        return {grant.extraction_id: rid for rid, grant in self.revisions.items()}

    @cached_property
    def extraction_ids(self) -> frozenset[str]:
        """Every selected extraction."""
        return frozenset(grant.extraction_id for grant in self.revisions.values())

    @cached_property
    def extraction_ids_json(self) -> str:
        """``extraction_ids`` as one JSON array binding, built once per view."""
        return json_array(self.extraction_ids)

    @cached_property
    def excluded_unit_ids_json(self) -> str:
        """``excluded_unit_ids`` as one JSON array binding, built once per view."""
        return json_array(self.excluded_unit_ids)

    @cached_property
    def excluded_asset_ids_json(self) -> str:
        """``excluded_asset_ids`` as one JSON array binding, built once per view."""
        return json_array(self.excluded_asset_ids)

    @cached_property
    def unit_filter_json(self) -> str | None:
        """``unit_filter`` as one JSON array binding, or None without a filter."""
        return None if self.unit_filter is None else json_array(self.unit_filter)

    @cached_property
    def batch_ids(self) -> frozenset[str]:
        """Every selected batch, whatever its slot."""
        return frozenset(
            batch for batches in self.selected_batches.values() for batch in batches
        )
