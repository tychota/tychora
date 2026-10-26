"""Short canonical reads for detached retrieval policy and ownership facts."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING

from tychora.adapters.sqlite.policy import overridden_uses
from tychora.adapters.sqlite.repositories import (
    SqliteBatchRepository,
    SqliteRepresentationRepository,
    SqliteWorkspaceRepository,
    artifact_ref,
)
from tychora.adapters.sqlite.repositories.errors import facts_missing
from tychora.adapters.sqlite.retained_bindings import SOURCE_COLUMNS, BindingScope
from tychora.application.collection_policy import collection_is_active
from tychora.configuration.global_facets import (
    ROLE as GLOBAL_FACET_ROLE,
)
from tychora.configuration.global_facets import (
    SOURCE_REF as GLOBAL_FACET_SOURCE_REF,
)
from tychora.configuration.global_facets import (
    binding_bytes as global_facet_binding_bytes,
)
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    ExtractionRow,
    IdentifierRow,
    LocatorRow,
    RetainedRepresentationRows,
    ReviewTarget,
    RevisionArtifactRow,
    RevisionRow,
    SourceRow,
    StructuredRecordRow,
    UnitAssetRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import digest
from tychora.contracts.publication import SnapshotMember, SnapshotView
from tychora.retrieval.eligibility import (
    AssetFact,
    ChunkFact,
    EligibilityFacts,
    RevisionFact,
    UnitFact,
    VectorFact,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from .corpus import CorpusStore


def _integrity(
    message: str, reason: str = "canonical.binding_mismatch"
) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason=reason)


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
            raise _integrity(
                "Retained representation artifact is unavailable", "payload.missing"
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
    store: CorpusStore, member, relative_path: str | None = None
) -> RetainedRepresentationRows:
    """Rehydrate only retained facts needed by the canonical ownership validator.

    With ``relative_path`` the rows cover one code file of the extraction: its
    units, their locators and records, and that file's source binding.
    """
    db = store.db
    scope = BindingScope.of(member, relative_path)
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


def _persisted(db, owner_ids: tuple[str, ...]) -> dict[str, frozenset[str]]:
    """The stored dependency set of every owner; any absent set is refused."""
    found = SqliteRepresentationRepository(db).dependencies(owner_ids)
    if len(found) != len(set(owner_ids)) or not all(found.values()):
        raise facts_missing()
    return dict(found)


def _unit_facets(db, unit_id: str) -> tuple[tuple[str, object], ...]:
    """A unit's facet values, each as its one typed value."""
    return tuple(
        (key, text if text is not None else boolean if boolean is not None else number)
        for key, text, number, boolean in db.execute(
            "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values "
            "WHERE unit_id=?",
            (unit_id,),
        )
    )


_SNAPSHOT_BATCHES = (
    "SELECT sb.slot,sb.batch_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
    "ORDER BY sb.slot,sb.batch_id"
)
_LEXICAL_CHUNKS = (
    "SELECT r.chunk_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN lexical_records r ON r.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot='lexical'"
)
_VECTOR_ROWS = (
    "SELECT v.record_id,v.chunk_id,v.asset_id,v.group_id,b.extraction_id "
    "FROM snapshot_batches sb JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN vector_records v ON v.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot IN ('text','image') "
    "AND b.extraction_id IN (SELECT value FROM json_each(?))"
)


def _vector_facts(
    db, snapshot_id: str, selected: set[str], assets: Mapping[str, AssetFact]
) -> tuple[VectorFact, ...]:
    """Each selected vector record with its context group's or asset's set."""
    rows = db.execute(
        _VECTOR_ROWS, (snapshot_id, json.dumps(sorted(selected)))
    ).fetchall()
    groups = _persisted(db, tuple({row[3] for row in rows if row[3] is not None}))
    facts = []
    for record_id, chunk_id, asset_id, group_id, _ in rows:
        if group_id is not None:
            dependencies = groups[group_id]
        elif asset_id in assets:
            dependencies = assets[asset_id].dependency_ids
        else:
            raise _integrity("Image vector has no canonical asset")
        facts.append(
            VectorFact(
                record_id,
                chunk_id=chunk_id,
                asset_id=asset_id,
                dependency_ids=dependencies,
            )
        )
    return tuple(facts)


