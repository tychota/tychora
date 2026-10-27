"""Model-free, exact external PNG composition and retained replay boundaries."""

import json
import random
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from PIL import Image

from tests.ingestion import test_docling_mapping as mapping_fixtures
from tychora.adapters.docling.ocr_evidence import plan_pdf_ocr
from tychora.adapters.docling.parser import DoclingParser
from tychora.application.owner import StagedOutput
from tychora.application.pdf_processing import PDF_LIMITS
from tychora.configuration.registry import Registry
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.composition import DocumentCompositionPlan
from tychora.contracts.documents import image_pixel_identity
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.contracts.pdf_policy import PdfProcessingPolicy
from tychora.contracts.requests import RequestScope, SourceInput
from tychora.ingestion import composition

document_kind = mapping_fixtures.document_kind
pytestmark = pytest.mark.parametrize("execution", ["pdf"], indirect=True)


@pytest.fixture
def pdf_case(execution, rich_capture):
    """Retain real synthetic PNGs, raw export, sidecars and local OCR receipts."""
    owner, ctx = execution.owner, execution.ctx
    registry = Registry.from_config(execution.config)
    scope = RequestScope(
        principal_id="fixture",
        collection_ids=frozenset({execution.registration.collection.collection_id}),
        access_ceiling=3,
        uses=frozenset({"lookup", "compose", "audit"}),
        capabilities=frozenset({"read", "write", "manage", "vision"}),
    )
    owner.scope = scope

    def read(ref):
        with owner.artifacts.open_verified(ref) as stream:
            return stream.read(ref.size_bytes + 1)

    def make():
        policy = PdfProcessingPolicy(
            mode="local_ocr",
            native_pages=(2,),
            ocr_pages=(1, 3),
            blank_pages=(),
            backend_identity=FrozenJsonMap({"parser": "synthetic-retained-export"}),
            runtime_inventory=FrozenJsonMap({"runtime": "synthetic"}),
            model_inventory_sha256="b" * 64,
            options=FrozenJsonMap({"remote_services": False}),
            limits=FrozenJsonMap({**PDF_LIMITS, "max_retained_bytes": 1073741824}),
        )
        parser = DoclingParser(
            owner, execution.registration, Path("/unused-models"), policy=policy
        )
        export, _ = mapping_fixtures.mapping_case()
        for number in (1, 2, 3):
            pixels = random.Random(number).randbytes(64 * 64 * 3)
            image = Image.frombytes("RGB", (64, 64), pixels)
            output = BytesIO()
            image.save(output, format="PNG")
            ref = owner.artifacts.put_stream(BytesIO(output.getvalue()), "image/png")
            parser._external_refs[ref.sha256] = ref
            export["pages"][str(number)] = {
                "page_no": number,
                "size": {"width": 200.0, "height": 300.0},
                "image": {
                    "uri": "urn:tychora:artifact:sha256:" + ref.sha256,
                    "size": {"width": 64, "height": 64},
                    "dpi": 72,
                    "mimetype": "image/png",
                },
            }
            if number == 2:
                continue
            transcript = "Synthetic OCR evidence." if number == 1 else ""
            transform = "exif-transpose-rgb-v1:crop:0,0,64,64"
            parser._evidence.append(
                {
                    "physical_page": number,
                    "model_input_image": ref.model_dump(mode="json"),
                    "model_input_pixel_sha256": image_pixel_identity(
                        pixels, 64, 64, "RGB", transform
                    ),
                    "original_pixel_sha256": image_pixel_identity(
                        pixels, 64, 64, "RGB", "exif-transpose-rgb-v1"
                    ),
                    "width": 64,
                    "height": 64,
                    "region_pixel_transform": transform,
                    "renderer": {"backend": "synthetic"},
                    "channel_interpretation": "pdfium-rgb-array-rapidocr-ndarray-no-channel-swap",
                    "cells": [{"text": transcript, "from_ocr": True}]
                    if transcript
                    else [],
                    "transcript": transcript,
                    "detected_boxes": 1 if transcript else 0,
                    "diagnostic": "transcribed" if transcript else "no-text-detected",
                }
            )
        payload = canonical_bytes(export)
        kind = registry.kinds["manual@1"]
        assert isinstance(kind, FrozenJsonMap)
        parsed = parser._retain(
            (
                (
                    StagedOutput("document.json", "application/json", len(payload)),
                    BytesIO(payload),
                ),
            ),
            rich_capture,
            execution.config,
            registry,
            kind,
            FrozenJsonMap({}),
        )
        proxies, regions = plan_pdf_ocr(owner, parsed, ctx)
        return parsed, proxies, regions

    parsed, proxies, regions = owner.run_write_sync(make, ctx)

    def plan(**updates):
        return DocumentCompositionPlan(
            **{
                "base_normalizer_fingerprint": parsed.normalizer_fingerprint,
                "proxies": proxies,
                "pixel_regions": regions,
                **updates,
            }
        )

    def build(selected_plan):
        refs = composition.composition_input_references(parsed, selected_plan)
        return composition.build_composed_document(
            parsed,
            registry,
            selected_plan,
            composition.DeclaredPayloads(
                refs, read, composition.MAX_COMPOSITION_REPLAY_BYTES
            ),
        )

    return SimpleNamespace(
        parsed=parsed,
        registry=registry,
        plan=plan,
        read=read,
        build=build,
        owner=owner,
        scope=scope,
        ctx=ctx,
        run=lambda action: owner.run_write_sync(action, ctx),
    )


