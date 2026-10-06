import json
import os
import shutil
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from tests.qualification import test_docling_records as qualification
from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Geometry, ImageLocator
from tychora.ports.execution import ExecutionContext

document_record_case = qualification.document_record_case


def _region(task: str = "ocr", *, with_context: bool = True):
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image

    from tychora.contracts.evidence import Geometry
    from tychora.ports.vision import (
        CandidateContext,
        RegionInput,
        pixel_identity,
        vision_context_identity,
    )

    context = (
        (
            CandidateContext(
                target_id="paragraph-1",
                source_text_sha256="263b0682bae228fd6874cac7f78d5a56e8bcd38dd7926d822f5ada03afa6deae",
                char_start=0,
                char_end=19,
                literal_text="Candidate paragraph",
            ),
        )
        if with_context
        else ()
    )
    image = Image.new("RGB", (2, 1), "blue")
    output = BytesIO()
    image.save(output, format="PNG")
    payload = output.getvalue()
    encoded_sha = sha256(payload).hexdigest()
    original_transform = "exif-transpose-rgb-v1"
    region_transform = original_transform + ":crop:0,0,2,1"

    return RegionInput(
        source_native_ref="#/pages/1",
        image=payload,
        image_ref=ArtifactRef(
            sha256=encoded_sha,
            size_bytes=len(payload),
            media_type="image/png",
            storage_key=encoded_sha,
        ),
        media_type="image/png",
        image_sha256=encoded_sha,
        original_image_sha256=encoded_sha,
        original_pixel_sha256=pixel_identity(
            image.tobytes(), 2, 1, "RGB", original_transform
        ),
        region_pixel_sha256=pixel_identity(
            image.tobytes(), 2, 1, "RGB", region_transform
        ),
        original_width=2,
        original_height=1,
        width=2,
        height=1,
        color_mode="RGB",
        original_pixel_transform=original_transform,
        region_pixel_transform=region_transform,
        geometry=Geometry(domain="pixels", x0=0, y0=0, x1=2, y1=1),
        context_sha256=vision_context_identity(context),
        task=task,
        allowed_candidate_target_ids=frozenset({"paragraph-1"} if with_context else ()),
        dependency_ids=frozenset(
            {"source-unit-1", "paragraph-1"} if with_context else {"source-unit-1"}
        ),
        candidate_context=context,
    )


def test_vision_cannot_invent_annotation_target():
    from tychora.adapters.vision.anthropic import validate_target

    with pytest.raises(TychoraError) as err:
        validate_target("invented", frozenset({"paragraph-1"}))
    assert err.value.code == "PROVIDER_FAILURE"


def test_vision_cache_identity_binds_image_task_model_prompt_and_context():
    from tychora.ports.vision import vision_cache_key, vision_prompt_identity

    assert (
        vision_cache_key(
            "a" * 64,
            "ocr",
            "claude-sonnet-4-5",
            "vision-response-v1",
            "b" * 64,
        )
        == "d0ad9854d4940d471b2260b50e692dadd2ad62963f2dcc430ea1055705df4791"
    )
    string_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }
    integer_schema = {
        **string_schema,
        "properties": {"summary": {"type": "integer"}},
    }
    assert vision_prompt_identity("vision-response-v1", string_schema) != (
        vision_prompt_identity("vision-response-v1", integer_schema)
    )


def test_default_composition_plan_preserves_task13_semantic_shape():
    from tychora.contracts.composition import DocumentCompositionPlan

    plan = DocumentCompositionPlan(base_normalizer_fingerprint="a" * 64)

    assert plan.semantic_data() == {
        "base_normalizer_fingerprint": "a" * 64,
        "table_strategy": "table-fragments@1",
        "proxy_strategy": "asset-proxies@1",
        "infer_table_continuations": True,
        "caption_copies": True,
        "proxies": [],
    }


def test_source_context_selection_binds_raw_field_identity_and_exact_interval():
    from pydantic import ValidationError

    from tychora.contracts.composition import (
        SourceContextSelection,
        SourceSelector,
    )

    selector = SourceSelector(
        native_ref="#/texts/0",
        field="text",
        source_start=4,
        source_end=13,
    )
    selected = SourceContextSelection.bind("a" * 64, selector, "The paragraph text")

    assert selected.literal_text == "paragraph"
    assert selected.candidate_context().target_id == selected.target_id
    assert selected.candidate_context().char_start == 4
    assert selected.candidate_context().char_end == 13
    with pytest.raises(ValidationError, match="source context"):
        SourceContextSelection.model_validate(
            {**selected.model_dump(), "literal_text": "different"}
        )


def test_pixel_identity_binds_dimensions_color_mode_and_transform():
    from tychora.ports.vision import pixel_identity

    pixels = b"\x00\x10\x20\x30\x40\x50"
    baseline = pixel_identity(pixels, 2, 1, "RGB", "exif-transpose-rgb-v1")

    assert baseline != pixel_identity(pixels, 1, 2, "RGB", "exif-transpose-rgb-v1")
    assert baseline != pixel_identity(pixels, 2, 1, "BGR", "exif-transpose-rgb-v1")
    assert baseline != pixel_identity(pixels, 2, 1, "RGB", "identity-rgb-v1")


def test_region_requires_exact_bytes_pixel_bounds_and_source_dependencies():
    from tychora.contracts.evidence import Geometry
    from tychora.ports.vision import RegionInput, vision_context_identity

    with pytest.raises(ValidationError):
        RegionInput(
            source_native_ref="#/pages/1",
            image=b"region",
            image_ref=ArtifactRef(
                sha256="c697d2981bf416569a16cfbcdec1542b5398f3cc77d2b905819aa99c46ecf6f6",
                size_bytes=6,
                media_type="image/png",
                storage_key="c697d2981bf416569a16cfbcdec1542b5398f3cc77d2b905819aa99c46ecf6f6",
            ),
            media_type="image/png",
            image_sha256="0" * 64,
            original_image_sha256="1" * 64,
            original_pixel_sha256="2" * 64,
            region_pixel_sha256="3" * 64,
            original_width=2,
            original_height=1,
            width=2,
            height=1,
            color_mode="RGB",
            original_pixel_transform="exif-transpose-rgb-v1",
            region_pixel_transform="exif-transpose-rgb-v1:crop:0,0,2,1",
            geometry=Geometry(domain="pixels", x0=0, y0=0, x1=3, y1=1),
            context_sha256=vision_context_identity(()),
            task="ocr",
            dependency_ids=frozenset(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 1),
        ("region_pixel_sha256", "f" * 64),
        ("region_pixel_transform", "identity"),
    ],
)
def test_region_rejects_false_decoded_pixel_facts(field: str, value: object):
    from tychora.ports.vision import RegionInput

    data = _region().model_dump()
    data[field] = value
    with pytest.raises(ValidationError, match="retained pixel facts"):
        RegionInput.model_validate(data)


def test_region_rejects_over_capacity_header_before_pixel_allocation():
    import struct
    import zlib
    from hashlib import sha256

    from tychora.ports.vision import RegionInput, pixel_identity

    baseline = _region()
    payload = bytearray(baseline.image)
    payload[16:20] = struct.pack(">I", 10_000)
    payload[20:24] = struct.pack(">I", 5_000)
    payload[29:33] = struct.pack(">I", zlib.crc32(payload[12:29]) & 0xFFFFFFFF)
    encoded = bytes(payload)
    encoded_sha = sha256(encoded).hexdigest()
    data = baseline.model_dump()
    data.update(
        {
            "image": encoded,
            "image_ref": baseline.image_ref.model_copy(
                update={
                    "sha256": encoded_sha,
                    "storage_key": encoded_sha,
                    "size_bytes": len(encoded),
                }
            ),
            "image_sha256": encoded_sha,
            "original_image_sha256": encoded_sha,
            "original_width": 10_000,
            "original_height": 5_000,
            "width": 10_000,
            "height": 5_000,
            "geometry": Geometry(domain="pixels", x0=0, y0=0, x1=10_000, y1=5_000),
            "region_pixel_transform": "exif-transpose-rgb-v1:crop:0,0,10000,5000",
            "region_pixel_sha256": pixel_identity(
                b"", 10_000, 5_000, "RGB", "exif-transpose-rgb-v1:crop:0,0,10000,5000"
            ),
        }
    )
    with pytest.raises(ValidationError, match="retained pixel facts"):
        RegionInput.model_validate(data)


