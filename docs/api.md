# Python API

`Tychora.open(path, *, scope=None, config=None, providers=None, token_counter=None, credential_resolver=None)` opens one local workspace and returns a blocking context manager. `AsyncTychora.open(...)` is a synchronous factory returning an async context manager. An empty path receives the bundled `documents@1` profile, offline lexical retrieval, and a stable default collection UUID. Existing workspaces are validated and are never migrated by `open`. The workspace owner is exclusive: close one client before opening the same path again.

`open` composes enabled configured Voyage text, multimodal image and rerank adapters with separate owner-derived Chroma text/image paths and one owner-owned Ladybug graph store. It does not construct remote HTTP clients, read credentials, load tokenizer weights, or download assets. Offline mode retains configured native storage while leaving remote ports unavailable; strict or degraded query policy decides how an unavailable channel affects search. The default output counter loads a verified local `cl100k_base` asset only when counting is first needed, from `TYCHORA_MODEL_CACHE` or `<workspace>/models`. Voyage model tokenizers and multimodal accounting use that same cache root and verified manifest. Missing provisioned assets raise a typed capability error at the operation that requires them. An injected output counter must implement the qualified `TokenCounter` contract with `tokenizer_id="cl100k_base"` for evidence selection. Text embedding uses its own model-specific counter, supplied by `ProviderBindings.text_query.counter` or `ProviderBindings.text_counter` for standalone ingestion; its identity must match the document embedder.

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
        result = kb.search(
            QueryRequest.from_mapping(
                {"question": "verification", "modalities": ["lexical"]}
            )
        )
        for segment in result.segments:
            print(segment.text)
