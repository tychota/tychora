"""Actual image bytes and canonical proxy ownership boundaries."""

from io import BytesIO

import pytest


def image_bytes(size=(8, 6)):
    from PIL import Image

    output = BytesIO()
    Image.new("RGB", size, (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


def test_pixels_mime_dimensions_and_recorded_resize():
    from tychora.ingestion.assets import validate_image

    data = image_bytes()
    exact = validate_image(data, "image/png", expected_size=(8, 6))
    assert exact.data == data and (exact.width, exact.height) == (8, 6)
    resized = validate_image(data, "image/png", expected_size=(8, 6), resize_to=(4, 3))
    assert (resized.width, resized.height) == (4, 3)
    assert resized.resize_from == (8, 6) and resized.resampling == "LANCZOS"
    assert resized.source_sha != resized.output_sha


@pytest.mark.parametrize("change", ["mime", "dimensions", "pixels"])
def test_invalid_image_is_rejected_before_decode(monkeypatch, change):
    from PIL import PngImagePlugin

    from tychora.ingestion import assets

    data = image_bytes()
    called = False

    def forbidden_decode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("pixel decode occurred")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_decode)
    if change == "pixels":
        monkeypatch.setattr(assets, "MAX_IMAGE_PIXELS", 47)
    with pytest.raises(ValueError):
        assets.validate_image(
            data,
            "image/jpeg" if change == "mime" else "image/png",
            expected_size=(9, 6) if change == "dimensions" else (8, 6),
        )
    assert not called


@pytest.fixture
def proxy_source():
    from tests.helpers.corpus import artifact, seed_bundle
    from tychora.contracts.corpus import AssetRow, RevisionArtifactRow, UnitAssetRow
    from tychora.contracts.evidence import ImageLocator
    from tychora.contracts.identity import digest

    bundle = seed_bundle("Original sample figure caption.")
    pixels = artifact(image_bytes(), "image/png")
    image = AssetRow(
        asset_id=digest(
            "asset-v1", bundle.extraction.extraction_id, "figure:1", pixels.sha256
        ),
        extraction_id=bundle.extraction.extraction_id,
        artifact_sha=pixels.sha256,
        asset_type="picture",
        native_ref="figure:1",
        locator=ImageLocator(artifact_ref=pixels.sha256, native_ref="figure.png"),
        caption=bundle.units[0].text,
    )
    return bundle.model_copy(
        update={
            "assets": (image,),
            "artifacts": (*bundle.artifacts, pixels),
            "revision_artifacts": (
                *bundle.revision_artifacts,
                RevisionArtifactRow(
                    revision_id=bundle.revision.revision_id,
                    role="sidecar",
                    source_ref="figure.png",
                    artifact_sha=pixels.sha256,
                ),
            ),
            "unit_assets": (
                UnitAssetRow(
                    unit_id=bundle.units[0].unit_id,
                    asset_id=image.asset_id,
                    role="caption",
                ),
            ),
        }
    )


def test_bound_proxy_preserves_original_caption_citation(proxy_source):
    from tests.helpers.corpus import artifact
    from tychora.ingestion.assets import ProxyFactory

    factory = ProxyFactory(proxy_source)
    derivation = artifact(b'{"task":"caption_copy"}', "application/json")
    proxy = factory.make_proxy(
        proxy_source.assets[0].asset_id,
        "caption_copy",
        proxy_source.units[0].text,
        (proxy_source.units[0].unit_id,),
        derivation,
    )
    assert proxy_source.units[0].citable
    assert not proxy.unit.citable and proxy.unit.text_origin == "source"
    assert proxy.unit.locators == (proxy_source.assets[0].locator,)
    assert proxy.binding.required_unit_ids == frozenset({proxy_source.units[0].unit_id})
    assert proxy.binding.derivation_sha == derivation.sha256


@pytest.mark.parametrize("change", ["empty", "foreign", "proxy_cycle"])
def test_proxy_dependencies_are_original_and_same_extraction(proxy_source, change):
    from tests.helpers.corpus import artifact, seed_bundle
    from tychora.ingestion.assets import ProxyFactory

    derivation = artifact(b'{"task":"caption_copy"}', "application/json")
    factory = ProxyFactory(proxy_source)
    if change == "empty":
        dependencies = ()
    elif change == "foreign":
        dependencies = (seed_bundle("Foreign.").units[0].unit_id,)
    else:
        copied = factory.make_proxy(
            proxy_source.assets[0].asset_id,
            "caption_copy",
            proxy_source.units[0].text,
            (proxy_source.units[0].unit_id,),
            derivation,
        )
        dependencies = (copied.unit.unit_id,)
    with pytest.raises(ValueError):
        factory.make_proxy(
            proxy_source.assets[0].asset_id,
            "description",
            "Derived observation",
            dependencies,
            derivation,
        )


def test_rejected_proxy_and_missing_original_support_are_excluded(proxy_source):
    from tests.helpers.corpus import artifact
    from tychora.ingestion.assets import ProxyFactory, proxy_context_allowed

    proxy = ProxyFactory(proxy_source).make_proxy(
        proxy_source.assets[0].asset_id,
        "description",
        "Visual observation",
        (proxy_source.units[0].unit_id,),
        artifact(b"receipt", "text/plain"),
    )
    args = {
        "review_state": "needs_review",
        "permitted_review_states": frozenset({"needs_review", "rejected"}),
        "eligible_original_ids": proxy.binding.required_unit_ids,
        "asset_eligible": True,
    }
    assert proxy_context_allowed(proxy, **args)
    assert not proxy_context_allowed(proxy, **{**args, "review_state": "rejected"})
    assert not proxy_context_allowed(
        proxy, **{**args, "eligible_original_ids": frozenset()}
    )
    assert not proxy_context_allowed(proxy, **{**args, "asset_eligible": False})


def test_image_only_original_dependency_and_existing_caption_proxy_cycle(proxy_source):
    from hashlib import sha256

    from tests.helpers.corpus import artifact
    from tychora.ingestion.assets import ProxyFactory

    unit = proxy_source.units[0].model_copy(
        update={
            "unit_type": "picture",
            "text": "",
            "text_sha": sha256(b"").hexdigest(),
            "locators": (proxy_source.assets[0].locator,),
        }
    )
    source = proxy_source.model_copy(update={"units": (unit,)})
    receipt = artifact(b"original image description input", "text/plain")
    generated = ProxyFactory(source).make_proxy(
        source.assets[0].asset_id,
        "description",
        "Blue marker",
        (unit.unit_id,),
        receipt,
    )
    assert generated.binding.required_unit_ids == frozenset({unit.unit_id})
    copied = ProxyFactory(proxy_source).make_proxy(
        proxy_source.assets[0].asset_id,
        "caption_copy",
        proxy_source.units[0].text,
        (unit.unit_id,),
        receipt,
    )
    augmented = proxy_source.model_copy(
        update={
            "units": (*proxy_source.units, copied.unit),
            "proxies": (copied.binding,),
        }
    )
    with pytest.raises(ValueError, match="original"):
        ProxyFactory(augmented).make_proxy(
            source.assets[0].asset_id,
            "description",
            "Proxy chain",
            (copied.unit.unit_id,),
            receipt,
        )


def test_decompression_bomb_and_invalid_resize_are_bounded(monkeypatch):
    from PIL import Image

    from tychora.ingestion.assets import validate_image

    payload = image_bytes()
    for resize in ((0, 1), (9, 6), (4,), (4, 3, 2)):
        with pytest.raises(ValueError):
            validate_image(payload, "image/png", resize_to=resize)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 20)
    with pytest.raises(ValueError, match="pixel capacity"):
        validate_image(payload, "image/png")


