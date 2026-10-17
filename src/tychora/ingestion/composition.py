"""One-shot immutable document composition and bounded deterministic replay."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from itertools import pairwise
from typing import Any, cast

from tychora.configuration.registry import Registry
from tychora.contracts.artifacts import ArtifactRef, ManifestRef, physical_identity
from tychora.contracts.assets import ProxyRecord
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.composition import (
    DocumentCompositionPlan,
    PlannedProxy,
    PlannedRegion,
    SourceSelector,
    VisionCapabilityDiagnostics,
    source_context_identity,
)
from tychora.contracts.corpus import ExtractionBundle, LocatorRow
from tychora.contracts.documents import DocumentUnit
from tychora.contracts.extraction import ExtractionArtifact
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.relationships import RelationshipAssertion, encode_assertions
from tychora.contracts.tables import TableFragment
from tychora.ports.artifacts import Artifacts
from tychora.ports.execution import ExecutionContext

from .assets import ProxyFactory, validate_image
from .document_identity import DocumentNormalizationContext
from .document_parts import _Budget
from .document_records import normalize_document_records
from .document_views import source_views
from .documents import normalize_standalone_image
from .tables import continuation, continuation_assertion, validate_assertion
from .vision import _materialize_pixel_source_view

MAX_COMPOSED_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_COMPOSITION_INPUT_BYTES = 256 * 1024 * 1024
MAX_COMPOSITION_GROUP_BYTES = 256 * 1024 * 1024
# Required input bytes plus bounded generated products; unrelated raw closure
# payloads are already validated by raw replay and are not consumed here.
MAX_COMPOSITION_REPLAY_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class ComposedDocument:
    bundle: ExtractionBundle
    fragments: tuple[TableFragment, ...]
    assertions: tuple[RelationshipAssertion, ...]
    manifest_ref: ArtifactRef
    payloads: Mapping[str, bytes]


def _ref(payload: bytes, media_type: str = "application/json") -> ArtifactRef:
    key = sha256(payload).hexdigest()
    return ArtifactRef(
        sha256=key, size_bytes=len(payload), storage_key=key, media_type=media_type
    )


def _checked(ref: ArtifactRef, inputs: Mapping[str, bytes]) -> bytes:
    value = inputs.get(ref.sha256)
    if (
        value is None
        or len(value) != ref.size_bytes
        or sha256(value).hexdigest() != ref.sha256
        or ref.storage_key != ref.sha256
    ):
        raise ValueError("Composition input payload differs from retained reference")
    return value


def _selected(
    selector: SourceSelector, parsed: ExtractionArtifact, bundle: ExtractionBundle
) -> tuple[str, ...]:
    item = next(
        (
            item
            for item in cast(tuple[DocumentUnit, ...], parsed.units)
            if item.native_ref == selector.native_ref
        ),
        None,
    )
    if item is None:
        raise ValueError("Composition selector has no original SDK item")
    if selector.field is not None:
        if selector.field == "table_cell":
            cells = cast(Mapping[str, Any], item.raw_item.get("data", {})).get(
                "table_cells", ()
            )
            if selector.cell_index is None or selector.cell_index >= len(cells):
                raise ValueError("Composition selector has no original cell")
            value = cells[selector.cell_index]["text"]
        else:
            value = item.raw_item.get(selector.field)
        if (
            not isinstance(value, str)
            or selector.source_end is None
            or selector.source_end > len(value)
            or selector.source_start == selector.source_end
        ):
            raise ValueError(
                "Composition selector requires a nonempty original field interval"
            )
    selected = []
    for unit in bundle.units:
        if unit.text_origin != "source" or not unit.citable:
            continue
        if selector.field is None:
            match = selector.native_ref in cast(
                tuple[str, ...], unit.attributes.get("source_refs", ())
            )
        else:
            match = any(
                part["native_ref"] == selector.native_ref
                and part["field"] == selector.field
                and part["cell_index"] == selector.cell_index
                and part["source_start"] < selector.source_end
                and part["source_end"] > selector.source_start
                for part in cast(
                    tuple[Mapping[str, Any], ...],
                    unit.attributes.get("source_parts", ()),
                )
            )
        if match:
            selected.append(unit.unit_id)
    if not selected:
        raise ValueError("Composition selector has no canonical original owner")
    return tuple(selected)


class _SelectorResolver:
    """Memoize validated selector owners for one composition and bundle version."""

    def __init__(self, parsed: ExtractionArtifact) -> None:
        self.parsed = parsed
        self._bundle: ExtractionBundle | None = None
        self._cache: dict[SourceSelector, tuple[str, ...]] = {}
        self._retained_ids = 0

    def selected(
        self, selector: SourceSelector, bundle: ExtractionBundle
    ) -> tuple[str, ...]:
        if self._bundle is not bundle:
            self._bundle = bundle
            self._cache.clear()
            self._retained_ids = 0
        cached = self._cache.get(selector)
        if cached is None:
            cached = _selected(selector, self.parsed, bundle)
            if len(self._cache) < 256 and self._retained_ids + len(cached) <= 20_000:
                self._cache[selector] = cached
                self._retained_ids += len(cached)
        return cached


def _bundle_projection(bundle: ExtractionBundle) -> dict[str, Any]:
    data = bundle.model_dump(mode="json")
    for proxy in data["proxies"]:
        proxy["required_unit_ids"] = sorted(proxy["required_unit_ids"])
    return data


def _input_references(
    parsed: ExtractionArtifact, plan: DocumentCompositionPlan
) -> dict[str, ArtifactRef]:
    references = _declared_input_references(parsed, plan)
    if sum(ref.size_bytes for ref in references.values()) > MAX_COMPOSITION_INPUT_BYTES:
        if parsed.pdf_processing is not None:
            from tychora.contracts.errors import TychoraError

            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF composition inputs exceed capacity"
            )
        raise ValueError("Composition retained inputs exceed aggregate capacity")
    return references


def _unique_references(refs: Iterable[ArtifactRef]) -> dict[str, ArtifactRef]:
    references: dict[str, ArtifactRef] = {}
    for ref in refs:
        previous = references.get(ref.sha256)
        if previous is not None and physical_identity(previous) != physical_identity(
            ref
        ):
            raise ValueError("Conflicting composition input artifact declarations")
        references.setdefault(ref.sha256, ref)
    return references


def _declared_input_references(
    parsed: ExtractionArtifact, plan: DocumentCompositionPlan
) -> dict[str, ArtifactRef]:
    return _unique_references(
        (
            *(asset.artifact_ref for asset in parsed.assets),
            *(region.image_ref for region in plan.pixel_regions),
            *(proxy.derivation for proxy in plan.proxies),
            *(
                proxy.region.image_ref
                for proxy in plan.proxies
                if proxy.region is not None
            ),
            *(
                (parsed.original_ref,)
                if any(
                    proxy.region is not None
                    and proxy.region.original_image_sha256 == parsed.original_ref.sha256
                    for proxy in plan.proxies
                )
                else ()
            ),
        )
    )


def _admit_group(refs: Iterable[ArtifactRef]) -> dict[str, ArtifactRef]:
    from tychora.contracts.errors import TychoraError

    group = _unique_references(refs)
    if sum(ref.size_bytes for ref in group.values()) > MAX_COMPOSITION_GROUP_BYTES:
        raise TychoraError(
            "LIMIT_EXCEEDED", "PDF composition verification group exceeds capacity"
        )
    return group


class _ReplayBudget:
    """Unique composition inputs and generated products, before retention."""

    def __init__(self, refs: Iterable[ArtifactRef]):
        self.refs: dict[str, ArtifactRef] = {}
        self.size = 0
        for ref in refs:
            self.reserve(ref)

    def reserve(self, ref: ArtifactRef) -> None:
        from tychora.contracts.errors import TychoraError

        previous = self.refs.get(ref.sha256)
        if previous is not None:
            if physical_identity(previous) != physical_identity(ref):
                raise ValueError("Conflicting composed replay artifact declarations")
            return
        if self.size + ref.size_bytes > MAX_COMPOSITION_REPLAY_BYTES:
            raise TychoraError(
                "LIMIT_EXCEEDED",
                "PDF composition inputs and products exceed replay capacity",
            )
        self.refs[ref.sha256] = ref
        self.size += ref.size_bytes


def composition_input_references(
    parsed: ExtractionArtifact, plan: DocumentCompositionPlan
) -> dict[str, ArtifactRef]:
    """Admit exact descriptors without reading bytes; return the authorized inputs.

    Legacy plans retain the 256 MiB aggregate. pdf-streamed-images@1 separates
    raw image bindings from the 256 MiB nonraw aggregate and bounds each unique
    encoded verification group to 256 MiB. All inputs share the 512 MiB replay
    allowance with generated products. These are encoded byte bounds, not RSS.
    """
    plan = DocumentCompositionPlan.model_validate(plan.model_dump())
    if plan.input_strategy is None:
        return _input_references(parsed, plan)
    from tychora.application.pdf_processing import PDF_LIMITS, PdfRetentionBudget
    from tychora.contracts.errors import TychoraError

    # Recheck raw-root, SDK mapping, policy/parser identity, sidecars and bindings;
    # a direct caller cannot grant streaming admission with an unchecked flag.
    parsed = ExtractionArtifact.model_validate(parsed.model_dump())
    if (
        parsed.pdf_processing is None
        or parsed.image_representation != "scan-external-images@1"
    ):
        raise ValueError("Streamed composition requires an explicit external-image PDF")
    limits = cast(Mapping[str, Any], parsed.pdf_processing["limits"])
    if any(limits.get(key) != value for key, value in PDF_LIMITS.items()):
        raise ValueError("Streamed composition requires qualified PDF limits")
    PdfRetentionBudget.for_extraction(parsed)
    assets = {asset.native_ref: asset.artifact_ref for asset in parsed.assets}
    bindings = {
        binding.native_ref: binding.artifact_ref for binding in parsed.image_bindings
    }
    if bindings != assets or len(bindings) != len(parsed.image_bindings):
        raise ValueError("Streamed composition requires exact raw image bindings")
    evidence = {f"#/pages/{page.physical_page}": page for page in parsed.ocr_pages}
    if {region.source_native_ref for region in plan.pixel_regions} != evidence.keys():
        raise ValueError("PDF pixel-owner coverage differs from evidence")
    for region in plan.pixel_regions:
        page = evidence[region.source_native_ref]
        if (
            region.image_ref != page.model_input_image
            or region.region_pixel_sha256 != page.model_input_pixel_sha256
        ):
            raise ValueError("PDF pixel owner differs from exact OCR input")
    references = _declared_input_references(parsed, plan)
    raw = _unique_references(assets.values())
    derived = [ref for sha, ref in references.items() if sha not in raw]
    if sum(ref.size_bytes for ref in derived) > MAX_COMPOSITION_INPUT_BYTES:
        raise TychoraError(
            "LIMIT_EXCEEDED", "PDF composition nonraw inputs exceed capacity"
        )
    _ReplayBudget(references.values())
    for ref in references.values():
        _admit_group((ref,))
    for region in (
        *plan.pixel_regions,
        *(proxy.region for proxy in plan.proxies if proxy.region is not None),
    ):
        original = assets.get(region.source_native_ref)
        if original is None or original.sha256 != region.original_image_sha256:
            raise ValueError("Vision region has no exact retained source asset")
        _admit_group((original, region.image_ref))
    return references


def _verify_region_inputs(
    region: PlannedRegion, original_ref: ArtifactRef, inputs: Mapping[str, bytes]
) -> None:
    """Release this original/crop pair before reading the next asset or receipt."""
    from tychora.ports.vision import verify_region_source

    refs = _unique_references((original_ref, region.image_ref))
    group = {sha: _checked(ref, inputs) for sha, ref in refs.items()}
    verify_region_source(
        _region_input(region, group[region.image_ref.sha256]),
        group[original_ref.sha256],
        original_ref,
        region.source_native_ref,
    )


def _region_input(region: PlannedRegion, image: bytes):
    from tychora.ports.vision import RegionInput

    return RegionInput.model_validate(
        {
            **region.model_dump(
                exclude={
                    "model",
                    "prompt_schema_version",
                    "task_schema_sha256",
                    "prompt_renderer_identity",
                    "prompt_identity",
                }
            ),
            "image": image,
        }
    )


def _verify_region_derivation(proxy: PlannedProxy, payload: bytes) -> None:
    region = proxy.region
    assert region is not None
    try:
        root = json.loads(payload)
        data = root["data"]
        result = data["result"]
        expected = {
            "task": region.task,
            "source_native_ref": region.source_native_ref,
            "image_sha256": region.image_sha256,
            "original_image_sha256": region.original_image_sha256,
            "original_pixel_sha256": region.original_pixel_sha256,
            "region_pixel_sha256": region.region_pixel_sha256,
            "context_sha256": region.context_sha256,
            "source_context_sha256": source_context_identity(proxy.source_context),
            "dependency_ids": sorted(region.dependency_ids),
            "model": region.model,
            "prompt_schema_version": region.prompt_schema_version,
            "task_schema_sha256": region.task_schema_sha256,
            "prompt_identity": region.prompt_identity,
        }
        if region.prompt_renderer_identity is not None:
            expected["prompt_renderer_identity"] = region.prompt_renderer_identity
        if (
            canonical_bytes(root) != payload
            or root.get("type") != "vision_derivation"
            or root.get("schema_version") != "1"
            or not isinstance(data, dict)
            or not isinstance(result, dict)
            or any(data.get(key) != value for key, value in expected.items())
            or region.task == "ocr"
            and result.get("literal_transcription") != proxy.text
            or region.task != "ocr"
            and canonical_bytes(result.get("interpretation")).decode("utf-8")
            != proxy.text
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(
            "Vision derivation differs from the retained observation"
        ) from None


def _verified_region_context_ids(
    proxy: PlannedProxy,
    parsed: ExtractionArtifact,
    final: ExtractionBundle,
    resolver: _SelectorResolver,
) -> tuple[str, ...]:
    if not proxy.source_context:
        return ()
    from tychora.ports.vision import verify_retained_source_context

    verify_retained_source_context(parsed, proxy.source_context)
    return tuple(
        sorted(
            {
                unit_id
                for selected in proxy.source_context
                for unit_id in resolver.selected(selected.selector, final)
            }
        )
    )


def build_composed_document(
    parsed: ExtractionArtifact,
    registry: Registry,
    plan: DocumentCompositionPlan,
    inputs: Mapping[str, bytes],
    *,
    legacy_v1: bool = False,
) -> ComposedDocument:
    """Pure build from validated raw SDK facts and explicit retained derived inputs."""
    context = DocumentNormalizationContext.bind(
        parsed, registry, plan, legacy_v1=legacy_v1
    )
    plan = context.plan
    references = composition_input_references(parsed, plan)
    replay_budget = _ReplayBudget(references.values()) if plan.input_strategy else None
    for ref in references.values():
        _checked(ref, inputs)
    for asset in parsed.assets:
        validate_image(
            _checked(asset.artifact_ref, inputs),
            asset.artifact_ref.media_type,
            expected_size=(asset.width, asset.height),
        )
    for proxy in plan.proxies:
        _checked(proxy.derivation, inputs)
    kind = cast(Mapping[str, Any], registry.kinds[parsed.registration.revision.kind_id])
    if parsed.original_image is not None:
        bundle = normalize_standalone_image(
            parsed,
            parsed.registration.revision.revision_id,
            registry,
            context=context,
        )
        fragments = ()
    else:
        base = (
            None
            if kind["normalizer"] == "structured_sections@1"
            else normalize_document_records(
                parsed,
                parsed.registration.revision.revision_id,
                registry,
                context=context,
            )
        )
        bundle, fragments = source_views(parsed, context, base)
    resolver = _SelectorResolver(parsed)
    payloads: dict[str, bytes] = {}
    refs: dict[str, ArtifactRef] = {}
    budget = _Budget()
    for unit in bundle.units:
        budget.add(
            work=1
            + len(cast(tuple[Any, ...], unit.attributes.get("source_parts", ())))
            + len(
                cast(
                    tuple[Any, ...],
                    unit.attributes.get("structural_separators", ()),
                )
            )
        )
    for collection in (
        "units",
        "locators",
        "assets",
        "unit_assets",
        "records",
        "identifiers",
    ):
        for index, row in enumerate(getattr(bundle, collection)):
            budget.account(collection, index, row)

    def retain(payload: bytes, mime: str = "application/json") -> ArtifactRef:
        ref = _ref(payload, mime)
        if replay_budget is not None:
            replay_budget.reserve(ref)
        budget.add(size=len(payload), work=1)
        payloads[ref.sha256], refs[ref.sha256] = payload, ref
        return ref

    assertions = []
    if plan.infer_table_continuations:
        for left, right in pairwise(fragments):
            decision = continuation(left, right)
            if decision.status == "separate":
                continue
            derivation = retain(
                canonical_bytes(
                    {
                        "algorithm": plan.table_strategy,
                        "source_fingerprint": context.source_fingerprint,
                        "source_refs": [left.native_ref, right.native_ref],
                        "decision": decision.model_dump(mode="json"),
                    }
                )
            )
            assertion = continuation_assertion(left, right, decision, derivation)
            assert assertion is not None
            validate_assertion(
                assertion,
                {unit.unit_id: unit for unit in bundle.units},
                registry,
                proxy_ids=frozenset(),
                kind_id=bundle.revision.kind_id,
                revision_id=bundle.revision.revision_id,
            )
            assertions.append(assertion)
    assertion_ref = (
        retain(encode_assertions(tuple(assertions)), "application/x-ndjson")
        if assertions
        else None
    )
    pdf_coverage = None
    if parsed.pdf_processing is not None:
        from tychora.contracts.composition import PdfCoverage
        from tychora.contracts.pdf_policy import PdfProcessingPolicy

        policy = PdfProcessingPolicy.model_validate_json(
            canonical_bytes(parsed.pdf_processing)
        )
        pdf_coverage = PdfCoverage(
            page_count=len(policy.native_pages)
            + len(policy.ocr_pages)
            + len(policy.blank_pages),
            native_pages=policy.native_pages,
            ocr_pages=policy.ocr_pages,
            blank_pages=policy.blank_pages,
            no_text_detected_pages=tuple(
                page.physical_page
                for page in parsed.ocr_pages
                if page.diagnostic == "no-text-detected"
            ),
        )
    diagnostics_ref = (
        retain(
            canonical_bytes(
                VisionCapabilityDiagnostics(
                    gaps=plan.vision_gaps, pdf_coverage=pdf_coverage
                ).model_dump(mode="json")
            )
        )
        if plan.vision_gaps or pdf_coverage is not None
        else None
    )
    assets = {asset.native_ref: asset.asset_id for asset in bundle.assets}
    parsed_assets = {asset.native_ref: asset for asset in parsed.assets}
    if plan.pixel_regions:
        if parsed.pdf_processing is None or {
            region.source_native_ref for region in plan.pixel_regions
        } != {f"#/pages/{page.physical_page}" for page in parsed.ocr_pages}:
            raise ValueError("PDF pixel-owner coverage differs from evidence")
        regions = sorted(
            plan.pixel_regions,
            key=lambda region: int(region.source_native_ref.rsplit("/", 1)[1]),
        )
        for region in regions:
            selected = parsed_assets[region.source_native_ref]
            evidence = next(
                page
                for page in parsed.ocr_pages
                if f"#/pages/{page.physical_page}" == region.source_native_ref
            )
            if (
                region.image_ref != evidence.model_input_image
                or region.region_pixel_sha256 != evidence.model_input_pixel_sha256
            ):
                raise ValueError("PDF pixel owner differs from exact OCR input")
            _verify_region_inputs(region, selected.artifact_ref, inputs)
            bundle, _ = _materialize_pixel_source_view(
                bundle, assets[region.source_native_ref], region
            )
        bundle = bundle.model_copy(
            update={
                "units": tuple(
                    unit.model_copy(
                        update={
                            "attributes": FrozenJsonMap(
                                {
                                    **unit.attributes,
                                    "document_representation": "docling-pdf-pixels@1",
                                }
                            )
                        }
                    )
                    if unit.unit_type == "image_region_original"
                    else unit
                    for unit in bundle.units
                )
            }
        )
    region_dependencies: dict[bytes, tuple[str, ...]] = {}
    for proxy in plan.proxies:
        if proxy.region is None:
            continue
        region = proxy.region
        selected_asset = parsed_assets.get(region.source_native_ref)
        standalone = (
            parsed.original_image is not None
            and region.source_native_ref == parsed.original_image.native_ref
        )
        if (
            selected_asset is None and not standalone
        ) or proxy.asset_native_ref not in assets:
            raise ValueError("Vision region has no retained source asset")
        original_ref = (
            parsed.original_ref
            if standalone
            else cast(Any, selected_asset).artifact_ref
        )
        _verify_region_inputs(region, original_ref, inputs)
        _verify_region_derivation(proxy, _checked(proxy.derivation, inputs))
        if standalone:
            owner_id = next(
                unit.unit_id
                for unit in bundle.units
                if unit.native_ref == region.source_native_ref
                and unit.text_origin == "source"
                and unit.citable
            )
        else:
            bundle, owner_id = _materialize_pixel_source_view(
                bundle, assets[proxy.asset_native_ref], region
            )
        region_dependencies[canonical_bytes(proxy.model_dump(mode="json"))] = tuple(
            sorted(
                {
                    owner_id,
                    *_verified_region_context_ids(proxy, parsed, bundle, resolver),
                }
            )
        )
    factory = ProxyFactory(bundle, legacy_v1=legacy_v1)
    proxies: list[ProxyRecord] = []

    def keep_proxy(proxy: ProxyRecord) -> None:
        budget.add(work=1 + len(proxy.binding.required_unit_ids))
        budget.account("units", len(bundle.units) + len(proxies), proxy.unit)
        budget.account("proxies", len(proxies), proxy.binding)
        budget.account(
            "locators",
            len(bundle.locators) + len(proxies),
            LocatorRow(
                locator_id=digest("locator-v1", proxy.unit.unit_id, 0),
                unit_id=proxy.unit.unit_id,
                fragment_ordinal=0,
                locator=proxy.unit.locators[0],
            ),
        )
        proxies.append(proxy)

    if plan.caption_copies:
        for asset in bundle.assets:
            dependencies = tuple(
                link.unit_id
                for link in bundle.unit_assets
                if link.asset_id == asset.asset_id and link.role == "caption"
            )
            if asset.caption and dependencies:
                receipt = retain(
                    canonical_bytes(
                        {
                            "algorithm": "caption-copy@1",
                            "source_fingerprint": context.source_fingerprint,
                            "asset_native_ref": asset.native_ref,
                            "caption": asset.caption,
                        }
                    )
                )
                keep_proxy(
                    factory.make_proxy(
                        asset.asset_id,
                        "caption_copy",
                        asset.caption,
                        dependencies,
                        receipt,
                    )
                )
    for proxy in plan.proxies:
        if proxy.asset_native_ref not in assets:
            raise ValueError("Composition proxy target has no original asset")
        if proxy.region is None:
            dependencies = tuple(
                sorted(
                    {
                        uid
                        for selector in proxy.dependencies
                        for uid in resolver.selected(selector, bundle)
                    }
                )
            )
        else:
            dependencies = region_dependencies[
                canonical_bytes(proxy.model_dump(mode="json"))
            ]
        if proxy.derivation.sha256 not in refs:
            retain(inputs[proxy.derivation.sha256], proxy.derivation.media_type)
        keep_proxy(
            factory.make_proxy(
                assets[proxy.asset_native_ref],
                proxy.kind,
                proxy.text,
                dependencies,
                proxy.derivation,
            )
        )
    if parsed.image_representation is not None:
        refs.update(references)
    units = (*bundle.units, *(proxy.unit for proxy in proxies))
    locators = (
        *bundle.locators,
        *(
            LocatorRow(
                locator_id=digest("locator-v1", proxy.unit.unit_id, i),
                unit_id=proxy.unit.unit_id,
                fragment_ordinal=i,
                locator=locator,
            )
            for proxy in proxies
            for i, locator in enumerate(proxy.unit.locators)
        ),
    )
    bundle = bundle.model_copy(
        update={
            "units": units,
            "locators": locators,
            "proxies": tuple(proxy.binding for proxy in proxies),
            "extraction": bundle.extraction.model_copy(
                update={
                    "assertions_artifact_sha": assertion_ref.sha256
                    if assertion_ref
                    else None,
                    "diagnostics_artifact_sha": diagnostics_ref.sha256
                    if diagnostics_ref
                    else None,
                }
            ),
            "artifacts": (
                *bundle.artifacts,
                *(
                    ref
                    for sha, ref in refs.items()
                    if sha not in {ref.sha256 for ref in bundle.artifacts}
                ),
            ),
        }
    )
    for collection in (
        "units",
        "locators",
        "assets",
        "unit_assets",
        "proxies",
        "records",
        "identifiers",
    ):
        for index, row in enumerate(getattr(bundle, collection)):
            budget.account(collection, index, row)
    links = [
        ManifestRef(
            artifact=parsed.raw_document_ref, type="extraction", schema_version="1"
        )
    ]
    if assertion_ref:
        links.append(
            ManifestRef(artifact=assertion_ref, type="assertion", schema_version="1")
        )
    manifest = canonical_bytes(
        {
            "type": "extraction",
            "schema_version": "1",
            "required_artifacts": [
                ref.model_dump(mode="json")
                for sha, ref in sorted(refs.items())
                if assertion_ref is None or sha != assertion_ref.sha256
            ],
            "required_manifests": [link.model_dump(mode="json") for link in links],
            "data": {
                "representation": "document-composition@1",
                "plan": plan.semantic_data(),
                "extraction_id": bundle.extraction.extraction_id,
                "revision_id": bundle.revision.revision_id,
                "source_fingerprint": context.source_fingerprint,
                "normalizer_fingerprint": context.normalizer_fingerprint,
                "canonical_bundle_sha": digest(
                    "composed-canonical-bundle-v1", _bundle_projection(bundle)
                ),
                "fragments": [
                    fragment.model_dump(mode="json") for fragment in fragments
                ],
            },
        }
    )
    if len(manifest) > MAX_COMPOSED_MANIFEST_BYTES:
        if parsed.pdf_processing is not None:
            from tychora.contracts.errors import TychoraError

            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF composed manifest exceeds capacity"
            )
        raise ValueError("Composed extraction manifest exceeds capacity")
    manifest_ref = retain(manifest)
    bundle = bundle.model_copy(
        update={
            "extraction": bundle.extraction.model_copy(
                update={"document_artifact_sha": manifest_ref.sha256}
            ),
            "artifacts": (*bundle.artifacts, manifest_ref),
        }
    )
    if parsed.pdf_processing is not None:
        from tychora.application.pdf_processing import PdfRetentionBudget
        from tychora.contracts.errors import TychoraError

        if plan.input_strategy is not None:
            PdfRetentionBudget.for_extraction(parsed).reserve_refs(bundle.artifacts)
        closure = {ref.sha256: ref for ref in bundle.artifacts}
        limits = parsed.pdf_processing["limits"]
        assert isinstance(limits, FrozenJsonMap)
        ceiling = limits["max_retained_bytes"]
        assert isinstance(ceiling, int)
        if sum(ref.size_bytes for ref in closure.values()) > ceiling:
            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF composed closure exceeds processing capacity"
            )
    return ComposedDocument(
        bundle, fragments, tuple(assertions), manifest_ref, payloads
    )


def compose_document(
    parsed: ExtractionArtifact,
    registry: Registry,
    plan: DocumentCompositionPlan,
    artifacts: Artifacts,
    ctx: ExecutionContext,
    *,
    pdf_budget=None,
) -> ComposedDocument:
    """Materialize all composed products before the caller's first corpus commit."""
    ctx.checkpoint()
    plan = DocumentNormalizationContext.bind(parsed, registry, plan).plan
    refs = composition_input_references(parsed, plan)

    def read(ref):
        ctx.checkpoint()
        with artifacts.open_verified(ref) as stream:
            return stream.read(ref.size_bytes + 1)

    if parsed.pdf_processing is not None:
        from tychora.application.pdf_processing import PdfRetentionBudget

        pdf_budget = PdfRetentionBudget.for_extraction(parsed, pdf_budget)
        pdf_budget.reserve_refs(refs.values())
    inputs = DeclaredPayloads(
        refs,
        read,
        MAX_COMPOSITION_REPLAY_BYTES
        if plan.input_strategy
        else MAX_COMPOSITION_INPUT_BYTES,
    )
    result = build_composed_document(parsed, registry, plan, inputs)
    bound_refs = {ref.sha256: ref for ref in result.bundle.artifacts}
    if pdf_budget is not None:
        # Admit the complete new batch before any composition payload is persisted.
        pdf_budget.reserve_refs(bound_refs.values())
    for sha, payload in result.payloads.items():
        ctx.checkpoint()
        if (
            artifacts.put_stream(BytesIO(payload), bound_refs[sha].media_type)
            != bound_refs[sha]
        ):
            raise ValueError("Persisted composition payload identity differs")
    return result