def test_streamed_raw_union_above_legacy_limit_composes_and_replays(
    pdf_case, monkeypatch
):
    """The strategy must not charge all exact raw images to the derived budget."""
    case = pdf_case
    monkeypatch.setattr(composition, "MAX_COMPOSITION_INPUT_BYTES", 20_000)
    with pytest.raises(TychoraError, match="composition inputs") as legacy:
        composition._input_references(case.parsed, case.plan())
    assert legacy.value.code == "LIMIT_EXCEEDED"
    plan = case.plan(input_strategy="pdf-streamed-images@1")

    def verify():
        result = case.build(plan)
        manifest = result.payloads[result.manifest_ref.sha256]
        refs = {ref.sha256: ref for ref in result.bundle.artifacts}
        inputs = composition.DeclaredPayloads(
            refs,
            lambda ref: (
                result.payloads[ref.sha256]
                if ref.sha256 in result.payloads
                else case.read(ref)
            ),
            composition.MAX_COMPOSITION_REPLAY_BYTES,
        )
        replayed = composition.replay_composed_document(
            manifest, result.manifest_ref, case.parsed, case.registry, inputs
        )
        assert replayed.bundle == result.bundle
        assert len(result.bundle.assets) == 3
        assert len(result.bundle.proxies) == 1
        assert (
            len(
                [
                    u
                    for u in result.bundle.units
                    if u.unit_type == "image_region_original"
                ]
            )
            == 2
        )
        assert (
            json.loads(manifest)["data"]["plan"]["input_strategy"]
            == "pdf-streamed-images@1"
        )
        assert all(
            not unit.citable and unit.review_state == "needs_review"
            for unit in result.bundle.units
            if unit.text_origin == "ocr"
        )

    case.run(verify)


def test_strategy_changes_derived_identity_and_keeps_exact_legacy_plan(pdf_case):
    case = pdf_case
    empty = case.plan(proxies=(), pixel_regions=())
    expected = {
        "base_normalizer_fingerprint": case.parsed.normalizer_fingerprint,
        "table_strategy": "table-fragments@1",
        "proxy_strategy": "asset-proxies@1",
        "infer_table_continuations": True,
        "caption_copies": True,
        "proxies": [],
    }
    assert canonical_bytes(empty.semantic_data()) == canonical_bytes(expected)
    assert "input_strategy" not in empty.model_dump()
    assert (
        DocumentCompositionPlan.model_validate_json(
            canonical_bytes(expected)
        ).semantic_data()
        == expected
    )
    legacy, streamed = case.run(
        lambda: (
            case.build(case.plan()),
            case.build(case.plan(input_strategy="pdf-streamed-images@1")),
        )
    )
    assert legacy.bundle.revision == streamed.bundle.revision
    assert (
        legacy.bundle.extraction.extraction_id
        != streamed.bundle.extraction.extraction_id
    )
    assert {a.asset_id for a in legacy.bundle.assets}.isdisjoint(
        a.asset_id for a in streamed.bundle.assets
    )
    assert {u.unit_id for u in legacy.bundle.units}.isdisjoint(
        u.unit_id for u in streamed.bundle.units
    )


