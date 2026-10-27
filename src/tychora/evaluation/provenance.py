"""Verify returned locators against pinned, retained canonical bindings."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.repositories.slices import retained_rows
from tychora.adapters.sqlite.retained_bindings import (
    BindingScope,
    asset_extraction,
    binding_footprint,
    binding_key,
)
from tychora.contracts.errors import TychoraError
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


MAX_BINDING_BYTES = 16 * 1024 * 1024
MAX_BINDING_ROWS = 16_384


@dataclass(frozen=True, slots=True)
class _Resident:
    """One hydrated binding's footprint and the units and assets it holds."""

    rows: int
    size: int
    unit_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]


class RetainedBindingCache:
    """Bounded, pinned comparison-local canonical bindings; never an authorization cache.

    The budget bounds the bindings held at once. A binding is a whole extraction or,
    for a code unit, the one file of the extraction it cites. A binding larger than
    the budget is refused; otherwise the least recently used bindings are released
    until it fits, and a released binding is hydrated again when next cited.
    """

    def __init__(self, owner: WorkspaceOwner, snapshot_id: str) -> None:
        self.owner = owner
        self.snapshot_id = snapshot_id
        self.units: dict[str, tuple | None] = {}
        self.assets: dict[str, tuple | None] = {}
        self.binding_count = 0
        self.bytes_used = 0
        self.rows_used = 0
        self._resident: OrderedDict[tuple[str, str | None], _Resident] = OrderedDict()
        self._holders: dict[str, tuple[str, str | None]] = {}

    def _release_until_fits(self, rows: int, size: int) -> None:
        """Refuse a binding above the budget; release least recently used ones for it."""
        if rows > MAX_BINDING_ROWS or size > MAX_BINDING_BYTES:
            raise TychoraError(
                "LIMIT_EXCEEDED",
                "Evaluation retained binding budget exceeded",
                reason="evaluation.binding_budget",
            )
        while self._resident and (
            rows + self.rows_used > MAX_BINDING_ROWS
            or size + self.bytes_used > MAX_BINDING_BYTES
        ):
            self._release(next(iter(self._resident)))

    def _release(self, key: tuple[str, str | None]) -> None:
        """Drop one held binding and the units and assets it still holds."""
        released = self._resident.pop(key)
        self.rows_used -= released.rows
        self.bytes_used -= released.size
        for unit_id in released.unit_ids:
            if self._holders.get(unit_id) == key:
                self.units.pop(unit_id, None)
                del self._holders[unit_id]
        for asset_id in released.asset_ids:
            if self._holders.get(asset_id) == key:
                self.assets.pop(asset_id, None)
                del self._holders[asset_id]

    def _load(self, extraction_id: str, relative_path: str | None = None) -> None:
        """Bind one pinned extraction, or one code file of it, within the budget."""
        member = self.owner.store.snapshot_member(self.snapshot_id, extraction_id)
        if member is None:
            return
        key = extraction_id, relative_path
        if key in self._resident:
            self._release(key)
        rows, size = binding_footprint(self.owner.db, member, relative_path)
        self._release_until_fits(rows, size)
        retained = retained_rows(self.owner.db, BindingScope.of(member, relative_path))
        self._resident[key] = _Resident(
            rows,
            size,
            tuple(unit.unit_id for unit in retained.units),
            tuple(asset.asset_id for asset in retained.assets),
        )
        self.rows_used += rows
        self.bytes_used += size
        self.binding_count += 1
        for unit in retained.units:
            self.units[unit.unit_id] = (member, retained, unit)
            self._holders[unit.unit_id] = key
        for asset in retained.assets:
            self.assets[asset.asset_id] = (member, retained, asset)
            self._holders[asset.asset_id] = key

    def _touch(self, identifier: str) -> None:
        """Mark the binding holding a unit or asset as most recently used."""
        key = self._holders.get(identifier)
        if key is not None:
            self._resident.move_to_end(key)

    def unit(self, unit_id: str):
        """The pinned binding of a unit; a code unit binds only its own file."""
        if unit_id not in self.units:
            key = binding_key(self.owner.db, unit_id)
            if key is not None:
                self._load(*key)
            self.units.setdefault(unit_id, None)
        self._touch(unit_id)
        return self.units[unit_id]

    def asset(self, asset_id: str):
        """The pinned binding of an asset, from its whole extraction."""
        if asset_id not in self.assets:
            extraction_id = asset_extraction(self.owner.db, asset_id)
            if extraction_id is not None:
                self._load(extraction_id)
            self.assets.setdefault(asset_id, None)
        self._touch(asset_id)
        return self.assets[asset_id]


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
    owner: WorkspaceOwner,
    result: QueryResult,
    *,
    cache: RetainedBindingCache | None = None,
) -> LocatorAssessment:
    """Use only owner-serialized retained metadata, never reopen source payloads."""
    citations = tuple(_citation_locators(result))
    total = len(citations) + len(result.visuals)
    if not total:
        return LocatorAssessment(0, 0)

    bindings = cache or RetainedBindingCache(owner, result.snapshot_id)
    if bindings.owner is not owner or bindings.snapshot_id != result.snapshot_id:
        raise TychoraError(
            "INVALID_REQUEST", "Evaluation locator cache differs from pinned snapshot"
        )

    def source_ref_valid(citation: Citation, rows, locator: Locator) -> bool:
        if isinstance(locator, CodeLocator):
            return citation.source_ref == locator.relative_path and any(
                item.role == "source_file"
                and item.source_ref == locator.relative_path
                and item.artifact_sha == locator.artifact_ref
                for item in rows.revision_artifacts
            )
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
            binding = bindings.unit(citation.unit_id)
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
            binding = bindings.asset(citation.asset_id)
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

    valid = 0
    for visual in result.visuals:
        binding = bindings.asset(visual.asset_id)
        if binding is not None:
            valid += visual_binding_matches(visual, binding[2], binding[0])
    valid += sum(
        citation_valid(citation, locator, parent)
        for citation, locator, parent in citations
    )
    return LocatorAssessment(valid, total)