def _graph_receipt(db, batches) -> GraphBatchReceipt | None:
    """The receipt of the snapshot's graph root batch, if it binds one."""
    graph = [batch for slot, batch, _ in batches if slot == "graph"]
    if not graph:
        return None
    if len(graph) != 1:
        raise _integrity("Snapshot binds more than one graph generation")
    receipt = SqliteBatchRepository(db, writable=False).receipt(graph[0])
    if not isinstance(receipt, GraphBatchReceipt):
        raise _integrity("Graph batch receipt is unavailable", "canonical.row_invalid")
    return receipt


def all_members(store: CorpusStore, snapshot_id: str) -> Iterator[SnapshotMember]:
    """Every member of the snapshot, page by page, inside the open read."""
    with store.uow.read() as tx:
        yield from tx.snapshots.all_members(snapshot_id)


def read_eligibility_facts(
    store: CorpusStore, snapshot: SnapshotView, members: Iterable[SnapshotMember]
) -> EligibilityFacts:
    """Detach one same-epoch policy observation; release the read before dispatch."""
    db = store.db
    if db.in_transaction:
        raise _integrity(
            "Eligibility requires its own short read", "invariant.unexpected"
        )
    db.execute("BEGIN")
    try:
        policy_epoch_value = SqliteWorkspaceRepository(db).policy_epoch()
        revision_facts: list[RevisionFact] = []
        unit_facts: list[UnitFact] = []
        chunk_facts: list[ChunkFact] = []
        asset_facts: list[AssetFact] = []
        revision_facets: list[tuple[str, str, object]] = []
        historical: dict[str, object] = {}
        selected_extractions: set[str] = set()
        for member in members:
            row = db.execute(
                "SELECT r.kind_id,r.access_rank,r.allowed_uses_json,r.published_on,"
                "r.effective_from,r.effective_until,c.collection_id,c.access_floor,"
                "s.access_floor,s.withdrawn_at,c.workspace_id,k.definition_json,k.definition_hash,r.metadata_json,r.metadata_sha,c.policy_json,"
                "s.allowed_uses_override "
                "FROM revisions r JOIN sources s USING(source_id) "
                "JOIN collections c USING(collection_id) JOIN kinds k USING(kind_id) "
                "JOIN extractions e USING(revision_id) "
                "WHERE r.revision_id=? AND s.source_id=? AND e.extraction_id=?",
                (member.revision_id, member.source_id, member.extraction_id),
            ).fetchone()
            if row is None or row[10] != snapshot.workspace_id:
                raise _integrity("Selected extraction no longer has canonical facts")
            selected_extractions.add(member.extraction_id)
            envelope = json.loads(row[11])
            recorded = envelope.pop("envelope_digest", None)
            if (
                recorded != row[12]
                or digest("historical-kind-v1", envelope) != recorded
            ):
                raise _integrity(
                    "Original kind envelope differs from canonical binding"
                )
            historical[row[0]] = {**envelope, "envelope_digest": recorded}
            kind_uses = frozenset(envelope["kind"].get("allowed_uses", ()))
            metadata = FrozenJsonMap(json.loads(row[13]))
            if digest("metadata-v1", metadata) != row[14]:
                raise _integrity("Selected metadata differs from immutable digest")
            global_facets, global_binding = global_facet_binding_bytes(
                metadata,
                member.revision_id,
                row[14],
                store.registry,
            )
            marker_rows = db.execute(
                "SELECT ra.source_ref,a.artifact_sha,a.size_bytes,a.media_type,a.storage_key "
                "FROM revision_artifacts ra LEFT JOIN artifacts a USING(artifact_sha) "
                "WHERE ra.revision_id=? AND ra.role=?",
                (member.revision_id, GLOBAL_FACET_ROLE),
            ).fetchall()
            if global_binding is None:
                if marker_rows:
                    raise _integrity("Undeclared global facet binding exists")
            else:
                if (
                    len(marker_rows) != 1
                    or marker_rows[0][0] != GLOBAL_FACET_SOURCE_REF
                    or marker_rows[0][1] is None
                    or marker_rows[0][2] > 65536
                ):
                    raise TychoraError(
                        "PROFILE_INCOMPATIBLE",
                        "Retained global facet lacks a verified binding; publish a new edition",
                    )
                marker = marker_rows[0]
                try:
                    with store.artifacts.open_verified(
                        artifact_ref(marker[1:5])
                    ) as stream:
                        if stream.read(65537) != global_binding:
                            raise TychoraError(
                                "PROFILE_INCOMPATIBLE", "Global facet semantics differ"
                            )
                except OSError as error:
                    raise _integrity(
                        "Global facet binding payload is unavailable"
                    ) from error
            revision_facts.append(
                RevisionFact(
                    source_id=member.source_id,
                    revision_id=member.revision_id,
                    extraction_id=member.extraction_id,
                    collection_id=row[6],
                    collection_archived=not collection_is_active(row[15]),
                    kind_id=row[0],
                    access_rank=row[1],
                    source_floor=row[8],
                    collection_floor=row[7],
                    allowed_uses=overridden_uses(
                        frozenset(json.loads(row[2])), row[16]
                    ),
                    review_state=store.review_state(
                        ReviewTarget(revision_id=member.revision_id)
                    ),
                    withdrawn=row[9] is not None,
                    published_on=row[3],
                    effective_from=row[4],
                    effective_until=row[5],
                    is_default=member.is_default,
                    kind_allowed_uses=kind_uses,
                    global_facets=global_facets,
                )
            )
            for key, text, number, boolean in db.execute(
                "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values WHERE revision_id=?",
                (member.revision_id,),
            ):
                value = (
                    text
                    if text is not None
                    else boolean
                    if boolean is not None
                    else number
                )
                revision_facets.append((member.revision_id, key, value))
            unit_rows = db.execute(
                "SELECT u.unit_id,u.unit_type,u.citable,p.asset_id FROM evidence_units u "
                "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                "WHERE u.extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT chunk_id,unit_id,context_group_key FROM chunks WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            asset_ids = tuple(
                row[0]
                for row in db.execute(
                    "SELECT asset_id FROM assets WHERE extraction_id=?",
                    (member.extraction_id,),
                )
            )
            dependencies = _persisted(
                db,
                (
                    *(row[0] for row in unit_rows),
                    *(row[0] for row in chunk_rows),
                    *asset_ids,
                ),
            )
            for uid, unit_type, citable, proxy_asset in unit_rows:
                unit_facts.append(
                    UnitFact(
                        uid,
                        member.revision_id,
                        member.extraction_id,
                        unit_type,
                        store.review_state(ReviewTarget(unit_id=uid)),
                        bool(citable),
                        dependencies[uid],
                        proxy_asset_id=proxy_asset,
                        facets=_unit_facets(db, uid),
                    )
                )
            source_asset_owners: dict[str, set[str]] = defaultdict(set)
            for uid, aid in db.execute(
                "SELECT ua.unit_id,ua.asset_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=? AND ua.role='source'",
                (member.extraction_id,),
            ):
                source_asset_owners[aid].add(uid)
            for aid in asset_ids:
                asset_facts.append(
                    AssetFact(
                        aid,
                        member.revision_id,
                        store.review_state(ReviewTarget(asset_id=aid)),
                        dependencies[aid],
                        source_owner_ids=frozenset(source_asset_owners.get(aid, ())),
                    )
                )
            chunk_facts.extend(
                ChunkFact(chunk_id, uid, dependencies[chunk_id], group)
                for chunk_id, uid, group in chunk_rows
            )
        assets_by_id = {item.asset_id: item for item in asset_facts}
        batches = tuple(db.execute(_SNAPSHOT_BATCHES, (snapshot.snapshot_id,)))
        result = EligibilityFacts(
            policy_epoch=policy_epoch_value,
            revisions=tuple(revision_facts),
            units=tuple(unit_facts),
            chunks=tuple(chunk_facts),
            assets=tuple(asset_facts),
            vectors=_vector_facts(
                db, snapshot.snapshot_id, selected_extractions, assets_by_id
            ),
            assertion_decisions=tuple(
                db.execute(
                    "SELECT assertion_id,MAX(decision_id) FROM assertion_decisions "
                    "GROUP BY assertion_id"
                )
            ),
            revision_facets=tuple(revision_facets),
            historical_definitions=tuple(sorted(historical.items())),
            batches=batches,
            lexical_chunks=tuple(db.execute(_LEXICAL_CHUNKS, (snapshot.snapshot_id,))),
            graph_receipt=_graph_receipt(db, batches),
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def policy_epoch(store: CorpusStore, workspace_id: str) -> int:
    return SqliteWorkspaceRepository(store.db).policy_epoch()
