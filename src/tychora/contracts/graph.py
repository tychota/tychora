"""Immutable graph generation membership and receipt contracts."""

from typing import Literal, Self

from pydantic import model_validator

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
            content_digest=digest(
                "graph-content-v1",
                sorted(
                    (item.model_dump(mode="json") for item in build.units),
                    key=lambda value: value["unit"]["unit_id"],
                ),
                sorted(
                    (item.model_dump(mode="json") for item in build.assertions),
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
