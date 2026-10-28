"""Store-side reads that compile a revision-level eligibility view and apply it."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from typing import Literal

from tychora.configuration.global_facets import ROLE as GLOBAL_FACET_ROLE
from tychora.contracts.artifacts import ArtifactRef
from tychora.ports.repositories import (
    FacetValue,
    MemberRow,
    OwnerFact,
    ReviewRow,
    UnitFlags,
)

from .artifacts import artifact_ref
from .errors import facts_missing
from .representations import SqliteRepresentationRepository

_IN = "IN (SELECT value FROM json_each(?))"
_MEMBER_COLUMNS = (
    "m.source_id,m.revision_id,m.extraction_id,m.is_default,r.kind_id,k.definition_hash,"
    "c.collection_id,c.policy_json,c.access_floor,s.access_floor,"
    "s.withdrawn_at IS NOT NULL,s.allowed_uses_override,r.access_rank,"
    "r.allowed_uses_json,COALESCE(rc.state,r.declared_review_state),"
    "r.published_on,r.effective_from,r.effective_until,r.edition_key,c.workspace_id"
)
_MEMBER_JOINS = (
    "JOIN revisions r ON r.revision_id=m.revision_id "
    "JOIN sources s ON s.source_id=m.source_id "
    "JOIN collections c ON c.collection_id=s.collection_id "
    "JOIN kinds k ON k.kind_id=r.kind_id "
    "LEFT JOIN review_current rc ON rc.target_kind='revision' AND rc.target_id=r.revision_id "
)
_SNAPSHOT_MEMBERS = (
    f"SELECT {_MEMBER_COLUMNS} FROM snapshot_members m {_MEMBER_JOINS}"
    "WHERE m.snapshot_id=? ORDER BY m.source_id,m.revision_id"
)
_LISTED_MEMBERS = (
    f"SELECT {_MEMBER_COLUMNS} FROM (SELECT r.source_id,e.revision_id,e.extraction_id,"
    "1 AS is_default FROM extractions e JOIN revisions r USING(revision_id) "
    f"WHERE e.extraction_id {_IN}) m {_MEMBER_JOINS}"
    "ORDER BY m.source_id,m.revision_id"
)
_UNIT_REVIEWS = (
    "SELECT u.unit_id,'unit',rc.state,p.proxy_unit_id IS NOT NULL FROM review_current rc "
    "JOIN evidence_units u ON u.unit_id=rc.target_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
    f"WHERE rc.target_kind='unit' AND u.extraction_id {_IN} "
    "UNION SELECT u.unit_id,'unit',u.declared_review_state,1 FROM asset_proxies p "
    "JOIN evidence_units u ON u.unit_id=p.proxy_unit_id "
    f"WHERE u.extraction_id {_IN} AND NOT EXISTS (SELECT 1 FROM review_current rc "
    "WHERE rc.target_kind='unit' AND rc.target_id=u.unit_id) "
    "UNION SELECT u.unit_id,'unit',u.declared_review_state,0 FROM evidence_units u "
    f"WHERE u.declared_review_state {_IN} AND u.extraction_id {_IN} "
    "AND NOT EXISTS (SELECT 1 FROM asset_proxies p WHERE p.proxy_unit_id=u.unit_id) "
    "AND NOT EXISTS (SELECT 1 FROM review_current rc "
    "WHERE rc.target_kind='unit' AND rc.target_id=u.unit_id)"
)
_ASSET_REVIEWS = (
    "SELECT a.asset_id,'asset',COALESCE(rc.state,a.declared_review_state),0 FROM assets a "
    "LEFT JOIN review_current rc ON rc.target_kind='asset' AND rc.target_id=a.asset_id "
    f"WHERE a.extraction_id {_IN} AND (rc.target_id IS NOT NULL "
    f"OR a.declared_review_state {_IN})"
)
_DEPENDENTS = (
    "WITH RECURSIVE removed(id) AS (SELECT value FROM json_each(?) UNION "
    "SELECT d.owner_id FROM representation_dependencies d "
    "JOIN removed r ON d.dependency_id=r.id) "
    "SELECT r.id,CASE WHEN a.asset_id IS NOT NULL THEN 'asset' ELSE 'unit' END "
    "FROM removed r LEFT JOIN assets a ON a.asset_id=r.id "
    "WHERE a.asset_id IS NOT NULL OR EXISTS "
    "(SELECT 1 FROM evidence_units u WHERE u.unit_id=r.id)"
)
_OWNERS = (
    "SELECT 'unit',u.unit_id,u.extraction_id,u.unit_id,NULL,p.asset_id,u.citable,"
    "NULL,u.unit_id,p.proxy_unit_id IS NOT NULL FROM evidence_units u "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
    f"WHERE u.unit_id {_IN} "
    "UNION ALL SELECT 'asset',a.asset_id,a.extraction_id,NULL,NULL,a.asset_id,0,"
    f"NULL,a.asset_id,0 FROM assets a WHERE a.asset_id {_IN} "
    "UNION ALL SELECT 'chunk',c.chunk_id,c.extraction_id,c.unit_id,c.chunk_id,"
    "p.asset_id,u.citable,NULL,c.chunk_id,p.proxy_unit_id IS NOT NULL FROM chunks c "
    "JOIN evidence_units u ON u.unit_id=c.unit_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    f"WHERE c.chunk_id {_IN} "
    # A proxy window's record is owned by the proxy's asset; the set is the
    # record's context group (verified by native health) or its image asset.
    "UNION ALL SELECT 'record',v.record_id,b.extraction_id,c.unit_id,v.chunk_id,"
    "COALESCE(v.asset_id,p.asset_id),0,v.batch_id,COALESCE(v.group_id,v.asset_id),"
    "v.chunk_id IS NOT NULL AND p.proxy_unit_id IS NOT NULL FROM vector_records v "
    "JOIN index_batches b ON b.batch_id=v.batch_id "
    "LEFT JOIN chunks c ON c.chunk_id=v.chunk_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    f"WHERE v.record_id {_IN}"
)

_ASSET_SOURCES = (
    f"SELECT asset_id,unit_id FROM unit_assets WHERE role='source' AND asset_id {_IN}"
)


def _ids(values) -> str:
    return json.dumps(sorted(set(values)))


def _typed(text, number, boolean) -> str | float | int:
    """The one stored value of a facet row; booleans stay the stored 0 or 1."""
    if text is not None:
        return text
    if boolean is not None:
        return boolean
    return number


def _member(row: sqlite3.Row | tuple) -> MemberRow:
    """A member row with SQLite's 0 or 1 flags as booleans."""
    values = list(row)
    values[3], values[10] = bool(values[3]), bool(values[10])
    return MemberRow._make(values)