@pytest.mark.asyncio
async def test_new_source_extraction_selects_strategy(pdf_case, monkeypatch):
    from tests.indexing.conftest import CharacterCounter
    from tychora.application.sources import SourceService
    from tychora.indexing.coordinator import ProjectionCoordinator

    case = pdf_case

    async def retained_parser(self, original, config, ctx):
        assert original == case.parsed.original_ref
        return case.parsed

    # Replace only the model-backed parser; receipt construction and composition
    # below are the production source path with real retained evidence and CAS.
    monkeypatch.setattr(DoclingParser, "aextract", retained_parser)
    monkeypatch.setattr(composition, "MAX_COMPOSITION_INPUT_BYTES", 20_000)
    service = SourceService(
        case.owner,
        ProjectionCoordinator(
            store=case.owner.store,
            artifacts=case.owner.artifacts,
            config=case.owner.config,
            counter=CharacterCounter(),
        ),
        docling_models=Path("/unused-models"),
    )
    registration = case.parsed.registration
    source = SourceInput.model_validate(
        {
            "type": "bytes",
            "data": b"synthetic parser boundary",
            "format": "pdf",
            "kind": registration.revision.kind_id,
            "metadata": registration.revision.metadata_json,
            "collection_id": UUID(registration.collection.collection_id),
            "source_key": registration.source.source_key,
            "pdf_processing": PdfProcessingPolicy.model_validate_json(
                canonical_bytes(case.parsed.pdf_processing)
            ),
        }
    )
    bundle = await service._extract(
        registration, registration.capture, case.ctx, source, case.scope
    )
    ref = next(
        ref
        for ref in bundle.artifacts
        if ref.sha256 == bundle.extraction.document_artifact_sha
    )
    root = json.loads(await case.owner.run_write(lambda: case.read(ref), case.ctx))
    assert root["data"]["plan"]["input_strategy"] == "pdf-streamed-images@1"


def test_external_pdf_selection_uses_authorized_raw_reader(pdf_case):
    from tychora.application.discovery import DiscoveryService

    case = pdf_case

    def commit():
        result = composition.compose_document(
            case.parsed,
            case.registry,
            case.plan(input_strategy="pdf-streamed-images@1"),
            case.owner.artifacts,
            case.ctx,
        )
        case.owner.store.commit_extraction(result.bundle)
        return result.bundle

    bundle = case.run(commit)
    discovery = DiscoveryService(case.owner.config, case.registry, case.owner)
    selected = discovery.source_selections(
        bundle.source.source_id,
        bundle.revision.revision_id,
        bundle.extraction.extraction_id,
        case.scope,
    )
    assert selected.retained_extraction_sha256 == case.parsed.raw_document_ref.sha256
    assert {
        item.source_native_ref for item in selected.candidates if item.kind == "image"
    } == {"#/pages/1", "#/pages/2", "#/pages/3"}
    assert any(
        item.kind == "context" and item.preview == "Retain the sample."
        for item in selected.candidates
    )
    with pytest.raises(TychoraError) as denied:
        discovery.source_selections(
            bundle.source.source_id,
            bundle.revision.revision_id,
            bundle.extraction.extraction_id,
            case.scope.model_copy(update={"collection_ids": frozenset()}),
        )
    assert denied.value.code == "NOT_FOUND"


@pytest.mark.parametrize("budget", ["derived", "group", "inputs"])
def test_input_admission_exact_boundary_and_one_byte_overflow_before_reads(
    pdf_case, monkeypatch, budget
):
    case = pdf_case
    plan = case.plan(input_strategy="pdf-streamed-images@1")
    references = composition.composition_input_references(case.parsed, plan)
    if budget == "derived":
        limit = sum(proxy.derivation.size_bytes for proxy in plan.proxies)
        constant = "MAX_COMPOSITION_INPUT_BYTES"
    elif budget == "group":
        limit = max(asset.artifact_ref.size_bytes for asset in case.parsed.assets)
        constant = "MAX_COMPOSITION_GROUP_BYTES"
    else:
        limit = sum(ref.size_bytes for ref in references.values())
        constant = "MAX_COMPOSITION_REPLAY_BYTES"
    monkeypatch.setattr(composition, constant, limit)
    assert composition.composition_input_references(case.parsed, plan) == references
    monkeypatch.setattr(composition, constant, limit - 1)
    reads = []

    def read(ref):
        reads.append(ref)
        return case.read(ref)

    with pytest.raises(TychoraError) as overflow:
        case.run(
            lambda: composition.build_composed_document(
                case.parsed,
                case.registry,
                plan,
                composition.DeclaredPayloads(references, read, 512 * 1024 * 1024),
            )
        )
    assert overflow.value.code == "LIMIT_EXCEEDED"
    assert reads == []