def test_jpeg_original_allows_only_an_exact_lossless_png_crop():
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image, ImageOps

    from tychora.ports.vision import (
        RegionInput,
        pixel_identity,
        verify_region_source,
        vision_context_identity,
    )

    source = Image.new("RGB", (6, 4), "white")
    source.putpixel((2, 1), (10, 80, 210))
    encoded = BytesIO()
    source.save(encoded, format="JPEG", quality=90)
    original = encoded.getvalue()
    original_sha = sha256(original).hexdigest()
    original_ref = ArtifactRef(
        sha256=original_sha,
        size_bytes=len(original),
        storage_key=original_sha,
        media_type="image/jpeg",
    )
    with Image.open(BytesIO(original)) as image:
        decoded = ImageOps.exif_transpose(image).convert("RGB")
        decoded.load()
    crop = decoded.crop((1, 1, 5, 4))
    encoded_crop = BytesIO()
    crop.save(encoded_crop, format="PNG")
    payload = encoded_crop.getvalue()
    payload_sha = sha256(payload).hexdigest()
    region_transform = "exif-transpose-rgb-v1:crop:1,1,5,4"
    region = RegionInput(
        source_native_ref="#/pages/1",
        image=payload,
        image_ref=ArtifactRef(
            sha256=payload_sha,
            size_bytes=len(payload),
            storage_key=payload_sha,
            media_type="image/png",
        ),
        media_type="image/png",
        image_sha256=payload_sha,
        original_image_sha256=original_sha,
        original_pixel_sha256=pixel_identity(
            decoded.tobytes(), 6, 4, "RGB", "exif-transpose-rgb-v1"
        ),
        region_pixel_sha256=pixel_identity(
            crop.tobytes(), 4, 3, "RGB", region_transform
        ),
        original_width=6,
        original_height=4,
        width=4,
        height=3,
        color_mode="RGB",
        original_pixel_transform="exif-transpose-rgb-v1",
        region_pixel_transform=region_transform,
        geometry=Geometry(domain="pixels", x0=1, y0=1, x1=5, y1=4),
        context_sha256=vision_context_identity(()),
        task="describe",
        dependency_ids=frozenset({"source"}),
    )

    verify_region_source(region, original, original_ref, "#/pages/1")

    lossy = BytesIO()
    crop.save(lossy, format="JPEG", quality=70)
    lossy_bytes = lossy.getvalue()
    lossy_sha = sha256(lossy_bytes).hexdigest()
    with Image.open(BytesIO(lossy_bytes)) as decoded_lossy:
        lossy_pixels = decoded_lossy.convert("RGB")
        lossy_pixels.load()
    lossy_region = RegionInput.model_validate(
        {
            **region.model_dump(),
            "image": lossy_bytes,
            "image_ref": {
                "sha256": lossy_sha,
                "size_bytes": len(lossy_bytes),
                "storage_key": lossy_sha,
                "media_type": "image/jpeg",
            },
            "media_type": "image/jpeg",
            "image_sha256": lossy_sha,
            "region_pixel_sha256": pixel_identity(
                lossy_pixels.tobytes(), 4, 3, "RGB", region_transform
            ),
        }
    )
    with pytest.raises(ValueError, match="verified retained original"):
        verify_region_source(lossy_region, original, original_ref, "#/pages/1")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"target_id":null,"literal_transcription":"Text","interpretation":null,"uncertainties":[],"confidence":0.99}',
    ],
)
def test_vision_rejects_malformed_json_and_fake_confidence(payload: str):
    from tychora.adapters.vision.anthropic import parse_response

    with pytest.raises(TychoraError) as err:
        parse_response(
            payload,
            _region(),
            "claude-sonnet-4-5",
            "vision-response-v1",
            {"type": "null"},
        )
    assert err.value.code == "PROVIDER_FAILURE"


def test_remote_timeout_is_sanitized():
    from tychora.adapters.vision.anthropic import AnthropicVisionProvider
    from tychora.ports.execution import ExecutionContext

    class Messages:
        def create(self, **kwargs):
            raise TimeoutError("secret provider body")

    class Client:
        messages = Messages()

    provider = AnthropicVisionProvider(
        Client(), model="claude-sonnet-4-5", prompt_schema_version="vision-response-v1"
    )
    with pytest.raises(TychoraError) as err:
        provider.interpret(
            _region(with_context=False), {"type": "object"}, ExecutionContext()
        )
    assert err.value.code == "PROVIDER_FAILURE"
    assert "secret provider body" not in str(err.value)


def test_unsupported_task_schema_is_rejected_before_client_use():
    from tychora.adapters.vision.anthropic import AnthropicVisionProvider

    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("unsupported schema must not reach the client")

    provider = AnthropicVisionProvider(
        type("Client", (), {"messages": Messages()})(),
        model="synthetic-model",
        prompt_schema_version="vision-response-v1",
    )
    with pytest.raises(TychoraError) as error:
        provider.interpret(
            _region("describe"),
            {"$ref": "https://example.invalid/ambient.json"},
            ExecutionContext(),
        )

    assert error.value.code == "INVALID_REQUEST"
    assert calls == []


def test_provider_interpretation_must_match_requested_nested_schema():
    from types import SimpleNamespace

    from tychora.adapters.vision.anthropic import AnthropicVisionProvider

    payload = json.dumps(
        {
            "target_id": None,
            "literal_transcription": None,
            "interpretation": {"summary": 7},
            "uncertainties": [],
        }
    )
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=payload)],
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: response)
    )
    provider = AnthropicVisionProvider(
        client, model="synthetic", prompt_schema_version="vision-response-v1"
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }

    with pytest.raises(TychoraError) as error:
        provider.interpret(
            _region("describe", with_context=False), schema, ExecutionContext()
        )
    assert error.value.code == "PROVIDER_FAILURE"
    assert "summary" not in str(error.value)


def test_anthropic_renderer_semantics_change_effective_prompt_identity():
    from types import SimpleNamespace

    from tychora.adapters.vision.anthropic import AnthropicVisionProvider

    payload = json.dumps(
        {
            "target_id": None,
            "literal_transcription": None,
            "interpretation": {"summary": "synthetic"},
            "uncertainties": [],
        }
    )
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=payload)],
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_kwargs: response)
    )
    region = _region("describe", with_context=False)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }

    first = AnthropicVisionProvider(
        client,
        model="synthetic",
        prompt_schema_version="unchanged-caller-version",
        renderer_semantics={"fixed_template": "renderer-a"},
    ).interpret(region, schema, ExecutionContext())
    second = AnthropicVisionProvider(
        client,
        model="synthetic",
        prompt_schema_version="unchanged-caller-version",
        renderer_semantics={"fixed_template": "renderer-b"},
    ).interpret(region, schema, ExecutionContext())

    assert first.prompt_schema_version == second.prompt_schema_version
    assert first.prompt_renderer_identity != second.prompt_renderer_identity
    assert first.prompt_identity != second.prompt_identity
    assert first.cache_key != second.cache_key


def test_anthropic_sdk_request_uses_strict_schema_with_synthetic_transport():
    import anthropic
    import httpx2

    from tychora.adapters.vision.anthropic import AnthropicVisionProvider
    from tychora.contracts.base import FrozenJsonMap

    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_synthetic",
                "type": "message",
                "role": "assistant",
                "model": "explicit-synthetic-model",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "target_id": None,
                                "literal_transcription": None,
                                "interpretation": {"summary": "synthetic"},
                                "uncertainties": [],
                            }
                        ),
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http = httpx2.Client(
        transport=httpx2.MockTransport(respond),
        timeout=httpx2.Timeout(5),
        trust_env=False,
        follow_redirects=False,
    )
    client = anthropic.Anthropic(
        api_key="synthetic-key",
        base_url="https://api.anthropic.com",
        max_retries=0,
        http_client=http,
    )
    try:
        observation = AnthropicVisionProvider(
            client,
            model="explicit-synthetic-model",
            prompt_schema_version="vision-response-v1",
        ).interpret(
            _region("describe"),
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            ExecutionContext(),
        )
    finally:
        client.close()
    assert observation.interpretation == FrozenJsonMap({"summary": "synthetic"})
    assert len(bodies) == 1
    assert bodies[0]["model"] == "explicit-synthetic-model"
    assert bodies[0]["output_config"]["format"]["type"] == "json_schema"
    assert bodies[0]["messages"][0]["content"][0]["type"] == "image"
    prompt = bodies[0]["messages"][0]["content"][1]["text"]
    assert '"literal_text":"Candidate paragraph"' in prompt
    assert '"target_id":"paragraph-1"' in prompt


def test_remote_candidate_context_is_rendered_canonically():
    from types import SimpleNamespace

    from tychora.adapters.vision.anthropic import AnthropicVisionProvider
    from tychora.contracts.identity import canonical_bytes

    calls = []
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {
                        "target_id": None,
                        "literal_transcription": None,
                        "interpretation": None,
                        "uncertainties": [],
                    }
                ),
            )
        ],
    )

    def create(**kwargs):
        calls.append(kwargs)
        return response

    provider = AnthropicVisionProvider(
        type(
            "Client",
            (),
            {
                "messages": type(
                    "Messages", (), {"create": lambda _self, **kw: create(**kw)}
                )()
            },
        )(),
        model="synthetic",
        prompt_schema_version="vision-response-v1",
    )
    region = _region("describe")
    provider.interpret(region, {"type": "object"}, ExecutionContext())

    expected = canonical_bytes(
        [item.model_dump(mode="json") for item in region.candidate_context]
    ).decode("utf-8")
    prompt = calls[0]["messages"][0]["content"][1]["text"]
    assert prompt.endswith("Candidate context: " + expected)


@pytest.mark.parametrize(
    ("offline", "capabilities", "model"),
    [
        (True, frozenset({"vision"}), "claude-sonnet-4-5"),
        (False, frozenset(), "claude-sonnet-4-5"),
        (False, frozenset({"vision"}), ""),
    ],
)
def test_remote_client_is_not_constructed_before_capability_checks(
    offline: bool, capabilities: frozenset[str], model: str
):
    from tychora.adapters.vision.anthropic import create_provider

    calls = []
    with pytest.raises(TychoraError) as err:
        create_provider(
            offline=offline,
            capabilities=capabilities,
            model=model,
            api_key="unused",
            client_factory=lambda **kwargs: calls.append(kwargs),
        )
    assert err.value.code == "CAPABILITY_MISSING"
    assert calls == []


