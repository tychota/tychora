"""Sealed synthetic native graph fixtures for traversal qualification."""

import io
from dataclasses import dataclass, field
from typing import Literal

from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.ladybug.generation import LadybugGraphStore
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import EvidenceUnit, TextLocator
from tychora.contracts.graph import (
    EligibilityView,
    GraphAssertionBinding,
    GraphBudget,
    GraphBuildInput,
    GraphExtraction,
    GraphGenerationSpec,
    GraphManifest,
    GraphPredicatePolicy,
    GraphTraversalPolicy,
    GraphUnitBinding,
)
from tychora.contracts.identity import digest
from tychora.contracts.relationships import (
    AssertionObservation,
    RelationshipAssertion,
    RelationshipEndpoint,
    encode_assertions,
)
from tychora.ports.execution import ExecutionContext


def unit(uid: str, ordinal: int = 0, *, parent_id: str | None = None) -> EvidenceUnit:
    return EvidenceUnit(
        unit_id=uid,
        extraction_id="extraction",
        parent_id=parent_id,
        ordinal=ordinal,
        unit_type="symbol",
        native_ref=uid,
        boundary_key="file",
        text=uid,
        text_sha=digest("text", uid),
        locators=(
            TextLocator(artifact_ref="text", native_ref=uid, char_start=0, char_end=1),
        ),
    )


def assertion(
    subject: str,
    target: str,
    supports: set[str] | None = None,
    *,
    predicate: str = "references@1",
    key: str | None = None,
    status: Literal["recorded", "proposed", "confirmed", "rejected"] = "recorded",
    confidence: float = 1.0,
) -> RelationshipAssertion:
    return RelationshipAssertion.from_observation(
        AssertionObservation(
            observation_key=key or f"{subject}-{target}-{predicate}",
            revision_id="revision",
            extraction_id="extraction",
            predicate=predicate,
            subject=RelationshipEndpoint(
                entity_id=subject, unit_id=subject, kind="symbol@1"
            ),
            object=RelationshipEndpoint(
                entity_id=target, unit_id=target, kind="symbol@1"
            ),
            origin_unit_id=subject,
            origin_locator=unit(subject).locators[0],
            required_support_ids=frozenset(supports or {subject, target}),
            method="native",
            status=status,
            confidence=confidence,
            derivation_fingerprint=digest("fixture"),
        )
    )


@dataclass
class GraphCase:
    graph: LadybugGraphStore
    spec: GraphGenerationSpec
    receipt_digest: str
    assertions: tuple[RelationshipAssertion, ...]
    ctx: ExecutionContext = field(default_factory=ExecutionContext)

    def budget(self, **kwargs) -> GraphBudget:
        return GraphBudget(**kwargs)

    def view(
        self,
        *,
        context: set[str],
        candidates: set[str],
        structural: set[str] | None = None,
        blocked: set[str] | None = None,
    ) -> EligibilityView:
        return EligibilityView(
            snapshot_id="snapshot",
            policy_epoch=0,
            scope_digest=digest("scope"),
            eligibility_policy_digest=digest("policy"),
            eligibility_request_digest=digest("request"),
            selected_batch_ids=(self.spec.generation_id,),
            revision_ids=frozenset({"revision"}),
            source_unit_ids=frozenset(candidates),
            context_unit_ids=frozenset(context),
            structural_unit_ids=frozenset(structural or set()),
            proxy_unit_ids=frozenset(),
            chunk_ids=frozenset(),
            asset_ids=frozenset(),
            vector_record_ids=frozenset(),
            blocked_assertion_ids=frozenset(blocked or set()),
            graph_generation_id=self.spec.generation_id,
            graph_family_id=self.spec.family_id,
            graph_config_sha=self.spec.config_sha,
            graph_decision_watermark=self.spec.decision_watermark,
            graph_manifest_digest=self.receipt_digest,
        )


def generation_spec(root, unit_ids: tuple[str, ...]) -> GraphGenerationSpec:
    """The single-extraction generation spec the graph tests build over one assertion root."""
    return GraphGenerationSpec(
        generation_id="generation",
        family_id=digest("family"),
        config_sha=digest("config"),
        registry_digest=digest("registry"),
        workspace_id="workspace",
        decision_watermark=0,
        memberships=(
            GraphExtraction(
                extraction_id="extraction",
                revision_id="revision",
                assertion_root=root,
                unit_ids=unit_ids,
            ),
        ),
    )


def sealed_graph(
    tmp_path,
    units: tuple[EvidenceUnit, ...],
    assertions: tuple[RelationshipAssertion, ...],
    policy: GraphTraversalPolicy | None = None,
) -> GraphCase:
    root = LocalArtifacts(tmp_path / "artifacts").put_stream(
        io.BytesIO(encode_assertions(assertions)), "application/x-ndjson"
    )
    spec = generation_spec(root, tuple(u.unit_id for u in units))
    predicates = {a.predicate for a in assertions}
    build = GraphBuildInput(
        spec=spec,
        units=tuple(GraphUnitBinding(unit=u, revision_id="revision") for u in units),
        assertions=tuple(
            GraphAssertionBinding(assertion=a, artifact=root, record_id=a.assertion_id)
            for a in assertions
        ),
        relations=FrozenJsonMap(
            {
                predicate: {
                    "subject_unit_types": ["symbol"],
                    "object_unit_types": ["symbol"],
                    "subject_kinds": ["symbol@1"],
                    "object_kinds": ["symbol@1"],
                    "methods": ["native"],
                    "revision_binding": "same_revision",
                    "cardinality": "many_to_many",
                    "self_links": False,
                }
                for predicate in predicates
            }
        ),
    )
    if policy is None:
        policy = GraphTraversalPolicy(
            predicates=tuple(
                GraphPredicatePolicy(predicate=p, directions=("outgoing", "incoming"))
                for p in sorted(predicates)
            ),
            semantic=True,
        )
    graph = LadybugGraphStore(tmp_path / "graph", build=build, traversal_policy=policy)
    graph.begin_generation(spec)
    graph.upsert_batch(spec.generation_id, assertions, units, ExecutionContext())
    receipt = graph.seal_generation(
        spec.generation_id, GraphManifest.from_build(build), ExecutionContext()
    )
    return GraphCase(graph, spec, receipt.manifest_digest, assertions)
