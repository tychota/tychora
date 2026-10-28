"""Synthetic visual requests through the real owner, SQLite, and CAS."""

import asyncio
import base64
import json
from hashlib import sha256
from io import BytesIO
from threading import Event, Lock
from typing import Any, cast

import pytest
from PIL import Image

from tests.helpers.corpus import COLLECTION, reader, seed_bundle
from tychora.adapters.voyage.request import RawVoyageResponse
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.setup import (
    load_voyage_multimodal_accounting,
    load_voyage_tokenizer,
)
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    AssetRow,
    LocatorRow,
    RevisionArtifactRow,
    UnitAssetRow,
)
from tychora.contracts.embeddings import (
    RegisteredImageQuery,
    TextQuery,
    VisualEmbeddingLimits,
    VisualInput,
    VisualResizeDerivation,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import Geometry, ImageLocator
from tychora.contracts.identity import digest
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


class RecordingRawTransport:
    """Records exact wire bytes and returns an indexed synthetic response."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def post_json(self, path, body, timeout):
        self.calls.append((path, body, timeout))
        if self.responses:
            return self.responses.pop(0)
        return RawVoyageResponse(
            200,
            json.dumps({"data": [{"index": 0, "embedding": [1.0] * 1024}]}).encode(),
        )

    def close(self):
        pass


class CharacterCounter:
    model = "voyage-multimodal-3.5"
    tokenizer_id = "voyage-multimodal-3.5:qualified"

    def count(self, text):
        return len(text)


def _limits():
    return VisualEmbeddingLimits(
        accounting_version="voyage-multimodal-accounting-v1",
        model_revision="e33df0507cb152ea6fc576e5d96d9c51443e4191",
        tokenizer_sha256="c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539",
        client_config_sha256="26f27fcc6ca6eada8b9330f2a3998d62bdaeb564dd3a252009b468c4a0db9c0e",
        accounting_image_pixels_min=50_000,
        accounting_image_pixels_max=2_000_000,
        image_pixels_per_token=560,
        source_pixels_max=16_000_000,
        image_encoded_bytes_max=20_000_000,
        input_tokens_max=32_000,
        request_assets_max=32,
        request_tokens_max=100_000,
        request_wire_bytes_max=64 * 1024 * 1024,
        decoded_image_bytes_max=48_000_000,
    )


def _image_bytes(color=(20, 40, 60), size=(256, 256)):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def _install(
    owner, *, caption="Synthetic figure caption.", image=None, separate_caption=False
):
    image = _image_bytes() if image is None else image
    with Image.open(BytesIO(image)) as opened:
        source_width, source_height = opened.size
    bundle = seed_bundle(caption)
    image_ref = owner.artifacts.put_stream(BytesIO(image), "image/png")
    for ref in bundle.artifacts:
        with reader().open_verified(ref) as stream:
            owner.artifacts.put_stream(stream, ref.media_type)
    asset = AssetRow(
        asset_id=digest(
            "asset-v1", bundle.extraction.extraction_id, "figure:1", image_ref.sha256
        ),
        extraction_id=bundle.extraction.extraction_id,
        artifact_sha=image_ref.sha256,
        asset_type="picture",
        native_ref="figure:1",
        locator=ImageLocator(
            artifact_ref=image_ref.sha256,
            native_ref="figure:1",
            geometry=Geometry(
                domain="pixels", x0=0, y0=0, x1=source_width, y1=source_height
            ),
        ),
        caption=caption,
    )
    caption_unit = bundle.units[0].model_copy(
        update={
            "unit_id": digest("synthetic-caption-unit-v1", bundle.units[0].unit_id),
            "ordinal": 1,
            "native_ref": "caption:1",
            "boundary_key": "caption:1",
        }
    )
    units = (*bundle.units, caption_unit) if separate_caption else bundle.units
    locators = (
        (
            *bundle.locators,
            LocatorRow(
                locator_id=digest("locator-v1", caption_unit.unit_id, 0),
                unit_id=caption_unit.unit_id,
                fragment_ordinal=0,
                locator=caption_unit.locators[0],
            ),
        )
        if separate_caption
        else bundle.locators
    )
    links = (
        UnitAssetRow(
            unit_id=bundle.units[0].unit_id, asset_id=asset.asset_id, role="source"
        ),
        UnitAssetRow(
            unit_id=caption_unit.unit_id
            if separate_caption
            else bundle.units[0].unit_id,
            asset_id=asset.asset_id,
            role="caption",
        ),
    )
    created = owner.db.execute("SELECT created_at FROM workspaces").fetchone()[0]
    bundle = bundle.model_copy(
        update={
            "workspace": bundle.workspace.model_copy(
                update={"workspace_id": owner.workspace_id, "created_at": created}
            ),
            "collection": bundle.collection.model_copy(
                update={
                    "workspace_id": owner.workspace_id,
                    "policy_json": FrozenJsonMap(
                        {"schema_version": 1, "upload_providers": ["voyage"]}
                    ),
                }
            ),
            "artifacts": (*bundle.artifacts, image_ref),
            "assets": (asset,),
            "units": units,
            "locators": locators,
            "unit_assets": links,
            "revision_artifacts": (
                *bundle.revision_artifacts,
                RevisionArtifactRow(
                    revision_id=bundle.revision.revision_id,
                    role="sidecar",
                    source_ref="figure:1",
                    artifact_sha=image_ref.sha256,
                ),
            ),
        }
    )
    owner.store.commit_extraction(bundle)
    return bundle, asset, image_ref, image


def _descriptor(bundle, asset, image_ref, image, *, context=None, dependencies=None):
    with Image.open(BytesIO(image)) as opened:
        pixels = opened.convert("RGB").tobytes()
        width, height = opened.size
    return VisualInput.from_registered(
        asset=asset,
        artifact_ref=image_ref,
        revision_id=bundle.revision.revision_id,
        context=asset.caption if context is None else context,
        dependency_ids=(
            frozenset({asset.asset_id, bundle.units[0].unit_id})
            if dependencies is None
            else dependencies
        ),
        pixel_sha256=sha256(pixels).hexdigest(),
        width=width,
        height=height,
        resize=VisualResizeDerivation(
            source_width=width,
            source_height=height,
            width=width,
            height=height,
            mode="none",
        ),
    )


@pytest.fixture
def visual_case(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(path, "test", {})
    config = config.model_copy(
        update={"values": config.values.model_copy(update={"offline": False})}
    )
    scope = RequestScope(
        principal_id="synthetic-principal",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        yield owner, scope


async def _provider(owner, scope, transport):
    ctx = ExecutionContext()
    runtime = VoyageRuntime(
        lambda: transport,
        workspace_id=owner.workspace_id,
        credential_binding_id="synthetic-credential",
    )
    provider = await create_visual_embedder(
        owner,
        capabilities=qualify_voyage_visual_capabilities(
            counter=CharacterCounter(), limits=_limits()
        ),
        counter=CharacterCounter(),
        runtime=runtime,
        scope=scope,
        ctx=ctx,
        priority="background",
    )
    return provider, ctx


def test_multimodal_accounting_requires_explicit_verified_bundle(tmp_path):
    with pytest.raises(TychoraError, match="accounting resources"):
        load_voyage_multimodal_accounting(tmp_path)
    with pytest.raises(TychoraError) as tokenizer:
        load_voyage_tokenizer(tmp_path, "voyage-multimodal-3.5")
    assert tokenizer.value.code == "CAPABILITY_MISSING"


@pytest.mark.asyncio
async def test_visual_input_contains_exact_pixels_and_context(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image)
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)

    records = await owner.embed_visual_assets(provider, (item,), ctx, scope=scope)

    assert len(records) == 1
    assert records[0].space_id == provider.capabilities.space_id
    assert records[0].input_sha == digest(
        "visual-embedding-input-v1",
        "document",
        item.input_sha,
        provider.capabilities.space_id,
        "exif-transpose-rgb-lossless-png-v1",
    )
    path, body, _ = transport.calls[0]
    request = json.loads(body)
    content = request["inputs"][0]["content"]
    assert path == "/v1/multimodalembeddings"
    assert request["model"] == "voyage-multimodal-3.5"
    assert request["truncation"] is False
    assert content[0] == {"type": "text", "text": asset.caption}
    assert content[1]["type"] == "image_base64"
    assert content[1]["image_base64"].startswith("data:image/png;base64,")
    decoded = base64.b64decode(content[1]["image_base64"].split(",", 1)[1])
    with Image.open(BytesIO(decoded)) as opened:
        assert (
            opened.convert("RGB").tobytes()
            == Image.open(BytesIO(image)).convert("RGB").tobytes()
        )


@pytest.mark.asyncio
async def test_image_only_query_and_text_query_share_visual_space(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image, context="")
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)

    image_record = await owner.embed_visual_query(
        provider, RegisteredImageQuery(image=item), ctx, scope=scope
    )
    text_record = await owner.embed_visual_query(
        provider, TextQuery(text="What color is the diagram?"), ctx, scope=scope
    )

    assert (
        image_record.space_id == text_record.space_id == provider.capabilities.space_id
    )
    assert len(transport.calls) == 2
    assert (
        json.loads(transport.calls[0][1])["inputs"][0]["content"][0]["type"]
        == "image_base64"
    )
    assert json.loads(transport.calls[1][1])["inputs"][0]["content"] == [
        {"type": "text", "text": "What color is the diagram?"}
    ]


@pytest.mark.asyncio
async def test_forged_descriptor_and_omitted_caption_dependency_are_denied(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner, separate_caption=True), ExecutionContext()
    )
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    bad_context = _descriptor(bundle, asset, ref, image, context="invented caption")
    bad_deps = _descriptor(
        bundle, asset, ref, image, dependencies=frozenset({asset.asset_id})
    )
    hidden_caption = _descriptor(
        bundle,
        asset,
        ref,
        image,
        dependencies=frozenset({asset.asset_id, bundle.units[0].unit_id}),
    )

    for item in (bad_context, bad_deps, hidden_caption):
        with pytest.raises(TychoraError) as error:
            await owner.embed_visual_assets(provider, (item,), ctx, scope=scope)
        assert error.value.code == "FORBIDDEN_SCOPE"
    assert transport.calls == []

    complete = _descriptor(
        bundle,
        asset,
        ref,
        image,
        dependencies=frozenset(
            {asset.asset_id, bundle.units[0].unit_id, bundle.units[1].unit_id}
        ),
    )
    assert (
        len(await owner.embed_visual_assets(provider, (complete,), ctx, scope=scope))
        == 1
    )
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_complete_smaller_batches_preserve_order_and_request_count(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    captioned = _descriptor(bundle, asset, ref, image)
    image_only = _descriptor(bundle, asset, ref, image, context="")
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)

    result = await owner.embed_visual_assets(
        provider, (captioned, image_only, captioned), ctx, scope=scope
    )

    assert len(result) == len(transport.calls) == 3
    content = [json.loads(call[1])["inputs"][0]["content"] for call in transport.calls]
    assert [len(parts) for parts in content] == [2, 1, 2]
    assert all(len(json.loads(call[1])["inputs"]) == 1 for call in transport.calls)


@pytest.mark.asyncio
async def test_cached_hit_checks_current_visibility_and_offline_miss_is_denied(
    visual_case,
):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image)
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    query = RegisteredImageQuery(image=item)
    first = await owner.embed_visual_query(provider, query, ctx, scope=scope)
    assert len(transport.calls) == 1

    owner.config = owner.config.model_copy(
        update={"values": owner.config.values.model_copy(update={"offline": True})}
    )
    assert await owner.embed_visual_query(provider, query, ctx, scope=scope) == first
    assert len(transport.calls) == 1
    with pytest.raises(TychoraError) as missing:
        await owner.embed_visual_query(
            provider, TextQuery(text="uncached query"), ctx, scope=scope
        )
    assert missing.value.code == "FORBIDDEN_SCOPE"
    assert len(transport.calls) == 1

    await owner.run_write(
        lambda: owner.db.execute(
            "UPDATE collections SET access_floor=3 WHERE collection_id=?",
            (COLLECTION,),
        ),
        ctx,
    )
    with pytest.raises(TychoraError) as changed:
        await owner.embed_visual_query(provider, query, ctx, scope=scope)
    assert changed.value.code == "FORBIDDEN_SCOPE"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_retry_checks_policy_epoch_before_second_wire_attempt(visual_case):
    owner, scope = visual_case
    await owner.run_write(lambda: _install(owner), ExecutionContext())
    transport = RecordingRawTransport()
    transport.responses.append(RawVoyageResponse(503, b'{"error":"synthetic"}'))
    provider, ctx = await _provider(owner, scope, transport)

    def advance_epoch(_attempt):
        owner.run_write_sync(
            lambda: owner.db.execute(
                "UPDATE workspaces SET policy_epoch=1 WHERE workspace_id=?",
                (owner.workspace_id,),
            ),
            ctx,
        )
        return 0.0

    provider._requests._backoff = advance_epoch
    with pytest.raises(TychoraError) as changed:
        await owner.embed_visual_query(
            provider, TextQuery(text="Which color?"), ctx, scope=scope
        )
    assert changed.value.code == "SCOPE_CHANGED"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"data": [{"index": 1, "embedding": [1.0] * 1024}]},
        {"data": [{"index": 0, "embedding": [1.0] * 1023}]},
        {"data": [{"index": 0, "embedding": [float("nan")] * 1024}]},
        {"data": [{"index": 0, "embedding": [10**400] + [0.0] * 1023}]},
    ],
)
async def test_raw_response_mapping_rejects_invalid_index_dimension_or_values(
    visual_case, response
):
    owner, scope = visual_case
    await owner.run_write(lambda: _install(owner), ExecutionContext())
    transport = RecordingRawTransport()
    transport.responses.append(RawVoyageResponse(200, json.dumps(response).encode()))
    provider, ctx = await _provider(owner, scope, transport)
    with pytest.raises(TychoraError) as invalid:
        await owner.embed_visual_query(
            provider, TextQuery(text="synthetic mapping"), ctx, scope=scope
        )
    assert invalid.value.code == "PROVIDER_FAILURE"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_oversized_declared_source_geometry_fails_before_decode(
    visual_case, monkeypatch
):
    from PIL import PngImagePlugin

    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image)
    item = VisualInput.from_registered(
        asset=asset,
        artifact_ref=ref,
        revision_id=bundle.revision.revision_id,
        context=asset.caption,
        dependency_ids=item.dependency_ids,
        pixel_sha256=item.pixel_sha256,
        width=4001,
        height=4001,
        resize=VisualResizeDerivation(
            source_width=4001, source_height=4001, width=4001, height=4001, mode="none"
        ),
    )
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)

    def forbidden_load(_image):
        raise AssertionError("source decoded before geometry limit")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)
    with pytest.raises(TychoraError) as too_large:
        await owner.embed_visual_assets(provider, (item,), ctx, scope=scope)
    assert too_large.value.code == "INVALID_REQUEST"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_mismatched_pixel_hash_and_missing_source_link_cannot_upload(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    item = _descriptor(bundle, asset, ref, image)
    forged = VisualInput.from_registered(
        asset=asset,
        artifact_ref=ref,
        revision_id=bundle.revision.revision_id,
        context=asset.caption,
        dependency_ids=item.dependency_ids,
        pixel_sha256="0" * 64,
        width=256,
        height=256,
        resize=item.resize,
    )
    with pytest.raises(TychoraError) as pixels:
        await owner.embed_visual_assets(provider, (forged,), ctx, scope=scope)
    assert pixels.value.code == "INTEGRITY_ERROR"
    assert transport.calls == []

    await owner.run_write(
        lambda: owner.db.execute(
            "DELETE FROM unit_assets WHERE asset_id=? AND role='source'",
            (asset.asset_id,),
        ),
        ctx,
    )
    with pytest.raises(TychoraError) as source:
        await owner.embed_visual_assets(provider, (item,), ctx, scope=scope)
    assert source.value.code == "FORBIDDEN_SCOPE"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_model_copy_cannot_forge_visual_input_identity(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image)
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)

    for forged in (
        item.model_copy(update={"input_sha": "0" * 64}),
        item.model_copy(update={"asset": item.asset.model_dump(mode="python")}),
    ):
        with pytest.raises(TychoraError) as rejected:
            await owner.embed_visual_assets(provider, (forged,), ctx, scope=scope)
        assert rejected.value.code == "FORBIDDEN_SCOPE"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_recomputed_visual_input_with_modified_locator_is_denied(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    item = _descriptor(bundle, asset, ref, image)
    changed = asset.model_copy(
        update={
            "locator": asset.locator.model_copy(update={"native_ref": "forged-source"})
        }
    )
    forged = VisualInput.from_registered(
        asset=changed,
        artifact_ref=ref,
        revision_id=bundle.revision.revision_id,
        context=item.context,
        dependency_ids=item.dependency_ids,
        pixel_sha256=item.pixel_sha256,
        width=item.width,
        height=item.height,
        resize=item.resize,
    )
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    with pytest.raises(TychoraError) as denied:
        await owner.embed_visual_assets(provider, (forged,), ctx, scope=scope)
    assert denied.value.code == "FORBIDDEN_SCOPE"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_explicit_lanczos_resize_sends_recorded_pixels(visual_case):
    owner, scope = visual_case
    bundle, asset, ref, image = await owner.run_write(
        lambda: _install(owner), ExecutionContext()
    )
    with Image.open(BytesIO(image)) as opened:
        expected = opened.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)
        pixels = expected.tobytes()
    item = VisualInput.from_registered(
        asset=asset,
        artifact_ref=ref,
        revision_id=bundle.revision.revision_id,
        context=asset.caption,
        dependency_ids=frozenset({asset.asset_id, bundle.units[0].unit_id}),
        pixel_sha256=sha256(pixels).hexdigest(),
        width=128,
        height=128,
        resize=VisualResizeDerivation(
            source_width=256,
            source_height=256,
            width=128,
            height=128,
            mode="LANCZOS",
        ),
    )
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    await owner.embed_visual_assets(provider, (item,), ctx, scope=scope)
    content = json.loads(transport.calls[0][1])["inputs"][0]["content"]
    encoded = base64.b64decode(content[1]["image_base64"].split(",", 1)[1])
    with Image.open(BytesIO(encoded)) as transmitted:
        assert transmitted.size == (128, 128)
        assert transmitted.convert("RGB").tobytes() == pixels


@pytest.mark.asyncio
async def test_missing_collection_upload_grant_blocks_miss(visual_case):
    owner, scope = visual_case
    await owner.run_write(lambda: _install(owner), ExecutionContext())
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    await owner.run_write(
        lambda: owner.db.execute(
            "UPDATE collections SET policy_json=? WHERE collection_id=?",
            ('{"schema_version":1,"upload_providers":[]}', COLLECTION),
        ),
        ctx,
    )
    with pytest.raises(TychoraError) as denied:
        await owner.embed_visual_query(
            provider, TextQuery(text="What is shown?"), ctx, scope=scope
        )
    assert denied.value.code == "FORBIDDEN_SCOPE"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_query_wire_hash_is_authorized_at_actual_attempt(visual_case):
    owner, scope = visual_case
    await owner.run_write(lambda: _install(owner), ExecutionContext())
    transport = RecordingRawTransport()
    provider, ctx = await _provider(owner, scope, transport)
    actual = owner.upload_policy.authorize_query
    authorizations = []

    def checked(scope_arg, provider_arg, authorization):
        authorizations.append(authorization)
        return actual(scope_arg, provider_arg, authorization)

    owner.upload_policy.authorize_query = checked
    await owner.embed_visual_query(
        provider, TextQuery(text="Which shape?"), ctx, scope=scope
    )
    assert len(transport.calls) == 1
    assert any(
        getattr(auth, "payload_sha", None) == sha256(transport.calls[0][1]).hexdigest()
        for auth in authorizations
    )
    assert any(
        not hasattr(auth, "payload_sha") and hasattr(auth, "operation_sha")
        for auth in authorizations
    )


@pytest.mark.asyncio
async def test_three_query_misses_observe_two_worker_provider_lane(visual_case):
    owner, scope = visual_case
    await owner.run_write(lambda: _install(owner), ExecutionContext())
    entered = []
    lock = Lock()
    two_entered = Event()
    release = Event()

    class GatedTransport(RecordingRawTransport):
        def post_json(self, path, body, timeout):
            with lock:
                entered.append(json.loads(body)["inputs"][0]["content"][0]["text"])
                if len(entered) == 2:
                    two_entered.set()
            assert release.wait(3)
            return super().post_json(path, body, timeout)

    transport = GatedTransport()
    provider, ctx = await _provider(owner, scope, transport)
    jobs = [
        asyncio.create_task(
            owner.embed_visual_query(
                provider, TextQuery(text=f"question {n}"), ctx, scope=scope
            )
        )
        for n in range(3)
    ]
    try:
        assert await asyncio.to_thread(two_entered.wait, 3)
        assert len(entered) == 2
    finally:
        release.set()
    results = await asyncio.gather(*jobs)
    assert len(results) == len(transport.calls) == 3


def test_visual_cache_key_binds_lossless_png_representation():
    limits = _limits()
    visual = qualify_voyage_visual_capabilities(
        counter=CharacterCounter(), limits=limits
    )
    expected = digest(
        "visual-representation-v1",
        visual.model,
        visual.dimensions,
        visual.dtype,
        visual.tokenizer_id,
        limits.model_revision,
        limits.tokenizer_sha256,
        limits.client_config_sha256,
        limits.accounting_version,
        "exif-transpose-rgb-lossless-png-v1",
        visual.options,
    )
    assert visual.representation_fingerprint == expected


def test_visual_model_unavailability_and_distinct_space():
    from tychora.contracts.identity import space_id

    with pytest.raises(TychoraError) as missing:
        qualify_voyage_visual_capabilities(counter=None, limits=_limits())
    assert missing.value.code == "CAPABILITY_MISSING"
    visual = qualify_voyage_visual_capabilities(
        counter=CharacterCounter(), limits=_limits()
    )
    assert visual.modality == "visual"
    assert visual.space_id != space_id(
        "text",
        "voyage",
        visual.model,
        visual.dimensions,
        "cosine",
        visual.representation_fingerprint,
    )


@pytest.mark.asyncio
async def test_query_binding_creates_the_visual_embedder_on_the_interactive_lane(
    monkeypatch,
):
    from types import SimpleNamespace

    from tychora.adapters.voyage import query as voyage_query

    seen = {}

    async def recording(owner, **kwargs):
        seen.update(kwargs)
        return "bound"

    monkeypatch.setattr(voyage_query, "create_visual_embedder", recording)
    base = SimpleNamespace(
        capabilities="caps", _counter="counter", _runtime=SimpleNamespace()
    )
    provider = voyage_query.VoyageVisualQueryProvider(cast(Any, base))
    bound = await provider.bind(
        cast(Any, object()), cast(Any, "scope"), ExecutionContext()
    )
    assert bound == "bound"
    # A query binds per call: a background lane would queue it behind ingestion.
    assert seen["priority"] == "interactive"