def test_local_ocr_success_causes_zero_remote_requests():
    from tychora.contracts.identity import digest
    from tychora.ports.execution import ExecutionContext
    from tychora.ports.vision import (
        DerivedObservation,
        selective_interpret,
        vision_cache_key,
        vision_prompt_identity,
    )

    region = _region("ocr")
    task_schema = {"type": "null"}
    prompt_version = "local-ocr-v1"
    prompt_identity = vision_prompt_identity(prompt_version, task_schema)
    local = DerivedObservation(
        cache_key=vision_cache_key(
            region.image_sha256,
            region.task,
            "rapidocr-3.9.2-pp-ocrv6",
            prompt_identity,
            region.context_sha256,
        ),
        task="ocr",
        image_sha256=region.image_sha256,
        original_image_sha256=region.original_image_sha256,
        original_pixel_sha256=region.original_pixel_sha256,
        region_pixel_sha256=region.region_pixel_sha256,
        context_sha256=region.context_sha256,
        literal_transcription="Literal source text",
        interpretation=None,
        uncertainties=(),
        model="rapidocr-3.9.2-pp-ocrv6",
        prompt_schema_version=prompt_version,
        task_schema_sha256=digest("vision-task-schema-v1", task_schema),
        prompt_identity=prompt_identity,
    )

    class Local:
        def interpret(self, region, task_schema, ctx):
            return local

    def remote() -> DerivedObservation:
        remote_calls.append("requested")
        return local

    remote_calls = []
    result = selective_interpret(
        region,
        task_schema,
        Local(),
        remote,
        ExecutionContext(),
    )

    assert result == local
    assert remote_calls == []


def test_local_ocr_rejects_unqualified_runtime_versions(tmp_path: Path, monkeypatch):
    from tychora.adapters.vision import local
    from tychora.application.owner import WorkspaceOwner

    monkeypatch.setattr(
        local,
        "_verify_models",
        lambda _root: tuple(tmp_path / name for name, _size, _sha in local._MODELS),
    )
    monkeypatch.setattr(
        local,
        "version",
        lambda package: "0.0.0" if package == "onnxruntime" else "3.9.2",
    )

    with pytest.raises(TychoraError) as error:
        local.LocalOcrProvider(cast(WorkspaceOwner, None), tmp_path)

    assert error.value.code == "CAPABILITY_MISSING"


