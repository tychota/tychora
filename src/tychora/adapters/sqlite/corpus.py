"""Prepared, explicit transactions over immutable corpus rows."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import NoReturn, cast

from tychora.adapters.sqlite.repositories import (
    SqliteRepresentationRepository,
    SqliteUnitOfWork,
    artifact_ref,
)
from tychora.application.collection_policy import require_active_collection
from tychora.application.leases import LeaseSet
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry
from tychora.contracts.base import Contract, FrozenJsonMap
from tychora.contracts.candidates import ChannelCandidates
from tychora.contracts.corpus import ExtractionBundle, ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit, Geometry
from tychora.contracts.graph import EligibilityView
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.indexing import RepresentationFacts
from tychora.contracts.publication import (
    PublicationPlan,
    RetainedBatch,
    SnapshotMember,
    SnapshotView,
)
from tychora.contracts.snapshots import SlotFamily
from tychora.ports.artifacts import Artifacts
from tychora.ports.execution import ExecutionContext
from tychora.ports.graph import GraphStore
from tychora.ports.vectors import VectorIndex

from .migrations import SCHEMA_VERSION
from .policy import PolicyRepository
from .publication import publish_snapshot
from .retained_slots import iter_retained_batches
from .validation import validate_bundle, verify_artifacts


def _json(value) -> str:
    return canonical_bytes(value).decode("utf-8")


def _row(value: Contract) -> dict:
    result = value.model_dump(mode="json")
    return {
        key: _json(item) if key.endswith("_json") else item
        for key, item in result.items()
    }


def _fail(message: str) -> NoReturn:
    raise TychoraError("INTEGRITY_ERROR", message)


class CorpusStore(PolicyRepository):
    artifacts: Artifacts

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        registry: Registry,
        artifacts: Artifacts,
        required_slots: frozenset[str] = frozenset({"lexical"}),
        vector_indexes: dict[str, VectorIndex] | None = None,
        graph_indexes: dict[str, GraphStore] | None = None,
        leases: LeaseSet | None = None,
        publication_context: ExecutionContext | None = None,
    ):
        self.db = db
        self.registry = registry
        self.artifacts = artifacts
        self.required_slots = required_slots
        self.vector_indexes = vector_indexes if vector_indexes is not None else {}
        self.graph_indexes = graph_indexes if graph_indexes is not None else {}
        # The attached graph store serves every generation, including those of
        # unmounted snapshots that ``graph_indexes`` does not list.
        self.graph_store: GraphStore | None = None
        self.leases = leases if leases is not None else LeaseSet()
        self.publication_context = publication_context
        self.uow = SqliteUnitOfWork(db)

    @contextmanager
    def immediate_transaction(self):
        if self.db.in_transaction:
            _fail("Publication requires a fresh transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def active_snapshot_id(self) -> str | None:
        with self.uow.read() as tx:
            return tx.snapshots.active_id()

    def publish_snapshot(self, plan: PublicationPlan) -> str:
        """Verify a plan's new pages and batches, then activate its snapshot."""
        return publish_snapshot(self, plan)

    def read_eligibility_facts(
        self,
        snapshot: SnapshotView,
        members: tuple[SnapshotMember, ...] | None = None,
    ):
        """Detach the snapshot's eligibility facts; no artifact is reopened.

        The configuration artifact was verified when the snapshot was published.
        """
        from .eligibility import all_members, read_eligibility_facts

        return read_eligibility_facts(
            self,
            snapshot,
            members if members is not None else all_members(self, snapshot.snapshot_id),
        )

    def retained_config(self, snapshot: SnapshotView) -> ResolvedConfig:
        return self.retained_config_sha(snapshot.config_sha)

    def retained_config_sha(self, config_sha: str) -> ResolvedConfig:
        """Open a canonical retained config artifact with CAS verification."""

        row = self.db.execute(
            "SELECT size_bytes,media_type,storage_key,payload_state FROM artifacts WHERE artifact_sha=?",
            (config_sha,),
        ).fetchone()
        if row is None or row[2] != config_sha or row[3] != "present":
            _fail("Retained resolved-config artifact is unavailable")
        ref = artifact_ref((config_sha, *row[:3]))
        with self.artifacts.open_verified(ref) as stream:
            try:
                payload = json.load(stream)
            except (ValueError, UnicodeError):
                _fail("Retained resolved-config artifact is invalid")
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "registry"
            or payload.get("schema_version") != "1"
            or not isinstance(payload.get("data"), dict)
        ):
            _fail("Retained resolved-config artifact is invalid")

        try:
            return ResolvedConfig.model_validate_json(canonical_bytes(payload["data"]))
        except ValueError:
            _fail("Retained resolved-config artifact is invalid")

    def policy_epoch(self, workspace_id: str) -> int:
        from .eligibility import policy_epoch

        return policy_epoch(self, workspace_id)

    def lookup_identifiers(
        self, namespaced: tuple[str, ...], view: EligibilityView, limit: int
    ) -> ChannelCandidates:
        from .lexical import lookup_identifiers

        return lookup_identifiers(self, namespaced, view, limit)

    def resolve_snapshot(self, selector: str | None) -> SnapshotView:
        """Read the selected or active snapshot from SQL; no artifact is opened."""
        snapshot_id = selector or self.active_snapshot_id()
        if snapshot_id is None:
            raise TychoraError(
                "NOT_FOUND",
                "No published snapshot is available",
                reason="snapshot.absent",
            )
        with self.uow.read() as tx:
            return tx.snapshots.view(snapshot_id)

    def snapshot_member(
        self, snapshot_id: str, extraction_id: str
    ) -> SnapshotMember | None:
        """Return the member of a snapshot that selects this extraction, in a short read."""
        with self.uow.read() as tx:
            return tx.snapshots.member(snapshot_id, extraction_id)

    @contextmanager
    def pin_snapshot(self, selector: str | None):
        """Lease one snapshot for a read; retention and native GC honour the lease."""
        view = self.resolve_snapshot(selector)
        with self.leases.pin(frozenset({view.snapshot_id})):
            yield view

    def iter_snapshot_batches(
        self,
        view: SnapshotView,
        *,
        slots: frozenset[str] | None = None,
        before_each: Callable[[], None] | None = None,
    ) -> Iterator[tuple[SlotFamily, RetainedBatch]]:
        """Stream a snapshot's batches fresh from SQL, one held at a time."""
        return iter_retained_batches(
            self.db, self.artifacts, view, slots=slots, before_each=before_each
        )

    def _insert(self, table: str, row: dict, keys: tuple[str, ...]):
        # Identifiers originate only in the closed repository mappings below.
        columns = tuple(row)
        found = self.db.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE "
            + " AND ".join(f"{k} IS ?" for k in keys),
            tuple(row[key] for key in keys),
        ).fetchone()
        values = tuple(row.values())
        if found is not None:
            if tuple(found) != values:
                _fail("Immutable identifier has different content")
            return
        self.db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            values,
        )

    def _register(self, table: str, values: dict, key: str) -> bool:
        current = self.db.execute(
            f"SELECT access_floor FROM {table} WHERE {key}=?", (values[key],)
        ).fetchone()
        raised = current is not None and values["access_floor"] > current[0]
        if current:
            floor = max(current[0], values["access_floor"])
            if raised:
                self.db.execute(
                    f"UPDATE {table} SET access_floor=? WHERE {key}=?",
                    (floor, values[key]),
                )
            values["access_floor"] = floor
        self._insert(table, values, (key,))
        return raised

    def commit_extraction(
        self, bundle: ExtractionBundle, facts: RepresentationFacts | None = None
    ) -> str:
        """Store one extraction and its representation facts in one transaction."""
        if self.db.in_transaction:
            _fail("Corpus writes require their own transaction")
        payloads = verify_artifacts(bundle, self.artifacts)
        validate_bundle(bundle, self.registry, payloads)
        try:
            with (
                self.artifacts.pin_payloads(
                    frozenset(ref.sha256 for ref in bundle.artifacts)
                ),
                self._write(),
            ):
                self._check_membership(bundle)
                self._insert(
                    "workspaces",
                    {**_row(bundle.workspace), "schema_version": SCHEMA_VERSION},
                    ("workspace_id",),
                )
                collection = _row(bundle.collection)
                current = self.db.execute(
                    "SELECT workspace_id,slug,title,access_floor,policy_json FROM collections WHERE collection_id=?",
                    (bundle.collection.collection_id,),
                ).fetchone()
                if current is not None:
                    if (
                        current[0] != bundle.workspace.workspace_id
                        or bundle.source.collection_id
                        != bundle.collection.collection_id
                    ):
                        _fail("Collection immutable membership differs")
                    require_active_collection(current[4])
                    if current[3] > bundle.collection.access_floor:
                        raise TychoraError(
                            "SCOPE_CHANGED", "Collection floor changed since capture."
                        )
                    # Captured bundles cannot restore stale names, consent or lifecycle.
                    collection.update(
                        workspace_id=current[0],
                        slug=current[1],
                        title=current[2],
                        policy_json=current[4],
                    )
                else:
                    require_active_collection(collection["policy_json"])
                raised = self._register("collections", collection, "collection_id")
                existing_source = self.db.execute(
                    "SELECT withdrawn_at FROM sources WHERE source_id=?",
                    (bundle.source.source_id,),
                ).fetchone()
                if existing_source is not None and existing_source[0] is not None:
                    raise TychoraError(
                        "FORBIDDEN_SCOPE", "Withdrawn source cannot be republished."
                    )
                raised |= self._register("sources", _row(bundle.source), "source_id")
                if raised:
                    self._bump(bundle.workspace.workspace_id)
                # Registry construction has validated the immutable envelope shape.
                envelope = cast(
                    FrozenJsonMap,
                    self.registry.historical_definitions()[bundle.revision.kind_id],
                )
                kind = cast(FrozenJsonMap, envelope["kind"])
                self._insert(
                    "kinds",
                    {
                        "kind_id": kind["id"],
                        "definition_hash": envelope["envelope_digest"],
                        "label": kind["label"],
                        "description": kind["description"],
                        "role": kind["role"],
                        "definition_json": _json(envelope),
                    },
                    ("kind_id",),
                )
                for ref in bundle.artifacts:
                    current = self.db.execute(
                        "SELECT payload_state,created_at,media_type FROM artifacts WHERE artifact_sha=?",
                        (ref.sha256,),
                    ).fetchone()
                    if current and current[0] != "present":
                        if (
                            self.db.execute(
                                "SELECT 1 FROM revisions WHERE revision_id=?",
                                (bundle.revision.revision_id,),
                            ).fetchone()
                            or not self.db.execute(
                                "SELECT 1 FROM original_payload_tombstones WHERE artifact_sha=?",
                                (ref.sha256,),
                            ).fetchone()
                        ):
                            _fail("Deleted artifact cannot satisfy an old binding")
                        # verify_artifacts already proved that the new binding
                        # rematerialized and verified the complete hash bytes.
                        self.db.execute(
                            "UPDATE artifacts SET payload_state='present',payload_deleted_at=NULL WHERE artifact_sha=?",
                            (ref.sha256,),
                        )
                    self._insert(
                        "artifacts",
                        {
                            "artifact_sha": ref.sha256,
                            "size_bytes": ref.size_bytes,
                            "media_type": current[2] if current else ref.media_type,
                            "storage_key": ref.storage_key,
                            "created_at": current[1]
                            if current
                            else bundle.extraction.created_at,
                        },
                        ("artifact_sha",),
                    )
                self._insert("revisions", _row(bundle.revision), ("revision_id",))
                for row in bundle.revision_artifacts:
                    self._insert(
                        "revision_artifacts",
                        _row(row),
                        ("revision_id", "role", "source_ref"),
                    )
                self._insert("extractions", _row(bundle.extraction), ("extraction_id",))
                for unit in bundle.units:
                    self._insert(
                        "evidence_units",
                        {
                            "unit_id": unit.unit_id,
                            "extraction_id": unit.extraction_id,
                            "parent_id": unit.parent_id,
                            "ordinal": unit.ordinal,
                            "unit_type": unit.unit_type,
                            "native_ref": unit.native_ref,
                            "boundary_key": unit.boundary_key,
                            "heading_path_json": _json(unit.heading_path),
                            "text": unit.text,
                            "text_sha": unit.text_sha,
                            "text_origin": unit.text_origin,
                            "citable": int(unit.citable),
                            "declared_review_state": unit.review_state,
                            "attributes_json": _json(unit.attributes),
                        },
                        ("unit_id",),
                    )
                for row in bundle.locators:
                    loc = row.locator
                    geometry = cast(Geometry | None, getattr(loc, "geometry", None))
                    self._insert(
                        "locators",
                        {
                            "locator_id": row.locator_id,
                            "unit_id": row.unit_id,
                            "fragment_ordinal": row.fragment_ordinal,
                            "locator_type": loc.type,
                            "artifact_sha": loc.artifact_ref,
                            "source_ref": loc.native_ref,
                            "physical_page": getattr(loc, "physical_page", None),
                            "page_label": getattr(loc, "printed_page", None),
                            "line_start": getattr(loc, "line_start", None),
                            "line_end": getattr(loc, "line_end", None),
                            "byte_start": getattr(loc, "byte_start", None),
                            "byte_end": getattr(loc, "byte_end", None),
                            "geometry_json": _json(geometry.model_dump(mode="json"))
                            if geometry
                            else None,
                            "details_json": loc.model_dump_json(),
                        },
                        ("locator_id",),
                    )
                for row in bundle.chunks:
                    values = _row(row)
                    values.pop("text")
                    self._insert("chunks", values, ("chunk_id",))
                for row in bundle.assets:
                    values = _row(row)
                    values["locator_json"] = _json(values.pop("locator"))
                    self._insert("assets", values, ("asset_id",))
                for row in bundle.unit_assets:
                    self._insert(
                        "unit_assets", _row(row), ("unit_id", "asset_id", "role")
                    )
                for row in bundle.proxies:
                    values = _row(row)
                    values.pop("required_unit_ids")
                    self._insert("asset_proxies", values, ("proxy_unit_id",))
                    for uid in sorted(row.required_unit_ids):
                        self._insert(
                            "asset_proxy_dependencies",
                            {
                                "proxy_unit_id": row.proxy_unit_id,
                                "required_unit_id": uid,
                            },
                            ("proxy_unit_id", "required_unit_id"),
                        )
                for row in bundle.records:
                    self._insert("structured_records", _row(row), ("unit_id",))
                self._insert_facets(bundle)
                for row in bundle.identifiers:
                    self._insert(
                        "identifiers",
                        _row(row),
                        ("namespace", "normalized_value", "unit_id"),
                    )
                self._write_facts(bundle, facts)
            return bundle.extraction.extraction_id
        except sqlite3.IntegrityError:
            _fail("Corpus constraint violation")

    def _insert_facets(self, bundle: ExtractionBundle) -> None:
        """Insert each facet value and its registered definition."""
        for row in bundle.facets:
            definition = self.registry.facet(row.facet_key)
            storage_type = {
                "string": "text",
                "integer": "number",
                "number": "number",
                "boolean": "boolean",
                "date": "date",
                "date-time": "text",
            }[cast(str, definition["value_type"])]
            self._insert(
                "facet_definitions",
                {
                    "facet_key": row.facet_key,
                    "value_type": storage_type,
                    "operators_json": _json(definition["operators"]),
                    "filter_scope": definition["filter_scope"],
                    "definition_hash": digest("facet-v1", definition),
                },
                ("facet_key",),
            )
            values = {
                "revision_id": row.revision_id,
                "unit_id": row.unit_id,
                "facet_key": row.facet_key,
                "ordinal": row.ordinal,
                "text_value": None,
                "number_value": None,
                "boolean_value": None,
            }
            column = (
                "text_value"
                if storage_type in ("text", "date")
                else storage_type + "_value"
            )
            values[column] = int(row.value) if storage_type == "boolean" else row.value
            self._insert(
                "facet_values",
                values,
                ("revision_id", "unit_id", "facet_key", "ordinal"),
            )

    def _write_facts(
        self, bundle: ExtractionBundle, facts: RepresentationFacts | None
    ) -> None:
        """Write an extraction's facts inside its commit transaction."""
        if facts is None:
            return
        if facts.extraction_id != bundle.extraction.extraction_id:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Representation facts belong to another extraction",
                reason="canonical.binding_mismatch",
            )
        SqliteRepresentationRepository(self.db, writable=True).write(facts)

    def _check_membership(self, bundle: ExtractionBundle) -> None:
        eid = bundle.extraction.extraction_id
        rid = bundle.revision.revision_id
        if self.db.execute(
            "SELECT 1 FROM revisions WHERE revision_id=?", (rid,)
        ).fetchone():
            existing = set(
                self.db.execute(
                    "SELECT role,source_ref,artifact_sha FROM revision_artifacts WHERE revision_id=?",
                    (rid,),
                )
            )
            expected = {
                (r.role, r.source_ref, r.artifact_sha)
                for r in bundle.revision_artifacts
            }
            if existing != expected:
                _fail("Immutable revision artifact membership differs")
            existing_facets = set(
                self.db.execute(
                    "SELECT facet_key,ordinal FROM facet_values WHERE revision_id=?",
                    (rid,),
                )
            )
            expected_facets = {
                (row.facet_key, row.ordinal)
                for row in bundle.facets
                if row.revision_id == rid
            }
            if existing_facets != expected_facets:
                _fail("Immutable revision facet membership differs")
        if not self.db.execute(
            "SELECT 1 FROM extractions WHERE extraction_id=?", (eid,)
        ).fetchone():
            return
        checks = (
            (
                "SELECT unit_id FROM evidence_units WHERE extraction_id=?",
                {(r.unit_id,) for r in bundle.units},
            ),
            (
                "SELECT chunk_id FROM chunks WHERE extraction_id=?",
                {(r.chunk_id,) for r in bundle.chunks},
            ),
            (
                "SELECT asset_id FROM assets WHERE extraction_id=?",
                {(r.asset_id,) for r in bundle.assets},
            ),
            (
                "SELECT locator_id FROM locators JOIN evidence_units USING(unit_id) WHERE extraction_id=?",
                {(r.locator_id,) for r in bundle.locators},
            ),
            (
                "SELECT unit_id FROM structured_records JOIN evidence_units USING(unit_id) WHERE extraction_id=?",
                {(r.unit_id,) for r in bundle.records},
            ),
            (
                "SELECT namespace,normalized_value,unit_id FROM identifiers JOIN evidence_units USING(unit_id) WHERE extraction_id=?",
                {
                    (r.namespace, r.normalized_value, r.unit_id)
                    for r in bundle.identifiers
                },
            ),
            (
                "SELECT unit_id,asset_id,role FROM unit_assets JOIN evidence_units USING(unit_id) WHERE extraction_id=?",
                {(r.unit_id, r.asset_id, r.role) for r in bundle.unit_assets},
            ),
            (
                "SELECT proxy_unit_id FROM asset_proxies JOIN evidence_units ON unit_id=proxy_unit_id WHERE extraction_id=?",
                {(r.proxy_unit_id,) for r in bundle.proxies},
            ),
            (
                "SELECT proxy_unit_id,required_unit_id FROM asset_proxy_dependencies JOIN evidence_units ON unit_id=proxy_unit_id WHERE extraction_id=?",
                {
                    (r.proxy_unit_id, uid)
                    for r in bundle.proxies
                    for uid in r.required_unit_ids
                },
            ),
        )
        facets = {
            (r.unit_id, r.facet_key, r.ordinal)
            for r in bundle.facets
            if r.unit_id is not None
        }
        existing_facets = set(
            self.db.execute(
                "SELECT unit_id,facet_key,ordinal FROM facet_values WHERE unit_id IN (SELECT unit_id FROM evidence_units WHERE extraction_id=?)",
                (eid,),
            )
        )
        if facets != existing_facets:
            _fail("Immutable facet membership differs")
        for query, expected in checks:
            if set(self.db.execute(query, (eid,))) != expected:
                _fail("Immutable extraction membership differs")

    def hydrate(self, unit_ids: tuple[str, ...]) -> tuple[EvidenceUnit, ...]:
        if self.db.in_transaction:
            _fail("Hydration requires its own short read transaction")
        self.db.execute("BEGIN")
        try:
            result = self._hydrate(unit_ids)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _hydrate(self, unit_ids: tuple[str, ...]) -> tuple[EvidenceUnit, ...]:
        result = []
        for uid in unit_ids:
            cursor = self.db.execute(
                "SELECT * FROM evidence_units WHERE unit_id=?", (uid,)
            )
            row = cursor.fetchone()
            if row is None:
                raise TychoraError("NOT_FOUND", "Evidence unit is unavailable")
            values = dict(zip((c[0] for c in cursor.description), row))
            values["heading_path"] = json.loads(values.pop("heading_path_json"))
            values["attributes"] = json.loads(values.pop("attributes_json"))
            values.pop("declared_review_state")
            values["review_state"] = self.review_state(ReviewTarget(unit_id=uid))
            values["citable"] = bool(values["citable"])
            values["locators"] = [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT details_json FROM locators WHERE unit_id=? ORDER BY fragment_ordinal",
                    (uid,),
                )
            ]
            result.append(EvidenceUnit.from_mapping(values))
        return tuple(result)
