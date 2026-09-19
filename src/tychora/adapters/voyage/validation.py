"""Provider-independent validation for stored embedding values."""

import math
import struct
from collections.abc import Sequence
from typing import Literal

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import EmbeddingCapabilities
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, space_id
from tychora.ports.tokens import TokenCounter


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
