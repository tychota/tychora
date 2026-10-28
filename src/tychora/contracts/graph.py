"""Graph generation, eligibility and bounded traversal contracts."""

from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import SerializerFunctionWrapHandler, model_serializer, model_validator

from .artifacts import ArtifactRef
from .base import (
    Contract,
    FrozenJsonMap,
    Identifier,
    NonEmptyStr,
    NonNegativeInt,
    Sha256,
)
from .evidence import EvidenceUnit
from .identity import digest
from .relationships import RelationshipAssertion


class GraphExtraction(Contract):
    extraction_id: Identifier
    revision_id: Identifier
    assertion_root: ArtifactRef | None = None
    unit_ids: tuple[Identifier, ...]


class GraphGenerationSpec(Contract):
    generation_id: Identifier
    family_id: Sha256
    config_sha: Sha256
    registry_digest: Sha256
    workspace_id: Identifier
    decision_watermark: NonNegativeInt
    memberships: tuple[GraphExtraction, ...] = ()
    projection_version: Literal["legacy-v1", "stable-v2"] = "legacy-v1"

    @model_serializer(mode="wrap")
    def retained_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Keep byte-for-byte v1 spec shape; mark only new stable projections."""
        value = handler(self)
        if self.projection_version == "legacy-v1":
            value.pop("projection_version", None)
        return value

    @model_validator(mode="after")
    def unique_membership(self) -> Self:
        ids = tuple(member.extraction_id for member in self.memberships)
        if len(ids) != len(set(ids)):
            raise ValueError("Graph extraction membership differs")
        units = [unit for member in self.memberships for unit in member.unit_ids]
        if len(units) != len(set(units)):
            raise ValueError("Graph unit membership is duplicated")
        return self


class GraphUnitBinding(Contract):
    unit: EvidenceUnit
    revision_id: Identifier


class GraphAssertionBinding(Contract):
    assertion: RelationshipAssertion
    artifact: ArtifactRef
    record_id: Identifier
    status_at_watermark: (
        Literal["recorded", "proposed", "confirmed", "rejected"] | None
    ) = None
    decision_id: NonNegativeInt | None = None
    decision_actor_id: NonEmptyStr | None = None

    @model_validator(mode="after")
    def decision_binding(self) -> Self:
        if (self.decision_id is None) != (self.decision_actor_id is None):
            raise ValueError("Graph decision identity and actor must be paired")
        if self.decision_id is not None and self.decision_id < 1:
            raise ValueError("Graph decision identity must be positive")
        return self


def graph_assertion_binding_json(
    binding: GraphAssertionBinding, spec: GraphGenerationSpec
) -> dict:
    """Preserve legacy proof input or project v2 support IDs consistently."""
    projected = binding.model_dump(mode="json")
    if spec.projection_version == "stable-v2":
        projected["assertion"]["required_support_ids"] = sorted(
            binding.assertion.required_support_ids
        )
    return projected


def graph_proof_digest(base: str, spec: GraphGenerationSpec, *parts) -> str:
    suffix = "v2" if spec.projection_version == "stable-v2" else "v1"
    return digest(f"{base}-{suffix}", *parts)


def graph_manifest_digest(manifest: "GraphManifest") -> str:
    return graph_proof_digest(
        "graph-manifest", manifest.spec, manifest.model_dump(mode="json")
    )


class GraphBuildInput(Contract):
    spec: GraphGenerationSpec
    units: tuple[GraphUnitBinding, ...]
    assertions: tuple[GraphAssertionBinding, ...]
    relations: FrozenJsonMap

    @model_validator(mode="after")
    def exact_membership(self) -> Self:
        members = {item.extraction_id: item for item in self.spec.memberships}
        expected_units = {uid for item in members.values() for uid in item.unit_ids}
        actual_units = {binding.unit.unit_id for binding in self.units}
        if len(actual_units) != len(self.units) or actual_units != expected_units:
            raise ValueError("Graph build unit membership differs")
        for binding in self.units:
            member = members.get(binding.unit.extraction_id)
            if member is None or member.revision_id != binding.revision_id:
                raise ValueError("Graph unit revision binding differs")
        if len({item.assertion.assertion_id for item in self.assertions}) != len(
            self.assertions
        ):
            raise ValueError("Duplicate graph assertion")
        for binding in self.assertions:
            member = members.get(binding.assertion.extraction_id)
            if (
                member is None
                or member.revision_id != binding.assertion.revision_id
                or member.assertion_root != binding.artifact
                or binding.record_id != binding.assertion.assertion_id
            ):
                raise ValueError("Graph assertion artifact binding differs")
        return self


class GraphManifest(Contract):
    spec: GraphGenerationSpec
    entity_ids: tuple[Identifier, ...]
    evidence_ids: tuple[Identifier, ...]
    assertion_ids: tuple[Sha256, ...]
    content_digest: Sha256

    @classmethod
    def from_build(cls, build: GraphBuildInput) -> Self:
        entities = {
            endpoint.entity_id
            for item in build.assertions
            for endpoint in (item.assertion.subject, item.assertion.object)
        }
        return cls(
            spec=build.spec,
            entity_ids=tuple(sorted(entities)),
            evidence_ids=tuple(sorted(item.unit.unit_id for item in build.units)),
            assertion_ids=tuple(
                sorted(item.assertion.assertion_id for item in build.assertions)
            ),
            content_digest=graph_proof_digest(
                "graph-content",
                build.spec,
                sorted(
                    (item.model_dump(mode="json") for item in build.units),
                    key=lambda value: value["unit"]["unit_id"],
                ),
                sorted(
                    (
                        graph_assertion_binding_json(item, build.spec)
                        for item in build.assertions
                    ),
                    key=lambda value: value["assertion"]["assertion_id"],
                ),
            ),
        )


class GraphBatchReceipt(Contract):
    generation_id: Identifier
    family_id: Sha256
    config_sha: Sha256
    decision_watermark: NonNegativeInt
    manifest_digest: Sha256
    entity_count: NonNegativeInt
    evidence_count: NonNegativeInt
    assertion_count: NonNegativeInt


class GraphScope(Contract):
    """The units, batches and blocked assertions one query's traversal may read."""

    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    scope_digest: Sha256
    eligibility_policy_digest: Sha256
    eligibility_request_digest: Sha256
    receipt: GraphBatchReceipt
    revision_ids: frozenset[Identifier]
    selected_batch_ids: tuple[Identifier, ...]
    source_unit_ids: frozenset[Identifier]
    context_unit_ids: frozenset[Identifier]
    structural_unit_ids: frozenset[Identifier]
    blocked_assertion_ids: frozenset[Sha256]

    @property
    def graph_generation_id(self) -> str:
        """The pinned graph generation."""
        return self.receipt.generation_id

    @property
    def graph_family_id(self) -> str:
        """The pinned generation's index family."""
        return self.receipt.family_id

    @property
    def graph_config_sha(self) -> str:
        """The pinned generation's configuration digest."""
        return self.receipt.config_sha

    @property
    def graph_decision_watermark(self) -> int:
        """The last assertion decision the pinned generation saw."""
        return self.receipt.decision_watermark

    @property
    def graph_manifest_digest(self) -> str:
        """The pinned generation's sealed manifest digest."""
        return self.receipt.manifest_digest

    @property
    def graph_eligible_unit_ids(self) -> frozenset[str]:
        """Every unit traversal may reach: context and structural."""
        return self.context_unit_ids | self.structural_unit_ids


