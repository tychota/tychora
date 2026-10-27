"""Retained rows of a scope and their ownership from persisted intervals."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    ExtractionRow,
    IdentifierRow,
    LocatorRow,
    RetainedRepresentationRows,
    RevisionArtifactRow,
    RevisionRow,
    SourceRow,
    StructuredRecordRow,
    UnitAssetRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.indexing import (
    IntervalRow,
    OwnedInterval,
    PreparedExtraction,
    RepresentationUnit,
    symbol_path,
)
from tychora.contracts.publication import SnapshotMember

from ..retained_bindings import SOURCE_COLUMNS, BindingScope
from .errors import facts_missing


def _rows(db, query: str, args: tuple[str, ...] = ()) -> list[dict]:
    cursor = db.execute(query, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _retained_heads(db, member) -> tuple[dict, dict, dict]:
    """The source, revision and extraction rows of one member, JSON decoded."""
    source = _rows(
        db,
        f"SELECT {SOURCE_COLUMNS} FROM sources WHERE source_id=?",
        (member.source_id,),
    )[0]
    source["registration_json"] = json.loads(source["registration_json"])
    revision = _rows(
        db, "SELECT * FROM revisions WHERE revision_id=?", (member.revision_id,)
    )[0]
    revision["allowed_uses_json"] = tuple(json.loads(revision["allowed_uses_json"]))
    revision["metadata_json"] = json.loads(revision["metadata_json"])
    extraction = _rows(
        db, "SELECT * FROM extractions WHERE extraction_id=?", (member.extraction_id,)
    )[0]
    return source, revision, extraction


def _retained_units(
    db, scope: BindingScope
) -> tuple[list[EvidenceUnit], list[LocatorRow]]:
    """The scoped units in ordinal order, with their locator rows in fragment order."""
    where, args = scope.units()
    by_unit: dict[str, list[dict]] = defaultdict(list)
    for row in _rows(
        db,
        "SELECT l.locator_id,l.unit_id,l.fragment_ordinal,l.details_json "
        f"FROM locators l JOIN evidence_units u USING(unit_id) WHERE {where} "
        "ORDER BY l.unit_id,l.fragment_ordinal",
        args,
    ):
        by_unit[row["unit_id"]].append(row)
    locators: list[LocatorRow] = []
    units: list[EvidenceUnit] = []
    for unit in _rows(
        db, f"SELECT u.* FROM evidence_units u WHERE {where} ORDER BY u.ordinal", args
    ):
        locator_rows = by_unit.get(unit["unit_id"], [])
        locators.extend(
            LocatorRow(
                locator_id=row["locator_id"],
                unit_id=row["unit_id"],
                fragment_ordinal=row["fragment_ordinal"],
                locator=json.loads(row["details_json"]),
            )
            for row in locator_rows
        )
        unit["heading_path"] = json.loads(unit.pop("heading_path_json"))
        unit["attributes"] = json.loads(unit.pop("attributes_json"))
        unit["review_state"] = unit.pop("declared_review_state")
        unit["citable"] = bool(unit["citable"])
        unit["locators"] = [json.loads(row["details_json"]) for row in locator_rows]
        units.append(EvidenceUnit.from_mapping(unit))
    return units, locators


def _retained_assets(
    db, scope: BindingScope
) -> tuple[list[AssetRow], list[UnitAssetRow], list[AssetProxyRow]]:
    """The scoped assets, unit-asset links and asset proxies with their dependencies."""
    where, args = scope.assets()
    assets = []
    for row in _rows(db, f"SELECT a.* FROM assets a WHERE {where}", args):
        row["locator"] = json.loads(row.pop("locator_json"))
        assets.append(AssetRow.model_validate(row))
    units, unit_args = scope.units()
    links = [
        UnitAssetRow.model_validate(row)
        for row in _rows(
            db,
            f"SELECT ua.* FROM unit_assets ua JOIN evidence_units u USING(unit_id) WHERE {units}",
            unit_args,
        )
    ]
    proxies = []
    for row in _rows(
        db,
        "SELECT p.* FROM asset_proxies p JOIN evidence_units u "
        f"ON u.unit_id=p.proxy_unit_id WHERE {units}",
        unit_args,
    ):
        row["required_unit_ids"] = frozenset(
            uid
            for (uid,) in db.execute(
                "SELECT required_unit_id FROM asset_proxy_dependencies WHERE proxy_unit_id=?",
                (row["proxy_unit_id"],),
            )
        )
        proxies.append(AssetProxyRow.model_validate(row))
    return assets, links, proxies


def _retained_records(
    db, scope: BindingScope
) -> tuple[list[StructuredRecordRow], list[IdentifierRow]]:
    """The structured records and identifiers of the scoped units."""
    units, args = scope.units()
    records = []
    for row in _rows(
        db,
        f"SELECT sr.* FROM structured_records sr JOIN evidence_units u USING(unit_id) WHERE {units}",
        args,
    ):
        row["data_json"] = json.loads(row["data_json"])
        records.append(StructuredRecordRow.model_validate(row))
    identifiers = [
        IdentifierRow.model_validate(row)
        for row in _rows(
            db,
            f"SELECT i.* FROM identifiers i JOIN evidence_units u USING(unit_id) WHERE {units}",
            args,
        )
    ]
    return records, identifiers


def _retained_artifacts(db, artifact_ids: set[str]) -> list[ArtifactRef]:
    """The artifact references for the given hashes, in hash order; all must exist."""
    artifacts = []
    for sha in sorted(artifact_ids):
        row = _rows(
            db,
            "SELECT artifact_sha,size_bytes,media_type,storage_key FROM artifacts WHERE artifact_sha=?",
            (sha,),
        )
        if len(row) != 1:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Retained representation artifact is unavailable",
                reason="payload.missing",
            )
        value = row[0]
        artifacts.append(
            ArtifactRef(
                sha256=value["artifact_sha"],
                size_bytes=value["size_bytes"],
                media_type=value["media_type"],
                storage_key=value["storage_key"],
            )
        )
    return artifacts


def retained_rows(
    db: sqlite3.Connection, scope: BindingScope
) -> RetainedRepresentationRows:
    """The retained rows of one extraction, one code file or some units of it."""
    (source_id,) = db.execute(
        "SELECT source_id FROM revisions WHERE revision_id=?", (scope.revision_id,)
    ).fetchone()
    member = SnapshotMember(
        source_id=source_id,
        revision_id=scope.revision_id,
        extraction_id=scope.extraction_id,
    )
    source, revision, extraction = _retained_heads(db, member)
    units, locators = _retained_units(db, scope)
    assets, links, proxies = _retained_assets(db, scope)
    records, identifiers = _retained_records(db, scope)
    where, args = scope.revision_artifacts()
    revision_artifacts = [
        RevisionArtifactRow.model_validate(row)
        for row in _rows(
            db, f"SELECT ra.* FROM revision_artifacts ra WHERE {where}", args
        )
    ]
    artifact_ids = {revision["original_sha"], extraction["document_artifact_sha"]}
    artifact_ids.update(row.artifact_sha for row in revision_artifacts)
    artifact_ids.update(row.artifact_sha for row in assets)
    artifact_ids.update(row.derivation_sha for row in proxies)
    artifact_ids.update(locator.locator.artifact_ref for locator in locators)
    return RetainedRepresentationRows(
        source=SourceRow.model_validate(source),
        artifacts=tuple(_retained_artifacts(db, artifact_ids)),
        revision=RevisionRow.model_validate(revision),
        revision_artifacts=tuple(revision_artifacts),
        extraction=ExtractionRow.model_validate(extraction),
        units=tuple(units),
        locators=tuple(locators),
        assets=tuple(assets),
        unit_assets=tuple(links),
        proxies=tuple(proxies),
        records=tuple(records),
        identifiers=tuple(identifiers),
    )


def _symbol_path(rows: RetainedRepresentationRows, unit_id: str) -> tuple[str, ...]:
    """The unit's symbol path; a unit with several symbol records is refused."""
    try:
        return symbol_path(rows.records, unit_id)
    except ValueError:
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Code unit has multiple symbol records.",
            reason="canonical.row_invalid",
        ) from None


