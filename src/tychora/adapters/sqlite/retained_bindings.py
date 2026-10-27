"""Scope and measure the retained rows one evaluation binding hydrates.

A binding covers a whole extraction, or one source file of a code extraction. A
repository source is a single extraction holding every file, so a citation into
one file binds only that file's units, their locators and records, and that
file's source binding. Code ownership and context dependencies never cross a
file, so such a slice validates on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tychora.contracts.corpus import SourceRow
from tychora.contracts.errors import TychoraError

if TYPE_CHECKING:
    from tychora.contracts.publication import SnapshotMember

# The ``sources`` columns a retained binding carries: withdrawal and use
# overrides are policy state, not representation.
SOURCE_COLUMNS = ",".join(SourceRow.model_fields)


@dataclass(frozen=True, slots=True)
class BindingScope:
    """The rows of one extraction, one code file, some units or some assets, as SQL fragments.

    An asset scope holds the listed assets and the units linked to them as a
    source, caption or proxy.
    """

    extraction_id: str
    revision_id: str
    relative_path: str | None = None
    unit_ids: tuple[str, ...] | None = None
    asset_ids: tuple[str, ...] | None = None

    @classmethod
    def of(cls, member: SnapshotMember, relative_path: str | None) -> BindingScope:
        """Scope a snapshot member's extraction, optionally to one code file."""
        return cls(member.extraction_id, member.revision_id, relative_path)

    @property
    def partial(self) -> bool:
        """Whether the scope lists units or assets rather than a whole file or extraction."""
        return self.unit_ids is not None or self.asset_ids is not None

    def units(self) -> tuple[str, tuple[str, ...]]:
        """Select the scope's evidence units through the alias ``u``."""
        if self.asset_ids is not None:
            listed = json.dumps(sorted(set(self.asset_ids)))
            return (
                (
                    "u.extraction_id=? AND (u.unit_id IN (SELECT unit_id FROM unit_assets "
                    "WHERE asset_id IN (SELECT value FROM json_each(?))) OR u.unit_id IN "
                    "(SELECT proxy_unit_id FROM asset_proxies "
                    "WHERE asset_id IN (SELECT value FROM json_each(?))))"
                ),
                (self.extraction_id, listed, listed),
            )
        if self.unit_ids is not None:
            return (
                "u.extraction_id=? AND u.unit_id IN (SELECT value FROM json_each(?))",
                (self.extraction_id, json.dumps(sorted(set(self.unit_ids)))),
            )
        if self.relative_path is None:
            return "u.extraction_id=?", (self.extraction_id,)
        return (
            (
                "u.extraction_id=? AND EXISTS(SELECT 1 FROM locators c "
                "WHERE c.unit_id=u.unit_id AND c.locator_type='code' "
                "AND c.source_ref=?)"
            ),
            (self.extraction_id, self.relative_path),
        )

    def assets(self) -> tuple[str, tuple[str, ...]]:
        """Select the scope's assets through the alias ``a``: listed and linked ones for a part."""
        if self.relative_path is None and not self.partial:
            return "a.extraction_id=?", (self.extraction_id,)
        units, args = self.units()
        listed = json.dumps(sorted(set(self.asset_ids or ())))
        return (
            (
                "a.extraction_id=? AND a.asset_id IN ("
                "SELECT value FROM json_each(?) UNION "
                "SELECT ua.asset_id FROM unit_assets ua JOIN evidence_units u "
                f"USING(unit_id) WHERE {units} UNION "
                "SELECT p.asset_id FROM asset_proxies p JOIN evidence_units u "
                f"ON u.unit_id=p.proxy_unit_id WHERE {units})"
            ),
            (self.extraction_id, listed, *args, *args),
        )

    def revision_artifacts(self) -> tuple[str, tuple[str, ...]]:
        """Select the revision artifacts, keeping only the scoped files' source bindings.

        A unit or asset scope keeps the source bindings its units' code locators name.
        """
        if self.partial:
            units, args = self.units()
            return (
                (
                    "ra.revision_id=? AND (ra.role<>'source_file' OR ra.source_ref IN ("
                    "SELECT c.source_ref FROM locators c JOIN evidence_units u "
                    f"USING(unit_id) WHERE {units} AND c.locator_type='code'))"
                ),
                (self.revision_id, *args),
            )
        if self.relative_path is None:
            return "ra.revision_id=?", (self.revision_id,)
        return (
            "ra.revision_id=? AND (ra.role<>'source_file' OR ra.source_ref=?)",
            (self.revision_id, self.relative_path),
        )

    def artifacts(self) -> tuple[str, tuple[str, ...]]:
        """Select every artifact the scope's rows reference through the alias ``x``."""
        units, unit_args = self.units()
        assets, asset_args = self.assets()
        bindings, binding_args = self.revision_artifacts()
        return (
            (
                "x.artifact_sha IN ("
                "SELECT original_sha FROM revisions WHERE revision_id=? UNION "
                "SELECT document_artifact_sha FROM extractions "
                "WHERE extraction_id=? UNION "
                "SELECT ra.artifact_sha FROM revision_artifacts ra "
                f"WHERE {bindings} UNION "
                f"SELECT a.artifact_sha FROM assets a WHERE {assets} UNION "
                "SELECT p.derivation_sha FROM asset_proxies p JOIN evidence_units u "
                f"ON u.unit_id=p.proxy_unit_id WHERE {units} UNION "
                "SELECT l.artifact_sha FROM locators l JOIN evidence_units u "
                f"USING(unit_id) WHERE {units})"
            ),
            (
                self.revision_id,
                self.extraction_id,
                *binding_args,
                *asset_args,
                *unit_args,
                *unit_args,
            ),
        )


