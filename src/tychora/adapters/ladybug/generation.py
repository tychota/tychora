"""Immutable evidence-grounded native graph generations."""

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Literal, NoReturn, cast

from pydantic import ValidationError

from ladybug import Connection, Database
from ladybug.query_result import QueryResult
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import EvidenceUnit
from tychora.contracts.graph import (
    AssertionPage,
    EligibilityView,
    GraphAssertionBinding,
    GraphBatchReceipt,
    GraphBudget,
    GraphBuildInput,
    GraphCandidates,
    GraphGenerationSpec,
    GraphManifest,
    GraphTraversalPolicy,
    GraphUnitBinding,
)
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.relationships import RelationshipAssertion
from tychora.ports.execution import ExecutionContext

from .traversal import GraphTraversal

_DDL = (
    "CREATE NODE TABLE Entity(pk STRING PRIMARY KEY, generation_id STRING, entity_id STRING, kind STRING, display_name STRING, content_hash STRING)",
    "CREATE NODE TABLE Evidence(pk STRING PRIMARY KEY, generation_id STRING, evidence_id STRING, revision_id STRING, extraction_id STRING, locator_json STRING, content_hash STRING)",
    "CREATE NODE TABLE RelationAssertion(pk STRING PRIMARY KEY, generation_id STRING, assertion_id STRING, predicate STRING, status STRING, confidence DOUBLE, method STRING, origin_evidence_id STRING, required_support_ids STRING[], artifact_hash STRING, artifact_record_id STRING, content_hash STRING)",
    "CREATE REL TABLE GroundedIn(FROM Entity TO Evidence, MANY_ONE)",
    "CREATE REL TABLE Subject(FROM RelationAssertion TO Entity, MANY_ONE)",
    "CREATE REL TABLE Object(FROM RelationAssertion TO Entity, MANY_ONE)",
    "CREATE REL TABLE SupportedBy(FROM RelationAssertion TO Evidence)",
)
_MAX_NATIVE_OPERATIONS = 128


