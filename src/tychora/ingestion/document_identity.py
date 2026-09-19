"""Validated pre-ID composition context; raw observations remain immutable."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from tychora.configuration.registry import Registry
from tychora.contracts.composition import DocumentCompositionPlan
from tychora.contracts.documents import DocumentRegistration
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import digest


@dataclass(frozen=True)
class DocumentNormalizationContext:
    plan: DocumentCompositionPlan
    raw_manifest_sha: str
    source_fingerprint: str
    normalizer_fingerprint: str

    @classmethod
    def bind(
        cls,
        parsed: ExtractionArtifact,
        registry: Registry,
        plan: DocumentCompositionPlan,
    ):
        from .document_records import document_normalizer_fingerprint

        parsed = ExtractionArtifact.model_validate(parsed.model_dump())
        plan = DocumentCompositionPlan.model_validate(plan.model_dump())
        if not isinstance(parsed.registration, DocumentRegistration):
            raise TypeError(
                "Document composition requires retained rich-document facts"
            )
        kind = cast(
            Mapping[str, Any], registry.kinds[parsed.registration.revision.kind_id]
        )
        if (
            registry.registry_digest != parsed.registry_digest
            or parsed.normalizer_fingerprint
            != document_normalizer_fingerprint(kind, registry)
            or plan.base_normalizer_fingerprint != parsed.normalizer_fingerprint
        ):
            raise ValueError("Composition base normalizer or registry binding differs")
        registry.validate_metadata(
            parsed.registration.revision.kind_id,
            parsed.registration.revision.metadata_json,
        )
        # Capture times, source paths outside registration, and raw-manifest SHA
        # are observation links, not semantic inputs. These mapped facts retain
        # source text/structure/geometry and actual asset pixels without them.
        source_inputs = [
            [unit.model_dump(mode="json") for unit in parsed.units],
            [asset.model_dump(mode="json") for asset in parsed.assets],
        ]
        if parsed.original_image is not None:
            source_inputs.append(parsed.original_image.model_dump(mode="json"))
        source = digest("document-composition-source-v1", *source_inputs)
        fingerprint = digest(
            "document-composition-normalizer-v1", source, plan.semantic_data()
        )
        return cls(plan, parsed.raw_document_ref.sha256, source, fingerprint)

    def verify(self, parsed: ExtractionArtifact, registry: Registry) -> None:
        if self != self.bind(parsed, registry, self.plan):
            raise ValueError(
                "Composition context differs from verified original inputs"
            )