```

`SourceInput` is the strict canonical DTO: `collection_id` is a UUID and kinds are versioned. For `sources.add` only, a Mapping may use `"collection_id": "default"`; the public boundary resolves this alias to the workspace's stable registered default UUID before canonical parsing. It grants no extra collection access. The typed `kb.default_collection_id` property is available only when that collection is in the client's scope.

If an existing workspace already assigns the `default` slug or the fixed default UUID to a different collection identity, `open` raises `PROFILE_INCOMPATIBLE`; it never silently reassigns the alias or migrates that collection.

`sources.add` and `sources.sync` durably accept a bounded job and return its `JobHandle` before extraction. `jobs.get`, `jobs.wait`, `jobs.cancel`, and `jobs.events` observe the canonical job record while work runs. States are `queued`, `running`, `succeeded`, `failed`, `cancelled`, and `interrupted`; `wait` may return a nonterminal status at its timeout. An idempotency key binds the accepted canonical intent and effective configuration. Client close requests cancellation for its active jobs, waits for actual workers to exit, and then releases the owner. Explicit cancellation is durable and never changes an already succeeded receipt.

Selected document vision is explicit and disabled by default. Configure `[configuration.providers.vision]` with `provider="anthropic"`, a qualified `model`, and `credential_ref="env:NAME"`. After ingesting a PDF, DOCX, PNG or JPEG, call `kb.sources.selections(source_id, revision_id, extraction_id, limit=50)` for bounded, policy-scoped image dimensions and native text selectors. Submit a complete source mapping through ordinary `sources.add` with a `vision` array. Each selection uses the returned `captured_original_sha256`, `retained_extraction_sha256`, and image `source_native_ref`, integer pixel `geometry`, `task` (`ocr`, `describe`, or `extract`), and `task_schema`. Optional `context` entries name a listed text selector's `native_ref`, `field`, `source_start`, and `source_end`; `table_cell` also requires `cell_index`. A selection inside a complete source request looks like:

```json
{"vision":[{"captured_original_sha256":"<sha256 from selections>","retained_extraction_sha256":"<sha256 from selections>","source_native_ref":"<image ref from selections>","geometry":{"domain":"pixels","x0":0,"y0":0,"x1":200,"y1":100},"task":"describe","task_schema":{"type":"object","properties":{"label":{"type":"string"}},"required":["label"],"additionalProperties":false}}]}
```

For a first source revision with no retained extraction, `retained_extraction_sha256` may be omitted: the service binds its freshly retained parser root and still verifies the captured original SHA, native image ref and pixels. Once any extraction exists for that revision, the inspected extraction SHA is required. Caller-supplied hashes are selection intent, not trusted proof.

New qualified external-image PDFs select `DocumentCompositionPlan.input_strategy="pdf-streamed-images@1"` during source extraction. This semantic marker changes the normalizer/extraction and derived identities while preserving original/revision and raw-parser identity. Unmarked retained plans keep their exact serialization, 256 MiB total-input admission and replay/reuse behavior. The new strategy streams all exact raw image bindings separately from a 256 MiB nonraw-input aggregate; every unique encoded verification group is capped at 256 MiB. Its 512 MiB unique input-plus-generated-product ledger includes the composed manifest and deduplicates existing receipt bytes. Whole-PDF retention remains at most 1 GiB, narrowed by the captured policy, with the complete product batch admitted before persistence. The outer `extraction@1` / `document-composition@1` representation, raw manifest link and required-artifact declarations remain explicit for backup/restore.

For model-free retained-data preflight, `ingestion.composition.composition_input_references(parsed, plan)` validates and admits descriptors without loading payloads. Feed those descriptors to `DeclaredPayloads(refs, authorized_read, MAX_COMPOSITION_REPLAY_BYTES)` and then `build_composed_document(parsed, registry, plan, inputs)`. Raw replay must first use `replay_document(..., read_declared=authorized_read)` to verify declared images and sidecars. The pure builder still performs full image decoding, exact pixel/crop, receipt and context verification; the reader must verify each declared payload and can supply cancellation checkpoints. No helper accepts an “already verified” exemption or an arbitrary raw-image classification.

Only the selected crop and verified context may be sent. Configuration is not upload consent: the principal and target collection must currently allow Anthropic, the caller needs `vision`, and the workspace must be online. A newly granted default owner must reopen to acquire its immutable scope. Local OCR is tried first for `ocr`; successful OCR sends nothing. Selection intent is retained for idempotence, while ordinary `sources.sync` uses stable registration without replaying a previous selection. After valid selection and authorization, an unavailable provider or invalid provider output publishes original evidence without a fabricated proxy. `sources.extractions(...).extractions[*].stages.vision` reports `incomplete` from a retained extraction-bound diagnostic. A later attempt uses a fresh add/idempotency key and the now-inspected extraction SHA; a completed partial job is not retryable. Invalid or stale selection and upload denial remain hard failures. Public `evidence.get(proxy_id)` exposes a verified `vision_derivation` record with literal transcription, interpretation, uncertainty and target binding for readable derived proxies; their text remains non-citable and needs review.

`build_services(owner, config, providers, counter)` constructs `Services.source_jobs`, the single bounded accepted-job scheduler for this owner. Python facades use its `add`/`sync` or `add_async`/`sync_async` methods; HTTP transport can await the same async methods from its own event loop and must not create a second job scheduler. `source_jobs` dispatches to its own owned loop, so its task registry and semaphore never cross loop boundaries. A transport that constructs `Services` directly must await `services.source_jobs.aclose()` and wait for drain before closing `WorkspaceOwner`. Blocking methods may run on a caller's event loop thread, but cannot be called from the scheduler's own loop.

Both clients expose `discover`, `inspect`, `search`, and namespaces `sources`, `evidence`, `assets`, `relations`, `jobs`, `evaluations`, and `management`. The namespace methods have synchronous signatures on `Tychora` and awaitable equivalents on `AsyncTychora`; `jobs.events` is an iterator or async iterator, respectively. `search` returns `QueryResult`, `evidence.get` and `assets.get` return `InspectionResult`, `assets.read` returns bounded registered bytes, and `relations.traverse` returns `RelationResult`. Exact text ranges return `partial_evidence` and authorized `excerpts`; `canonical_text_sha` and `canonical_text_length` are null when the whole unit is not readable. A full evidence inspection returns `evidence` instead. `format_result(result, output_profile, *, counter=None)` renders a prior authorized result within its recorded budget.

Pass `scope=` to `search` to narrow the client's `RequestScope`; it cannot broaden the principal, collections, uses, access ceiling, or provider grants. Synchronous calls are permitted inside an active event loop and block its thread; use `AsyncTychora` for a responsive loop. Async cancellation waits for admitted worker cleanup and retained snapshot pin release before surfacing cancellation.

Configured `env:NAME` credentials resolve when the first authorized remote operation needs a transport. `secret:NAME` requires an explicit `credential_resolver` callable supplied to sync or async `open`; without it, the operation fails with `CAPABILITY_MISSING`. Resolved values remain in runtime memory and are never written to a snapshot, history entry, or error. A settings activation recomposes automatically owned adapters against current references and retires their old runtimes after work drains. It preserves the indexed representation family when only credentials rotate. Passing explicit `ProviderBindings` keeps caller ownership and the existing activation behavior. Provider selection does not grant upload permission: the principal and collection policy must both allow the provider for the exact operation.

`ProviderBindings` accepts typed text/visual query providers, explicit document embedders and indexes, shared query caches, graph ports, and a qualified Voyage reranker with its counter. A local document embedder must use `LocalTextBinding(embedder=...)` plus a matching model counter. This explicit local binding runs on a bounded local lane with current owner-checked read dependencies and policy epoch before and after embedding. A remote document embedder uses the ordinary owner upload authorization path; offline mode denies its calls while retaining configured provider identity. Text and visual query factories must share the injected `QueryCaches`, match configured capabilities, and match the selected pinned vector family before result-cache reuse. Arbitrary reranker substitution is not qualified; the rerank binding is Voyage-only.

The base installation can import and run lexical-only local workflows without Pillow. Image decoding loads Pillow only at an image operation and reports `CAPABILITY_MISSING` when the optional documents extra is absent.

`connect` uses the authenticated loopback transport. `evaluations.run` requires management scope; `management.run` permits read-only health under read scope and requires management scope for mutation or deletion resume. Unsupported optional channels still return typed capability errors; discovery distinguishes installed packages from usable configured channels.

For an explicit physical audit, call `kb.management.run({"operation": "health", "verify_native": True})` or await the same call on `AsyncTychora`. Its `RecoveryStatus` identifies the active snapshot and counts verified native records/generations plus pending batches skipped; absent or corrupt ready storage fails closed. The audit does not call a provider or modify native projections. See [operations](operations.md) for scope and limits.

An asynchronous call uses the same typed request and evidence identity:

```python
import asyncio
from tychora import AsyncTychora


async def show_evidence() -> None:
    async with AsyncTychora.open("knowledge") as kb:
        await kb.discover({"subject": "kinds"})
        result = await kb.search(
            {"question": "verification", "modalities": ["lexical"]}
        )
        for segment in result.segments:
            for unit_id in segment.matched_unit_ids:
                inspection = await kb.evidence.get(unit_id)
                print(
                    unit_id, inspection.evidence.text if inspection.evidence else None
                )


asyncio.run(show_evidence())
```

`knowledge_adapter.py` is framework-neutral. It keeps segment, source, revision, extraction, unit, asset, run and snapshot IDs inside metadata as well as top-level segment IDs. It also carries each complete citation record, keeping its unit/asset/source/revision binding, source reference, page fields, and locators together. Serializers that drop top-level IDs can still preserve provenance. No Agno connector or LLM answer generation is installed.
