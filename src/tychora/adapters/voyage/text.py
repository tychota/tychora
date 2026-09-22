"""Qualified raw Voyage text embedding adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from tychora.adapters.voyage.request import HttpxVoyageTransport, VoyageRequestExecutor
from tychora.adapters.voyage.request_types import RawVoyageResponse
from tychora.adapters.voyage.response import (
    _flat_stored_response,
    _indexed,
    _response_object,
    _stored_vector,
)
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    EmbeddingRecord,
    QueryEmbeddingProbe,
    QueryUploadAuthorization,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import JsonValue, canonical_bytes, digest
from tychora.contracts.indexing import ContextGroup
from tychora.ports.execution import ExecutionContext
from tychora.ports.tokens import TokenCounter

INDEPENDENT_RENDERER_VERSION = "capsule-window-v1"

__all__ = ["HttpxVoyageTransport", "RawVoyageResponse", "VoyageRuntime"]


@dataclass(frozen=True, slots=True)
class RenderedInput:
    text: str
    utf8_sha: str
    input_sha: str
    token_count: int


@dataclass(frozen=True, slots=True)
class QueryPlan:
    prepared: tuple[RenderedInput, ...]
    keys: tuple[tuple[object, ...], ...]
    stored: tuple[bytes | None, ...]
    missing: tuple[int, ...]
    path: str | None
    body: bytes | None
    authorization: QueryUploadAuthorization | None


@dataclass(frozen=True, slots=True)
class QueryOperationBinding:
    """Trusted operation-local scope facts for caller-query upload."""

    principal_upload_scope_sha: str
    collection_ids: frozenset[str]
    dependency_ids: frozenset[str]
    policy_epoch: int

    def __post_init__(self) -> None:
        if (
            len(self.principal_upload_scope_sha) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.principal_upload_scope_sha
            )
            or not self.collection_ids
            or len(self.collection_ids) > 1000
            or len(self.dependency_ids) > 2048
            or type(self.policy_epoch) is not int
            or self.policy_epoch < 0
        ):
            raise ValueError("Invalid query operation binding")


type DocumentAuthorizer = Callable[[str, frozenset[str], ExecutionContext], None]
type QueryAuthorizer = Callable[[QueryUploadAuthorization, ExecutionContext], None]


class VoyageTextEmbedder:
    """Synchronous kernel for bounded owner-dispatched text embedding calls."""

    def __init__(
        self,
        *,
        capabilities: EmbeddingCapabilities,
        counter: TokenCounter,
        runtime: VoyageRuntime,
        authorize_document: DocumentAuthorizer,
        query_binding: QueryOperationBinding | None = None,
        authorize_query: QueryAuthorizer | None = None,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        if (
            counter.model != capabilities.model
            or counter.tokenizer_id != capabilities.tokenizer_id
        ):
            raise TychoraError(
                "CAPABILITY_MISSING",
                "Configured embedding tokenizer is unavailable.",
            )
        self.capabilities = capabilities
        self._counter = counter
        self._runtime = runtime
        self._authorize_document = authorize_document
        self._query_binding = query_binding
        self._authorize_query = authorize_query
        self._requests = VoyageRequestExecutor(runtime, backoff=backoff)

    def embed_groups(
        self, groups: tuple[ContextGroup, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]:
        if not groups:
            return ()
        group_ids = [group.group_id for group in groups]
        window_ids = [window_id for group in groups for window_id in group.window_ids]
        request_indices = [group.request_index for group in groups]
        if (
            len(set(group_ids)) != len(group_ids)
            or len(set(window_ids)) != len(window_ids)
            or request_indices != sorted(request_indices)
            or set(request_indices) != set(range(request_indices[-1] + 1))
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Embedding group mapping is ambiguous."
            )
        output: list[EmbeddingRecord] = []
        start = 0
        while start < len(groups):
            request_index = groups[start].request_index
            end = start + 1
            while end < len(groups) and groups[end].request_index == request_index:
                end += 1
            batch = groups[start:end]
            output.extend(self._embed_group_batch(batch, ctx))
            start = end
        return tuple(output)

    def embed_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]:
        plan = self._query_plan(queries, ctx)
        if not plan.missing:
            return self._query_records(plan)
        authorizer = self._authorize_query
        if (
            authorizer is None
            or plan.authorization is None
            or plan.path is None
            or plan.body is None
        ):
            raise TychoraError("INTEGRITY_ERROR", "Query upload plan is incomplete.")
        authorization = plan.authorization

        def authorize(payload_sha: str, attempt_ctx: ExecutionContext) -> None:
            if payload_sha != authorization.payload_sha:
                raise TychoraError(
                    "INTEGRITY_ERROR", "Query upload payload identity changed."
                )
            authorizer(authorization, attempt_ctx)

        response = self._post(plan.path, plan.body, authorize, ctx)
        mapper = (
            _contextual_query_stored_response
            if self.capabilities.contextualization == "joint"
            else _flat_stored_response
        )
        returned = mapper(
            response.body, len(plan.missing), self.capabilities.dimensions
        )
        stored = list(plan.stored)
        for index, value in zip(plan.missing, returned, strict=True):
            stored[index] = value
            self._runtime.cache_query(plan.keys[index], value)
        return self._query_records(
            QueryPlan(
                prepared=plan.prepared,
                keys=plan.keys,
                stored=tuple(stored),
                missing=(),
                path=None,
                body=None,
                authorization=None,
            )
        )

    def probe_queries(
        self, queries: tuple[str, ...], ctx: ExecutionContext
    ) -> QueryEmbeddingProbe:
        plan = self._query_plan(queries, ctx)
        query_batch_sha = digest("query-embedding-probe-v1", queries)
        if not plan.missing:
            return QueryEmbeddingProbe(
                workspace_id=self._runtime.workspace_id,
                provider=self.capabilities.provider,
                query_batch_sha=query_batch_sha,
                input_count=len(queries),
                state="hit",
                records=self._query_records(plan),
            )
        return QueryEmbeddingProbe(
            workspace_id=self._runtime.workspace_id,
            provider=self.capabilities.provider,
            query_batch_sha=query_batch_sha,
            input_count=len(queries),
            state="miss",
            authorization=plan.authorization,
        )

    def _query_records(self, plan: QueryPlan) -> tuple[EmbeddingRecord, ...]:
        output = []
        for item, value in zip(plan.prepared, plan.stored, strict=True):
            if value is None:
                raise TychoraError("INTEGRITY_ERROR", "Query cache value is absent.")
            output.append(
                EmbeddingRecord.from_normalized_bytes(
                    record_id=digest(
                        "query-embedding-record-v1",
                        item.input_sha,
                        self.capabilities.space_id,
                    ),
                    input_sha=item.input_sha,
                    space_id=self.capabilities.space_id,
                    stored=value,
                )
            )
        return tuple(output)

    def _query_plan(self, queries: tuple[str, ...], ctx: ExecutionContext) -> QueryPlan:
        ctx.checkpoint()
        if len(queries) > 256:
            raise TychoraError(
                "INVALID_REQUEST", "At most 256 queries may be embedded."
            )
        if not queries:
            return QueryPlan((), (), (), (), None, None, None)
        binding, authorizer = self._query_binding, self._authorize_query
        if binding is None or authorizer is None:
            raise TychoraError(
                "CAPABILITY_MISSING", "Query embedding is not bound for this operation."
            )
        prepared = tuple(self._prepare_query(query) for query in queries)
        if sum(item.token_count for item in prepared) > (
            self.capabilities.max_request_tokens
        ):
            raise TychoraError(
                "INVALID_REQUEST", "Embedding request exceeds qualified limits."
            )
        keys = tuple(
            self._query_cache_key(binding, item.input_sha) for item in prepared
        )
        stored = tuple(self._runtime.cached_query(key) for key in keys)
        missing = tuple(index for index, value in enumerate(stored) if value is None)
        if not missing:
            ctx.checkpoint()
            return QueryPlan(prepared, keys, stored, (), None, None, None)
        if self.capabilities.contextualization == "joint":
            payload: dict[str, JsonValue] = {
                "inputs": [[prepared[index].text] for index in missing],
                "model": self.capabilities.model,
                "input_type": "query",
                "output_dimension": self.capabilities.dimensions,
                "output_dtype": "float",
                "enable_auto_chunking": False,
            }
            path = "/v1/contextualizedembeddings"
        else:
            payload = {
                "input": [prepared[index].text for index in missing],
                "model": self.capabilities.model,
                "input_type": "query",
                "output_dimension": self.capabilities.dimensions,
                "output_dtype": "float",
                "truncation": False,
            }
            path = "/v1/embeddings"
        body = canonical_bytes(payload)
        authorization = QueryUploadAuthorization(
            payload_sha=sha256(body).hexdigest(),
            input_shas=tuple(prepared[index].input_sha for index in missing),
            collection_ids=binding.collection_ids,
            dependency_ids=binding.dependency_ids,
            policy_epoch=binding.policy_epoch,
        )
        ctx.checkpoint()
        return QueryPlan(prepared, keys, stored, missing, path, body, authorization)

    def _prepare_query(self, query: str) -> RenderedInput:
        if not query:
            raise TychoraError("INVALID_REQUEST", "Query text must be nonempty.")
        token_count = self._counter.count(query)
        if token_count <= 0 or token_count > self.capabilities.max_group_tokens:
            raise TychoraError(
                "INVALID_REQUEST", "One query exceeds the provider token limit."
            )
        utf8_sha = sha256(query.encode("utf-8")).hexdigest()
        input_sha = digest(
            "query-embedding-input-v1",
            utf8_sha,
            self.capabilities.provider,
            self.capabilities.model,
            self.capabilities.dimensions,
            self.capabilities.dtype,
            "query",
            self.capabilities.representation_fingerprint,
            self.capabilities.options,
        )
        return RenderedInput(query, utf8_sha, input_sha, token_count)

    def _query_cache_key(
        self, binding: QueryOperationBinding, input_sha: str
    ) -> tuple[object, ...]:
        return (
            self._runtime.workspace_id,
            binding.principal_upload_scope_sha,
            self._runtime.credential_binding_id,
            self.capabilities.endpoint,
            self.capabilities.model,
            self.capabilities.space_id,
            self.capabilities.representation_fingerprint,
            self.capabilities.dimensions,
            self.capabilities.dtype,
            "query",
            digest("embedding-options-v1", self.capabilities.options),
            input_sha,
        )

    def _embed_group_batch(
        self, groups: tuple[ContextGroup, ...], ctx: ExecutionContext
    ) -> tuple[EmbeddingRecord, ...]:
        if self.capabilities.contextualization == "joint":
            for group in groups:
                _validate_group(group, self.capabilities, "joint")
            token_counts = tuple(
                sum(self._counter.count(text) for text in group.input_texts)
                for group in groups
            )
            if (
                any(
                    group.contextualization != "joint"
                    or group.token_count != count
                    or count > self.capabilities.max_group_tokens
                    for group, count in zip(groups, token_counts, strict=True)
                )
                or sum(token_counts) > self.capabilities.max_request_tokens
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Embedding request exceeds qualified limits."
                )
            payload: dict[str, JsonValue] = {
                "inputs": [list(group.input_texts) for group in groups],
                "model": self.capabilities.model,
                "input_type": "document",
                "output_dimension": self.capabilities.dimensions,
                "output_dtype": "float",
                "enable_auto_chunking": False,
            }
            path = "/v1/contextualizedembeddings"
            mapper = lambda body: map_contextual_response(
                groups, body, self.capabilities
            )
        else:
            rendered = tuple(
                render_independent_input(group, self._counter, self.capabilities)
                for group in groups
            )
            if sum(item.token_count for item in rendered) > (
                self.capabilities.max_request_tokens
            ):
                raise TychoraError(
                    "INVALID_REQUEST", "Embedding request exceeds qualified limits."
                )
            payload = {
                "input": [item.text for item in rendered],
                "model": self.capabilities.model,
                "input_type": "document",
                "output_dimension": self.capabilities.dimensions,
                "output_dtype": "float",
                "truncation": False,
            }
            path = "/v1/embeddings"
            mapper = lambda body: _map_flat_group_response(
                groups, rendered, body, self.capabilities
            )
        body = canonical_bytes(payload)
        dependencies = frozenset().union(*(group.dependency_ids for group in groups))
        response = self._post(
            path,
            body,
            lambda payload_sha, attempt_ctx: self._authorize_document(
                payload_sha, dependencies, attempt_ctx
            ),
            ctx,
        )
        return mapper(response.body)

    def _post(
        self,
        path: str,
        body: bytes,
        authorize: Callable[[str, ExecutionContext], None],
        ctx: ExecutionContext,
    ) -> RawVoyageResponse:
        return self._requests.post(path, body, authorize, ctx)


def map_contextual_response(
    groups: tuple[ContextGroup, ...],
    body: bytes,
    capabilities: EmbeddingCapabilities,
) -> tuple[EmbeddingRecord, ...]:
    """Validate raw document/chunk indices before projecting window vectors."""
    response = _response_object(body)
    documents = _indexed(response.get("data"), len(groups), nested_key="data")
    validated: list[tuple[bytes, ...]] = []
    for group, document in zip(groups, documents, strict=True):
        if (
            group.contextualization != "joint"
            or capabilities.contextualization != "joint"
            or group.provider != capabilities.provider
            or group.model != capabilities.model
            or group.dimensions != capabilities.dimensions
            or group.dtype != capabilities.dtype
            or group.options != capabilities.options
            or group.tokenizer_id != capabilities.tokenizer_id
        ):
            raise TychoraError(
                "INVALID_REQUEST",
                "Embedding group differs from provider capabilities.",
            )
        chunks = _indexed(document["data"], len(group.input_texts))
        validated.append(
            tuple(_stored_vector(chunk, capabilities.dimensions) for chunk in chunks)
        )
    output: list[EmbeddingRecord] = []
    for group, stored in zip(groups, validated, strict=True):
        for window_id, value in zip(group.window_ids, stored[1:], strict=True):
            output.append(
                EmbeddingRecord.from_normalized_bytes(
                    record_id=digest(
                        "embedding-record-v1",
                        window_id,
                        group.input_sha,
                        capabilities.space_id,
                    ),
                    input_sha=group.input_sha,
                    space_id=capabilities.space_id,
                    stored=value,
                )
            )
    return tuple(output)


def _map_flat_group_response(
    groups: tuple[ContextGroup, ...],
    rendered: tuple[RenderedInput, ...],
    body: bytes,
    capabilities: EmbeddingCapabilities,
) -> tuple[EmbeddingRecord, ...]:
    response = _response_object(body)
    entries = _indexed(response.get("data"), len(groups))
    output = []
    for group, prepared, entry in zip(groups, rendered, entries, strict=True):
        stored = _stored_vector(entry, capabilities.dimensions)
        window_id = group.window_ids[0]
        output.append(
            EmbeddingRecord.from_normalized_bytes(
                record_id=digest(
                    "embedding-record-v1",
                    window_id,
                    prepared.input_sha,
                    capabilities.space_id,
                ),
                input_sha=prepared.input_sha,
                space_id=capabilities.space_id,
                stored=stored,
            )
        )
    return tuple(output)


def _contextual_query_stored_response(
    body: bytes, expected: int, dimension: int
) -> tuple[bytes, ...]:
    response = _response_object(body)
    documents = _indexed(response.get("data"), expected, nested_key="data")
    chunks = tuple(_indexed(document["data"], 1) for document in documents)
    return tuple(_stored_vector(chunk[0], dimension) for chunk in chunks)


def render_independent_input(
    group: ContextGroup,
    counter: TokenCounter,
    capabilities: EmbeddingCapabilities,
) -> RenderedInput:
    """Render and measure one independent capsule/window provider input."""
    _validate_group(group, capabilities, "independent")
    if (
        len(group.window_ids) != 1
        or counter.model != capabilities.model
        or counter.tokenizer_id != capabilities.tokenizer_id
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Embedding group differs from provider capabilities."
        )
    text = f"Context:\n{group.capsule}\n\nContent:\n{group.input_texts[1]}"
    encoded_sha = sha256(text.encode("utf-8")).hexdigest()
    input_sha = digest(
        "rendered-embedding-input-v1",
        INDEPENDENT_RENDERER_VERSION,
        encoded_sha,
        capabilities.provider,
        capabilities.model,
        capabilities.dimensions,
        capabilities.dtype,
        capabilities.representation_fingerprint,
        capabilities.options,
    )
    token_count = counter.count(text)
    if (
        token_count <= 0
        or token_count > capabilities.max_group_tokens
        or token_count > capabilities.max_request_tokens
    ):
        raise TychoraError(
            "INVALID_REQUEST", "One rendered input exceeds the provider token limit."
        )
    return RenderedInput(text, encoded_sha, input_sha, token_count)


def _validate_group(
    group: ContextGroup,
    capabilities: EmbeddingCapabilities,
    contextualization: str,
) -> None:
    if (
        group.contextualization != contextualization
        or capabilities.contextualization != contextualization
        or group.provider != capabilities.provider
        or group.model != capabilities.model
        or group.dimensions != capabilities.dimensions
        or group.dtype != capabilities.dtype
        or group.options != capabilities.options
        or group.tokenizer_id != capabilities.tokenizer_id
    ):
        raise TychoraError(
            "INVALID_REQUEST", "Embedding group differs from provider capabilities."
        )
