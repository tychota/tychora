"""Verify returned locators against pinned, retained canonical bindings."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.eligibility import _retained_rows
from tychora.contracts.evidence import (
    Citation,
    CodeLocator,
    DocumentLocator,
    ImageLocator,
    QueryResult,
    TextLocator,
    VisualEvidence,
)
from tychora.evaluation.metrics import LocatorAssessment
from tychora.indexing.representations import project_code_excerpt_locator

if TYPE_CHECKING:
    from tychora.application.owner import WorkspaceOwner
    from tychora.contracts.evidence import Locator, Segment


def canonical_locator_matches(
    candidate: Locator, retained: Locator, rows, unit_id: str
) -> bool:
    """A clipped citation must retain its original identity and exact code map."""
    if type(candidate) is not type(retained):
        return False
    if (
        candidate.artifact_ref != retained.artifact_ref
        or candidate.native_ref != retained.native_ref
    ):
        return False
    if isinstance(candidate, ImageLocator):
        return candidate == retained
    assert isinstance(retained, (TextLocator, DocumentLocator, CodeLocator))
    if (
        not retained.char_start
        <= candidate.char_start
        < candidate.char_end
        <= retained.char_end
    ):
        return False
    if isinstance(candidate, CodeLocator):
        assert isinstance(retained, CodeLocator)
        if candidate.relative_path != retained.relative_path:
            return False
        if rows is None:
            return candidate == retained
        try:
            projected = project_code_excerpt_locator(
                rows,
                unit_id,
                candidate.char_start - retained.char_start,
                candidate.char_end - retained.char_start,
            )
        except (ValueError, KeyError, IndexError):
            return False
        return candidate == projected
    return candidate == retained.model_copy(
        update={
            "char_start": candidate.char_start,
            "char_end": candidate.char_end,
        }
    )


def visual_binding_matches(visual: VisualEvidence, asset, member) -> bool:
    """The returned visual must be the asset of this pinned extraction."""
    return (
        visual.asset_id == asset.asset_id
        and asset.extraction_id == member.extraction_id
        and visual.source_id == member.source_id
        and visual.revision_id == member.revision_id
        and visual.asset_ref == asset.artifact_sha
        and isinstance(asset.locator, ImageLocator)
        and visual.locator == asset.locator
        and visual.locator.artifact_ref == asset.artifact_sha
    )


def _citation_locators(
    result: QueryResult,
) -> Iterator[tuple[Citation, Locator, Segment | VisualEvidence | None]]:
    for segment in result.segments:
        for citation in segment.citations:
            for locator in citation.locators:
                yield citation, locator, segment
    for visual in result.visuals:
        for citation in visual.citations:
            for locator in citation.locators:
                yield citation, locator, visual
    for citation in result.citations:
        for locator in citation.locators:
            yield citation, locator, None


def verify_result_locators(
    owner: WorkspaceOwner, result: QueryResult
) -> LocatorAssessment:
    """Use only owner-serialized retained metadata, never reopen source payloads."""
    citations = tuple(_citation_locators(result))
    total = len(citations) + len(result.visuals)
    if not total:
        return LocatorAssessment(0, 0)

    snapshot = owner.store.resolve_snapshot(result.snapshot_id)
    units = {}
    assets = {}
    for member in snapshot.members:
        rows = _retained_rows(owner.store, member)
        for unit in rows.units:
            units[unit.unit_id] = (member, rows, unit)
        for asset in rows.assets:
            assets[asset.asset_id] = (member, rows, asset)

    def source_ref_valid(citation: Citation, rows, locator: Locator) -> bool:
        expected = next(
            (
                item.source_ref
                for item in rows.revision_artifacts
                if item.artifact_sha == locator.artifact_ref
            ),
            rows.source.source_key,
        )
        return citation.source_ref == expected

    def citation_valid(citation: Citation, locator: Locator, parent) -> bool:
        if citation.unit_id is not None and citation.asset_id is None:
            binding = units.get(citation.unit_id)
            if binding is None:
                return False
            member, rows, unit = binding
            if isinstance(parent, VisualEvidence):
                return False
            if parent is not None and (
                parent.source_id != member.source_id
                or parent.revision_id != member.revision_id
                or parent.extraction_id != member.extraction_id
                or citation.unit_id
                not in (*parent.matched_unit_ids, *parent.context_only_unit_ids)
            ):
                return False
            return (
                citation.source_id == member.source_id
                and citation.revision_id == member.revision_id
                and source_ref_valid(citation, rows, locator)
                and any(
                    canonical_locator_matches(locator, retained, rows, unit.unit_id)
                    for retained in unit.locators
                )
            )
        if citation.asset_id is not None and citation.unit_id is None:
            binding = assets.get(citation.asset_id)
            if binding is None:
                return False
            member, rows, asset = binding
            if isinstance(parent, VisualEvidence) and not visual_binding_matches(
                parent, asset, member
            ):
                return False
            if parent is not None and not isinstance(parent, VisualEvidence):
                return False
            return (
                citation.source_id == member.source_id
                and citation.revision_id == member.revision_id
                and source_ref_valid(citation, rows, locator)
                and locator == asset.locator
                and locator.artifact_ref == asset.artifact_sha
            )
        return False

    valid = sum(
        visual_binding_matches(
            visual, assets[visual.asset_id][2], assets[visual.asset_id][0]
        )
        if visual.asset_id in assets
        else False
        for visual in result.visuals
    )
    valid += sum(
        citation_valid(citation, locator, parent)
        for citation, locator, parent in citations
    )
    return LocatorAssessment(valid, total)
