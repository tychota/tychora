"""Short canonical reads for detached retrieval policy and ownership facts."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from tychora.adapters.sqlite.repositories import SqliteWorkspaceRepository, artifact_ref
from tychora.adapters.sqlite.retained_bindings import BindingScope
from tychora.adapters.sqlite.retained_slots import load_retained_slots
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
from tychora.contracts.identity import digest
from tychora.contracts.publication import RetainedBatch, RetainedSlot, SnapshotView
from tychora.indexing.representations import prepare_extraction
from tychora.retrieval.eligibility import (
    AssetFact,
    ChunkFact,
    EligibilityFacts,
    RevisionFact,
    UnitFact,
    VectorFact,
)

if TYPE_CHECKING:
    from .corpus import CorpusStore


def _integrity(
    message: str, reason: str = "canonical.binding_mismatch"
) -> TychoraError:
    return TychoraError("INTEGRITY_ERROR", message, reason=reason)


def _owners_by_locator(
    rows: list[tuple[str, str, str, int, int]],
) -> dict[str, set[str]]:
    """Every overlapping original range owner, including descendants."""
    owners: dict[str, set[str]] = defaultdict(set)
    for uid, artifact, source, start, end in rows:
        for other, other_artifact, other_source, left, right in rows:
            if (
                artifact == other_artifact
                and source == other_source
                and start < right
                and left < end
            ):
                owners[uid].add(other)
    return owners


def _rows(db, query: str, args: tuple[str, ...] = ()) -> list[dict]:
    cursor = db.execute(query, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _retained_heads(db, member) -> tuple[dict, dict, dict]:
    """The source, revision and extraction rows of one member, JSON decoded."""
    source = _rows(db, "SELECT * FROM sources WHERE source_id=?", (member.source_id,))[
        0
    ]
    source.pop("withdrawn_at")
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


class _Owners(NamedTuple):
    """Selected chunks, their document order and selected assets, by ID."""

    chunks: dict[str, ChunkFact]
    order: dict[str, tuple[int, int, str, str]]
    assets: dict[str, AssetFact]


def _require_text_ownership(
    store: CorpusStore, batch: RetainedBatch, owners: _Owners
) -> dict[str, list]:
    """Check the text batch input records its groups; return bindings by group."""
    with store.artifacts.open_verified(batch.input_manifest) as stream:
        manifest = json.load(stream)
    data = manifest.get("data", {})
    expected = [
        {
            "record_id": binding.record_id,
            "group_id": binding.group_id,
            "dependency_ids": sorted(binding.dependency_ids),
        }
        for binding in batch.vector_records
    ]
    if (
        data.get("entries") != [binding.record_id for binding in batch.vector_records]
        or data.get("representations") != expected
        or any(
            binding.group_id is None
            or not binding.dependency_ids
            or binding.chunk_id not in owners.chunks
            for binding in batch.vector_records
        )
    ):
        raise TychoraError(
            "INDEX_INCOMPLETE",
            "Text batch needs rebuilt group ownership",
            reason="batch.missing",
        )
    groups: dict[str, list] = defaultdict(list)
    for binding in batch.vector_records:
        groups[binding.group_id or ""].append(binding)
    return groups


def _group_vector_facts(
    batch: RetainedBatch, group_id: str, bindings: list, owners: _Owners
) -> list[VectorFact]:
    """Check one context group's bindings and return its vector facts."""
    ordered = sorted(bindings, key=lambda binding: owners.order[binding.chunk_id])
    first = owners.chunks[ordered[0].chunk_id]
    if any(
        owners.chunks[item.chunk_id].context_group_key != first.context_group_key
        or owners.order[item.chunk_id][3] != batch.extraction_id
        or item.input_sha != ordered[0].input_sha
        or item.dependency_ids != ordered[0].dependency_ids
        for item in ordered
    ):
        raise _integrity("Text group binding differs from selected extraction")
    expected_group = digest(
        "context-group-input-v1",
        batch.extraction_id,
        first.context_group_key,
        [item.chunk_id for item in ordered],
        ordered[0].input_sha,
    )
    dependencies = frozenset(
        dep for item in ordered for dep in owners.chunks[item.chunk_id].dependency_ids
    )
    if group_id != expected_group or dependencies != ordered[0].dependency_ids:
        raise _integrity("Text group omits or adds represented owners")
    return [
        VectorFact(item.record_id, chunk_id=item.chunk_id, dependency_ids=dependencies)
        for item in ordered
    ]