@dataclass
class GraphBudget:
    """Shared, request-local mutable graph accounting."""

    max_seeds: int = 5
    max_new_units: int = 32
    max_rows: int = 2000
    max_inspection_edges: int = 100
    max_inspection_hops: int = 2
    inspected_rows: int = 0
    new_unit_ids: set[str] = field(default_factory=set)
    inspected_edges: int = 0
    seed_unit_ids: set[str] = field(default_factory=set)
    completed_strategies: set[Literal["semantic", "structural"]] = field(
        default_factory=set
    )
    strategy_counts: dict[Literal["semantic", "structural"], int] = field(
        default_factory=dict
    )
    strategy_unit_ids: dict[Literal["semantic", "structural"], set[str]] = field(
        default_factory=dict
    )
    view_digest: str | None = None

    def __post_init__(self) -> None:
        for name, cap in (
            ("max_seeds", 5),
            ("max_new_units", 32),
            ("max_rows", 2000),
            ("max_inspection_edges", 100),
            ("max_inspection_hops", 2),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= cap:
                raise ValueError(f"{name} exceeds the graph request bound")
        if (
            self.inspected_rows < 0
            or self.inspected_rows > self.max_rows
            or self.inspected_edges < 0
            or self.inspected_edges > self.max_inspection_edges
            or len(self.new_unit_ids) > self.max_new_units
            or len(self.seed_unit_ids) > self.max_seeds
        ):
            raise ValueError("Graph work counters exceed the request bound")


class GraphPredicatePolicy(Contract):
    predicate: Identifier
    view: Literal["semantic", "structural"] = "semantic"
    directions: tuple[Literal["outgoing", "incoming"], ...]
    statuses: tuple[Literal["recorded", "proposed", "confirmed", "rejected"], ...] = (
        "recorded",
        "confirmed",
    )


class GraphTraversalPolicy(Contract):
    predicates: tuple[GraphPredicatePolicy, ...]
    inspection_predicates: tuple[GraphPredicatePolicy, ...] = ()
    structural: Literal["none", "code_context@1"] = "none"
    semantic: bool = False
    table_continuations: Literal["derived", "confirmed_only", "none"] = "derived"

    @model_validator(mode="after")
    def normal_search_stays_recorded_or_confirmed(self) -> Self:
        if any(
            set(item.statuses) - {"recorded", "confirmed"} for item in self.predicates
        ):
            raise ValueError(
                "Normal graph traversal cannot use proposed or rejected assertions"
            )
        if any(
            not item.directions or not item.statuses
            for item in (*self.predicates, *self.inspection_predicates)
        ):
            raise ValueError("Graph predicate requires direction and status")
        return self


class GraphPath(Contract):
    assertion_ids: tuple[Sha256, ...]
    required_support_ids: frozenset[Identifier]
    predicate: Identifier
    status: Literal["recorded", "proposed", "confirmed", "rejected"]
    confidence: float
    source_unit_id: Identifier
    target_unit_id: Identifier
    assertion_occurrences: int = 1


class GraphCandidate(Contract):
    unit_id: Identifier
    paths: tuple[GraphPath, ...]
    omitted_path_count: NonNegativeInt = 0
    reasons: tuple[NonEmptyStr, ...]
    strategy: Literal["semantic", "structural"]
    source_order_distance: NonNegativeInt | None = None


class GraphCandidates(Contract):
    candidates: tuple[GraphCandidate, ...] = ()
    enrichments: tuple[GraphCandidate, ...] = ()
    inspected_rows: NonNegativeInt = 0
    truncation: Literal["candidate_limit", "edge_work_limit", "deadline"] | None = None
    exhausted: bool = True


class AssertionPage(Contract):
    assertions: tuple[GraphPath, ...] = ()
    inspected_edges: NonNegativeInt = 0
    next_offset: NonNegativeInt | None = None
    truncation: Literal["edge_limit", "edge_work_limit", "deadline"] | None = None
    exhausted: bool = True
