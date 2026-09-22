"""Actual-pixel multimodal kernel, used through the canonical workspace owner."""

from __future__ import annotations

import base64
import math
from hashlib import sha256
from io import BytesIO
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer
    from PIL.Image import Image

from tychora.application.visual import OwnerVisualAccess
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    EmbeddingRecord,
    RegisteredImageQuery,
    TextOrImage,
    TextQuery,
    VisualInput,
    VisualQueryEmbeddingProbe,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes, digest
from tychora.ports.execution import ExecutionContext
from tychora.ports.tokens import TokenCounter

from .request import VoyageRequestExecutor
from .response import _flat_stored_response
from .runtime import VoyageRuntime
from .validation import qualify_voyage_visual_capabilities


class _EncodedBuffer(BytesIO):
    def write(self, value: ReadableBuffer) -> int:
        if self.tell() + memoryview(value).nbytes > 20_000_000:
            raise TychoraError(
                "INVALID_REQUEST", "Encoded visual representation exceeds its limit."
            )
        return super().write(value)


class VoyageVisualEmbedder:
    """One decoded image and one complete input per deterministic request.

    Smaller batches intentionally bound staging. Trusted Python can replace
    objects or call internals; this interface is not an extension sandbox.
    """

    def __init__(
        self,
        *,
        capabilities: EmbeddingCapabilities,
        counter: TokenCounter,
        runtime: VoyageRuntime,
        access: OwnerVisualAccess,
    ) -> None:
        if (
            type(access) is not OwnerVisualAccess
            or capabilities.visual_limits is None
            or capabilities
            != qualify_voyage_visual_capabilities(
                counter=counter, limits=capabilities.visual_limits
            )
            or runtime.workspace_id != access.owner.workspace_id
        ):
            raise TychoraError(
                "CAPABILITY_MISSING", "Visual embedding composition is unavailable."
            )
        self.capabilities = capabilities
        self._limits = capabilities.visual_limits
        self._counter = counter
        self._runtime = runtime
        self._access = access
        self._requests = VoyageRequestExecutor(runtime)

    def _key(self, query: TextOrImage) -> tuple[object, ...]:
        _, ids, operation_sha = self._access.operation(query, "query")
        return (
            self._runtime.workspace_id,
            self._access.cache_scope,
            self._runtime.credential_binding_id,
            self.capabilities.endpoint,
            self.capabilities.space_id,
            self.capabilities.representation_fingerprint,
            self.capabilities.dimensions,
            self.capabilities.dtype,
            "query",
            digest("embedding-options-v1", self.capabilities.options),
            ids,
            operation_sha,
        )

    def _record(
        self, identity: str, stored: bytes, mode: Literal["query", "document"]
    ) -> EmbeddingRecord:
        input_sha = digest(
            "visual-embedding-input-v1",
            mode,
            identity,
            self.capabilities.space_id,
            "exif-transpose-rgb-lossless-png-v1",
        )
        return EmbeddingRecord.from_normalized_bytes(
            record_id=digest(
                "visual-embedding-record-v1", input_sha, self.capabilities.space_id
            ),
            input_sha=input_sha,
            space_id=self.capabilities.space_id,
            stored=stored,
        )

    def probe_query(
        self, query: TextOrImage, ctx: ExecutionContext
    ) -> VisualQueryEmbeddingProbe:
        items, ids, operation_sha = self._access.operation(query, "query")
        self._access.read_scope(items, ctx)
        self._tokens(
            query.text if isinstance(query, TextQuery) else query.image.context,
            None if isinstance(query, TextQuery) else query.image,
        )
        stored = self._runtime.cached_query(self._key(query))
        return VisualQueryEmbeddingProbe(
            workspace_id=self._runtime.workspace_id,
            provider="voyage",
            query_sha=digest("visual-query-probe-v1", query.model_dump(mode="json")),
            operation_sha=operation_sha,
            state="hit" if stored is not None else "miss",
            record=self._record(ids[0], stored, "query")
            if stored is not None
            else None,
        )

    def embed_query(self, query: TextOrImage, ctx: ExecutionContext) -> EmbeddingRecord:
        self._access.require_lane()
        probe = self.probe_query(query, ctx)
        if probe.record is not None:
            return probe.record
        self._access.authorize(query, "query", ctx)
        stored = self._one(query, "query", ctx)
        self._runtime.cache_query(self._key(query), stored)
        _, ids, _ = self._access.operation(query, "query")
        return self._record(ids[0], stored, "query")

    def embed_assets(
        self, items: tuple[VisualInput, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]:
        self._access.require_lane()
        # A document operation may contain many items; each actual request has
        # exactly one complete asset and preserves original order and pixels.
        self._access.read_scope(items, ctx)
        result = []
        for item in items:
            ctx.checkpoint()
            result.append(
                self._record(
                    item.input_sha,
                    self._one(RegisteredImageQuery(image=item), "document", ctx),
                    "document",
                )
            )
        return tuple(result)

    def _tokens(self, text: str, item: VisualInput | None) -> int:
        if len(text.encode("utf-8")) > 1_048_576:
            raise TychoraError(
                "INVALID_REQUEST", "Visual context exceeds staging capacity."
            )
        limits = self._limits
        count = self._counter.count(text) if text else 0
        if type(count) is not int or count < 0:
            raise TychoraError(
                "CAPABILITY_MISSING", "Visual accounting is unavailable."
            )
        if item is not None:
            pixels = item.width * item.height
            if (
                pixels > limits.source_pixels_max
                or pixels * 3 > limits.decoded_image_bytes_max
                or item.resize.source_width * item.resize.source_height
                > limits.source_pixels_max
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Visual source geometry exceeds its limit."
                )
            count += math.ceil(
                min(
                    limits.accounting_image_pixels_max,
                    max(limits.accounting_image_pixels_min, pixels),
                )
                / limits.image_pixels_per_token
            )
        if count <= 0 or count > min(
            limits.input_tokens_max, limits.request_tokens_max
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Visual input exceeds the token limit."
            )
        return count

    def _image(
        self,
        item: VisualInput,
        mode: Literal["query", "document"],
        ctx: ExecutionContext,
    ) -> str:
        from PIL import Image, ImageOps

        raw = self._access.read(item, mode, ctx)
        limits = self._limits
        if len(raw) > limits.image_encoded_bytes_max:
            raise TychoraError(
                "INVALID_REQUEST", "Visual source exceeds the encoded limit."
            )
        try:
            with Image.open(BytesIO(raw)) as source:
                # Image.open parses headers lazily. Check before load/transpose.
                if (
                    source.width <= 0
                    or source.height <= 0
                    or source.width * source.height > limits.source_pixels_max
                    or getattr(source, "n_frames", 1) != 1
                ):
                    raise ValueError
                ctx.checkpoint()
                with ImageOps.exif_transpose(source) as oriented:
                    if oriented.size != (
                        item.resize.source_width,
                        item.resize.source_height,
                    ):
                        raise ValueError
                    with oriented.convert("RGB") as rgb:
                        del raw
                        if item.resize.mode == "none":
                            return self._encode(rgb, item, ctx)
                        with rgb.resize(
                            (item.width, item.height), Image.Resampling.LANCZOS
                        ) as resized:
                            return self._encode(resized, item, ctx)
        except TychoraError:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted decoder failures
            raise TychoraError(
                "INTEGRITY_ERROR", "Visual pixels could not be verified."
            ) from None

    def _encode(self, rgb: Image, item: VisualInput, ctx: ExecutionContext) -> str:
        if rgb.size != (item.width, item.height):
            raise TychoraError("INTEGRITY_ERROR", "Visual geometry differs.")
        pixels = rgb.tobytes()
        if sha256(pixels).hexdigest() != item.pixel_sha256:
            raise TychoraError("INTEGRITY_ERROR", "Visual pixel identity differs.")
        del pixels
        ctx.checkpoint()
        with _EncodedBuffer() as output:
            rgb.save(output, format="PNG")
            if output.tell() > self._limits.image_encoded_bytes_max:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Visual representation exceeds the encoded limit.",
                )
            with output.getbuffer() as encoded:
                # The normalized representation is lossless; original bytes stay
                # retained and need not equal the transmitted PNG representation.
                return "data:image/png;base64," + base64.b64encode(encoded).decode(
                    "ascii"
                )

    def _one(
        self,
        query: TextOrImage,
        mode: Literal["query", "document"],
        ctx: ExecutionContext,
    ) -> bytes:
        item = query.image if isinstance(query, RegisteredImageQuery) else None
        text = query.text if isinstance(query, TextQuery) else query.image.context
        self._tokens(text, item)
        content = []
        if text:
            content.append({"type": "text", "text": text})
        if item is not None:
            content.append(
                {"type": "image_base64", "image_base64": self._image(item, mode, ctx)}
            )
        body = canonical_bytes(
            {
                "inputs": [{"content": content}],
                "model": self.capabilities.model,
                "input_type": mode,
                "output_dimension": self.capabilities.dimensions,
                "output_dtype": "float",
                "truncation": False,
            }
        )
        del content
        limits = self._limits
        if len(body) > min(limits.request_wire_bytes_max, 64 * 1024 * 1024):
            raise TychoraError(
                "INVALID_REQUEST", "Visual wire request exceeds its limit."
            )
        expected_sha = sha256(body).hexdigest()

        def authorize(actual_sha: str, attempt_ctx: ExecutionContext) -> None:
            if actual_sha != expected_sha:
                raise TychoraError("INTEGRITY_ERROR", "Visual wire identity differs.")
            self._access.authorize(query, mode, attempt_ctx, body_sha=actual_sha)

        response = self._requests.post("/v1/multimodalembeddings", body, authorize, ctx)
        return _flat_stored_response(response.body, 1, self.capabilities.dimensions)[0]
