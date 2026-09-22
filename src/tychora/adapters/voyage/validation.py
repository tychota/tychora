"""Provider-independent validation for stored embedding values."""

import math
import struct
from collections.abc import Sequence
from typing import Literal

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import EmbeddingCapabilities, VisualEmbeddingLimits
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, space_id
from tychora.ports.reranking import RerankCapabilities
from tychora.ports.tokens import TokenCounter


def qualify_voyage_rerank(model: str, tokenizer_id: str) -> RerankCapabilities:
    """Only the independently pinned stable model has qualified public limits."""
    revision = "5cf631991ecd82024c3dc64d8e34de27318d5b9e"
    if model != "rerank-2.5" or tokenizer_id != f"{model}:{revision}":
        raise TychoraError(
            "CAPABILITY_MISSING", "Configured reranker capability is unavailable."
        )
    return RerankCapabilities(
        provider="voyage",
        endpoint="https://api.voyageai.com/v1/rerank",
        model=model,
        model_revision=revision,
        tokenizer_id=tokenizer_id,
        tokenizer_sha256="c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539",
        max_query_tokens=2000,
        max_representation_tokens=1200,
        max_candidates=64,
        max_pair_tokens=32000,
        max_aggregate_tokens=600000,
    )


def qualify_voyage_capabilities(
    *,
    model: str,
    contextualization: Literal["joint", "independent"],
    counter: TokenCounter | None,
) -> EmbeddingCapabilities:
    """Validate explicitly provisioned model/tokenizer capability facts."""
    expected_mode = {
        "voyage-context-4": "joint",
        "voyage-code-4": "independent",
        "voyage-code-3": "independent",
    }.get(model)
    if (
        expected_mode != contextualization
        or counter is None
        or counter.model != model
        or not counter.tokenizer_id
    ):
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Configured embedding model or tokenizer is unavailable.",
        )
    options = FrozenJsonMap(
        {
            "input_type": "document",
            "enable_auto_chunking": False,
            "truncation": False,
        }
    )
    representation = digest(
        "text-representation-v1",
        model,
        1024,
        "float32",
        contextualization,
        counter.tokenizer_id,
        "capsule-window-v1"
        if contextualization == "independent"
        else "contextual-group-v1",
        options,
    )
    return EmbeddingCapabilities(
        provider="voyage",
        endpoint="https://api.voyageai.com",
        model=model,
        dimensions=1024,
        input_types=frozenset({"document", "query"}),
        contextualization=contextualization,
        tokenizer_id=counter.tokenizer_id,
        space_id=space_id("text", "voyage", model, 1024, "cosine", representation),
        representation_fingerprint=representation,
        max_group_tokens=24_000,
        max_request_tokens=28_000,
        options=options,
    )


def qualify_voyage_visual_capabilities(
    *, counter: TokenCounter | None, limits: VisualEmbeddingLimits
) -> EmbeddingCapabilities:
    """Bind one locally qualified multimodal tokenizer/configuration receipt."""
    model = "voyage-multimodal-3.5"
    if (
        counter is None
        or counter.model != model
        or not counter.tokenizer_id
        or limits.accounting_image_pixels_min != 50_000
        or limits.accounting_image_pixels_max != 2_000_000
        or limits.image_pixels_per_token != 560
    ):
        raise TychoraError(
            "CAPABILITY_MISSING",
            "Configured visual embedding model or accounting resources are unavailable.",
        )
    options = FrozenJsonMap(
        {
            "input_type": "document",
            "output_dimension": 1024,
            "output_dtype": "float",
            "truncation": False,
        }
    )
    representation = digest(
        "visual-representation-v1",
        model,
        1024,
        "float32",
        counter.tokenizer_id,
        limits.model_revision,
        limits.tokenizer_sha256,
        limits.client_config_sha256,
        limits.accounting_version,
        "exif-transpose-rgb-lossless-png-v1",
        options,
    )
    return EmbeddingCapabilities(
        provider="voyage",
        endpoint="https://api.voyageai.com",
        model=model,
        dimensions=1024,
        input_types=frozenset({"document", "query"}),
        contextualization="independent",
        tokenizer_id=counter.tokenizer_id,
        space_id=space_id("visual", "voyage", model, 1024, "cosine", representation),
        representation_fingerprint=representation,
        max_group_tokens=limits.input_tokens_max,
        max_request_tokens=limits.request_tokens_max,
        options=options,
        modality="visual",
        visual_limits=limits,
    )


def normalize_vector(values: Sequence[float], dimension: int) -> bytes:
    """Return the exact normalized little-endian float32 storage bytes."""
    norm = math.sqrt(sum(value * value for value in values))
    if (
        len(values) != dimension
        or not all(math.isfinite(value) for value in values)
        or not math.isfinite(norm)
        or norm == 0
    ):
        raise TychoraError("PROVIDER_FAILURE", "Invalid embedding shape or values.")
    return struct.pack("<" + "f" * dimension, *(value / norm for value in values))