def test_generated_manifest_and_deduplicated_receipt_share_replay_allowance(
    pdf_case, monkeypatch
):
    case = pdf_case
    plan = case.plan(input_strategy="pdf-streamed-images@1")
    built = case.run(lambda: case.build(plan))
    refs = composition.composition_input_references(case.parsed, plan)
    artifacts = {ref.sha256: ref for ref in built.bundle.artifacts}
    unique = {**refs, **{sha: artifacts[sha] for sha in built.payloads}}
    limit = sum(ref.size_bytes for ref in unique.values())
    assert built.manifest_ref.sha256 in unique
    receipt = plan.proxies[0].derivation
    assert receipt.sha256 in refs and receipt.sha256 in built.payloads
    monkeypatch.setattr(composition, "MAX_COMPOSITION_REPLAY_BYTES", limit)
    assert case.run(lambda: case.build(plan)).manifest_ref == built.manifest_ref
    monkeypatch.setattr(composition, "MAX_COMPOSITION_REPLAY_BYTES", limit - 1)
    writes = []
    put = case.owner.artifacts.put_stream

    def write(stream, mime):
        writes.append(mime)
        return put(stream, mime)

    monkeypatch.setattr(case.owner.artifacts, "put_stream", write)
    with pytest.raises(TychoraError, match="replay capacity"):
        case.run(
            lambda: composition.compose_document(
                case.parsed, case.registry, plan, case.owner.artifacts, case.ctx
            )
        )
    assert writes == []


@pytest.fixture
def crop_plan(pdf_case):
    from tychora.adapters.vision.local import retain_local_ocr_observation
    from tychora.application.vision_selection import derive_selected_region
    from tychora.contracts.requests import (
        VisionContextSelector,
        VisionCrop,
        VisionSelection,
    )
    from tychora.ingestion.vision import planned_vision_proxy

    case = pdf_case

    def make():
        selected = VisionSelection(
            captured_original_sha256=case.parsed.original_ref.sha256,
            source_native_ref="#/pages/2",
            geometry=VisionCrop(x0=1, y0=2, x1=17, y1=18),
            task="ocr",
            task_schema=FrozenJsonMap({"type": "null"}),
            context=(
                VisionContextSelector(
                    native_ref="#/texts/0", field="text", source_start=0, source_end=6
                ),
            ),
        )
        region, _ = derive_selected_region(case.owner, case.parsed, selected)
        observation = retain_local_ocr_observation(
            case.owner,
            region,
            {"literal_transcription": "Selected crop."},
            model="synthetic-observation",
            prompt_schema_version="local-ocr-v1",
        )
        return case.plan(
            input_strategy="pdf-streamed-images@1",
            proxies=(*case.plan().proxies, planned_vision_proxy(region, observation)),
        )

    return case.run(make)


def test_crop_group_exact_bound_and_payload_lifetime(pdf_case, crop_plan, monkeypatch):
    case, plan = pdf_case, crop_plan
    crop = plan.proxies[-1].region
    original = next(
        a.artifact_ref
        for a in case.parsed.assets
        if a.native_ref == crop.source_native_ref
    )
    group_size = original.size_bytes + crop.image_ref.size_bytes
    monkeypatch.setattr(composition, "MAX_COMPOSITION_GROUP_BYTES", group_size)
    refs = composition.composition_input_references(case.parsed, plan)
    alive = []
    high_water = 0

    class TrackedImage(bytes):
        sha: str

        def __new__(cls, value, sha):
            nonlocal high_water
            obj = super().__new__(cls, value)
            obj.sha = sha
            alive.append(sha)
            high_water = max(high_water, len(alive))
            return obj

        def __del__(self):
            alive.remove(self.sha)

    class NoEagerIteration(composition.DeclaredPayloads):
        def __iter__(self):
            pytest.fail("Composition eagerly iterated the payload mapping")

    def read(ref):
        payload = case.read(ref)
        if ref.media_type.startswith("image/"):
            return TrackedImage(payload, ref.sha256)
        assert not alive, "A previous image group survived into receipt verification"
        return payload

    built = case.run(
        lambda: composition.build_composed_document(
            case.parsed,
            case.registry,
            plan,
            NoEagerIteration(refs, read, composition.MAX_COMPOSITION_REPLAY_BYTES),
        )
    )
    assert high_water == 2
    assert alive == []
    assert len(built.bundle.proxies) == 2
    proxy = built.bundle.proxies[-1]
    units = {u.unit_id: u for u in built.bundle.units}
    assert any(
        units[uid].unit_type == "image_region_original"
        for uid in proxy.required_unit_ids
    )
    assert any(
        "Retain the sample." in units[uid].text for uid in proxy.required_unit_ids
    )
    monkeypatch.setattr(composition, "MAX_COMPOSITION_GROUP_BYTES", group_size - 1)
    with pytest.raises(TychoraError, match="verification group"):
        composition.composition_input_references(case.parsed, plan)


