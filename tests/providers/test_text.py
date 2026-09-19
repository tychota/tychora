"""Synthetic text-provider acceptance tests."""

import json
from hashlib import sha256

import httpx
import pytest
from pydantic import ValidationError

from tychora.adapters.voyage.text import (
    HttpxVoyageTransport,
    QueryOperationBinding,
    RawVoyageResponse,
    VoyageRuntime,
    VoyageTextEmbedder,
    map_contextual_response,
    render_independent_input,
)
from tychora.application.owner import WorkspaceOwner
from tychora.configuration.resolve import resolve_config
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.embeddings import (
    EmbeddingCapabilities,
    QueryUploadAuthorization,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest, space_id
from tychora.contracts.indexing import ContextGroup
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


class CharacterCounter:
    def __init__(self, model="voyage-code-4"):
        self.model = model
        self.tokenizer_id = f"{model}:qualified"

    def count(self, text: str) -> int:
        return len(text)


def _group(
    *,
    model="voyage-code-4",
    contextualization="independent",
    dimensions=1024,
    windows=("def example():\n    return 1",),
):
    capsule = "path: src/example.py"
    options = FrozenJsonMap({"output": "float32"})
    input_sha = digest(
        "embedding-input-v1",
        (capsule, *windows),
        "voyage",
        model,
        dimensions,
        "float32",
        "1",
        contextualization,
        options,
    )
    window_ids = tuple(f"window-{index}" for index in range(len(windows)))
    window_input_shas = (
        (input_sha,) * len(windows)
        if contextualization == "joint"
        else tuple(digest("window-input", window) for window in windows)
    )
    return ContextGroup(
        group_id=digest(
            "context-group-input-v1",
            "extraction-a",
            "context-a",
            window_ids,
            input_sha,
        ),
        context_group_key="context-a",
        extraction_id="extraction-a",
        revision_id="revision-a",
        capsule=capsule,
        input_texts=(capsule, *windows),
        window_ids=window_ids,
        window_input_shas=window_input_shas,
        input_sha=input_sha,
        dependency_ids=frozenset({"revision-a", "unit-a"}),
        token_count=len(capsule) + sum(map(len, windows)),
        tokenizer_id=f"{model}:qualified",
        provider="voyage",
        model=model,
        dimensions=dimensions,
        representation_version="1",
        contextualization=contextualization,
        options=options,
    )


def _capabilities(
    *, contextualization="independent", model="voyage-code-4", dimensions=1024
):
    representation = digest("representation", model, contextualization)
    return EmbeddingCapabilities(
        provider="voyage",
        endpoint="https://api.voyageai.com",
        model=model,
        dimensions=dimensions,
        input_types=frozenset({"document", "query"}),
        contextualization=contextualization,
        tokenizer_id=f"{model}:qualified",
        space_id=space_id(
            "text", "voyage", model, dimensions, "cosine", representation
        ),
        representation_fingerprint=representation,
        max_group_tokens=24_000,
        max_request_tokens=28_000,
        options=FrozenJsonMap({"output": "float32"}),
    )


def test_embedding_capabilities_bind_both_input_modes_and_space():
    representation = "a" * 64
    expected_space = space_id(
        "text", "voyage", "voyage-context-4", 1024, "cosine", representation
    )

    capabilities = EmbeddingCapabilities(
        provider="voyage",
        endpoint="https://api.voyageai.com",
        model="voyage-context-4",
        dimensions=1024,
        input_types=frozenset({"document", "query"}),
        contextualization="joint",
        tokenizer_id="voyage-context-4:qualified",
        space_id=expected_space,
        representation_fingerprint=representation,
        max_group_tokens=24_000,
        max_request_tokens=28_000,
    )

    assert capabilities.input_types == frozenset({"document", "query"})
    assert capabilities.space_id == expected_space


@pytest.mark.parametrize(
    "change",
    [
        {"collection_ids": frozenset()},
        {"policy_epoch": True},
        {"mode": "document"},
    ],
)
def test_query_authorization_is_bounded_query_only(change):
    values = {
        "payload_sha": "a" * 64,
        "input_shas": ("b" * 64,),
        "collection_ids": frozenset({"collection-a"}),
        "dependency_ids": frozenset(),
        "policy_epoch": 0,
    }
    values.update(change)

    with pytest.raises(ValidationError):
        QueryUploadAuthorization(**values)


def test_independent_group_is_one_versioned_measured_wire_input():
    group = _group()

    rendered = render_independent_input(group, CharacterCounter(), _capabilities())

    assert rendered.text == (
        "Context:\npath: src/example.py\n\nContent:\ndef example():\n    return 1"
    )
    assert rendered.token_count == len(rendered.text)
    assert rendered.input_sha != group.input_sha


@pytest.mark.parametrize(
    ("nested", "valid"),
    [
        (
            [
                {"index": 2, "embedding": [0.0, 2.0]},
                {"index": 0, "embedding": [9.0, 9.0]},
                {"index": 1, "embedding": [3.0, 4.0]},
            ],
            True,
        ),
        (
            [
                {"index": 0, "embedding": [9.0, 9.0]},
                {"index": 1, "embedding": [3.0, 4.0]},
            ],
            False,
        ),
        (
            [
                {"index": 0, "embedding": [9.0, 9.0]},
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 1, "embedding": [0.0, 2.0]},
            ],
            False,
        ),
    ],
)
def test_contextual_response_requires_complete_unambiguous_raw_indices(nested, valid):
    group = _group(
        model="voyage-context-4",
        contextualization="joint",
        dimensions=2,
        windows=("first", "second"),
    )
    body = json.dumps({"data": [{"index": 0, "data": nested}]}).encode()

    if not valid:
        with pytest.raises(TychoraError, match="mapping"):
            map_contextual_response(
                (group,),
                body,
                _capabilities(
                    model="voyage-context-4", contextualization="joint", dimensions=2
                ),
            )
        return

    records = map_contextual_response(
        (group,),
        body,
        _capabilities(
            model="voyage-context-4", contextualization="joint", dimensions=2
        ),
    )
    assert [record.record_id for record in records] == [
        digest("embedding-record-v1", window_id, group.input_sha, records[0].space_id)
        for window_id in group.window_ids
    ]
    assert records[0].values == pytest.approx((0.6, 0.8))
    assert records[1].values == pytest.approx((0.0, 1.0))