def _text_vector_facts(
    store: CorpusStore, batch: RetainedBatch, owners: _Owners
) -> list[VectorFact]:
    """Check a text batch's group ownership and return its vector facts."""
    groups = _require_text_ownership(store, batch, owners)
    return [
        fact
        for group_id, bindings in groups.items()
        for fact in _group_vector_facts(batch, group_id, bindings, owners)
    ]


def _image_vector_facts(batch: RetainedBatch, owners: _Owners) -> list[VectorFact]:
    """Bind each image vector to its canonical asset."""
    facts = []
    for binding in batch.vector_records:
        asset = owners.assets.get(binding.asset_id or "")
        if asset is None:
            raise _integrity("Image vector has no canonical asset")
        facts.append(
            VectorFact(
                binding.record_id,
                asset_id=asset.asset_id,
                dependency_ids=asset.dependency_ids,
            )
        )
    return facts


def _vector_facts(
    store: CorpusStore,
    slots: tuple[RetainedSlot, ...],
    selected: set[str],
    owners: _Owners,
) -> tuple[VectorFact, ...]:
    """Vector facts of the selected extractions' text and image batches."""
    facts: list[VectorFact] = []
    for slot in slots:
        for batch in slot.batches:
            if batch.extraction_id not in selected:
                continue
            if slot.slot == "text":
                facts.extend(_text_vector_facts(store, batch, owners))
            elif slot.slot == "image":
                facts.extend(_image_vector_facts(batch, owners))
    return tuple(facts)