@pytest.mark.parametrize("target", ["raw", "crop", "receipt"])
@pytest.mark.parametrize("mutation", ["missing", "byte", "size"])
def test_streamed_input_payload_tampering_fails(pdf_case, crop_plan, target, mutation):
    case, plan = pdf_case, crop_plan
    ref = (
        case.parsed.assets[1].artifact_ref
        if target == "raw"
        else plan.proxies[-1].region.image_ref
        if target == "crop"
        else plan.proxies[-1].derivation
    )
    refs = composition.composition_input_references(case.parsed, plan)
    if mutation == "missing":
        refs.pop(ref.sha256)

    def read(item):
        payload = case.read(item)
        if item.sha256 == ref.sha256:
            if mutation == "byte":
                return bytes([payload[0] ^ 1]) + payload[1:]
            if mutation == "size":
                return payload + b"x"
        return payload

    with pytest.raises(ValueError, match="payload|artifact|retained reference"):
        case.run(
            lambda: composition.build_composed_document(
                case.parsed,
                case.registry,
                plan,
                composition.DeclaredPayloads(
                    refs, read, composition.MAX_COMPOSITION_REPLAY_BYTES
                ),
            )
        )


@pytest.mark.parametrize(
    "mutation", ["pixel", "dimension", "crop", "receipt", "context", "mime", "conflict"]
)
def test_streamed_crop_requires_exact_pixel_receipt_and_context_proofs(
    pdf_case, crop_plan, mutation
):
    case, plan = pdf_case, crop_plan
    proxy = plan.proxies[-1]
    region = proxy.region
    if mutation == "pixel":
        region = region.model_copy(update={"original_pixel_sha256": "0" * 64})
    elif mutation == "dimension":
        region = region.model_copy(update={"original_width": 65})
    elif mutation == "crop":
        geometry = region.geometry.model_copy(update={"x0": 2.0, "x1": 18.0})
        transform = "exif-transpose-rgb-v1:crop:2,2,18,18"
        with Image.open(
            BytesIO(case.run(lambda: case.read(region.image_ref)))
        ) as image:
            pixel_sha = image_pixel_identity(
                image.convert("RGB").tobytes(), 16, 16, "RGB", transform
            )
        region = region.model_copy(
            update={
                "geometry": geometry,
                "region_pixel_transform": transform,
                "region_pixel_sha256": pixel_sha,
            }
        )
    elif mutation == "receipt":
        proxy = proxy.model_copy(update={"derivation": plan.proxies[0].derivation})
    elif mutation == "context":
        from tychora.contracts.composition import SourceContextSelection

        original = proxy.source_context[0]
        forged = SourceContextSelection.bind(
            original.captured_original_sha256, original.selector, "Forged the sample."
        )
        proxy = proxy.model_copy(update={"source_context": (forged,)})
    elif mutation == "mime":
        region = region.model_copy(
            update={
                "media_type": "image/jpeg",
                "image_ref": region.image_ref.model_copy(
                    update={"media_type": "image/jpeg"}
                ),
            }
        )
    else:
        # An OCR proxy repeats a raw page descriptor, so a size mismatch must
        # remain a conflict rather than being silently deduplicated as raw.
        proxy = plan.proxies[0]
        region = proxy.region.model_copy(
            update={
                "image_ref": proxy.region.image_ref.model_copy(
                    update={"size_bytes": proxy.region.image_ref.size_bytes + 1}
                )
            }
        )
    proxy = proxy.model_copy(update={"region": region})
    forged_plan = plan.model_copy(update={"proxies": (*plan.proxies[:-1], proxy)})
    with pytest.raises(ValueError):
        case.run(lambda: case.build(forged_plan))


