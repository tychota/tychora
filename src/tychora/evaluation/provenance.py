"""Verify returned locators against pinned, retained canonical bindings."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.eligibility import _retained_rows
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


class RetainedBindingCache:
    """Bounded, pinned comparison-local canonical bindings; never an authorization cache."""

    def __init__(self, owner: WorkspaceOwner, snapshot_id: str) -> None:
        self.owner = owner
        self.snapshot_id = snapshot_id
        self.members = None
        self.units: dict[str, tuple | None] = {}
        self.assets: dict[str, tuple | None] = {}
        self.binding_count = 0
        self.bytes_used = 0
        self.rows_used = 0

    def _preflight(self, member) -> tuple[int, int]:
        """Measure UTF-8 payloads and row overhead in SQLite before Python hydration."""
        db = self.owner.db
        extraction = member.extraction_id
        revision = member.revision_id
        specs = (
            (
                "sources",
                "source_id=?",
                (member.source_id,),
                ("source_key", "registration_json"),
            ),
            (
                "revisions",
                "revision_id=?",
                (revision,),
                ("title", "metadata_json", "allowed_uses_json"),
            ),
            (
                "extractions",
                "extraction_id=?",
                (extraction,),
                ("parser_fingerprint", "normalizer_fingerprint"),
            ),
            (
                "evidence_units",
                "extraction_id=?",
                (extraction,),
                (
                    "text",
                    "heading_path_json",
                    "attributes_json",
                    "native_ref",
                    "boundary_key",
                ),
            ),
            (
                "locators l JOIN evidence_units u USING(unit_id)",
                "u.extraction_id=?",
                (extraction,),
                ("l.details_json", "l.source_ref"),
            ),
            (
                "assets",
                "extraction_id=?",
                (extraction,),
                ("locator_json", "caption", "native_ref"),
            ),
            (
                "unit_assets ua JOIN evidence_units u USING(unit_id)",
                "u.extraction_id=?",
                (extraction,),
                ("ua.role",),
            ),
            (
                "asset_proxies p JOIN evidence_units u ON u.unit_id=p.proxy_unit_id",
                "u.extraction_id=?",
                (extraction,),
                ("p.origin_type",),
            ),
            (
                "asset_proxy_dependencies d JOIN evidence_units u ON u.unit_id=d.proxy_unit_id",
                "u.extraction_id=?",
                (extraction,),
                (),
            ),
            (
                "structured_records sr JOIN evidence_units u USING(unit_id)",
                "u.extraction_id=?",
                (extraction,),
                ("sr.data_json",),
            ),
            (
                "identifiers i JOIN evidence_units u USING(unit_id)",
                "u.extraction_id=?",
                (extraction,),
                ("i.normalized_value", "i.original_value"),
            ),
            (
                "revision_artifacts",
                "revision_id=?",
                (revision,),
                ("source_ref", "role"),
            ),
            (
                "artifacts a",
                (
                    "a.artifact_sha IN ("
                    "SELECT original_sha FROM revisions WHERE revision_id=? UNION "
                    "SELECT document_artifact_sha FROM extractions WHERE extraction_id=? UNION "
                    "SELECT artifact_sha FROM revision_artifacts WHERE revision_id=? UNION "
                    "SELECT artifact_sha FROM assets WHERE extraction_id=? UNION "
                    "SELECT p.derivation_sha FROM asset_proxies p JOIN evidence_units u "
                    "ON u.unit_id=p.proxy_unit_id WHERE u.extraction_id=? UNION "
                    "SELECT l.artifact_sha FROM locators l JOIN evidence_units u "
                    "USING(unit_id) WHERE u.extraction_id=?)"
                ),
                (revision, extraction, revision, extraction, extraction, extraction),
                ("a.media_type", "a.storage_key"),
            ),
        )
        rows = 0
        size = 0
        for table, where, args, columns in specs:
            payload = "+".join(
                f"COALESCE(LENGTH(CAST({column} AS BLOB)),0)" for column in columns
            )
            estimated = f"1024+{payload}" if payload else "1024"
            count, byte_count = db.execute(
                f"SELECT COUNT(*),COALESCE(SUM({estimated}),0) FROM {table} WHERE {where}",
                args,
            ).fetchone()
            rows += count
            size += byte_count
            if (
                rows + self.rows_used > MAX_BINDING_ROWS
                or size + self.bytes_used > MAX_BINDING_BYTES
            ):
                raise TychoraError(
                    "LIMIT_EXCEEDED", "Evaluation retained binding budget exceeded"
                )
        return rows, size

    def _load(self, extraction_id: str) -> None:
        if self.members is None:
            snapshot = self.owner.store.resolve_snapshot(self.snapshot_id)
            self.members = {member.extraction_id: member for member in snapshot.members}
        member = self.members.get(extraction_id)
        if member is None:
            return
        rows, size = self._preflight(member)
        retained = _retained_rows(self.owner.store, member)
        self.rows_used += rows
        self.bytes_used += size
        self.binding_count += 1
        for unit in retained.units:
            self.units[unit.unit_id] = (member, retained, unit)
        for asset in retained.assets:
            self.assets[asset.asset_id] = (member, retained, asset)

    def unit(self, unit_id: str):
        if unit_id not in self.units:
            row = self.owner.db.execute(
                "SELECT extraction_id FROM evidence_units WHERE unit_id=?", (unit_id,)
            ).fetchone()
            if row is not None:
                self._load(row[0])
            self.units.setdefault(unit_id, None)
        return self.units[unit_id]

    def asset(self, asset_id: str):
        if asset_id not in self.assets:
            row = self.owner.db.execute(
                "SELECT extraction_id FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
            if row is not None:
                self._load(row[0])
            self.assets.setdefault(asset_id, None)
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