def _owned(
    intervals: tuple[IntervalRow, ...], sets: Mapping[str, frozenset[str]]
) -> dict[str, list[OwnedInterval]]:
    """Each unit's intervals with the dependency set its digest names."""
    owned: dict[str, list[OwnedInterval]] = defaultdict(list)
    for row in intervals:
        dependencies = sets.get(row.dependency_digest)
        if not dependencies:
            raise facts_missing()
        owned[row.unit_id].append(
            OwnedInterval(
                char_start=row.char_start,
                char_end=row.char_end,
                dependency_ids=dependencies,
            )
        )
    return owned


def prepared_slice(
    rows: RetainedRepresentationRows,
    intervals: tuple[IntervalRow, ...],
    sets: Mapping[str, frozenset[str]],
) -> PreparedExtraction:
    """The rows' units with the ownership their persisted intervals record.

    A unit with text but no persisted interval is ``canonical.facts_missing``.
    """
    owned = _owned(intervals, sets)
    if any(unit.text and unit.unit_id not in owned for unit in rows.units):
        raise facts_missing()
    return PreparedExtraction(
        extraction_id=rows.extraction.extraction_id,
        revision_id=rows.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=rows.revision.revision_id,
                title=rows.revision.title,
                kind_id=rows.revision.kind_id,
                symbol_path=_symbol_path(rows, unit.unit_id),
                ownership=tuple(owned.get(unit.unit_id, ())),
            )
            for unit in sorted(
                rows.units, key=lambda item: (item.ordinal, item.unit_id)
            )
        ),
    )