def test_joint_embedder_authorizes_exact_payload_before_lazy_transport():
    group = _group(
        model="voyage-context-4",
        contextualization="joint",
        dimensions=2,
        windows=("first", "second"),
    )
    response = RawVoyageResponse(
        200,
        json.dumps(
            {
                "data": [
                    {
                        "index": 0,
                        "data": [
                            {"index": 0, "embedding": [1.0, 1.0]},
                            {"index": 1, "embedding": [3.0, 4.0]},
                            {"index": 2, "embedding": [0.0, 2.0]},
                        ],
                    }
                ]
            }
        ).encode(),
    )
    calls = []
    authorizations = []

    class Transport:
        def post_json(self, path, body, timeout):
            calls.append((path, body, timeout))
            return response

        def close(self):
            calls.append(("close", b"", 0.0))

    runtime = VoyageRuntime(
        lambda: Transport(),
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    embedder = VoyageTextEmbedder(
        capabilities=_capabilities(
            model="voyage-context-4", contextualization="joint", dimensions=2
        ),
        counter=CharacterCounter("voyage-context-4"),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: authorizations.append(
            (payload_sha, dependencies)
        ),
    )

    records = embedder.embed_groups((group,), ExecutionContext())

    assert len(records) == 2
    assert calls[0][0] == "/v1/contextualizedembeddings"
    payload = json.loads(calls[0][1])
    assert payload["inputs"] == [list(group.input_texts)]
    assert payload["enable_auto_chunking"] is False
    assert authorizations == [(sha256(calls[0][1]).hexdigest(), group.dependency_ids)]
    runtime.close()


def test_independent_embedder_maps_one_rendered_input_to_its_window():
    group = _group(dimensions=2)
    calls = []

    class Transport:
        def post_json(self, path, body, timeout):
            calls.append((path, json.loads(body)))
            return RawVoyageResponse(
                200,
                json.dumps({"data": [{"index": 0, "embedding": [5.0, 12.0]}]}).encode(),
            )

        def close(self):
            pass

    runtime = VoyageRuntime(
        lambda: Transport(),
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    capabilities = _capabilities(dimensions=2)
    embedder = VoyageTextEmbedder(
        capabilities=capabilities,
        counter=CharacterCounter(),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: None,
    )

    (record,) = embedder.embed_groups((group,), ExecutionContext())

    rendered = render_independent_input(group, CharacterCounter(), capabilities)
    assert calls == [
        (
            "/v1/embeddings",
            {
                "input": [rendered.text],
                "input_type": "document",
                "model": "voyage-code-4",
                "output_dimension": 2,
                "output_dtype": "float",
                "truncation": False,
            },
        )
    ]
    assert record.input_sha == rendered.input_sha
    assert record.values == pytest.approx((5 / 13, 12 / 13))


@pytest.mark.parametrize(
    ("statuses", "succeeds"),
    [((429, 503, 200), True), ((503, 503, 503), False)],
)
def test_transient_attempts_are_bounded_and_each_is_reauthorized(statuses, succeeds):
    group = _group(
        model="voyage-context-4",
        contextualization="joint",
        dimensions=2,
        windows=("first",),
    )
    attempted = []
    authorized = []

    class Transport:
        def post_json(self, path, body, timeout):
            status = statuses[len(attempted)]
            attempted.append(status)
            body = (
                json.dumps(
                    {
                        "data": [
                            {
                                "index": 0,
                                "data": [
                                    {"index": 0, "embedding": [1.0, 1.0]},
                                    {"index": 1, "embedding": [3.0, 4.0]},
                                ],
                            }
                        ]
                    }
                ).encode()
                if status == 200
                else b'{"error":"private detail"}'
            )
            return RawVoyageResponse(status, body)

        def close(self):
            pass

    runtime = VoyageRuntime(
        lambda: Transport(),
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    embedder = VoyageTextEmbedder(
        capabilities=_capabilities(
            model="voyage-context-4", contextualization="joint", dimensions=2
        ),
        counter=CharacterCounter("voyage-context-4"),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: authorized.append(
            payload_sha
        ),
        backoff=lambda attempt: 0.0,
    )

    if succeeds:
        assert len(embedder.embed_groups((group,), ExecutionContext())) == 1
    else:
        with pytest.raises(TychoraError) as exc:
            embedder.embed_groups((group,), ExecutionContext())
        assert exc.value.retryable
        assert "private detail" not in exc.value.message
    assert attempted == list(statuses)
    assert len(authorized) == 3


def test_query_cache_hit_needs_no_remote_construction_or_authorization():
    created = []
    posts = []
    authorized = []

    class Transport:
        def post_json(self, path, body, timeout):
            posts.append((path, body))
            return RawVoyageResponse(
                200,
                json.dumps({"data": [{"index": 0, "embedding": [8.0, 15.0]}]}).encode(),
            )

        def close(self):
            pass

    def make_transport():
        created.append(True)
        return Transport()

    runtime = VoyageRuntime(
        make_transport,
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    binding = QueryOperationBinding(
        principal_upload_scope_sha=digest("scope", "principal-a", "voyage"),
        collection_ids=frozenset({"collection-a"}),
        dependency_ids=frozenset(),
        policy_epoch=0,
    )
    capabilities = _capabilities(
        model="voyage-context-4", contextualization="joint", dimensions=2
    )
    embedder = VoyageTextEmbedder(
        capabilities=capabilities,
        counter=CharacterCounter("voyage-context-4"),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: None,
        query_binding=binding,
        authorize_query=lambda authorization, ctx: authorized.append(authorization),
    )

    first = embedder.embed_queries(("Where is the example?",), ExecutionContext())
    assert len(first) == 1
    assert len(created) == len(posts) == len(authorized) == 1
    assert authorized[0].payload_sha == sha256(posts[0][1]).hexdigest()
    assert authorized[0].input_shas == (first[0].input_sha,)

    offline_hit = VoyageTextEmbedder(
        capabilities=capabilities,
        counter=CharacterCounter("voyage-context-4"),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: None,
        query_binding=binding,
        authorize_query=lambda authorization, ctx: (_ for _ in ()).throw(
            TychoraError("FORBIDDEN_SCOPE", "Offline query upload denied.")
        ),
    )
    second = offline_hit.embed_queries(("Where is the example?",), ExecutionContext())
    assert second == first
    assert len(created) == len(posts) == len(authorized) == 1


@pytest.mark.asyncio
async def test_source_free_query_has_a_separate_canonical_collection_route(tmp_path):
    settings_path = tmp_path / "workspace.toml"
    settings_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    settings = resolve_config(settings_path, "test", {})
    settings = settings.model_copy(
        update={"values": settings.values.model_copy(update={"offline": False})}
    )
    scope = RequestScope(
        principal_id="principal-a",
        collection_ids=frozenset({"collection-a"}),
        access_ceiling=1,
        upload_providers=frozenset({"voyage"}),
    )
    authorization = QueryUploadAuthorization(
        payload_sha="a" * 64,
        input_shas=("b" * 64,),
        collection_ids=frozenset({"collection-a"}),
        dependency_ids=frozenset(),
        policy_epoch=0,
    )
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections "
                "(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "collection-a",
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    1,
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )

        await owner.run_blocking(
            lambda: owner.upload_policy.authorize_query(scope, "voyage", authorization),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as canonical:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize(scope, "voyage", frozenset(), 0),
                ExecutionContext(),
            )
        assert canonical.value.code == "FORBIDDEN_SCOPE"

        await owner.run_blocking(
            lambda: owner.db.execute(
                "UPDATE workspaces SET policy_epoch=1 WHERE workspace_id=?",
                (owner.workspace_id,),
            ),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as stale:
            await owner.run_blocking(
                lambda: owner.upload_policy.authorize_query(
                    scope, "voyage", authorization
                ),
                ExecutionContext(),
            )
        assert stale.value.code == "SCOPE_CHANGED"


@pytest.mark.asyncio
async def test_query_retry_reauthorizes_after_backoff_with_real_owner(tmp_path):
    settings_path = tmp_path / "workspace.toml"
    settings_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    settings = resolve_config(settings_path, "test", {})
    settings = settings.model_copy(
        update={"values": settings.values.model_copy(update={"offline": False})}
    )
    scope = RequestScope(
        principal_id="principal-a",
        collection_ids=frozenset({"collection-a"}),
        access_ceiling=1,
        upload_providers=frozenset({"voyage"}),
    )
    authorization = QueryUploadAuthorization(
        payload_sha="a" * 64,
        input_shas=("b" * 64,),
        collection_ids=frozenset({"collection-a"}),
        dependency_ids=frozenset(),
        policy_epoch=0,
    )
    with WorkspaceOwner.open(tmp_path / "data", settings, scope) as owner:
        await owner.run_blocking(
            lambda: owner.db.execute(
                "INSERT INTO collections "
                "(collection_id,workspace_id,slug,title,access_floor,policy_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "collection-a",
                    owner.workspace_id,
                    "manuals",
                    "Manuals",
                    1,
                    '{"schema_version":1,"upload_providers":["voyage"]}',
                ),
            ),
            ExecutionContext(),
        )
        posts = []

        class Transport:
            def post_json(self, path, body, timeout):
                posts.append(True)
                return RawVoyageResponse(503, b'{"error":"transient"}')

            def close(self):
                pass

        runtime = VoyageRuntime(
            lambda: Transport(),
            workspace_id=owner.workspace_id,
            credential_binding_id="credential-a",
        )
        binding = QueryOperationBinding(
            principal_upload_scope_sha=digest("scope", "principal-a", "voyage"),
            collection_ids=frozenset({"collection-a"}),
            dependency_ids=frozenset(),
            policy_epoch=0,
        )
        ctx = ExecutionContext()

        def change_epoch(attempt):
            owner.run_blocking_sync(
                lambda: owner.db.execute(
                    "UPDATE workspaces SET policy_epoch=1 WHERE workspace_id=?",
                    (owner.workspace_id,),
                ),
                ctx,
            )
            return 0.0

        embedder = VoyageTextEmbedder(
            capabilities=_capabilities(
                model="voyage-context-4", contextualization="joint", dimensions=2
            ),
            counter=CharacterCounter("voyage-context-4"),
            runtime=runtime,
            authorize_document=lambda payload_sha, dependencies, ctx: None,
            query_binding=binding,
            authorize_query=lambda query_authorization, attempt_ctx: (
                owner.run_blocking_sync(
                    lambda: owner.upload_policy.authorize_query(
                        scope, "voyage", query_authorization
                    ),
                    attempt_ctx,
                )
            ),
            backoff=change_epoch,
        )

        with pytest.raises(TychoraError) as changed:
            await owner.run_query_provider(
                "voyage",
                lambda: embedder.embed_queries(("query",), ctx),
                ctx,
                scope=scope,
                authorization=authorization,
            )
        assert changed.value.code == "SCOPE_CHANGED"
        assert posts == [True]


def test_httpx_transport_posts_exact_bytes_without_redirect_or_hidden_retry():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == "https://api.voyageai.com/v1/embeddings"
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        assert request.content == b'{"input":["synthetic"]}'
        return httpx.Response(200, content=b'{"data":[]}')

    transport = HttpxVoyageTransport(
        api_key="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )

    response = transport.post_json("/v1/embeddings", b'{"input":["synthetic"]}', 2.0)

    assert response == RawVoyageResponse(200, b'{"data":[]}')
    assert len(seen) == 1
    transport.close()


@pytest.mark.asyncio
async def test_owner_closes_registered_generic_provider_runtime(tmp_path):
    settings_path = tmp_path / "workspace.toml"
    settings_path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
    )
    settings = resolve_config(settings_path, "test", {})
    closed = []

    class Runtime:
        def close(self):
            closed.append(True)

    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="principal-a")
    ) as owner:
        await owner.run_blocking(
            lambda: owner.register_runtime(Runtime()), ExecutionContext()
        )
        assert closed == []
    assert closed == [True]


def test_runtime_credential_binding_cannot_be_rebound_over_cached_values():
    class Transport:
        def post_json(self, path, body, timeout):
            raise AssertionError("transport is not used")

        def close(self):
            pass

    runtime = VoyageRuntime(
        lambda: Transport(),
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )

    with pytest.raises(AttributeError):
        runtime.credential_binding_id = "credential-b"
    assert runtime.credential_binding_id == "credential-a"


def test_duplicate_group_or_window_mapping_is_rejected_before_transport():
    group = _group(
        model="voyage-context-4",
        contextualization="joint",
        dimensions=2,
        windows=("first",),
    )
    created = []

    class Transport:
        def post_json(self, path, body, timeout):
            raise AssertionError("invalid mapping reached transport")

        def close(self):
            pass

    runtime = VoyageRuntime(
        lambda: created.append(True) or Transport(),
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    embedder = VoyageTextEmbedder(
        capabilities=_capabilities(
            model="voyage-context-4", contextualization="joint", dimensions=2
        ),
        counter=CharacterCounter("voyage-context-4"),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: None,
    )

    with pytest.raises(TychoraError, match="mapping"):
        embedder.embed_groups((group, group), ExecutionContext())
    assert created == []


@pytest.mark.parametrize("contextualization", ["joint", "independent"])
def test_actual_capsule_and_window_wire_tokens_are_enforced_before_transport(
    contextualization,
):
    model = "voyage-context-4" if contextualization == "joint" else "voyage-code-4"
    group = _group(
        model=model,
        contextualization=contextualization,
        dimensions=2,
        windows=("first",),
    )
    base = _capabilities(model=model, contextualization=contextualization, dimensions=2)
    actual = (
        group.token_count
        if contextualization == "joint"
        else render_independent_input(group, CharacterCounter(model), base).token_count
    )
    limited = base.model_copy(
        update={"max_group_tokens": actual - 1, "max_request_tokens": actual - 1}
    )
    created = []
    runtime = VoyageRuntime(
        lambda: created.append(True),  # type: ignore[arg-type,func-returns-value]
        workspace_id="workspace-a",
        credential_binding_id="credential-a",
    )
    embedder = VoyageTextEmbedder(
        capabilities=limited,
        counter=CharacterCounter(model),
        runtime=runtime,
        authorize_document=lambda payload_sha, dependencies, ctx: None,
    )

    with pytest.raises(TychoraError, match="limit"):
        embedder.embed_groups((group,), ExecutionContext())
    assert created == []