def read_eligibility_facts(
    store: CorpusStore, snapshot: SnapshotView
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
        chunk_order: dict[str, tuple[int, int, str, str]] = {}
        for member in snapshot.members:
            row = db.execute(
                "SELECT r.kind_id,r.access_rank,r.allowed_uses_json,r.published_on,"
                "r.effective_from,r.effective_until,c.collection_id,c.access_floor,"
                "s.access_floor,s.withdrawn_at,c.workspace_id,k.definition_json,k.definition_hash,r.metadata_json,r.metadata_sha,c.policy_json "
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
                    allowed_uses=frozenset(json.loads(row[2])),
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
                "SELECT unit_id,unit_type,citable,attributes_json FROM evidence_units WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            proxy_rows = {
                uid: asset
                for uid, asset in db.execute(
                    "SELECT proxy_unit_id,asset_id FROM asset_proxies "
                    "WHERE proxy_unit_id IN (SELECT unit_id FROM evidence_units WHERE extraction_id=?)",
                    (member.extraction_id,),
                )
            }
            locator_rows: list[tuple[str, str, str, int, int]] = []
            for uid, artifact, source, details in db.execute(
                "SELECT l.unit_id,l.artifact_sha,l.source_ref,l.details_json FROM locators l "
                "JOIN evidence_units u USING(unit_id) WHERE u.extraction_id=?",
                (member.extraction_id,),
            ):
                locator = json.loads(details)
                if locator.get("type") in ("text", "code"):
                    locator_rows.append(
                        (
                            uid,
                            artifact,
                            source,
                            locator["char_start"],
                            locator["char_end"],
                        )
                    )
            overlap = _owners_by_locator(locator_rows)
            prepared = prepare_extraction(retained_rows(store, member))
            ownership = {item.unit.unit_id: item.ownership for item in prepared.units}
            attributes = {uid: json.loads(raw) for uid, _, _, raw in unit_rows}
            unit_deps: dict[str, frozenset[str]] = {}
            for uid, unit_type, citable, _ in unit_rows:
                attrs = attributes[uid]
                declared = attrs.get(
                    "overlapping_owner_ids", attrs.get("range_dependency_ids")
                )
                owners = (
                    set(declared) if declared is not None else overlap.get(uid, {uid})
                )
                owners.add(uid)
                proxy_asset = proxy_rows.get(uid)
                if proxy_asset is not None:
                    owners.add(proxy_asset)
                    owners.update(
                        child
                        for (child,) in db.execute(
                            "SELECT required_unit_id FROM asset_proxy_dependencies WHERE proxy_unit_id=?",
                            (uid,),
                        )
                    )
                deps = frozenset(
                    dependency
                    for interval in ownership[uid]
                    for dependency in interval.dependency_ids
                ) or frozenset({member.revision_id, *owners})
                unit_deps[uid] = deps
                facets = []
                for key, text, number, boolean in db.execute(
                    "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values WHERE unit_id=?",
                    (uid,),
                ):
                    facets.append(
                        (
                            key,
                            text
                            if text is not None
                            else boolean
                            if boolean is not None
                            else number,
                        )
                    )
                unit_facts.append(
                    UnitFact(
                        uid,
                        member.revision_id,
                        member.extraction_id,
                        unit_type,
                        store.review_state(ReviewTarget(unit_id=uid)),
                        bool(citable),
                        deps,
                        proxy_asset_id=proxy_asset,
                        facets=tuple(facets),
                    )
                )
            asset_owners: dict[str, set[str]] = defaultdict(set)
            source_asset_owners: dict[str, set[str]] = defaultdict(set)
            for uid, aid, role in db.execute(
                "SELECT ua.unit_id,ua.asset_id,ua.role FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=?",
                (member.extraction_id,),
            ):
                asset_owners[aid].add(uid)
                if role == "source":
                    source_asset_owners[aid].add(uid)
            for (aid,) in db.execute(
                "SELECT asset_id FROM assets WHERE extraction_id=?",
                (member.extraction_id,),
            ):
                deps = {member.revision_id, aid}
                for uid in asset_owners.get(aid, ()):
                    deps.update(unit_deps[uid])
                asset_facts.append(
                    AssetFact(
                        aid,
                        member.revision_id,
                        store.review_state(ReviewTarget(asset_id=aid)),
                        frozenset(deps),
                        source_owner_ids=frozenset(source_asset_owners.get(aid, ())),
                    )
                )
            unit_ordinals = {
                uid: ordinal
                for uid, ordinal in db.execute(
                    "SELECT unit_id,ordinal FROM evidence_units WHERE extraction_id=?",
                    (member.extraction_id,),
                )
            }
            for chunk_id, uid, group, start, end in db.execute(
                "SELECT chunk_id,unit_id,context_group_key,char_start,char_end FROM chunks WHERE extraction_id=?",
                (member.extraction_id,),
            ):
                interval_deps = frozenset(
                    dependency
                    for interval in ownership[uid]
                    if interval.char_start < end and start < interval.char_end
                    for dependency in interval.dependency_ids
                )
                chunk_facts.append(
                    ChunkFact(chunk_id, uid, interval_deps or unit_deps[uid], group)
                )
                chunk_order[chunk_id] = (
                    unit_ordinals[uid],
                    start,
                    chunk_id,
                    member.extraction_id,
                )
        chunks_by_id = {item.chunk_id: item for item in chunk_facts}
        assets_by_id = {item.asset_id: item for item in asset_facts}
        slots = load_retained_slots(db, store.artifacts, snapshot)
        owners = _Owners(chunks_by_id, chunk_order, assets_by_id)
        vectors = _vector_facts(store, slots, selected_extractions, owners)
        decisions = tuple(
            (assertion, decision)
            for assertion, decision in db.execute(
                "SELECT assertion_id,MAX(decision_id) FROM assertion_decisions GROUP BY assertion_id"
            )
        )
        result = EligibilityFacts(
            policy_epoch=policy_epoch_value,
            revisions=tuple(revision_facts),
            units=tuple(unit_facts),
            chunks=tuple(chunk_facts),
            assets=tuple(asset_facts),
            vectors=vectors,
            slots=slots,
            assertion_decisions=decisions,
            revision_facets=tuple(revision_facets),
            historical_definitions=tuple(sorted(historical.items())),
        )
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def policy_epoch(store: CorpusStore, workspace_id: str) -> int:
    return SqliteWorkspaceRepository(store.db).policy_epoch()
