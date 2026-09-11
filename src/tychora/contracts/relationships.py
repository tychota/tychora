"""Canonical relationship observations and existing assertion JSONL framing."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .artifacts import ArtifactRef, ManifestRef
from .base import CodepointRange, Contract, Identifier, NonEmptyStr, Sha256
from .evidence import Locator
from .identity import canonical_bytes, digest


class RelationshipEndpoint(Contract):
    entity_id: Identifier
    unit_id: Identifier
    kind: NonEmptyStr


class SupportSpan(CodepointRange):
    unit_id: Identifier


class AssertionObservation(Contract):
    observation_key: NonEmptyStr
    revision_id: Identifier
    extraction_id: Identifier
    predicate: Annotated[
        str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*@[A-Za-z0-9][A-Za-z0-9_.-]*$")
    ]
    subject: RelationshipEndpoint
    object: RelationshipEndpoint
    origin_unit_id: Identifier
    origin_locator: Locator
    required_support_ids: Annotated[frozenset[Identifier], Field(min_length=1)]
    support_spans: tuple[SupportSpan, ...] = ()
    target_spans: tuple[SupportSpan, ...] = ()
    method: Literal["native", "parser_inferred", "human", "model"]
    status: Literal["recorded", "proposed", "confirmed", "rejected"]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    derivation_fingerprint: Sha256
    required_artifacts: tuple[ArtifactRef, ...] = ()
    required_manifests: tuple[ManifestRef, ...] = ()

    @model_validator(mode="after")
    def complete_grounding(self) -> Self:
        if (
            not {self.origin_unit_id, self.subject.unit_id, self.object.unit_id}
            <= self.required_support_ids
        ):
            raise ValueError(
                "Assertion must require its origin and both endpoint supports"
            )
        if any(
            span.unit_id not in self.required_support_ids for span in self.support_spans
        ):
            raise ValueError("Assertion span is not a declared source support")
        if any(
            span.unit_id not in {self.subject.unit_id, self.object.unit_id}
            for span in self.target_spans
        ):
            raise ValueError("Assertion target span is not an endpoint")
        if any(ref.storage_key != ref.sha256 for ref in self.required_artifacts):
            raise ValueError("Assertion dependencies require immutable content keys")
        return self


def _observation_data(value: AssertionObservation) -> dict:
    data = value.model_dump(mode="json", exclude={"assertion_id"})
    data["required_support_ids"] = sorted(value.required_support_ids)
    return data


class RelationshipAssertion(AssertionObservation):
    assertion_id: Sha256

    @classmethod
    def from_observation(cls, observation: AssertionObservation) -> Self:
        data = _observation_data(observation)
        return cls.model_validate_json(
            canonical_bytes(
                {
                    **data,
                    "assertion_id": digest("relationship-assertion-v1", data),
                }
            )
        )

    @model_validator(mode="after")
    def canonical_identity(self) -> Self:
        if self.assertion_id != digest(
            "relationship-assertion-v1", _observation_data(self)
        ):
            raise ValueError("Assertion identity differs from its complete observation")
        return self


def encode_assertions(assertions: tuple[RelationshipAssertion, ...]) -> bytes:
    """Each observation is one typed line; no independent header line."""
    if not assertions or len(assertions) > 10_000:
        raise ValueError(
            "Assertion artifact requires a bounded nonempty observation set"
        )
    lines = []
    seen = set()
    size = references = 0
    for assertion in assertions:
        assertion = RelationshipAssertion.model_validate(assertion.model_dump())
        if assertion.assertion_id in seen:
            raise ValueError("Duplicate assertion observation identity")
        seen.add(assertion.assertion_id)
        data = _observation_data(assertion)
        line = (
            canonical_bytes(
                {
                    "type": "assertion",
                    "schema_version": "1",
                    "record_id": assertion.assertion_id,
                    "assertion_id": assertion.assertion_id,
                    "required_artifacts": data["required_artifacts"],
                    "required_manifests": data["required_manifests"],
                    "data": {**data, "assertion_id": assertion.assertion_id},
                }
            )
            + b"\n"
        )
        size += len(line)
        references += len(assertion.required_artifacts) + len(
            assertion.required_manifests
        )
        if len(line) > 1024 * 1024 or size > 16 * 1024 * 1024 or references > 10_000:
            raise ValueError("Assertion artifact exceeds canonical framing capacity")
        lines.append(line)
    return b"".join(lines)