class LadybugGraphStore:
    """Own one Database and issue writes through its native connections."""

    def __init__(
        self,
        path: str | Path,
        *,
        build: GraphBuildInput | None = None,
        traversal_policy: GraphTraversalPolicy | None = None,
    ) -> None:
        self.path = Path(path)
        self._state_dir = self.path.parent / (self.path.name + ".generations")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._database = Database(str(self.path))
        self._connection = Connection(self._database)
        if not cast(
            QueryResult, self._connection.execute("CALL SHOW_TABLES() RETURN name")
        ).get_all():
            for statement in _DDL:
                self._connection.execute(statement)
        self._build = build
        self._lock = RLock()
        self._traversal = (
            GraphTraversal(self, traversal_policy) if traversal_policy else None
        )

    def expand(
        self,
        view: EligibilityView,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        if self._traversal is None:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Graph traversal policy is unavailable"
            )
        return self._traversal.expand(view, seed_unit_ids, limits, ctx)

    def structural_context(
        self,
        view: EligibilityView,
        seed_unit_ids: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
    ) -> GraphCandidates:
        if self._traversal is None:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Graph traversal policy is unavailable"
            )
        return self._traversal.structural_context(view, seed_unit_ids, limits, ctx)

    def get_assertions(
        self,
        view: EligibilityView,
        selected_unit_ids: tuple[str, ...],
        predicates: tuple[str, ...],
        limits: GraphBudget,
        ctx: ExecutionContext,
        *,
        direction: Literal["outgoing", "incoming", "both"] | None = None,
        relation_view: Literal["semantic", "structural"] | None = None,
    ) -> AssertionPage:
        if self._traversal is None:
            raise TychoraError(
                "INDEX_INCOMPLETE", "Graph traversal policy is unavailable"
            )
        return self._traversal.get_assertions(
            view,
            selected_unit_ids,
            predicates,
            limits,
            ctx,
            direction=direction,
            relation_view=relation_view,
        )

    def close(self) -> None:
        self._connection.close()
        self._database.close()

    def _state_path(self, generation_id: str) -> Path:
        return self._state_dir / digest("graph-generation-state-v1", generation_id)

    def _read_state(self, generation_id: str) -> dict | None:
        path = self._state_path(generation_id)
        return json.loads(path.read_bytes()) if path.exists() else None

    def _write_state(self, generation_id: str, value: dict) -> None:
        path = self._state_path(generation_id)
        temporary = path.with_suffix(".next")
        with temporary.open("wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(self._state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _pk(generation_id: str, external_id: str) -> str:
        return digest("graph-node-v1", generation_id, external_id)

    @staticmethod
    def _fail(message: str) -> NoReturn:
        raise TychoraError("INTEGRITY_ERROR", message)

    def _rows(self, query: str, parameters: dict | None = None) -> list[list[Any]]:
        result = cast(QueryResult, self._connection.execute(query, parameters or {}))
        return cast(list[list[Any]], result.get_all())

    def _one(self, query: str, parameters: dict) -> list | None:
        rows = self._rows(query, parameters)
        if len(rows) > 1:
            self._fail("Graph identity is duplicated")
        return rows[0] if rows else None

    def _transaction(
        self, operations: list[tuple[str, dict]], ctx: ExecutionContext
    ) -> None:
        if len(operations) > _MAX_NATIVE_OPERATIONS:
            self._fail("Native graph transaction exceeds operation bound")
        self._connection.execute("BEGIN TRANSACTION")
        try:
            for query, params in operations:
                ctx.checkpoint()
                self._connection.execute(query, params)
            ctx.checkpoint()
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def begin_generation(self, spec: GraphGenerationSpec) -> str:
        with self._lock:
            if self._build is not None and self._build.spec != spec:
                self._fail("Graph build input differs from generation specification")
            state = self._read_state(spec.generation_id)
            encoded = spec.model_dump(mode="json")
            if state is not None and state["spec"] != encoded:
                self._fail("Graph generation specification differs")
            if state is not None and state.get("dropping", False):
                self._fail("Graph generation removal is in progress")
            if state is None:
                self._write_state(
                    spec.generation_id, {"spec": encoded, "receipt": None}
                )
            return spec.generation_id

    def upsert_batch(
        self,
        generation_id: str,
        assertions: tuple[RelationshipAssertion, ...],
        units: tuple[EvidenceUnit, ...],
        ctx: ExecutionContext,
    ) -> None:
        with self._lock:
            self._upsert_batch_locked(generation_id, assertions, units, ctx)

    def _upsert_batch_locked(
        self,
        generation_id: str,
        assertions: tuple[RelationshipAssertion, ...],
        units: tuple[EvidenceUnit, ...],
        ctx: ExecutionContext,
    ) -> None:
        ctx.checkpoint()
        state = self._read_state(generation_id)
        if state is None:
            raise TychoraError("NOT_FOUND", "Graph generation is unavailable")
        if state.get("dropping", False):
            self._fail("Graph generation removal is in progress")
        if state["receipt"] is not None:
            self._fail("Sealed graph generation is immutable")
        for assertion in assertions:
            try:
                RelationshipAssertion.model_validate(assertion.model_dump())
            except ValidationError:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Graph assertion is invalid"
                ) from None
        if self._build is None:
            if assertions or units:
                self._fail("Graph build lacks verified canonical input")
            return
        canonical_units = {item.unit.unit_id: item for item in self._build.units}
        canonical_assertions = {
            item.assertion.assertion_id: item for item in self._build.assertions
        }
        self._validate_cardinality()
        if any(
            canonical_units.get(unit.unit_id) is None
            or canonical_units[unit.unit_id].unit != unit
            for unit in units
        ):
            self._fail("Graph unit differs from canonical build input")
        if any(
            canonical_assertions.get(item.assertion_id) is None
            or canonical_assertions[item.assertion_id].assertion != item
            for item in assertions
        ):
            self._fail("Graph assertion differs from verified canonical record")
        available = {unit.unit_id for unit in units}
        available.update(
            row[0]
            for row in self._rows(
                "MATCH (e:Evidence) WHERE e.generation_id=$gid RETURN e.evidence_id",
                {"gid": generation_id},
            )
        )
        for assertion in assertions:
            if not assertion.required_support_ids <= available:
                self._fail("Graph assertion has unresolved support")
            if assertion.origin_unit_id not in available:
                self._fail("Graph assertion has unresolved origin")
            self._validate_assertion(
                assertion, canonical_units, canonical_assertions[assertion.assertion_id]
            )
        self._upsert_units(generation_id, units, canonical_units, ctx)
        self._upsert_assertions(generation_id, assertions, canonical_assertions, ctx)

    def _validate_assertion(
        self,
        assertion: RelationshipAssertion,
        units: dict[str, GraphUnitBinding],
        binding: GraphAssertionBinding,
    ) -> None:
        relation = (
            self._build.relations.get(assertion.predicate) if self._build else None
        )
        if not isinstance(relation, FrozenJsonMap):
            self._fail("Graph predicate is not registered")
        subject_types = cast(tuple[str, ...], relation["subject_unit_types"])
        object_types = cast(tuple[str, ...], relation["object_unit_types"])
        subject_kinds = cast(tuple[str, ...], relation["subject_kinds"])
        object_kinds = cast(tuple[str, ...], relation["object_kinds"])
        methods = cast(tuple[str, ...], relation["methods"])
        subject = units.get(assertion.subject.unit_id)
        object_ = units.get(assertion.object.unit_id)
        if subject is None or object_ is None:
            self._fail("Graph endpoint is unresolved")
        if (
            assertion.subject.unit_id not in assertion.required_support_ids
            or assertion.object.unit_id not in assertion.required_support_ids
            or assertion.origin_unit_id not in assertion.required_support_ids
        ):
            self._fail("Graph endpoint or origin lacks required support")
        if (
            subject.unit.unit_type not in subject_types
            or object_.unit.unit_type not in object_types
            or bool(subject_kinds)
            and assertion.subject.kind not in subject_kinds
            or bool(object_kinds)
            and assertion.object.kind not in object_kinds
            or assertion.method not in methods
        ):
            self._fail("Graph predicate endpoint or method differs")
        if relation["revision_binding"] == "same_revision" and (
            subject.revision_id != object_.revision_id
            or subject.revision_id != assertion.revision_id
        ):
            self._fail("Graph assertion crosses revision binding")
        if (
            not relation.get("self_links", True)
            and assertion.subject.entity_id == assertion.object.entity_id
        ):
            self._fail("Graph predicate forbids self links")
        if assertion.method == "model" and assertion.status != "proposed":
            self._fail("Model assertion source status must be proposed")
        if (
            assertion.method == "model"
            and binding.status_at_watermark in ("recorded", "confirmed")
            and binding.decision_id is None
        ):
            self._fail("Model assertion promotion requires a pinned human decision")
        for span in (*assertion.support_spans, *assertion.target_spans):
            unit = units.get(span.unit_id)
            if unit is None or span.char_end > len(unit.unit.text):
                self._fail("Graph assertion span differs from canonical unit text")
        origin = units.get(assertion.origin_unit_id)
        if origin is None or assertion.origin_locator not in origin.unit.locators:
            self._fail("Graph origin locator differs from canonical unit")

    def _validate_cardinality(self) -> None:
        if self._build is None:
            return
        by_predicate: dict[str, list[RelationshipAssertion]] = {}
        for binding in self._build.assertions:
            by_predicate.setdefault(binding.assertion.predicate, []).append(
                binding.assertion
            )
        for predicate, assertions in by_predicate.items():
            relation = self._build.relations.get(predicate)
            if not isinstance(relation, FrozenJsonMap):
                self._fail("Graph predicate is not registered")
            cardinality = relation.get("cardinality", "many_to_many")
            subjects: dict[str, str] = {}
            objects: dict[str, str] = {}
            for assertion in assertions:
                source = assertion.subject.entity_id
                target = assertion.object.entity_id
                if (
                    cardinality in ("one_to_one", "many_to_one")
                    and subjects.setdefault(source, target) != target
                ):
                    self._fail("Graph predicate subject cardinality differs")
                if (
                    cardinality in ("one_to_one", "one_to_many")
                    and objects.setdefault(target, source) != source
                ):
                    self._fail("Graph predicate object cardinality differs")

    def _upsert_units(
        self,
        gid: str,
        units: tuple[EvidenceUnit, ...],
        canonical: dict,
        ctx: ExecutionContext,
    ) -> None:
        operations = []
        for unit in units:
            ctx.checkpoint()
            pk = self._pk(gid, unit.unit_id)
            binding = canonical[unit.unit_id]
            content_hash = digest("graph-evidence-v1", binding.model_dump(mode="json"))
            row = self._one(
                "MATCH (e:Evidence) WHERE e.pk=$pk RETURN e.content_hash,e.generation_id",
                {"pk": pk},
            )
            if row is not None:
                if row != [content_hash, gid]:
                    self._fail("Graph evidence retry differs")
                continue
            operations.append(
                (
                    "CREATE (:Evidence {pk:$pk,generation_id:$gid,evidence_id:$id,revision_id:$rid,extraction_id:$eid,locator_json:$loc,content_hash:$hash})",
                    {
                        "pk": pk,
                        "gid": gid,
                        "id": unit.unit_id,
                        "rid": binding.revision_id,
                        "eid": unit.extraction_id,
                        "loc": canonical_bytes(
                            [loc.model_dump(mode="json") for loc in unit.locators]
                        ).decode(),
                        "hash": content_hash,
                    },
                )
            )
        for start in range(0, len(operations), _MAX_NATIVE_OPERATIONS):
            ctx.checkpoint()
            self._transaction(operations[start : start + _MAX_NATIVE_OPERATIONS], ctx)

    def _upsert_assertions(
        self,
        gid: str,
        assertions: tuple[RelationshipAssertion, ...],
        canonical: dict,
        ctx: ExecutionContext,
    ) -> None:
        nodes: list[tuple[str, dict]] = []
        edges: list[tuple[str, dict]] = []
        entities: dict[str, tuple[str, str, str]] = {}
        pending_entities: set[str] = set()
        pending_grounding: set[str] = set()
        for assertion in assertions:
            ctx.checkpoint()
            for endpoint in (assertion.subject, assertion.object):
                identity = (endpoint.kind, endpoint.entity_id, endpoint.unit_id)
                previous = entities.setdefault(endpoint.entity_id, identity)
                if previous != identity:
                    self._fail("Graph entity has contradictory grounding")
                pk = self._pk(gid, endpoint.entity_id)
                content_hash = digest("graph-entity-v1", identity)
                row = self._one(
                    "MATCH (n:Entity) WHERE n.pk=$pk RETURN n.content_hash,n.generation_id",
                    {"pk": pk},
                )
                if row is not None and row != [content_hash, gid]:
                    self._fail("Graph entity retry differs")
                if row is None and endpoint.entity_id not in pending_entities:
                    pending_entities.add(endpoint.entity_id)
                    nodes.append(
                        (
                            "CREATE (:Entity {pk:$pk,generation_id:$gid,entity_id:$id,kind:$kind,display_name:$name,content_hash:$hash})",
                            {
                                "pk": pk,
                                "gid": gid,
                                "id": endpoint.entity_id,
                                "kind": endpoint.kind,
                                "name": endpoint.entity_id,
                                "hash": content_hash,
                            },
                        )
                    )
                grounded = self._rows(
                    "MATCH (n:Entity)-[:GroundedIn]->(e:Evidence) WHERE n.pk=$pk RETURN e.pk,e.generation_id",
                    {"pk": pk},
                )
                if grounded and grounded != [[self._pk(gid, endpoint.unit_id), gid]]:
                    self._fail("Graph entity grounding retry differs")
                if not grounded and endpoint.entity_id not in pending_grounding:
                    pending_grounding.add(endpoint.entity_id)
                    edges.append(
                        (
                            "MATCH (n:Entity),(e:Evidence) WHERE n.pk=$n AND e.pk=$e CREATE (n)-[:GroundedIn]->(e)",
                            {"n": pk, "e": self._pk(gid, endpoint.unit_id)},
                        )
                    )
            binding = canonical[assertion.assertion_id]
            pk = self._pk(gid, assertion.assertion_id)
            content_hash = digest("graph-assertion-v1", binding.model_dump(mode="json"))
            row = self._one(
                "MATCH (a:RelationAssertion) WHERE a.pk=$pk RETURN a.content_hash,a.generation_id",
                {"pk": pk},
            )
            if row is not None:
                if row != [content_hash, gid]:
                    self._fail("Graph assertion retry differs")
            else:
                nodes.append(
                    (
                        "CREATE (:RelationAssertion {pk:$pk,generation_id:$gid,assertion_id:$id,predicate:$pred,status:$status,confidence:$confidence,method:$method,origin_evidence_id:$origin,required_support_ids:$supports,artifact_hash:$artifact,artifact_record_id:$record,content_hash:$hash})",
                        {
                            "pk": pk,
                            "gid": gid,
                            "id": assertion.assertion_id,
                            "pred": assertion.predicate,
                            "status": binding.status_at_watermark or assertion.status,
                            "confidence": assertion.confidence,
                            "method": assertion.method,
                            "origin": assertion.origin_unit_id,
                            "supports": sorted(assertion.required_support_ids),
                            "artifact": binding.artifact.sha256,
                            "record": binding.record_id,
                            "hash": content_hash,
                        },
                    )
                )
            for table, endpoint in (
                ("Subject", assertion.subject),
                ("Object", assertion.object),
            ):
                actual = self._rows(
                    f"MATCH (a:RelationAssertion)-[:{table}]->(n:Entity) WHERE a.pk=$pk RETURN n.pk,n.generation_id",
                    {"pk": pk},
                )
                if actual and actual != [[self._pk(gid, endpoint.entity_id), gid]]:
                    self._fail("Graph assertion endpoint retry differs")
                if not actual:
                    edges.append(
                        (
                            f"MATCH (a:RelationAssertion),(n:Entity) WHERE a.pk=$a AND n.pk=$n CREATE (a)-[:{table}]->(n)",
                            {"a": pk, "n": self._pk(gid, endpoint.entity_id)},
                        )
                    )
            present = self._rows(
                "MATCH (a:RelationAssertion)-[:SupportedBy]->(e:Evidence) WHERE a.pk=$pk RETURN e.evidence_id,e.pk,e.generation_id",
                {"pk": pk},
            )
            seen_supports: set[str] = set()
            for evidence_id, evidence_pk, evidence_generation in present:
                if (
                    evidence_id not in assertion.required_support_ids
                    or evidence_id in seen_supports
                    or evidence_pk != self._pk(gid, evidence_id)
                    or evidence_generation != gid
                ):
                    self._fail("Graph support retry differs")
                seen_supports.add(evidence_id)
            for support in sorted(assertion.required_support_ids):
                ctx.checkpoint()
                if support not in seen_supports:
                    edges.append(
                        (
                            "MATCH (a:RelationAssertion),(e:Evidence) WHERE a.pk=$a AND e.pk=$e CREATE (a)-[:SupportedBy]->(e)",
                            {"a": pk, "e": self._pk(gid, support)},
                        )
                    )
        # Native relationship writes follow node commits in bounded transactions.
        for start in range(0, len(nodes), _MAX_NATIVE_OPERATIONS):
            ctx.checkpoint()
            self._transaction(nodes[start : start + _MAX_NATIVE_OPERATIONS], ctx)
        for start in range(0, len(edges), _MAX_NATIVE_OPERATIONS):
            ctx.checkpoint()
            self._transaction(edges[start : start + _MAX_NATIVE_OPERATIONS], ctx)

    def seal_generation(
        self, generation_id: str, expected: GraphManifest, ctx: ExecutionContext
    ) -> GraphBatchReceipt:
        ctx.checkpoint()
        with self._lock:
            state = self._read_state(generation_id)
            if state is None:
                raise TychoraError("NOT_FOUND", "Graph generation is unavailable")
            if state.get("dropping", False):
                self._fail("Graph generation removal is in progress")
            build = self._build
            if build is None or build.spec.generation_id != generation_id:
                self._fail("Graph canonical build input is unavailable")
            if expected != GraphManifest.from_build(build):
                self._fail("Graph manifest differs from complete canonical membership")
            for table, field, ids in (
                ("Evidence", "evidence_id", expected.evidence_ids),
                ("Entity", "entity_id", expected.entity_ids),
                ("RelationAssertion", "assertion_id", expected.assertion_ids),
            ):
                rows = self._rows(
                    f"MATCH (n:{table}) WHERE n.generation_id=$gid RETURN n.{field} ORDER BY n.{field}",
                    {"gid": generation_id},
                )
                if tuple(row[0] for row in rows) != ids:
                    self._fail("Graph generation node membership is incomplete")
            for binding in build.units:
                ctx.checkpoint()
                unit = binding.unit
                row = self._one(
                    "MATCH (e:Evidence) WHERE e.pk=$pk RETURN e.generation_id,e.revision_id,e.extraction_id,e.locator_json,e.content_hash",
                    {"pk": self._pk(generation_id, unit.unit_id)},
                )
                expected_row = [
                    generation_id,
                    binding.revision_id,
                    unit.extraction_id,
                    canonical_bytes(
                        [loc.model_dump(mode="json") for loc in unit.locators]
                    ).decode(),
                    digest("graph-evidence-v1", binding.model_dump(mode="json")),
                ]
                if row != expected_row:
                    self._fail("Graph evidence content differs")
            for binding in build.assertions:
                ctx.checkpoint()
                assertion = binding.assertion
                pk = self._pk(generation_id, assertion.assertion_id)
                row = self._one(
                    "MATCH (a:RelationAssertion) WHERE a.pk=$pk RETURN a.generation_id,a.predicate,a.status,a.confidence,a.method,a.origin_evidence_id,a.required_support_ids,a.artifact_hash,a.artifact_record_id,a.content_hash",
                    {"pk": pk},
                )
                if row != [
                    generation_id,
                    assertion.predicate,
                    binding.status_at_watermark or assertion.status,
                    assertion.confidence,
                    assertion.method,
                    assertion.origin_unit_id,
                    sorted(assertion.required_support_ids),
                    binding.artifact.sha256,
                    binding.record_id,
                    digest("graph-assertion-v1", binding.model_dump(mode="json")),
                ]:
                    self._fail("Graph assertion content or provenance differs")
                for table, endpoint in (
                    ("Subject", assertion.subject),
                    ("Object", assertion.object),
                ):
                    rows = self._rows(
                        f"MATCH (a:RelationAssertion)-[:{table}]->(n:Entity) WHERE a.pk=$pk RETURN n.pk,n.generation_id",
                        {"pk": pk},
                    )
                    if rows != [
                        [self._pk(generation_id, endpoint.entity_id), generation_id]
                    ]:
                        self._fail("Graph assertion endpoint differs")
                rows = self._rows(
                    "MATCH (a:RelationAssertion)-[:SupportedBy]->(e:Evidence) WHERE a.pk=$pk RETURN e.pk,e.generation_id ORDER BY e.pk",
                    {"pk": pk},
                )
                expected_supports = sorted(
                    (
                        [self._pk(generation_id, support), generation_id]
                        for support in assertion.required_support_ids
                    ),
                    key=lambda value: value[0],
                )
                if rows != expected_supports:
                    self._fail("Graph support edges differ from required list")
            for endpoint in {
                endpoint.entity_id: endpoint
                for binding in build.assertions
                for endpoint in (binding.assertion.subject, binding.assertion.object)
            }.values():
                row = self._one(
                    "MATCH (n:Entity) WHERE n.pk=$pk RETURN n.generation_id,n.kind,n.display_name,n.content_hash",
                    {"pk": self._pk(generation_id, endpoint.entity_id)},
                )
                if row != [
                    generation_id,
                    endpoint.kind,
                    endpoint.entity_id,
                    digest(
                        "graph-entity-v1",
                        (endpoint.kind, endpoint.entity_id, endpoint.unit_id),
                    ),
                ]:
                    self._fail("Graph entity content differs")
                rows = self._rows(
                    "MATCH (n:Entity)-[:GroundedIn]->(e:Evidence) WHERE n.pk=$pk RETURN e.pk,e.generation_id",
                    {"pk": self._pk(generation_id, endpoint.entity_id)},
                )
                if rows != [[self._pk(generation_id, endpoint.unit_id), generation_id]]:
                    self._fail("Graph entity grounding differs")
            receipt = GraphBatchReceipt(
                generation_id=generation_id,
                family_id=expected.spec.family_id,
                config_sha=expected.spec.config_sha,
                decision_watermark=expected.spec.decision_watermark,
                manifest_digest=digest(
                    "graph-manifest-v1", expected.model_dump(mode="json")
                ),
                entity_count=len(expected.entity_ids),
                evidence_count=len(expected.evidence_ids),
                assertion_count=len(expected.assertion_ids),
            )
            if state["receipt"] is not None:
                if GraphBatchReceipt.from_mapping(state["receipt"]) != receipt:
                    self._fail("Sealed graph receipt differs from native generation")
                return receipt
            self._write_state(
                generation_id,
                {"spec": state["spec"], "receipt": receipt.model_dump(mode="json")},
            )
            return receipt

    def drop_generation(self, generation_id: str, ctx: ExecutionContext) -> None:
        with self._lock:
            state = self._read_state(generation_id)
            if state is None:
                return
            ctx.checkpoint()
            if not state.get("dropping", False):
                self._write_state(generation_id, {**state, "dropping": True})
            for table in ("RelationAssertion", "Entity", "Evidence"):
                while True:
                    ctx.checkpoint()
                    pks = self._rows(
                        f"MATCH (n:{table}) WHERE n.generation_id=$gid "
                        f"RETURN n.pk ORDER BY n.pk LIMIT {_MAX_NATIVE_OPERATIONS}",
                        {"gid": generation_id},
                    )
                    if not pks:
                        break
                    self._transaction(
                        [
                            (
                                f"MATCH (n:{table}) WHERE n.pk=$pk AND n.generation_id=$gid DETACH DELETE n",
                                {"pk": row[0], "gid": generation_id},
                            )
                            for row in pks
                        ],
                        ctx,
                    )
            self._state_path(generation_id).unlink(missing_ok=True)
            directory = os.open(self._state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