def test_proxy_derivation_cannot_reuse_retained_source_payload(proxy_source):
    from tests.helpers.corpus import artifact
    from tychora.contracts.corpus import RevisionArtifactRow
    from tychora.ingestion.assets import ProxyFactory

    source_ref = artifact(b'{"source":"retained SDK sidecar"}', "application/json")
    proxy_source = proxy_source.model_copy(
        update={
            "artifacts": (*proxy_source.artifacts, source_ref),
            "revision_artifacts": (
                *proxy_source.revision_artifacts,
                RevisionArtifactRow(
                    revision_id=proxy_source.revision.revision_id,
                    role="sidecar",
                    source_ref="source.json",
                    artifact_sha=source_ref.sha256,
                ),
            ),
        }
    )
    with pytest.raises(ValueError, match="original"):
        ProxyFactory(proxy_source).make_proxy(
            proxy_source.assets[0].asset_id,
            "description",
            "Derived text",
            (proxy_source.units[0].unit_id,),
            source_ref,
        )


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_supported_pillow_formats_use_actual_bytes(format, mime):
    from PIL import Image

    from tychora.ingestion.assets import validate_image

    stream = BytesIO()
    Image.new("RGB", (8, 6), "blue").save(stream, format=format)
    validated = validate_image(
        stream.getvalue(), mime, expected_size=(8, 6), resize_to=(4, 3)
    )
    assert validated.mime == mime
    assert (
        validate_image(validated.data, mime, expected_size=(4, 3)).output_sha
        == validated.output_sha
    )
