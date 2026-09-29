# Python API

`Tychora.open(path, *, scope=None, config=None, providers=None, token_counter=None)` opens one local workspace and returns a blocking context manager. `AsyncTychora.open(...)` is a synchronous factory returning an async context manager. An empty path receives the bundled `documents@1` profile, offline lexical retrieval, and a stable default collection UUID. Existing workspaces are validated and are never migrated by `open`. The workspace owner is exclusive: close one client before opening the same path again.

No remote provider or model is constructed by `open`. The default output counter loads a verified local `cl100k_base` asset only when counting is first needed, from `TYCHORA_MODEL_CACHE` or `<workspace>/models`. Missing provisioned assets raise the existing capability error at the operation that requires them. An injected output counter must implement the qualified `TokenCounter` contract with `tokenizer_id="cl100k_base"` for evidence selection. Text embedding uses its own model-specific counter, supplied by `ProviderBindings.text_query.counter` or `ProviderBindings.text_counter` for standalone ingestion; its identity must match the document embedder.

```python
from tychora import QueryRequest, SourceInput, Tychora

with Tychora.open("knowledge") as kb:
    source = SourceInput(
        type="text",
        source_key="manual",
        collection_id=kb.default_collection_id,
        kind="manual@1",
        format="text",
        metadata={"title": "Verification", "publisher": "Example Team", "edition": "1"},
        text="Record verification outcomes.",
    )
    job = kb.sources.add(source, idempotency_key="manual-v1")
    status = kb.jobs.wait(job.id)
    if status.state == "succeeded":
        result = kb.search(QueryRequest.from_mapping(
            {"question": "verification", "modalities": ["lexical"]}
        ))
        for segment in result.segments:
            print(segment.text)
```

`SourceInput` is the strict canonical DTO: `collection_id` is a UUID and kinds are versioned. For `sources.add` only, a Mapping may use `"collection_id": "default"`; the public boundary resolves this alias to the workspace's stable registered default UUID before canonical parsing. It grants no extra collection access. The typed `kb.default_collection_id` property is available only when that collection is in the client's scope.

`sources.add` and `sources.sync` durably accept a bounded job and return its `JobHandle` before extraction. `jobs.get`, `jobs.wait`, `jobs.cancel`, and `jobs.events` observe the canonical job record while work runs. States are `queued`, `running`, `succeeded`, `failed`, `cancelled`, and `interrupted`; `wait` may return a nonterminal status at its timeout. An idempotency key binds the accepted canonical intent and effective configuration. Client close requests cancellation for its active jobs, waits for actual workers to exit, and then releases the owner. Explicit cancellation is durable and never changes an already succeeded receipt.

`build_services(owner, config, providers, counter)` constructs `Services.source_jobs`, the single bounded accepted-job scheduler for this owner. Python facades use its `add`/`sync` or `add_async`/`sync_async` methods; HTTP transport can await the same async methods from its own event loop and must not create a second job scheduler. `source_jobs` dispatches to its own owned loop, so its task registry and semaphore never cross loop boundaries. A transport that constructs `Services` directly must await `services.source_jobs.aclose()` and wait for drain before closing `WorkspaceOwner`. Blocking methods may run on a caller's event loop thread, but cannot be called from the scheduler's own loop.

Both clients expose `discover`, `search`, and namespaces `sources`, `evidence`, `assets`, `relations`, `jobs`, `evaluations`, and `management`. The namespace methods have synchronous signatures on `Tychora` and awaitable equivalents on `AsyncTychora`; `jobs.events` is an iterator or async iterator, respectively. `search` returns `QueryResult`, `evidence.get` and `assets.get` return `InspectionResult`, `assets.read` returns bounded registered bytes, and `relations.traverse` returns `RelationResult`. Exact text ranges return `partial_evidence` and authorized `excerpts`; `canonical_text_sha` and `canonical_text_length` are null when the whole unit is not readable. A full evidence inspection returns `evidence` instead. `format_result(result, output_profile, *, counter=None)` renders a prior authorized result within its recorded budget.

Pass `scope=` to `search` to narrow the client's `RequestScope`; it cannot broaden the principal, collections, uses, access ceiling, or provider grants. Synchronous calls are permitted inside an active event loop and block its thread; use `AsyncTychora` for a responsive loop. Async cancellation waits for admitted worker cleanup and retained snapshot pin release before surfacing cancellation.

`ProviderBindings` accepts typed text/visual query providers, explicit document embedders and indexes, shared query caches, graph ports, and a qualified Voyage reranker with its counter. A local document embedder must use `LocalTextBinding(embedder=...)` plus a matching model counter. This explicit local binding runs on a bounded local lane with current owner-checked read dependencies and policy epoch before and after embedding. A remote document embedder uses the ordinary owner upload authorization path; offline mode denies its calls while retaining configured provider identity. Text and visual query factories must share the injected `QueryCaches`, match configured capabilities, and match the selected pinned vector family before result-cache reuse. Arbitrary reranker substitution is not qualified; the rerank binding is Voyage-only.

`connect` awaits Task 36 transport. `evaluations.run` and `management.run` await their owning later services. These methods currently raise `TychoraError(code="CAPABILITY_MISSING")` after relevant local scope checks and do not return fabricated jobs or results. Discovery's capability inventory reports installed optional packages, not completion of these later service routes. The framework-neutral [`knowledge_adapter.py`](../examples/knowledge_adapter.py) maps each segment into `{id, text, metadata}`, preserving canonical IDs and provenance in metadata so a later framework serializer cannot silently drop them.