# Estimated bytes of one row beyond its payload columns, by table: about 1 KiB
# for wide rows, a few hundred bytes for narrow link and reference rows.
ROW_OVERHEAD = {
    "sources": 1024,
    "revisions": 1024,
    "extractions": 1024,
    "evidence_units": 1024,
    "locators": 1024,
    "structured_records": 1024,
    "unit_assets": 128,
    "asset_proxies": 256,
    "asset_proxy_dependencies": 128,
    "identifiers": 256,
    "assets": 256,
    "revision_artifacts": 192,
    "artifacts": 192,
}

# Tables joined to the scoped units, with the payload columns each row carries.
_UNIT_TABLES = (
    (
        "evidence_units u",
        (
            "u.text",
            "u.heading_path_json",
            "u.attributes_json",
            "u.native_ref",
            "u.boundary_key",
        ),
    ),
    (
        "locators l JOIN evidence_units u USING(unit_id)",
        ("l.details_json", "l.source_ref"),
    ),
    ("unit_assets ua JOIN evidence_units u USING(unit_id)", ("ua.role",)),
    (
        "asset_proxies p JOIN evidence_units u ON u.unit_id=p.proxy_unit_id",
        ("p.origin_type",),
    ),
    (
        "asset_proxy_dependencies d JOIN evidence_units u ON u.unit_id=d.proxy_unit_id",
        (),
    ),
    ("structured_records sr JOIN evidence_units u USING(unit_id)", ("sr.data_json",)),
    (
        "identifiers i JOIN evidence_units u USING(unit_id)",
        ("i.normalized_value", "i.original_value"),
    ),
)


def _measured_tables(
    source_id: str, scope: BindingScope
) -> tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]:
    """Every table the binding hydrates: (from clause, where, arguments, payload columns)."""
    units, unit_args = scope.units()
    assets, asset_args = scope.assets()
    bindings, binding_args = scope.revision_artifacts()
    artifacts, artifact_args = scope.artifacts()
    return (
        (
            "sources",
            "source_id=?",
            (source_id,),
            ("source_key", "registration_json"),
        ),
        (
            "revisions",
            "revision_id=?",
            (scope.revision_id,),
            ("title", "metadata_json", "allowed_uses_json"),
        ),
        (
            "extractions",
            "extraction_id=?",
            (scope.extraction_id,),
            ("parser_fingerprint", "normalizer_fingerprint"),
        ),
        *((table, units, unit_args, columns) for table, columns in _UNIT_TABLES),
        (
            "assets a",
            assets,
            asset_args,
            ("a.locator_json", "a.caption", "a.native_ref"),
        ),
        ("revision_artifacts ra", bindings, binding_args, ("ra.source_ref", "ra.role")),
        ("artifacts x", artifacts, artifact_args, ("x.media_type", "x.storage_key")),
    )


def binding_footprint(
    db, member: SnapshotMember, relative_path: str | None = None
) -> tuple[int, int]:
    """Count rows and estimate bytes of one member's binding; see ``scope_footprint``."""
    scope = BindingScope.of(member, relative_path)
    return scope_footprint(db, member.source_id, scope)


def scope_footprint(db, source_id: str, scope: BindingScope) -> tuple[int, int]:
    """Count rows and estimate UTF-8 payload bytes in SQLite before Python hydration.

    Each row costs its table's ``ROW_OVERHEAD`` plus the length of its payload
    columns. The source, revision and extraction rows count once per scope, so
    slices of one extraction each count them again.
    A locator whose recorded artifact differs from its row's artifact fails here,
    before any row is hydrated.
    """
    units, unit_args = scope.units()
    if db.execute(
        "SELECT 1 FROM locators l JOIN evidence_units u USING(unit_id) "
        f"WHERE {units} "
        "AND json_extract(l.details_json,'$.artifact_ref') IS NOT l.artifact_sha "
        "LIMIT 1",
        unit_args,
    ).fetchone():
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Retained locator artifact binding differs",
            reason="canonical.binding_mismatch",
        )
    rows = 0
    size = 0
    for table, where, args, columns in _measured_tables(source_id, scope):
        payload = "+".join(
            f"COALESCE(LENGTH(CAST({column} AS BLOB)),0)" for column in columns
        )
        overhead = ROW_OVERHEAD[table.split(" ", 1)[0]]
        estimated = f"{overhead}+{payload}" if payload else str(overhead)
        count, byte_count = db.execute(
            f"SELECT COUNT(*),COALESCE(SUM({estimated}),0) FROM {table} WHERE {where}",
            args,
        ).fetchone()
        rows += count
        size += byte_count
    return rows, size


def binding_key(db, unit_id: str) -> tuple[str, str | None] | None:
    """The extraction of a unit and, for a code unit, the file its code locator names."""
    row = db.execute(
        "SELECT u.extraction_id,(SELECT c.source_ref FROM locators c "
        "WHERE c.unit_id=u.unit_id AND c.locator_type='code' "
        "ORDER BY c.fragment_ordinal LIMIT 1) "
        "FROM evidence_units u WHERE u.unit_id=?",
        (unit_id,),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def asset_extraction(db, asset_id: str) -> str | None:
    """The extraction that holds an asset, if any."""
    row = db.execute(
        "SELECT extraction_id FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    return None if row is None else row[0]