@pytest.mark.parametrize(
    "mutation",
    [
        "no_regions",
        "missing_zero_owner",
        "wrong_raw",
        "undeclared",
        "unbound_policy",
        "extra_binding",
    ],
)
def test_streaming_cannot_be_enabled_with_unqualified_or_incomplete_facts(
    pdf_case, mutation
):
    case = pdf_case
    parsed = case.parsed
    plan = case.plan(input_strategy="pdf-streamed-images@1")
    if mutation == "no_regions":
        plan = plan.model_copy(update={"pixel_regions": ()})
    elif mutation == "missing_zero_owner":
        plan = plan.model_copy(update={"pixel_regions": plan.pixel_regions[:1]})
    elif mutation == "wrong_raw":
        parsed = parsed.model_copy(
            update={
                "assets": (
                    parsed.assets[0].model_copy(
                        update={"artifact_ref": plan.proxies[0].derivation}
                    ),
                    *parsed.assets[1:],
                )
            }
        )
    elif mutation == "undeclared":
        parsed = parsed.model_copy(
            update={"image_bindings": parsed.image_bindings[:-1]}
        )
    elif mutation == "unbound_policy":
        parsed = parsed.model_copy(update={"parser_fingerprint": "f" * 64})
    else:
        parsed = parsed.model_copy(
            update={
                "image_bindings": (
                    *parsed.image_bindings,
                    parsed.image_bindings[0].model_copy(
                        update={"native_ref": "#/pages/99"}
                    ),
                )
            }
        )
    with pytest.raises(ValueError):
        composition.composition_input_references(parsed, plan)


def test_composition_checks_cancellation_between_actual_reads(pdf_case, monkeypatch):
    from contextlib import contextmanager

    from tychora.ports.execution import ExecutionContext

    case = pdf_case
    ctx = ExecutionContext()
    reads, writes = [], []
    opened = case.owner.artifacts.open_verified
    put = case.owner.artifacts.put_stream

    @contextmanager
    def read(ref):
        reads.append(ref.sha256)
        with opened(ref) as stream:
            yield stream
        ctx.cancelled.set()

    def write(stream, mime):
        writes.append(mime)
        return put(stream, mime)

    monkeypatch.setattr(case.owner.artifacts, "open_verified", read)
    monkeypatch.setattr(case.owner.artifacts, "put_stream", write)
    with pytest.raises(TychoraError) as cancelled:
        case.run(
            lambda: composition.compose_document(
                case.parsed,
                case.registry,
                case.plan(input_strategy="pdf-streamed-images@1"),
                case.owner.artifacts,
                ctx,
            )
        )
    assert cancelled.value.code == "CANCELLED"
    assert len(reads) == 1
    assert writes == []


@pytest.mark.parametrize("target", ["image", "evidence", "bindings"])
def test_raw_replay_still_verifies_image_and_sidecar_payloads(pdf_case, target):
    from tychora.ingestion.documents import replay_document

    case = pdf_case
    ref = (
        case.parsed.assets[0].artifact_ref
        if target == "image"
        else case.parsed.ocr_evidence_ref
        if target == "evidence"
        else case.parsed.image_bindings_ref
    )

    def replay():
        def read(item):
            payload = case.read(item)
            return payload + b"x" if item.sha256 == ref.sha256 else payload

        with pytest.raises(ValueError):
            replay_document(
                case.read(case.parsed.raw_document_ref),
                case.read(case.parsed.docling_json_ref),
                case.parsed.raw_document_ref,
                read_declared=read,
            )

    case.run(replay)


def _resealed_raw(parsed, mutate):
    """Build internally consistent raw descriptors for negative byte validation."""
    from tychora.application.pdf_processing import policy_sha256
    from tychora.contracts.extraction import ExtractionArtifact, extraction_document

    data = json.loads(canonical_bytes(parsed.model_dump(mode="json")))
    mutate(data)

    def ref(payload):
        key = sha256(payload).hexdigest()
        return {
            "sha256": key,
            "storage_key": key,
            "size_bytes": len(payload),
            "media_type": "application/json",
        }

    data["docling_json_ref"] = ref(canonical_bytes(data["document_json"]))
    policy = PdfProcessingPolicy.model_validate_json(
        canonical_bytes(data["pdf_processing"])
    )
    policy_sha = policy_sha256(policy)
    data["parser_fingerprint"] = digest("docling-pdf-policy-parser-v1", policy_sha)
    for page in data["ocr_pages"]:
        page["policy_sha256"] = policy_sha
    data["ocr_evidence_ref"] = ref(canonical_bytes(data["ocr_pages"]))
    data["image_bindings"] = [
        {"native_ref": asset["native_ref"], "artifact_ref": asset["artifact_ref"]}
        for asset in data["assets"]
    ]
    data["image_bindings_ref"] = ref(
        canonical_bytes(
            {
                "version": data["image_representation"],
                "bindings": data["image_bindings"],
            }
        )
    )
    data["raw_document_ref"] = ref(
        extraction_document(
            {
                key: value
                for key, value in data.items()
                if key not in {"document_json", "raw_document_ref"}
            }
        )
    )
    return ExtractionArtifact.model_validate_json(canonical_bytes(data))