class SqliteEligibilityRepository:
    """Eligibility reads bound to one connection's current transaction."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def members(
        self, snapshot_id: str, extraction_ids: tuple[str, ...] = ()
    ) -> tuple[MemberRow, ...]:
        """One row per snapshot member, or per listed extraction when given."""
        if extraction_ids:
            rows = self._db.execute(_LISTED_MEMBERS, (_ids(extraction_ids),))
        else:
            rows = self._db.execute(_SNAPSHOT_MEMBERS, (snapshot_id,))
        return tuple(_member(row) for row in rows)

    def kind_definitions(
        self, kind_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(kind_id, definition_hash, definition_json)`` of each listed kind."""
        return tuple(
            self._db.execute(
                "SELECT kind_id,definition_hash,definition_json FROM kinds "
                f"WHERE kind_id {_IN} ORDER BY kind_id",
                (_ids(kind_ids),),
            )
        )

    def batches(self, snapshot_id: str) -> tuple[tuple[str, str, str | None], ...]:
        """``(slot, batch_id, extraction_id)`` of every batch the snapshot serves."""
        return tuple(
            self._db.execute(
                "SELECT sb.slot,sb.batch_id,b.extraction_id FROM snapshot_batches sb "
                "JOIN index_batches b ON b.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
                "ORDER BY sb.slot,sb.batch_id",
                (snapshot_id,),
            )
        )

    def review_rows(
        self, extraction_ids: tuple[str, ...], excluded_states: tuple[str, ...]
    ) -> tuple[ReviewRow, ...]:
        """Reviewed and proxy units, reviewed assets, and declared excluded states."""
        extractions, states = _ids(extraction_ids), _ids(excluded_states)
        units = self._db.execute(
            _UNIT_REVIEWS, (extractions, extractions, states, extractions)
        ).fetchall()
        assets = self._db.execute(_ASSET_REVIEWS, (extractions, states)).fetchall()
        return tuple(
            ReviewRow(row[0], row[1], row[2], bool(row[3]))
            for row in sorted((*units, *assets))
        )

    def unit_flags(self, extraction_ids: tuple[str, ...]) -> tuple[UnitFlags, ...]:
        """Every unit of these extractions with its citability and proxy status."""
        return tuple(
            UnitFlags(row[0], row[1], bool(row[2]), bool(row[3]))
            for row in self._db.execute(
                "SELECT u.unit_id,u.extraction_id,u.citable,p.proxy_unit_id IS NOT NULL "
                "FROM evidence_units u LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                f"WHERE u.extraction_id {_IN} ORDER BY u.unit_id",
                (_ids(extraction_ids),),
            )
        )

    def blocked_assertions(self, watermark: int) -> frozenset[str]:
        """Assertions with a decision recorded after ``watermark``."""
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT DISTINCT assertion_id FROM assertion_decisions WHERE decision_id>?",
                (watermark,),
            )
        )

    def dependents(
        self, owner_ids: tuple[str, ...]
    ) -> Mapping[str, Literal["unit", "asset"]]:
        """The listed units and assets with every unit or asset depending on them."""
        if not owner_ids:
            return {}
        return dict(self._db.execute(_DEPENDENTS, (_ids(owner_ids),)))

    def _facets(
        self, sql: str, keys: tuple[str, ...], owners: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Typed facet values of one ``(owner, key, text, number, boolean)`` query."""
        rows = self._db.execute(sql, (_ids(keys), _ids(owners)))
        return tuple(
            FacetValue(owner, key, _typed(*values)) for owner, key, *values in rows
        )

    def revision_facets(
        self, revision_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Revision-level values of the listed facets."""
        return self._facets(
            "SELECT revision_id,facet_key,text_value,number_value,boolean_value "
            f"FROM facet_values WHERE facet_key {_IN} AND revision_id {_IN} "
            "ORDER BY revision_id,facet_key,ordinal",
            facet_keys,
            revision_ids,
        )

    def unit_facets(
        self, extraction_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Unit-level values of the listed facets for units of these extractions."""
        return self._facets(
            "SELECT f.unit_id,f.facet_key,f.text_value,f.number_value,f.boolean_value "
            "FROM facet_values f JOIN evidence_units u ON u.unit_id=f.unit_id "
            f"WHERE f.facet_key {_IN} AND u.extraction_id {_IN} "
            "ORDER BY f.unit_id,f.facet_key,f.ordinal",
            facet_keys,
            extraction_ids,
        )

    def revision_metadata(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(revision_id, metadata_json, metadata_sha)`` of each listed revision."""
        return tuple(
            self._db.execute(
                "SELECT revision_id,metadata_json,metadata_sha FROM revisions "
                f"WHERE revision_id {_IN} ORDER BY revision_id",
                (_ids(revision_ids),),
            )
        )

    def global_facet_markers(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, ArtifactRef | None], ...]:
        """``(revision_id, source_ref, artifact)`` of each global-facet binding row."""
        return tuple(
            (
                row[0],
                row[1],
                artifact_ref(row[2:6]) if row[2] is not None else None,
            )
            for row in self._db.execute(
                "SELECT ra.revision_id,ra.source_ref,a.artifact_sha,a.size_bytes,"
                "a.media_type,a.storage_key FROM revision_artifacts ra "
                "LEFT JOIN artifacts a USING(artifact_sha) "
                f"WHERE ra.role=? AND ra.revision_id {_IN} ORDER BY ra.revision_id",
                (GLOBAL_FACET_ROLE, _ids(revision_ids)),
            )
        )

    def _asset_sources(self, asset_ids: tuple[str, ...]) -> Mapping[str, set[str]]:
        """The source units of each listed asset; no query without assets."""
        sources: dict[str, set[str]] = defaultdict(set)
        if asset_ids:
            for asset_id, unit_id in self._db.execute(
                _ASSET_SOURCES, (_ids(asset_ids),)
            ):
                sources[asset_id].add(unit_id)
        return sources

    def owner_facts(self, owner_ids: tuple[str, ...]) -> tuple[OwnerFact, ...]:
        """Facts of the listed owners; a known owner without a set is refused."""
        ids = _ids(owner_ids)
        rows = self._db.execute(_OWNERS, (ids, ids, ids, ids)).fetchall()
        sets = SqliteRepresentationRepository(self._db).dependencies(
            tuple({row[8] for row in rows if row[8] is not None})
        )
        sources = self._asset_sources(
            tuple(row[1] for row in rows if row[0] == "asset")
        )
        facts = []
        for (
            kind,
            owner,
            extraction,
            unit,
            chunk,
            asset,
            citable,
            batch,
            set_owner,
            proxy,
        ) in rows:
            dependencies = sets.get(set_owner) if set_owner is not None else None
            if not dependencies:
                raise facts_missing()
            facts.append(
                OwnerFact(
                    owner_id=owner,
                    kind=kind,
                    extraction_id=extraction,
                    dependency_ids=dependencies,
                    unit_id=unit,
                    chunk_id=chunk,
                    asset_id=asset,
                    proxy=bool(proxy),
                    citable=bool(citable),
                    batch_id=batch,
                    source_owner_ids=frozenset(sources.get(owner, ())),
                )
            )
        return tuple(facts)