@pytest.mark.asyncio
async def test_first_source_capture_is_registered_and_reauthorized_after_queue(
    tmp_path: Path,
    monkeypatch,
):
    import asyncio
    import threading
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image, ImageOps

    from tests.helpers.corpus import COLLECTION, reader, seed_bundle
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts import SourceInput, identity
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.corpus import RevisionRow, SourceRow
    from tychora.contracts.evidence import Geometry
    from tychora.contracts.requests import RequestScope
    from tychora.ingestion.acquire import Acquirer
    from tychora.ports.vision import (
        CandidateContext,
        CapturedOriginalRegionProof,
        CapturedRegionAuthorization,
        CapturedSourceRegistration,
        RegionInput,
        pixel_identity,
    )

    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    config = config.model_copy(
        update={"values": config.values.model_copy(update={"offline": False})}
    )
    scope = RequestScope(
        principal_id="operator",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=3,
        uses=frozenset({"lookup"}),
        capabilities=frozenset({"manage", "vision"}),
        upload_providers=frozenset({"synthetic", "anthropic"}),
    )
    image = Image.new("RGB", (8, 6), "blue")
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    payload = encoded.getvalue()
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        bundle = seed_bundle()
        for ref in bundle.artifacts:
            with reader().open_verified(ref) as data:
                owner.artifacts.put_stream(data, ref.media_type)
        created = await owner.run_blocking(
            lambda: owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0],
            ExecutionContext(),
        )
        bundle = bundle.model_copy(
            update={
                "workspace": bundle.workspace.model_copy(
                    update={"workspace_id": owner.workspace_id, "created_at": created}
                ),
                "collection": bundle.collection.model_copy(
                    update={
                        "workspace_id": owner.workspace_id,
                        "policy_json": FrozenJsonMap(
                            {
                                "schema_version": 1,
                                "upload_providers": ["anthropic", "synthetic"],
                            }
                        ),
                    }
                ),
            }
        )
        await owner.run_blocking(
            lambda: owner.store.commit_extraction(bundle), ExecutionContext()
        )
        metadata = bundle.revision.metadata_json
        source_key = "new-image.png"
        source_id = identity.source_id(UUID(COLLECTION), source_key)
        metadata_sha = identity.digest("metadata-v1", metadata)
        source = SourceRow(
            source_id=source_id,
            collection_id=COLLECTION,
            source_key=source_key,
            registration_json=FrozenJsonMap(
                {
                    "schema_version": 1,
                    "kind_id": "manual@1",
                }
            ),
            created_at=created,
        )
        registration = CapturedSourceRegistration(source=source, kind_id="manual@1")
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.register_captured_source(
                    registration,
                    scope.model_copy(update={"capabilities": frozenset()}),
                    policy_epoch=0,
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        await owner.run_blocking(
            lambda: owner.upload_policy.register_captured_source(
                registration, scope, policy_epoch=0
            ),
            ExecutionContext(),
        )
        captured = await owner.run_blocking(
            lambda: Acquirer(owner.artifacts, config).capture(
                SourceInput.model_validate(
                    {
                        "type": "bytes",
                        "data": payload,
                        "format": "png",
                        "media_type": "image/png",
                        "kind": "manual@1",
                        "metadata": metadata,
                        "collection_id": UUID(COLLECTION),
                        "source_key": source_key,
                    }
                ),
                ExecutionContext(),
            ),
            ExecutionContext(),
        )
        pixels = image.tobytes()
        region_transform = "exif-transpose-rgb-v1:crop:0,0,8,6"
        region = RegionInput(
            source_native_ref="#/original",
            image=payload,
            image_ref=captured.original_ref,
            media_type="image/png",
            image_sha256=sha256(payload).hexdigest(),
            original_image_sha256=captured.original_sha,
            original_pixel_sha256=pixel_identity(
                pixels, 8, 6, "RGB", "exif-transpose-rgb-v1"
            ),
            region_pixel_sha256=pixel_identity(pixels, 8, 6, "RGB", region_transform),
            original_width=8,
            original_height=6,
            width=8,
            height=6,
            color_mode="RGB",
            original_pixel_transform="exif-transpose-rgb-v1",
            region_pixel_transform=region_transform,
            geometry=Geometry(domain="pixels", x0=0, y0=0, x1=8, y1=6),
            context_sha256=identity.digest("vision-context-v2", []),
            task="describe",
            dependency_ids=frozenset({source_id}),
        )
        revision = RevisionRow(
            revision_id=identity.revision_id(
                source_id, captured.original_sha, "1", metadata_sha
            ),
            source_id=source_id,
            kind_id="manual@1",
            original_sha=captured.original_sha,
            edition_key="1",
            metadata_sha=metadata_sha,
            title=str(metadata["title"]),
            metadata_json=metadata,
            captured_at=captured.captured_at.isoformat(),
            allowed_uses_json=("lookup",),
        )
        authorization = CapturedRegionAuthorization(
            registration=registration,
            revision=revision,
            proof=CapturedOriginalRegionProof(capture=captured),
            region=region,
        )
        await owner.run_blocking(
            lambda: owner.upload_policy.authorize_captured(
                scope, "synthetic", authorization, 0
            ),
            ExecutionContext(),
        )
        jpeg_output = BytesIO()
        image.save(jpeg_output, format="JPEG", quality=90)
        jpeg_payload = jpeg_output.getvalue()
        jpeg_capture = await owner.run_blocking(
            lambda: Acquirer(owner.artifacts, config).capture(
                SourceInput.model_validate(
                    {
                        "type": "bytes",
                        "data": jpeg_payload,
                        "format": "jpeg",
                        "media_type": "image/jpeg",
                        "kind": "manual@1",
                        "metadata": metadata,
                        "collection_id": UUID(COLLECTION),
                        "source_key": source_key,
                    }
                ),
                ExecutionContext(),
            ),
            ExecutionContext(),
        )
        with Image.open(BytesIO(jpeg_payload)) as encoded_jpeg:
            jpeg_pixels = ImageOps.exif_transpose(encoded_jpeg).convert("RGB")
            jpeg_pixels.load()
        crop = jpeg_pixels.crop((1, 1, 7, 5))
        crop_output = BytesIO()
        crop.save(crop_output, format="PNG")
        crop_payload = crop_output.getvalue()
        crop_ref = await owner.run_blocking(
            lambda: owner.artifacts.put_stream(BytesIO(crop_payload), "image/png"),
            ExecutionContext(),
        )
        crop_transform = "exif-transpose-rgb-v1:crop:1,1,7,5"
        jpeg_region = RegionInput(
            source_native_ref="#/original",
            image=crop_payload,
            image_ref=crop_ref,
            media_type="image/png",
            image_sha256=crop_ref.sha256,
            original_image_sha256=jpeg_capture.original_sha,
            original_pixel_sha256=pixel_identity(
                jpeg_pixels.tobytes(), 8, 6, "RGB", "exif-transpose-rgb-v1"
            ),
            region_pixel_sha256=pixel_identity(
                crop.tobytes(), 6, 4, "RGB", crop_transform
            ),
            original_width=8,
            original_height=6,
            width=6,
            height=4,
            color_mode="RGB",
            original_pixel_transform="exif-transpose-rgb-v1",
            region_pixel_transform=crop_transform,
            geometry=Geometry(domain="pixels", x0=1, y0=1, x1=7, y1=5),
            context_sha256=identity.digest("vision-context-v2", []),
            task="describe",
            dependency_ids=frozenset({source_id}),
        )
        jpeg_revision = revision.model_copy(
            update={
                "revision_id": identity.revision_id(
                    source_id, jpeg_capture.original_sha, "3", metadata_sha
                ),
                "original_sha": jpeg_capture.original_sha,
                "edition_key": "3",
            }
        )
        await owner.run_blocking(
            lambda: owner.upload_policy.authorize_captured(
                scope,
                "synthetic",
                CapturedRegionAuthorization(
                    registration=registration,
                    revision=jpeg_revision,
                    proof=CapturedOriginalRegionProof(capture=jpeg_capture),
                    region=jpeg_region,
                ),
                0,
            ),
            ExecutionContext(),
        )
        from tychora.adapters.vision import anthropic as anthropic_adapter

        sequence = []
        original_create_provider = anthropic_adapter.create_provider
        original_authorize = owner.upload_policy.authorize_captured

        def authorize(*args, **kwargs):
            original_authorize(*args, **kwargs)
            sequence.append("reauthorized")

        monkeypatch.setattr(owner.upload_policy, "authorize_captured", authorize)

        class Provider:
            def interpret(self, selected, task_schema, _ctx):
                sequence.append("interpreted")
                return anthropic_adapter.parse_response(
                    json.dumps(
                        {
                            "target_id": None,
                            "literal_transcription": None,
                            "interpretation": {"summary": "synthetic"},
                            "uncertainties": [],
                        }
                    ),
                    selected,
                    "synthetic-model",
                    "vision-response-v1",
                    task_schema,
                )

            def close(self):
                sequence.append("closed")

        def construct(**_kwargs):
            sequence.append("constructed")
            return Provider()

        monkeypatch.setattr(anthropic_adapter, "create_provider", construct)
        dispatched = await anthropic_adapter.dispatch_captured_vision(
            owner,
            authorization,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            ExecutionContext(),
            scope=scope,
            policy_epoch=0,
            model="synthetic-model",
            api_key="synthetic-key",
        )
        assert dispatched.interpretation == FrozenJsonMap({"summary": "synthetic"})
        assert sequence[:4] == [
            "reauthorized",
            "constructed",
            "interpreted",
            "closed",
        ]
        canonical_unit = bundle.units[0]
        approved_context = CandidateContext(
            target_id=canonical_unit.unit_id,
            source_text_sha256=canonical_unit.text_sha,
            char_start=0,
            char_end=len(canonical_unit.text),
            literal_text=canonical_unit.text,
        )
        approved_region = RegionInput.model_validate(
            {
                **region.model_dump(),
                "context_sha256": identity.digest(
                    "vision-context-v2",
                    [approved_context.model_dump(mode="json")],
                ),
                "dependency_ids": frozenset({source_id, canonical_unit.unit_id}),
                "allowed_candidate_target_ids": frozenset({canonical_unit.unit_id}),
                "candidate_context": (approved_context,),
            }
        )
        approved_authorization = authorization.model_copy(
            update={"region": approved_region}
        )

        def context_client_factory(**_kwargs):
            sequence.append("constructed")
            raise AssertionError("foreign context must not construct a client")

        monkeypatch.setattr(
            anthropic_adapter, "create_provider", original_create_provider
        )
        sequence.clear()
        with pytest.raises(TychoraError) as error:
            await anthropic_adapter.dispatch_captured_vision(
                owner,
                approved_authorization,
                {"type": "object"},
                ExecutionContext(),
                scope=scope,
                policy_epoch=0,
                model="synthetic-model",
                api_key="synthetic-key",
                client_factory=context_client_factory,
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        assert sequence == []
        changed_metadata = FrozenJsonMap(
            {**metadata, "title": "Changed revision title"}
        )
        changed_capture = await owner.run_blocking(
            lambda: Acquirer(owner.artifacts, config).capture(
                SourceInput.model_validate(
                    {
                        "type": "bytes",
                        "data": payload,
                        "format": "png",
                        "media_type": "image/png",
                        "kind": "manual@1",
                        "metadata": changed_metadata,
                        "collection_id": UUID(COLLECTION),
                        "source_key": source_key,
                    }
                ),
                ExecutionContext(),
            ),
            ExecutionContext(),
        )
        changed_metadata_sha = identity.digest("metadata-v1", changed_metadata)
        changed_revision = revision.model_copy(
            update={
                "revision_id": identity.revision_id(
                    source_id, changed_capture.original_sha, "2", changed_metadata_sha
                ),
                "edition_key": "2",
                "metadata_sha": changed_metadata_sha,
                "metadata_json": changed_metadata,
                "title": changed_metadata["title"],
            }
        )
        await owner.run_blocking(
            lambda: owner.upload_policy.authorize_captured(
                scope,
                "synthetic",
                authorization.model_copy(
                    update={
                        "revision": changed_revision,
                        "proof": CapturedOriginalRegionProof(capture=changed_capture),
                    }
                ),
                0,
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_captured(
                    scope, "synthetic", authorization, 1
                ),
                ExecutionContext(),
            )
        assert error.value.code == "SCOPE_CHANGED"
        bad_region = region.model_copy(update={"region_pixel_sha256": "f" * 64})
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_captured(
                    scope,
                    "synthetic",
                    authorization.model_copy(update={"region": bad_region}),
                    0,
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        unknown = "unknown-context-unit"
        unknown_context = CandidateContext(
            target_id=unknown,
            source_text_sha256="7437a75f545b93a075acafbefb85316e6f90dbacba2512f34a131d54fea8dfb4",
            char_start=0,
            char_end=15,
            literal_text="Unknown context",
        )
        context_region = RegionInput.model_validate(
            {
                **region.model_dump(),
                "context_sha256": identity.digest(
                    "vision-context-v2",
                    [unknown_context.model_dump(mode="json")],
                ),
                "dependency_ids": frozenset({source_id, unknown}),
                "allowed_candidate_target_ids": frozenset({unknown}),
                "candidate_context": (unknown_context,),
            }
        )
        with pytest.raises(ValidationError, match="candidate source"):
            CapturedRegionAuthorization(
                registration=registration,
                revision=revision,
                proof=CapturedOriginalRegionProof(capture=captured),
                region=context_region,
            )
        with pytest.raises(ValidationError, match="candidate source"):
            CapturedRegionAuthorization(
                registration=registration,
                revision=revision,
                proof=CapturedOriginalRegionProof(
                    capture=captured.model_copy(
                        update={"source_key": "different-image.png"}
                    )
                ),
                region=region,
            )
        await owner.run_blocking(
            lambda: owner.db.execute(
                "UPDATE collections SET policy_json='{}' WHERE collection_id=?",
                (COLLECTION,),
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_captured(
                    scope, "synthetic", authorization, 0
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        await owner.run_blocking(
            lambda: owner.db.execute(
                "UPDATE collections SET policy_json=? WHERE collection_id=?",
                (
                    '{"schema_version":1,"upload_providers":["anthropic","synthetic"]}',
                    COLLECTION,
                ),
            ),
            ExecutionContext(),
        )
        await owner.run_blocking(
            lambda: owner.db.execute(
                "UPDATE sources SET withdrawn_at='2026-01-01T00:00:00Z' "
                "WHERE source_id=?",
                (source_id,),
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_captured(
                    scope, "synthetic", authorization, 0
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        await owner.run_blocking(
            lambda: owner.db.execute(
                "UPDATE sources SET withdrawn_at=NULL WHERE source_id=?", (source_id,)
            ),
            ExecutionContext(),
        )
        await owner.run_blocking(
            lambda: owner.db.execute(
                "DELETE FROM sources WHERE source_id=?", (source_id,)
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as error:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_captured(
                    scope, "synthetic", authorization, 0
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        await owner.run_blocking(
            lambda: owner.upload_policy.register_captured_source(
                registration, scope, policy_epoch=0
            ),
            ExecutionContext(),
        )
        release = threading.Event()
        started: list[bool] = []
        lock = threading.Lock()

        def provider_call() -> str:
            with lock:
                started.append(True)
            assert release.wait(5)
            return "authorized"

        tasks = [
            asyncio.create_task(
                owner.run_captured_provider(
                    "synthetic",
                    provider_call,
                    ExecutionContext(),
                    scope=scope,
                    authorization=authorization,
                    policy_epoch=0,
                )
            )
            for _ in range(3)
        ]
        try:
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(started) == 2
            await owner.run_blocking(
                lambda: owner.store.withdraw_source(source_id), ExecutionContext()
            )
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert results[:2] == ["authorized", "authorized"]
            assert isinstance(results[2], TychoraError)
            assert results[2].code == "SCOPE_CHANGED"
            assert len(started) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.qualification
@pytest.mark.parametrize(
    ("format", "media_type"), [("png", "image/png"), ("jpeg", "image/jpeg")]
)
def test_registered_image_keeps_pixel_owner_docling_json_and_sql(
    tmp_path: Path,
    format: Literal["png", "jpeg"],
    media_type: Literal["image/png", "image/jpeg"],
):
    from io import BytesIO

    model_root = os.environ.get("TYCHORA_TEST_DOCLING_MODELS")
    ocr_root = os.environ.get("TYCHORA_TEST_OCR_MODELS")
    if not model_root or not ocr_root:
        pytest.skip("requires explicitly provisioned Docling and OCR model bundles")
    from PIL import Image, ImageDraw, ImageFont

    from tychora.adapters.docling.parser import DoclingParser
    from tychora.adapters.vision.local import LocalOcrProvider
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.registry import Registry
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts import SourceInput, identity
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.composition import DocumentCompositionPlan
    from tychora.contracts.corpus import (
        CollectionRow,
        RevisionRow,
        SourceRow,
        WorkspaceRow,
    )
    from tychora.contracts.documents import validate_document_assets
    from tychora.contracts.extraction import DocumentRegistration
    from tychora.contracts.requests import RequestScope
    from tychora.ingestion.acquire import Acquirer
    from tychora.ingestion.composition import compose_document
    from tychora.ingestion.normalize import normalize
    from tychora.ingestion.vision import planned_vision_proxy
    from tychora.ports.vision import (
        RegionInput,
        pixel_identity,
    )

    pack = tmp_path / "pack"
    shutil.copytree(Path(__file__).parents[2] / "src/tychora/profiles/documents", pack)
    (pack / "profile.toml").write_text(
        (pack / "profile.toml").read_text() + '\n[extraction]\nparser="docling"\n'
    )
    image_path = tmp_path / ("source." + format)
    image = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(image).text(
        (35, 55), "SOURCE IMAGE 123", fill="black", font=ImageFont.load_default(size=42)
    )
    image.save(image_path, format="JPEG" if format == "jpeg" else "PNG")
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'schema_version=1\n[profiles.test]\npath="pack"\n[configuration.acquisition]\nroots='
        + json.dumps([str(tmp_path)])
        + "\n"
    )
    config = resolve_config(config_path, "test", {})
    ctx = ExecutionContext()
    with WorkspaceOwner.open(
        tmp_path / "workspace", config, RequestScope(principal_id="fixture")
    ) as owner:
        collection = CollectionRow(
            collection_id="00000000-0000-4000-8000-000000000010",
            workspace_id=owner.workspace_id,
            slug="synthetic",
            title="Synthetic images",
        )
        metadata = FrozenJsonMap(
            json.loads(
                (
                    Path(__file__).parents[1]
                    / "configuration/fixtures/generic-records.json"
                ).read_text()
            )["manual@1"]["metadata"]
        )
        captured = owner.run_blocking_sync(
            lambda: Acquirer(owner.artifacts, config).capture(
                SourceInput.model_validate(
                    {
                        "type": "file",
                        "path": str(image_path),
                        "format": format,
                        "media_type": media_type,
                        "kind": "manual@1",
                        "metadata": metadata,
                        "collection_id": UUID(collection.collection_id),
                        "source_key": image_path.name,
                    }
                ),
                ctx,
            ),
            ctx,
        )
        sid = identity.source_id(UUID(collection.collection_id), image_path.name)
        metadata_sha = identity.digest("metadata-v1", metadata)
        revision = RevisionRow(
            revision_id=identity.revision_id(
                sid, captured.original_sha, "1", metadata_sha
            ),
            source_id=sid,
            kind_id="manual@1",
            original_sha=captured.original_sha,
            edition_key="1",
            metadata_sha=metadata_sha,
            title=str(metadata["title"]),
            metadata_json=metadata,
            captured_at=captured.captured_at.isoformat(),
            allowed_uses_json=("lookup",),
        )
        registration = DocumentRegistration(
            capture=captured,
            workspace=WorkspaceRow(
                workspace_id=owner.workspace_id,
                created_at=owner.run_blocking_sync(
                    lambda: owner.db.execute(
                        "SELECT created_at FROM workspaces"
                    ).fetchone()[0],
                    ctx,
                ),
            ),
            collection=collection,
            source=SourceRow(
                source_id=sid,
                collection_id=collection.collection_id,
                source_key=image_path.name,
                created_at=captured.captured_at.isoformat(),
            ),
            revision=revision,
            format=format,
        )
        with pytest.raises(ValidationError):
            DocumentRegistration(
                capture=captured,
                workspace=registration.workspace,
                collection=collection,
                source=registration.source,
                revision=revision,
                format="jpeg" if format == "png" else "png",
            )
        bad_media = captured.model_copy(
            update={
                "source": FrozenJsonMap(
                    {
                        **captured.source,
                        "media_type": "image/jpeg"
                        if media_type == "image/png"
                        else "image/png",
                    }
                )
            }
        )
        with pytest.raises(ValidationError):
            DocumentRegistration(
                capture=bad_media,
                workspace=registration.workspace,
                collection=collection,
                source=registration.source,
                revision=revision,
                format=format,
            )
        parsed = DoclingParser(owner, registration, Path(model_root)).extract(
            captured.original_ref, config, ctx
        )
        assert parsed.original_image is not None
        assert parsed.document_json is not None
        from tychora.contracts.extraction import extraction_document

        changed = parsed.model_dump(
            mode="json", exclude={"raw_document_ref", "document_json"}
        )
        changed["registry_digest"] = "f" * 64
        changed_raw = extraction_document(changed)
        changed_ref = owner.artifacts.put_stream(
            BytesIO(changed_raw), "application/json"
        )
        changed_parsed = parsed.model_copy(
            update={"registry_digest": "f" * 64, "raw_document_ref": changed_ref}
        )
        with pytest.raises(TychoraError, match="registry binding"):
            normalize(
                changed_parsed,
                revision.revision_id,
                Registry.from_config(config),
            )
        with pytest.raises(ValueError, match="pixel"):
            validate_document_assets(
                parsed.document_json,
                cast(tuple, parsed.units),
                parsed.assets,
                parsed.original_ref,
                registration.source.source_key,
                parsed.original_image.model_copy(update={"pixel_sha256": "f" * 64}),
            )
        foreign = ArtifactRef(
            sha256="f" * 64,
            storage_key="f" * 64,
            size_bytes=parsed.original_image.artifact_ref.size_bytes,
            media_type=parsed.original_image.artifact_ref.media_type,
        )
        foreign_owner = parsed.original_image.model_copy(
            update={
                "artifact_ref": foreign,
                "encoded_sha256": foreign.sha256,
                "locator": parsed.original_image.locator.model_copy(
                    update={"artifact_ref": foreign.sha256}
                ),
            }
        )
        with pytest.raises(ValueError, match="retained image"):
            validate_document_assets(
                parsed.document_json,
                cast(tuple, parsed.units),
                parsed.assets,
                parsed.original_ref,
                registration.source.source_key,
                foreign_owner,
            )
        bundle = normalize(parsed, revision.revision_id, Registry.from_config(config))
        original = next(
            unit for unit in bundle.units if unit.unit_type == "image_original"
        )
        with Image.open(BytesIO(image_path.read_bytes())) as decoded:
            region_pixels = decoded.convert("RGB").tobytes()
        region_transform = (
            "exif-transpose-rgb-v1:crop:0,0,"
            f"{parsed.original_image.width},{parsed.original_image.height}"
        )
        region = RegionInput(
            source_native_ref=parsed.original_image.native_ref,
            image=image_path.read_bytes(),
            image_ref=captured.original_ref,
            media_type=media_type,
            image_sha256=captured.original_sha,
            original_image_sha256=captured.original_sha,
            original_pixel_sha256=parsed.original_image.pixel_sha256,
            region_pixel_sha256=pixel_identity(
                region_pixels,
                image.width,
                image.height,
                "RGB",
                region_transform,
            ),
            original_width=image.width,
            original_height=image.height,
            width=image.width,
            height=image.height,
            color_mode="RGB",
            original_pixel_transform=parsed.original_image.pixel_transform,
            region_pixel_transform=region_transform,
            geometry=Geometry(
                domain="pixels", x0=0, y0=0, x1=image.width, y1=image.height
            ),
            context_sha256=identity.digest("vision-context-v2", []),
            task="ocr",
            dependency_ids=frozenset({original.unit_id}),
        )
        observation = LocalOcrProvider(owner, Path(ocr_root)).interpret(
            region, {"type": "null"}, ctx
        )
        plan = DocumentCompositionPlan(
            base_normalizer_fingerprint=parsed.normalizer_fingerprint,
            proxies=(
                planned_vision_proxy(
                    region,
                    observation,
                    asset_native_ref=parsed.assets[0].native_ref,
                ),
            ),
        )
        enriched = owner.run_blocking_sync(
            lambda: compose_document(
                parsed, Registry.from_config(config), plan, owner.artifacts, ctx
            ),
            ctx,
        )
        bundle = enriched.bundle
        assert bundle.extraction.extraction_id != original.extraction_id
        original = next(
            unit for unit in bundle.units if unit.unit_type == "image_original"
        )
        owner.run_blocking_sync(lambda: owner.store.commit_extraction(bundle), ctx)
        proxy = next(unit for unit in bundle.units if unit.unit_type == "asset_proxy")
        assert original.text == "" and original.text_origin == "source"
        locator = cast(ImageLocator, original.locators[0])
        assert original.citable and locator.physical_page is None
        assert locator.geometry is not None and locator.geometry.domain == "pixels"
        assert proxy.text_origin == "ocr" and not proxy.citable
        assert proxy.review_state == "needs_review"
        assert proxy.text == observation.literal_transcription
        assert bundle.proxies[0].required_unit_ids == frozenset({original.unit_id})
        assert parsed.document_json is not None and parsed.docling_json_ref is not None
        assert parsed.original_image.pixel_sha256 == original.attributes["pixel_sha256"]
        assert owner.run_blocking_sync(
            lambda: owner.store.hydrate((original.unit_id,)), ctx
        ) == (original,)


@pytest.mark.qualification
def test_first_pdf_context_is_authorized_from_raw_selector_and_replays(
    document_record_case,
):
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image, ImageOps
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from tychora.adapters.vision.anthropic import dispatch_captured_vision
    from tychora.contracts import identity
    from tychora.contracts.base import FrozenJsonMap
    from tychora.contracts.composition import (
        DocumentCompositionPlan,
        SourceContextSelection,
        SourceSelector,
        source_context_identity,
    )
    from tychora.contracts.evidence import Geometry
    from tychora.contracts.requests import RequestScope
    from tychora.ingestion.composition import (
        compose_document,
        replay_composed_document,
    )
    from tychora.ingestion.vision import planned_vision_proxy
    from tychora.ports.vision import (
        CapturedRegionAuthorization,
        CapturedSourceRegistration,
        RegionInput,
        RetainedDocumentRegionProof,
        pixel_identity,
        vision_context_identity,
    )

    def mixed_pdf(path: Path, _content) -> None:
        page = canvas.Canvas(str(path), pagesize=(612, 792), invariant=1)
        page.setFont("Helvetica", 15)
        page.drawString(72, 700, "Native source paragraph remains authoritative.")
        page.showPage()
        scan = Image.new("RGB", (600, 140), "white")
        encoded = BytesIO()
        scan.save(encoded, format="PNG")
        page.drawImage(
            ImageReader(BytesIO(encoded.getvalue())),
            36,
            300,
            width=540,
            height=126,
        )
        page.showPage()
        page.save()

    collection_id = "00000000-0000-4000-8000-000000000010"
    scope = RequestScope(
        principal_id="operator",
        collection_ids=frozenset({collection_id}),
        access_ceiling=3,
        uses=frozenset({"lookup"}),
        capabilities=frozenset({"manage", "vision"}),
        upload_providers=frozenset({"anthropic"}),
    )
    case = document_record_case(
        "standard@1",
        "pdf",
        source_builder=mixed_pdf,
        commit=False,
        online=True,
        scope=scope,
    )
    parsed = case.parsed
    owner = case.owner
    registration = parsed.registration
    owner.run_blocking_sync(
        lambda: (
            owner.db.execute(
                "INSERT INTO collections "
                "(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    collection_id,
                    owner.workspace_id,
                    "synthetic",
                    "Synthetic documents",
                    0,
                    '{"schema_version":1,"upload_providers":["anthropic"]}',
                ),
            ),
            owner.db.commit(),
        ),
        ExecutionContext(),
    )
    captured_registration = CapturedSourceRegistration(
        source=registration.source.model_copy(
            update={
                "registration_json": FrozenJsonMap(
                    {"schema_version": 1, "kind_id": registration.revision.kind_id}
                )
            }
        ),
        kind_id=registration.revision.kind_id,
    )
    owner.run_blocking_sync(
        lambda: owner.upload_policy.register_captured_source(
            captured_registration, scope, policy_epoch=0
        ),
        ExecutionContext(),
    )
    context_item = next(
        item
        for item in parsed.units
        if isinstance(item.raw_item.get("text"), str)
        and "Native source paragraph" in item.raw_item["text"]
    )
    source_text = context_item.raw_item["text"]
    selector = SourceSelector(
        native_ref=context_item.native_ref,
        field="text",
        source_start=0,
        source_end=len(source_text),
    )
    selected = SourceContextSelection.bind(
        parsed.original_ref.sha256, selector, source_text
    )
    candidate = selected.candidate_context()
    parsed_page = next(
        asset
        for asset in parsed.assets
        if asset.asset_type == "page" and asset.locator.physical_page == 2
    )
    with owner.artifacts.open_verified(parsed_page.artifact_ref) as stream:
        page_bytes = stream.read()
    with Image.open(BytesIO(page_bytes)) as encoded_page:
        pixels = ImageOps.exif_transpose(encoded_page).convert("RGB")
        pixels.load()
    original_transform = "exif-transpose-rgb-v1"
    region_transform = f"{original_transform}:crop:0,0,{pixels.width},{pixels.height}"
    region_data = {
        "source_native_ref": parsed_page.native_ref,
        "image": page_bytes,
        "image_ref": parsed_page.artifact_ref,
        "media_type": parsed_page.artifact_ref.media_type,
        "image_sha256": sha256(page_bytes).hexdigest(),
        "original_image_sha256": parsed_page.artifact_ref.sha256,
        "original_pixel_sha256": pixel_identity(
            pixels.tobytes(),
            pixels.width,
            pixels.height,
            "RGB",
            original_transform,
        ),
        "region_pixel_sha256": pixel_identity(
            pixels.tobytes(),
            pixels.width,
            pixels.height,
            "RGB",
            region_transform,
        ),
        "original_width": pixels.width,
        "original_height": pixels.height,
        "width": pixels.width,
        "height": pixels.height,
        "color_mode": "RGB",
        "original_pixel_transform": original_transform,
        "region_pixel_transform": region_transform,
        "geometry": Geometry(
            domain="pixels", x0=0, y0=0, x1=pixels.width, y1=pixels.height
        ),
        "context_sha256": vision_context_identity((candidate,)),
        "task": "describe",
        "allowed_candidate_target_ids": frozenset({selected.target_id}),
        "dependency_ids": frozenset(
            {registration.source.source_id, selected.target_id}
        ),
        "candidate_context": (candidate,),
        "source_context": (selected,),
    }
    region = RegionInput.model_validate(region_data)
    proof = RetainedDocumentRegionProof(
        capture=registration.capture,
        extraction=parsed,
        asset_native_ref=parsed_page.native_ref,
    )
    authorization = CapturedRegionAuthorization(
        registration=captured_registration,
        revision=registration.revision,
        proof=proof,
        region=region,
    )
    assert (
        owner.run_blocking_sync(
            lambda: owner.db.execute("SELECT count(*) FROM evidence_units").fetchone()[
                0
            ],
            ExecutionContext(),
        )
        == 0
    )

    missing_selector = SourceSelector(
        native_ref="#/texts/missing",
        field="text",
        source_start=0,
        source_end=len(source_text),
    )
    out_of_bounds = selector.model_copy(update={"source_end": len(source_text) + 1})
    forged_selections = (
        SourceContextSelection.bind("f" * 64, selector, source_text),
        SourceContextSelection.bind(
            parsed.original_ref.sha256, selector, "X" * len(source_text)
        ),
        SourceContextSelection.bind(
            parsed.original_ref.sha256, out_of_bounds, source_text + "X"
        ),
        SourceContextSelection.bind(
            parsed.original_ref.sha256, missing_selector, source_text
        ),
    )
    import asyncio

    for index, forged in enumerate(forged_selections):
        forged_candidate = forged.candidate_context()
        forged_region = RegionInput.model_validate(
            {
                **region_data,
                "context_sha256": vision_context_identity((forged_candidate,)),
                "allowed_candidate_target_ids": frozenset({forged.target_id}),
                "dependency_ids": frozenset(
                    {registration.source.source_id, forged.target_id}
                ),
                "candidate_context": (forged_candidate,),
                "source_context": (forged,),
            }
        )
        with pytest.raises(TychoraError) as error:
            owner.run_blocking_sync(
                lambda forged_region=forged_region: (
                    owner.upload_policy.authorize_captured(
                        scope,
                        "anthropic",
                        authorization.model_copy(update={"region": forged_region}),
                        0,
                    )
                ),
                ExecutionContext(),
            )
        assert error.value.code == "FORBIDDEN_SCOPE"
        if index == 1:
            constructed = []

            def reject_client_factory(*, _constructed=constructed, **_kwargs):
                _constructed.append(True)
                raise AssertionError("forged raw context must not construct a client")

            with pytest.raises(TychoraError) as dispatched_error:
                asyncio.run(
                    dispatch_captured_vision(
                        owner,
                        authorization.model_copy(update={"region": forged_region}),
                        {"type": "object"},
                        ExecutionContext(),
                        scope=scope,
                        policy_epoch=0,
                        model="synthetic-model",
                        api_key="synthetic-key",
                        client_factory=reject_client_factory,
                    )
                )
            assert dispatched_error.value.code == "FORBIDDEN_SCOPE"
            assert constructed == []

    provider_requests = []

    class Messages:
        def create(self, **kwargs):
            provider_requests.append(kwargs)
            return type(
                "Response",
                (),
                {
                    "stop_reason": "end_turn",
                    "content": [
                        type(
                            "Text",
                            (),
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "target_id": selected.target_id,
                                        "literal_transcription": None,
                                        "interpretation": {"summary": "synthetic"},
                                        "uncertainties": [],
                                    }
                                ),
                            },
                        )()
                    ],
                },
            )()

    class Client:
        messages = Messages()

        def close(self):
            pass

    observation = asyncio.run(
        dispatch_captured_vision(
            owner,
            authorization,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
            },
            ExecutionContext(),
            scope=scope,
            policy_epoch=0,
            model="synthetic-model",
            api_key="synthetic-key",
            client_factory=lambda **_kwargs: Client(),
        )
    )
    assert provider_requests
    assert (
        selected.literal_text
        in provider_requests[0]["messages"][0]["content"][1]["text"]
    )
    receipt = identity.canonical_bytes(
        {
            "type": "vision_derivation",
            "schema_version": "1",
            "data": {
                "task": region.task,
                "source_native_ref": region.source_native_ref,
                "image_sha256": region.image_sha256,
                "original_image_sha256": region.original_image_sha256,
                "original_pixel_sha256": region.original_pixel_sha256,
                "region_pixel_sha256": region.region_pixel_sha256,
                "context_sha256": region.context_sha256,
                "source_context_sha256": source_context_identity(region.source_context),
                "dependency_ids": sorted(region.dependency_ids),
                "model": observation.model,
                "prompt_schema_version": observation.prompt_schema_version,
                "task_schema_sha256": observation.task_schema_sha256,
                "prompt_renderer_identity": observation.prompt_renderer_identity,
                "prompt_identity": observation.prompt_identity,
                "result": {
                    "target_id": observation.target_id,
                    "literal_transcription": observation.literal_transcription,
                    "interpretation": observation.interpretation,
                    "uncertainties": list(observation.uncertainties),
                },
            },
        }
    )
    derivation = owner.artifacts.put_stream(BytesIO(receipt), "application/json")
    observation = observation.model_copy(update={"derivation": derivation})
    plan = DocumentCompositionPlan(
        base_normalizer_fingerprint=parsed.normalizer_fingerprint,
        proxies=(planned_vision_proxy(region, observation),),
    )
    result = owner.run_blocking_sync(
        lambda: compose_document(
            parsed, case.registry, plan, owner.artifacts, case.ctx
        ),
        case.ctx,
    )
    forged_receipt = json.loads(receipt)
    forged_receipt["data"]["source_context_sha256"] = "f" * 64
    forged_payload = identity.canonical_bytes(forged_receipt)
    forged_ref = owner.artifacts.put_stream(BytesIO(forged_payload), "application/json")
    forged_plan = plan.model_copy(
        update={
            "proxies": (plan.proxies[0].model_copy(update={"derivation": forged_ref}),)
        }
    )
    with pytest.raises(ValueError, match="derivation"):
        owner.run_blocking_sync(
            lambda: compose_document(
                parsed, case.registry, forged_plan, owner.artifacts, case.ctx
            ),
            case.ctx,
        )
    context_owner_ids = {
        unit.unit_id
        for unit in result.bundle.units
        if unit.text_origin == "source"
        and any(
            part["native_ref"] == selector.native_ref
            and part["field"] == selector.field
            and part["source_start"] < selector.source_end
            and part["source_end"] > selector.source_start
            for part in unit.attributes.get("source_parts", ())
        )
    }
    pixel_owner = next(
        unit
        for unit in result.bundle.units
        if unit.unit_type == "image_region_original"
    )
    assert set(result.bundle.proxies[0].required_unit_ids) == {
        pixel_owner.unit_id,
        *context_owner_ids,
    }
    assert all(
        next(
            unit for unit in result.bundle.units if unit.unit_id == dependency
        ).extraction_id
        == result.bundle.extraction.extraction_id
        for dependency in result.bundle.proxies[0].required_unit_ids
    )
    retained = dict(result.payloads)
    for ref in (*[asset.artifact_ref for asset in parsed.assets], derivation):
        with owner.artifacts.open_verified(ref) as stream:
            retained[ref.sha256] = stream.read()
    case.source.unlink()
    replay = replay_composed_document(
        result.payloads[result.manifest_ref.sha256],
        result.manifest_ref,
        parsed,
        case.registry,
        retained,
    )
    assert replay.bundle == result.bundle
    assert (
        owner.run_blocking_sync(
            lambda: owner.db.execute("SELECT count(*) FROM evidence_units").fetchone()[
                0
            ],
            ExecutionContext(),
        )
        == 0
    )


@pytest.mark.qualification
def test_selected_scanned_pdf_page_adds_ocr_proxy_without_promoting_native_text(
    document_record_case,
):
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont, ImageOps
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from tychora.adapters.vision.local import LocalOcrProvider
    from tychora.contracts.composition import (
        DocumentCompositionPlan,
        SourceContextSelection,
        SourceSelector,
    )
    from tychora.ingestion.composition import compose_document
    from tychora.ingestion.vision import pixel_source_owner_id, planned_vision_proxy
    from tychora.ports.vision import (
        CapturedRegionAuthorization,
        CapturedSourceRegistration,
        RegionInput,
        RetainedDocumentRegionProof,
        pixel_identity,
        vision_context_identity,
    )

    ocr_root = os.environ.get("TYCHORA_TEST_OCR_MODELS")
    if not ocr_root:
        pytest.skip("requires an explicitly provisioned OCR model bundle")

    def mixed_pdf(path: Path, _content) -> None:
        page = canvas.Canvas(str(path), pagesize=(612, 792), invariant=1)
        page.setFont("Helvetica", 15)
        page.drawString(72, 700, "Native source paragraph remains authoritative.")
        page.showPage()
        scan = Image.new("RGB", (1200, 280), "white")
        ImageDraw.Draw(scan).text(
            (45, 70),
            "SCANNED PAGE 456",
            fill="black",
            font=ImageFont.load_default(size=48),
        )
        encoded = BytesIO()
        scan.save(encoded, format="PNG")
        page.drawImage(
            ImageReader(BytesIO(encoded.getvalue())),
            36,
            300,
            width=540,
            height=126,
        )
        page.showPage()
        page.save()

    case = document_record_case(
        "standard@1", "pdf", source_builder=mixed_pdf, commit=False
    )
    parsed = case.parsed
    base_result = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed,
            case.registry,
            DocumentCompositionPlan(
                base_normalizer_fingerprint=parsed.normalizer_fingerprint
            ),
            case.owner.artifacts,
            case.ctx,
        ),
        case.ctx,
    )
    bundle = base_result.bundle
    native = tuple(
        (unit.native_ref, unit.text, unit.text_origin, unit.citable)
        for unit in bundle.units
    )
    base_extraction_id = bundle.extraction.extraction_id
    parsed_page = next(
        asset
        for asset in parsed.assets
        if asset.asset_type == "page" and asset.locator.physical_page == 2
    )
    asset = next(
        row for row in bundle.assets if row.native_ref == parsed_page.native_ref
    )
    with case.owner.artifacts.open_verified(parsed_page.artifact_ref) as stream:
        page_bytes = stream.read()
    with Image.open(BytesIO(page_bytes)) as encoded_page:
        pixels = ImageOps.exif_transpose(encoded_page).convert("RGB")
        pixels.load()
    original_transform = "exif-transpose-rgb-v1"
    region_transform = f"{original_transform}:crop:0,0,{pixels.width},{pixels.height}"
    original_pixel_sha = pixel_identity(
        pixels.tobytes(), pixels.width, pixels.height, "RGB", original_transform
    )
    owner_id = pixel_source_owner_id(
        bundle,
        asset.asset_id,
        original_pixel_sha,
        pixels.width,
        pixels.height,
        original_transform,
    )
    region = RegionInput(
        source_native_ref=parsed_page.native_ref,
        image=page_bytes,
        image_ref=parsed_page.artifact_ref,
        media_type=parsed_page.artifact_ref.media_type,
        image_sha256=sha256(page_bytes).hexdigest(),
        original_image_sha256=parsed_page.artifact_ref.sha256,
        original_pixel_sha256=original_pixel_sha,
        region_pixel_sha256=pixel_identity(
            pixels.tobytes(), pixels.width, pixels.height, "RGB", region_transform
        ),
        original_width=pixels.width,
        original_height=pixels.height,
        width=pixels.width,
        height=pixels.height,
        color_mode="RGB",
        original_pixel_transform=original_transform,
        region_pixel_transform=region_transform,
        geometry=Geometry(
            domain="pixels", x0=0, y0=0, x1=pixels.width, y1=pixels.height
        ),
        context_sha256=vision_context_identity(()),
        task="ocr",
        dependency_ids=frozenset({owner_id}),
    )
    captured_region = region.model_copy(
        update={"dependency_ids": frozenset({parsed.registration.source.source_id})}
    )
    captured_registration = CapturedSourceRegistration(
        source=type(parsed.registration.source).model_validate(
            {
                **parsed.registration.source.model_dump(mode="json"),
                "registration_json": {
                    "schema_version": 1,
                    "kind_id": parsed.registration.revision.kind_id,
                },
            }
        ),
        kind_id=parsed.registration.revision.kind_id,
    )
    retained_proof = RetainedDocumentRegionProof(
        capture=parsed.registration.capture,
        extraction=parsed,
        asset_native_ref=parsed_page.native_ref,
    )
    retained_authorization = CapturedRegionAuthorization(
        registration=captured_registration,
        revision=parsed.registration.revision,
        proof=retained_proof,
        region=captured_region,
    )
    case.owner.run_blocking_sync(
        lambda: case.owner.upload_policy._verify_captured_pixels(
            retained_authorization
        ),
        case.ctx,
    )
    forged_raw = case.owner.artifacts.put_stream(BytesIO(b"{}"), "application/json")
    forged_proof = retained_proof.model_copy(
        update={
            "extraction": parsed.model_copy(update={"raw_document_ref": forged_raw})
        }
    )
    with pytest.raises(TychoraError):
        rejected_ctx = ExecutionContext()
        case.owner.run_blocking_sync(
            lambda: case.owner.upload_policy._verify_captured_pixels(
                retained_authorization.model_copy(update={"proof": forged_proof})
            ),
            rejected_ctx,
        )
    context_unit = next(
        unit
        for unit in bundle.units
        if unit.citable and unit.text_origin == "source" and unit.text
    )
    context_item = next(
        item
        for item in parsed.units
        if isinstance(item.raw_item.get("text"), str) and item.raw_item["text"]
    )
    context_source_text = context_item.raw_item["text"]
    source_context = SourceContextSelection.bind(
        parsed.original_ref.sha256,
        SourceSelector(
            native_ref=context_item.native_ref,
            field="text",
            source_start=0,
            source_end=len(context_source_text),
        ),
        context_source_text,
    )
    candidate_context = source_context.candidate_context()
    region = RegionInput.model_validate(
        {
            **region.model_dump(),
            "context_sha256": vision_context_identity((candidate_context,)),
            "allowed_candidate_target_ids": frozenset({source_context.target_id}),
            "dependency_ids": frozenset({owner_id, source_context.target_id}),
            "candidate_context": (candidate_context,),
            "source_context": (source_context,),
        }
    )
    observation = LocalOcrProvider(case.owner, Path(ocr_root)).interpret(
        region, {"type": "null"}, case.ctx
    )
    assert observation.derivation is not None
    with case.owner.artifacts.open_verified(observation.derivation) as stream:
        second_receipt = json.loads(stream.read())
    second_receipt["data"]["result"]["literal_transcription"] += "\nSECOND OBSERVATION"
    from tychora.contracts.identity import canonical_bytes

    second_payload = canonical_bytes(second_receipt)
    second_ref = case.owner.artifacts.put_stream(
        BytesIO(second_payload), "application/json"
    )
    second = observation.model_copy(
        update={
            "literal_transcription": second_receipt["data"]["result"][
                "literal_transcription"
            ],
            "derivation": second_ref,
        }
    )
    plan = DocumentCompositionPlan(
        base_normalizer_fingerprint=parsed.normalizer_fingerprint,
        proxies=(
            planned_vision_proxy(region, observation),
            planned_vision_proxy(region, second),
        ),
    )
    result = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed, case.registry, plan, case.owner.artifacts, case.ctx
        ),
        case.ctx,
    )
    bundle = result.bundle
    single = case.owner.run_blocking_sync(
        lambda: compose_document(
            parsed,
            case.registry,
            plan.model_copy(update={"proxies": (plan.proxies[0],)}),
            case.owner.artifacts,
            case.ctx,
        ),
        case.ctx,
    )
    assert single.bundle.extraction.extraction_id != bundle.extraction.extraction_id
    from tychora.ingestion.composition import replay_composed_document

    retained = dict(result.payloads)
    for ref in (
        *(asset.artifact_ref for asset in parsed.assets),
        region.image_ref,
        observation.derivation,
        second_ref,
    ):
        with case.owner.artifacts.open_verified(ref) as stream:
            retained[ref.sha256] = stream.read()
    replay = replay_composed_document(
        result.payloads[result.manifest_ref.sha256],
        result.manifest_ref,
        parsed,
        case.registry,
        retained,
    )
    assert replay.bundle == bundle
    assert parsed.original_ref.sha256 not in retained
    missing = dict(retained)
    missing.pop(observation.derivation.sha256)
    with pytest.raises(ValueError, match="retained"):
        replay_composed_document(
            result.payloads[result.manifest_ref.sha256],
            result.manifest_ref,
            parsed,
            case.registry,
            missing,
        )
    forged = json.loads(retained[observation.derivation.sha256])
    forged["data"]["result"]["literal_transcription"] = "forged"
    forged_payload = canonical_bytes(forged)
    forged_ref = case.owner.artifacts.put_stream(
        BytesIO(forged_payload), "application/json"
    )
    forged_plan = plan.model_copy(
        update={
            "proxies": (
                plan.proxies[0].model_copy(update={"derivation": forged_ref}),
                plan.proxies[1],
            )
        }
    )
    from tychora.ingestion.composition import build_composed_document

    with pytest.raises(ValueError, match="derivation"):
        build_composed_document(
            parsed,
            case.registry,
            forged_plan,
            {**retained, forged_ref.sha256: forged_payload},
        )
    case.owner.run_blocking_sync(
        lambda: case.owner.store.commit_extraction(bundle), case.ctx
    )

    assert (
        tuple(
            (unit.native_ref, unit.text, unit.text_origin, unit.citable)
            for unit in bundle.units[: len(native)]
        )
        == native
    )
    assert bundle.extraction.extraction_id != base_extraction_id
    pixel_owner = next(
        unit for unit in bundle.units if unit.unit_type == "image_region_original"
    )
    assert sum(unit.unit_type == "image_region_original" for unit in bundle.units) == 1
    assert pixel_owner.unit_id != owner_id
    proxies = tuple(unit for unit in bundle.units if unit.unit_type == "asset_proxy")
    proxy = proxies[0]
    assert pixel_owner.text == "" and pixel_owner.citable
    assert proxy.text_origin == "ocr" and not proxy.citable
    assert proxy.review_state == "needs_review"
    assert "SCANNED PAGE 456" in proxy.text
    assert len(proxies) == 2 and proxies[1].text.endswith("SECOND OBSERVATION")
    assert context_unit.unit_id not in bundle.proxies[0].required_unit_ids
    assert len(bundle.proxies[0].required_unit_ids) == 2


@pytest.mark.qualification
def test_local_ocr_uses_explicit_models_and_preserves_literal_text(tmp_path: Path):
    model_root = os.environ.get("TYCHORA_TEST_OCR_MODELS")
    if not model_root:
        pytest.skip("requires an explicitly provisioned RapidOCR model bundle")
    from hashlib import sha256
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    from tychora.adapters.vision.local import LocalOcrProvider
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config
    from tychora.contracts.evidence import Geometry
    from tychora.contracts.requests import RequestScope
    from tychora.ports.vision import (
        RegionInput,
        pixel_identity,
        vision_context_identity,
    )

    image = Image.new("RGB", (1200, 280), "white")
    ImageDraw.Draw(image).text(
        (42, 55),
        "SOURCE IMAGE 123",
        fill="black",
        font=ImageFont.load_default(size=48),
    )
    encoded = BytesIO()
    image.save(encoded, format="PNG")
    payload = encoded.getvalue()
    image_sha = sha256(payload).hexdigest()
    original_pixel_sha = pixel_identity(
        image.tobytes(), image.width, image.height, "RGB", "exif-transpose-rgb-v1"
    )
    region_transform = f"exif-transpose-rgb-v1:crop:0,0,{image.width},{image.height}"
    region_pixel_sha = pixel_identity(
        image.tobytes(), image.width, image.height, "RGB", region_transform
    )
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    with WorkspaceOwner.open(
        tmp_path / "workspace", config, RequestScope(principal_id="fixture")
    ) as owner:
        observation = LocalOcrProvider(owner, Path(model_root)).interpret(
            RegionInput(
                source_native_ref="#/region",
                image=payload,
                image_ref=ArtifactRef(
                    sha256=image_sha,
                    size_bytes=len(payload),
                    media_type="image/png",
                    storage_key=image_sha,
                ),
                media_type="image/png",
                image_sha256=image_sha,
                original_image_sha256=image_sha,
                original_pixel_sha256=original_pixel_sha,
                region_pixel_sha256=region_pixel_sha,
                original_width=image.width,
                original_height=image.height,
                width=image.width,
                height=image.height,
                color_mode="RGB",
                original_pixel_transform="exif-transpose-rgb-v1",
                region_pixel_transform=region_transform,
                geometry=Geometry(
                    domain="pixels", x0=0, y0=0, x1=image.width, y1=image.height
                ),
                context_sha256=vision_context_identity(()),
                task="ocr",
                dependency_ids=frozenset({"source-unit-1"}),
            ),
            {"type": "null"},
            ExecutionContext(),
        )
    assert observation.literal_transcription == "SOURCE IMAGE 123"
    assert observation.interpretation is None
    assert observation.review_state == "needs_review"
    assert observation.model.startswith("rapidocr==3.9.2:")