@pytest.mark.parametrize("mutation", ["mime", "dimension"])
def test_streaming_fully_decodes_even_unselected_raw_images(pdf_case, mutation):
    case = pdf_case

    def change(data):
        asset = next(
            asset for asset in data["assets"] if asset["native_ref"] == "#/pages/2"
        )
        image = data["document_json"]["pages"]["2"]["image"]
        if mutation == "mime":
            asset["artifact_ref"]["media_type"] = image["mimetype"] = "image/jpeg"
        else:
            asset["width"] = image["size"]["width"] = 63

    parsed = _resealed_raw(case.parsed, change)
    plan = case.plan(input_strategy="pdf-streamed-images@1")
    refs = composition.composition_input_references(parsed, plan)
    with pytest.raises(ValueError, match="MIME|dimensions"):
        case.run(
            lambda: composition.build_composed_document(
                parsed,
                case.registry,
                plan,
                composition.DeclaredPayloads(
                    refs, case.read, composition.MAX_COMPOSITION_REPLAY_BYTES
                ),
            )
        )


def test_whole_pdf_retention_exact_limit_and_one_byte_overflow(pdf_case, monkeypatch):
    case = pdf_case

    def prepare(ceiling):
        parsed = _resealed_raw(
            case.parsed,
            lambda data: data["pdf_processing"]["limits"].update(
                max_retained_bytes=ceiling
            ),
        )
        proxies, regions = plan_pdf_ocr(case.owner, parsed, case.ctx)
        plan = DocumentCompositionPlan(
            base_normalizer_fingerprint=parsed.normalizer_fingerprint,
            input_strategy="pdf-streamed-images@1",
            proxies=proxies,
            pixel_regions=regions,
        )
        refs = composition.composition_input_references(parsed, plan)
        return (
            parsed,
            plan,
            composition.DeclaredPayloads(
                refs, case.read, composition.MAX_COMPOSITION_REPLAY_BYTES
            ),
        )

    def build(ceiling):
        parsed, plan, inputs = prepare(ceiling)
        result = composition.build_composed_document(
            parsed, case.registry, plan, inputs
        )
        return sum(
            ref.size_bytes
            for ref in {ref.sha256: ref for ref in result.bundle.artifacts}.values()
        )

    ceiling = case.run(lambda: build(1_000_000))
    # The policy's decimal length affects the sealed raw root once; stabilize
    # that representation, then change precisely one byte of allowance.
    ceiling = case.run(lambda: build(ceiling))
    assert case.run(lambda: build(ceiling)) == ceiling
    parsed, plan, _ = case.run(lambda: prepare(ceiling - 1))
    writes = []
    put = case.owner.artifacts.put_stream

    def write(stream, mime):
        writes.append(mime)
        return put(stream, mime)

    monkeypatch.setattr(case.owner.artifacts, "put_stream", write)
    with pytest.raises(TychoraError, match="retained closure"):
        case.run(
            lambda: composition.compose_document(
                parsed, case.registry, plan, case.owner.artifacts, case.ctx
            )
        )
    assert writes == []