def replay_composed_document(
    manifest: bytes,
    manifest_ref: ArtifactRef,
    parsed: ExtractionArtifact,
    registry: Registry,
    inputs: Mapping[str, bytes],
) -> ComposedDocument:
    """Pure restore boundary: verify and recompute every canonical derived product."""
    if len(manifest) > MAX_COMPOSED_MANIFEST_BYTES:
        if parsed.pdf_processing is not None:
            from tychora.contracts.errors import TychoraError

            raise TychoraError(
                "LIMIT_EXCEEDED", "PDF composed manifest exceeds capacity"
            )
        raise ValueError("Composed extraction manifest exceeds capacity")
    _checked(manifest_ref, {manifest_ref.sha256: manifest})
    try:
        root = json.loads(manifest)
        canonical = canonical_bytes(root)
    except RecursionError:
        raise ValueError(
            "Composed extraction manifest nesting exceeds capacity"
        ) from None
    if (
        not isinstance(root, dict)
        or not isinstance(root.get("data"), dict)
        or "plan" not in root["data"]
        or canonical != manifest
        or root.get("type") != "extraction"
        or root.get("schema_version") != "1"
        or root.get("data", {}).get("representation") != "document-composition@1"
    ):
        raise ValueError("Unknown or noncanonical composed extraction manifest")
    plan = DocumentCompositionPlan.model_validate_json(
        canonical_bytes(root["data"]["plan"])
    )
    from .document_records import document_normalizer_fingerprint

    kind = cast(Mapping[str, Any], registry.kinds[parsed.registration.revision.kind_id])
    legacy_v1 = parsed.normalizer_fingerprint == document_normalizer_fingerprint(
        kind, registry, legacy_v1=True
    )
    result = build_composed_document(
        parsed, registry, plan, inputs, legacy_v1=legacy_v1
    )
    if (
        result.manifest_ref != manifest_ref
        or result.payloads[manifest_ref.sha256] != manifest
    ):
        raise ValueError("Composed extraction differs from verified replay inputs")
    references = composition_input_references(parsed, plan)
    artifacts = {ref.sha256: ref for ref in result.bundle.artifacts}
    for sha in result.payloads:
        ref = artifacts[sha]
        previous = references.get(sha)
        if previous is not None and physical_identity(previous) != physical_identity(
            ref
        ):
            raise ValueError("Conflicting composed replay artifact declarations")
        references.setdefault(sha, ref)
    if (
        sum(ref.size_bytes for ref in references.values())
        > MAX_COMPOSITION_REPLAY_BYTES
    ):
        raise ValueError("Composed replay required payloads exceed capacity")
    for sha, ref in references.items():
        _checked(ref, {sha: manifest} if sha == manifest_ref.sha256 else inputs)
    return result


class DeclaredPayloads(Mapping[str, bytes]):
    """Bounded authorized closure, loaded one verified payload at a time."""

    def __init__(self, refs, read, max_bytes):
        self.refs = dict(refs)
        self.read = read
        if sum(ref.size_bytes for ref in self.refs.values()) > max_bytes:
            raise ValueError("Declared payload closure exceeds capacity")

    def __len__(self):
        return len(self.refs)

    def __iter__(self):
        return iter(self.refs)

    def __getitem__(self, sha):
        ref = self.refs[sha]
        payload = self.read(ref)
        if len(payload) != ref.size_bytes or sha256(payload).hexdigest() != sha:
            raise ValueError("Declared payload differs from exact artifact")
        return payload
