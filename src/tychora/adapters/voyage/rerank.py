"""Strict raw Voyage rerank response mapping."""

from __future__ import annotations

import math
from hashlib import sha256
from typing import TYPE_CHECKING

from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import canonical_bytes
from tychora.ports.reranking import (
    RankedIndex,
    RankingInput,
    RerankCapabilities,
)
from tychora.ports.tokens import TokenCounter

from .request import VoyageRequestExecutor
from .response import _response_object
from .runtime import VoyageRuntime
from .validation import qualify_voyage_rerank

if TYPE_CHECKING:
    from tychora.application.reranking import OwnerRerankAccess
    from tychora.ports.execution import ExecutionContext


def build_rerank_body(
    model: str, query: str, representations: tuple[RankingInput, ...]
) -> bytes:
    return canonical_bytes(
        {
            "model": model,
            "query": query,
            "documents": [item.text for item in representations],
            "top_k": len(representations),
            "truncation": False,
        }
    )


class VoyageReranker:
    """Raw, complete one-request rerank through a mandatory owner gate."""

    @property
    def credential_binding_id(self) -> str:
        return self._runtime.credential_binding_id

    @property
    def available(self) -> bool:
        return self._runtime.available

    def __init__(
        self,
        *,
        capabilities: RerankCapabilities,
        counter: TokenCounter,
        runtime: VoyageRuntime,
        access: OwnerRerankAccess,
    ) -> None:
        if (
            capabilities != qualify_voyage_rerank(counter.model, counter.tokenizer_id)
            or runtime.workspace_id != access.owner.workspace_id
        ):
            raise TychoraError(
                "CAPABILITY_MISSING", "Reranker composition is unavailable."
            )
        self.capabilities = capabilities
        self._counter = counter
        self._runtime = runtime
        self._access = access
        self._requests = VoyageRequestExecutor(runtime)

    def rank(
        self,
        query: str,
        representations: tuple[RankingInput, ...],
        ctx: ExecutionContext,
    ) -> tuple[RankedIndex, ...]:
        validate_rerank_request(
            query, representations, self._counter, self.capabilities
        )
        body = build_rerank_body(self.capabilities.model, query, representations)
        self._access.require_prepared(query, representations, sha256(body).hexdigest())
        response = self._requests.post(
            "/v1/rerank",
            body,
            lambda payload_sha, attempt_ctx: self._access.authorize(
                payload_sha, attempt_ctx
            ),
            ctx,
        )
        return map_rerank_response(response.body, len(representations))


def validate_rerank_request(
    query: str,
    representations: tuple[RankingInput, ...],
    counter: TokenCounter,
    capabilities: RerankCapabilities,
) -> None:
    def counted(value: str) -> int:
        try:
            tokens = counter.count(value)
        except Exception:  # noqa: BLE001 - sanitize local tokenizer boundary
            raise TychoraError(
                "CAPABILITY_MISSING", "Reranker token accounting is unavailable."
            ) from None
        if type(tokens) is not int or tokens <= 0:
            raise TychoraError(
                "CAPABILITY_MISSING", "Reranker token accounting is unavailable."
            )
        return tokens

    if (
        counter.model != capabilities.model
        or counter.tokenizer_id != capabilities.tokenizer_id
        or not query
        or not representations
        or len(representations) > capabilities.max_candidates
        or len({item.key for item in representations}) != len(representations)
    ):
        raise TychoraError("INVALID_REQUEST", "Rerank request is unavailable or empty.")
    query_tokens = counted(query)
    if query_tokens > capabilities.max_query_tokens:
        raise TychoraError("INVALID_REQUEST", "Rerank query exceeds token limit.")
    total = query_tokens * len(representations)
    for item in representations:
        tokens = counted(item.text)
        if (
            tokens != item.token_count
            or tokens > capabilities.max_representation_tokens
            or query_tokens + tokens > capabilities.max_pair_tokens
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Rerank representation exceeds token limit."
            )
        total += tokens
    if total > capabilities.max_aggregate_tokens:
        raise TychoraError("INVALID_REQUEST", "Rerank request exceeds aggregate limit.")


def validate_indices(indices: tuple[object, ...], count: int) -> None:
    if (
        type(count) is not int
        or count < 0
        or len(indices) != count
        or any(type(index) is not int for index in indices)
        or set(indices) != set(range(count))
    ):
        raise TychoraError(
            "PROVIDER_FAILURE", "Reranker returned invalid candidate mapping."
        )


def map_rerank_response(body: bytes, count: int) -> tuple[RankedIndex, ...]:
    response = _response_object(body)
    entries = response.get("data")
    if not isinstance(entries, list) or len(entries) != count:
        raise TychoraError("PROVIDER_FAILURE", "Invalid reranker response.")
    indices = tuple(
        entry.get("index") if isinstance(entry, dict) else None for entry in entries
    )
    validate_indices(indices, count)
    mapped: list[RankedIndex] = []
    for entry in entries:
        assert isinstance(entry, dict)
        score = entry.get("relevance_score")
        if type(score) not in (float, int):
            raise TychoraError("PROVIDER_FAILURE", "Invalid reranker response.")
        try:
            numeric = float(score)
        except OverflowError:
            raise TychoraError(
                "PROVIDER_FAILURE", "Invalid reranker response."
            ) from None
        if not math.isfinite(numeric):
            raise TychoraError("PROVIDER_FAILURE", "Invalid reranker response.")
        mapped.append(RankedIndex(index=entry["index"], score=numeric))
    return tuple(sorted(mapped, key=lambda item: (-item.score, item.index)))
