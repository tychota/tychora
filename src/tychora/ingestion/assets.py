"""Bounded actual image validation and same-extraction proxy construction."""

import warnings
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import cast

from tychora.contracts.artifacts import ArtifactRef
from tychora.contracts.assets import ProxyRecord
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import AssetProxyRow, ExtractionBundle
from tychora.contracts.evidence import EvidenceUnit, ImageLocator
from tychora.contracts.identity import digest, unit_id

MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime: str
    width: int
    height: int
    source_sha: str
    output_sha: str
    resize_from: tuple[int, int] | None = None
    resampling: str | None = None


def validate_image(
    data: bytes,
    mime: str,
    *,
    expected_size: tuple[int, int] | None = None,
    resize_to: tuple[int, int] | None = None,
) -> ValidatedImage:
    from PIL import Image

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image payload exceeds capacity")
    formats = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as decoded:
                width, height = original_size = decoded.size
                image_format = decoded.format
                if (
                    formats.get(image_format or "") != mime
                    or width * height > MAX_IMAGE_PIXELS
                    or expected_size is not None
                    and expected_size != original_size
                ):
                    raise ValueError("Image MIME, dimensions or pixel capacity differs")
                if resize_to is not None and (
                    len(resize_to) != 2
                    or any(type(value) is not int or value <= 0 for value in resize_to)
                    or resize_to[0] > width
                    or resize_to[1] > height
                    or resize_to[0] * resize_to[1] > MAX_IMAGE_PIXELS
                ):
                    raise ValueError(
                        "Explicit resize must fit the original pixel bounds"
                    )
                decoded.load()
                output = data
                if resize_to is not None:
                    with decoded.resize(resize_to, Image.Resampling.LANCZOS) as resized:
                        encoded = BytesIO()
                        resized.save(encoded, format=image_format)
                        output = encoded.getvalue()
                    width, height = resize_to
                if len(output) > MAX_IMAGE_BYTES:
                    raise ValueError("Resized image payload exceeds capacity")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Image is invalid or exceeds decoded pixel capacity") from None
    return ValidatedImage(
        output,
        mime,
        width,
        height,
        sha256(data).hexdigest(),
        sha256(output).hexdigest(),
        original_size if resize_to is not None else None,
        "LANCZOS" if resize_to is not None else None,
    )


class ProxyFactory:
    """One deterministic construction pass over a complete original-unit set."""

    def __init__(self, bundle: ExtractionBundle):
        self.bundle = bundle
        self.assets = {asset.asset_id: asset for asset in bundle.assets}
        self.proxy_ids = {proxy.proxy_unit_id for proxy in bundle.proxies}
        self.originals = {
            unit.unit_id: unit
            for unit in bundle.units
            if unit.text_origin == "source" and unit.unit_id not in self.proxy_ids
        }
        self.ordinal = max((unit.ordinal for unit in bundle.units), default=-1) + 1

    def make_proxy(
        self,
        asset_id: str,
        kind: str,
        text: str,
        dependencies: tuple[str, ...],
        derivation: ArtifactRef,
    ) -> ProxyRecord:
        asset = self.assets.get(asset_id)
        eid = self.bundle.extraction.extraction_id
        if (
            asset is None
            or asset.extraction_id != eid
            or kind not in ("caption_copy", "ocr", "description", "structured_values")
            or not dependencies
            or len(set(dependencies)) != len(dependencies)
            or any(uid not in self.originals for uid in dependencies)
            or any(self.originals[uid].extraction_id != eid for uid in dependencies)
            or derivation.sha256 in {ref.sha256 for ref in self.bundle.artifacts}
        ):
            raise ValueError(
                "Proxy requires one asset and original same-extraction dependencies"
            )
        required = set(dependencies)
        if kind == "caption_copy":
            captions = {
                link.unit_id
                for link in self.bundle.unit_assets
                if link.asset_id == asset_id and link.role == "caption"
            }
            if text != asset.caption or not captions.intersection(required):
                raise ValueError("Caption copy must retain the linked original caption")
            required.update(captions)
        required.update(
            link.unit_id
            for link in self.bundle.unit_assets
            if link.asset_id == asset_id and link.role in {"source", "caption"}
        )
        if any(
            uid not in self.originals or self.originals[uid].extraction_id != eid
            for uid in required
        ):
            raise ValueError(
                "Caption dependencies must be original same-extraction units"
            )
        pending = list(required)
        while pending:
            uid = pending.pop()
            for owner_id in cast(
                tuple[str, ...],
                self.originals[uid].attributes.get("overlapping_owner_ids", ()),
            ):
                if (
                    owner_id not in self.originals
                    or self.originals[owner_id].extraction_id != eid
                ):
                    raise ValueError(
                        "Proxy overlapping owners must be original same-extraction units"
                    )
                if owner_id not in required:
                    required.add(owner_id)
                    pending.append(owner_id)
        key = digest(
            "asset-proxy-input-v1",
            asset_id,
            kind,
            text,
            sorted(required),
            derivation.sha256,
        )
        native_ref = "asset-proxy:" + key
        text_sha = sha256(text.encode()).hexdigest()
        uid = unit_id(eid, native_ref, self.ordinal, text_sha)
        locator = (
            asset.locator
            if asset.locator.type == "image"
            else ImageLocator(
                artifact_ref=asset.locator.artifact_ref,
                native_ref=asset.locator.native_ref,
            )
        )
        unit = EvidenceUnit(
            unit_id=uid,
            extraction_id=eid,
            parent_id=None,
            ordinal=self.ordinal,
            unit_type="asset_proxy",
            native_ref=native_ref,
            boundary_key=native_ref,
            text=text,
            text_sha=text_sha,
            text_origin="source"
            if kind == "caption_copy"
            else "ocr"
            if kind == "ocr"
            else "derived",
            citable=False,
            review_state="unreviewed" if kind == "caption_copy" else "needs_review",
            locators=(locator,),
            attributes=FrozenJsonMap({"asset_id": asset_id, "proxy_kind": kind}),
        )
        result = ProxyRecord(
            unit=unit,
            binding=AssetProxyRow.model_validate(
                {
                    "proxy_unit_id": uid,
                    "asset_id": asset_id,
                    "origin_type": kind,
                    "derivation_sha": derivation.sha256,
                    "required_unit_ids": frozenset(required),
                }
            ),
            derivation=derivation,
        )
        self.proxy_ids.add(uid)
        self.ordinal += 1
        return result


def proxy_context_allowed(
    proxy: ProxyRecord,
    *,
    review_state: str,
    permitted_review_states: frozenset[str],
    eligible_original_ids: frozenset[str],
    asset_eligible: bool,
) -> bool:
    """Narrow the caller's current source, review and asset eligibility."""
    return (
        asset_eligible
        and review_state != "rejected"
        and review_state in permitted_review_states
        and bool(proxy.binding.required_unit_ids)
        and proxy.binding.required_unit_ids <= eligible_original_ids
    )