def test_streamed_complete_raw_replay_commit_backup_restore_and_inspection(
    pdf_case, tmp_path
):
    from tychora.application.backup import BackupService
    from tychora.application.inspection import InspectionService
    from tychora.application.owner import WorkspaceOwner
    from tychora.application.restore import RestoreService, _validate_retained_documents
    from tychora.ingestion.documents import replay_document
    from tychora.ports.execution import ExecutionContext

    case = pdf_case

    def commit():
        parsed = replay_document(
            case.read(case.parsed.raw_document_ref),
            case.read(case.parsed.docling_json_ref),
            case.parsed.raw_document_ref,
            read_declared=case.read,
        )
        assert parsed == case.parsed
        result = composition.compose_document(
            parsed,
            case.registry,
            case.plan(input_strategy="pdf-streamed-images@1"),
            case.owner.artifacts,
            case.ctx,
        )
        case.owner.store.commit_extraction(result.bundle)
        _validate_retained_documents(
            case.owner.db, case.owner.artifacts, case.owner.config
        )
        inspection = InspectionService(case.owner)._inspect_diagnostics(
            None,
            SimpleNamespace(id=result.bundle.extraction.extraction_id),
            SimpleNamespace(
                members=(
                    SimpleNamespace(
                        extraction_id=result.bundle.extraction.extraction_id,
                        revision_id=result.bundle.revision.revision_id,
                    ),
                )
            ),
            SimpleNamespace(revisions={result.bundle.revision.revision_id: None}),
        )
        assert inspection.records[0].value["ocr_review_state"] == "needs_review"
        return result

    result = case.run(commit)
    archive, destination = tmp_path / "archive", tmp_path / "restored"
    backup = BackupService(case.owner).export(
        archive, case.scope, ctx=ExecutionContext()
    )
    required = {ref.sha256 for ref in backup.manifest.artifacts}
    assert {ref.sha256 for ref in result.bundle.artifacts} <= required
    RestoreService().import_backup(archive, destination, case.scope, ExecutionContext())
    with WorkspaceOwner.open(destination, case.owner.config, case.scope) as restored:
        rows = restored.run_write_sync(
            lambda: restored.store.hydrate(
                tuple(u.unit_id for u in result.bundle.units)
            ),
            ExecutionContext(),
        )
        assert rows == result.bundle.units


def test_discovery_distinguishes_legacy_and_streamed_budget_scopes(pdf_case):
    from tests.typed_json import json_object
    from tychora.application.discovery import DiscoveryService

    case = pdf_case
    settings = DiscoveryService(
        case.owner.config, case.registry, case.owner
    )._definitions("settings")["resolved"]
    strategies = json_object(settings, "document_composition_inputs")
    assert json_object(strategies, "legacy")["aggregate_bytes"] == 268435456
    streamed = json_object(strategies, "pdf-streamed-images@1")
    assert streamed["raw_images"] == "exact_declared_bindings_streamed"
    assert streamed["nonraw_aggregate_bytes"] == 268435456
    assert streamed["unique_encoded_group_bytes"] == 268435456
    assert streamed["unique_inputs_and_products_bytes"] == 536870912
    assert streamed["maximum_pdf_retained_bytes"] == 1073741824


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [None, "pdf-streamed-images@1"])
async def test_retained_source_reuse_preserves_stored_plan_strategy(pdf_case, strategy):
    from tests.indexing.conftest import CharacterCounter
    from tychora.application.sources import SourceService
    from tychora.indexing.coordinator import ProjectionCoordinator

    case = pdf_case
    service = SourceService(
        case.owner,
        ProjectionCoordinator(
            store=case.owner.store,
            artifacts=case.owner.artifacts,
            config=case.owner.config,
            counter=CharacterCounter(),
        ),
    )
    jobs, acquirer = await service._ports(case.ctx)
    config_ref = await service._config_ref(case.ctx)

    def reuse():
        original = composition.compose_document(
            case.parsed,
            case.registry,
            case.plan(input_strategy=strategy),
            case.owner.artifacts,
            case.ctx,
        )
        case.owner.store.commit_extraction(original.bundle)
        # Record the actual capture/config linkage required by retained reuse.
        handle = jobs.accept(
            "sync", case.scope, None, {"source": "synthetic"}, config_ref
        )
        registration = case.parsed.registration
        jobs.capture(handle.id, registration.capture.manifest_ref)
        current_capture = acquirer.capture(
            SourceInput.model_validate(
                {
                    "type": "bytes",
                    "data": case.read(case.parsed.original_ref),
                    "format": "pdf",
                    "kind": registration.revision.kind_id,
                    "metadata": registration.revision.metadata_json,
                    "source_key": registration.source.source_key,
                    "collection_id": UUID(registration.collection.collection_id),
                    "pdf_processing": PdfProcessingPolicy.model_validate_json(
                        canonical_bytes(case.parsed.pdf_processing)
                    ),
                }
            ),
            case.ctx,
        )
        current = registration.model_copy(update={"capture": current_capture})
        replayed = service._retained_bundle(current, case.ctx)
        assert replayed == original.bundle
        plan = json.loads(case.read(original.manifest_ref))["data"]["plan"]
        assert plan.get("input_strategy") == strategy
        if strategy is None:
            assert "input_strategy" not in plan

    await case.owner.run_write(reuse, case.ctx)
