"""Registered assets with their payloads, and the units linked to them."""

import json
import sqlite3

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.corpus import AssetRow
from tychora.contracts.identity import canonical_bytes
from tychora.ports.repositories import AssetLink, RegisteredAsset

_ASSET_FIELDS = (
    "asset_id",
    "extraction_id",
    "artifact_sha",
    "asset_type",
    "native_ref",
    "locator",
    "caption",
    "declared_review_state",
)


class SqliteAssetRepository:
    """Reads of ``assets`` joined to their extraction, payload and unit links."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def registered(self, asset_id: str) -> RegisteredAsset | None:
        """The asset with its revision and payload, or ``None``."""
        row = self._db.execute(
            "SELECT a.asset_id,a.extraction_id,a.artifact_sha,a.asset_type,a.native_ref,"
            "a.locator_json,a.caption,a.declared_review_state,e.revision_id,"
            "p.size_bytes,p.media_type,p.storage_key,p.payload_state "
            "FROM assets a JOIN extractions e USING(extraction_id) "
            "JOIN artifacts p ON p.artifact_sha=a.artifact_sha WHERE a.asset_id=?",
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        asset = AssetRow.model_validate_json(
            canonical_bytes(
                dict(zip(_ASSET_FIELDS, (*row[:5], json.loads(row[5]), *row[6:8])))
            )
        )
        return RegisteredAsset(
            asset=asset,
            revision_id=row[8],
            ref=ArtifactRef.from_row((row[2], *row[9:12])),
            payload_state=row[12],
        )

    def links(
        self, asset_id: str, roles: tuple[str, ...], limit: int
    ) -> tuple[AssetLink, ...]:
        """Up to ``limit`` units linked to the asset in the given roles, by unit ID."""
        rows = self._db.execute(
            "SELECT u.unit_id,ua.role,u.extraction_id,u.text_origin,u.citable "
            "FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
            "WHERE ua.asset_id=? AND ua.role IN (SELECT value FROM json_each(?)) "
            "ORDER BY u.unit_id LIMIT ?",
            (asset_id, json.dumps(list(roles)), limit),
        ).fetchall()
        return tuple(
            AssetLink(
                unit_id=unit_id,
                role=role,
                extraction_id=extraction_id,
                text_origin=text_origin,
                citable=citable == 1,
            )
            for unit_id, role, extraction_id, text_origin, citable in rows
        )
