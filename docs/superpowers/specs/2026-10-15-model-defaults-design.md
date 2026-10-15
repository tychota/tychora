# Model defaults

Date: 2026-10-15. Status: design, awaiting review. Sub-project 3 of the Tychora v2 programme (`2026-10-15-tychora-v2-program-design.md`). Depends on engine core v2 (usage ledger, `manage rebuild`, benchmark harness) and chunking v2 (chunk hard maximum).

## 1. Intent and success criteria

The product owner asked for "modern tools by default": Voyage `rerank-3`, Voyage 4-generation contextual embeddings, the current multimodal model and Claude Haiku 5.5 for vision. Today the runtime qualifies only `rerank-2.5` for reranking. Each model's limits are hard-coded in three modules. A new model therefore means new code branches in all three, not new data.

Success criteria:

| # | Criterion | Ties to programme |
| --- | --- | --- |
| M1 | Online presets default to `voyage-context-4` (documents), `voyage-code-4` (code), `voyage-multimodal-3.5` (images), `rerank-3` (rerank) and `claude-haiku-5-5` (vision). Each is `qualified` in the registry, and `inspect settings` shows it. | Modern defaults |
| M2 | The source contains no model-id comparison outside the registry data file. A CI grep (section 7) checks this. Adding a model is a registry entry, an asset entry, a price entry and one parametrized test case. | Code health (5) |
| M3 | On the benchmark corpus the warm query p95 stays below 1 s with the default reranker, including Voyage query embedding and reranking. The rerank stage p95 is reported separately in the query trace. | Criterion 1 |
| M4 | On the evaluation set, `rerank-3` is equal to or better than `rerank-2.5` on nDCG@10 and recall@8. The report also records `rerank-3-lite`. | Criterion 3 |
| M5 | Every provider call writes one usage-ledger row with units and latency, failed calls included. The cost of a query and of an ingestion job is computed from the versioned price table. | Criterion 4 (cost visible) |
| M6 | No workspace changes model silently. Changing the model, dimension or index data type of a vector role requires an explicit settings activation followed by a rebuild. | Identity principle (`HANDOFF.md:21`) |
| M7 | Each vector family's default dimension and data type is chosen by the evaluation in 4.8, and the report records storage, latency and quality for every configuration tried. | Criteria 1 and 3 |
| M8 | No vector family compares vectors from two embedding spaces. The registry loader rejects such a family, and a unit test covers every registered pair. | Identity principle |

## 2. Scope and non-goals

In scope: the model qualification registry, per-tier defaults, per-family dimension and quantization options, the optional asymmetric "fast/offline query" text family, rerank input limits aligned with chunking v2, the Haiku 5.5 vision path (downscaling, request options, batch dispatch), tokenizer assets for the new models, price-table entries and ledger recording at the provider adapters, qualification tests, and the upgrade policy.

Non-goals:
- **A 4-generation multimodal model.** None exists. On 2026-10-15, `voyage-multimodal-3.5` is Voyage's newest multimodal model, and the design makes a future one a registry entry.
- **Vector store support for binary search and rescoring.** This spec defines the option and its evaluation (4.8). Engine core v2 owns how vectors are stored and searched.
- Live quality claims beyond our evaluation set (programme non-goal).
- The ledger schema, the trace schema and `manage rebuild`. Engine core v2 owns these, and this spec consumes them.
- Which Docling pictures get described, and when. Sub-project 4 owns that; this spec supplies the vision model and the dispatch modes.
- Preset definitions themselves. Sub-project 5 owns them; this spec supplies the per-role default table that presets reference.
- Output-segment budgets that reuse the literal 1200 (`retrieval/segments.py:481,507,854`). These shape the evidence pack, not rerank input, and chunking v2 decides them.

## 3. Current state

**Reranking is pinned to one model in three places.**
- `adapters/voyage/validation.py:16-35`: `qualify_voyage_rerank` accepts only `rerank-2.5` with a hard-coded revision and sha, `max_query_tokens=2000`, `max_representation_tokens=1200` and `max_candidates=64`.
- `application/default_bindings.py:371`: rejects every other model.
- `application/reranking.py:635`: rejects every other model again in `create_reranker`.
- `configuration/models.py:95`: accepts `Literal["rerank-2.5", "rerank-3"]`, so a configured `rerank-3` passes validation and then fails at runtime with `CAPABILITY_MISSING`. `rerank-3-lite` is not accepted at all. `docs/design-choices.md:31-33` and `HANDOFF.md:123` already document this mismatch.

**Embedding qualification uses inline tables.**
- `validation.py:45-49` maps model to contextualization mode.
- `default_bindings.py:302-306` repeats the accepted text models.
- `validation.py:99` and `default_bindings.py:77,123,341` hard-code `voyage-multimodal-3.5`.
- `default_bindings.py:121-146` hard-codes the visual limits.
- `application/setup.py:670-676` lists the tokenizer models.
- `application/settings_draft.py:91-97` repeats them in `QUALIFIED_MODELS`.

Together that is seven copies of one fact.

**Vision is generic and unbounded by model.**
- `VisionSettings.model` accepts any string (`configuration/models.py:112-127`). The settings draft declares "no pinned model claims" (`settings_draft.py:95`).
- `AnthropicVisionProvider.interpret` (`adapters/vision/anthropic.py:111-169`) sends one image per request with `max_tokens=2048`. It rejects any response that is not exactly one text block (`:150-155`).
- The region byte limit is 20 MiB (`ports/vision.py:33`). The Claude API limit is 10 MB base64-encoded per image, about 7.5 MB raw. A large region therefore fails at the provider instead of being prepared locally.

**Tokenizer assets.**
- `resources/model-assets.json` pins `rerank-2.5`, `voyage-context-4`, `voyage-code-4`, `voyage-code-3` and `voyage-multimodal-3.5`, all with `tokenizer.json` sha256 `c0382117…87539`.
- Probe on 2026-10-15: Hugging Face publishes `voyageai/rerank-3` at revision `2d2c2951…8158` and `voyageai/rerank-3-lite` at `a44217a6…0f40`, both Apache-2.0. Their `tokenizer.json` is byte-identical to that sha (7,031,645 bytes). Their `tokenizer_config.json` differs from rerank-2.5's (7,228 vs 7,308 bytes).
- Byte identity of the local file does not prove that the server counts tokens the same way. Section 4.4 adds that check.

**No usage is recorded.** The Voyage adapters never read the response `usage` object (no `usage` reference under `adapters/voyage/`), and the vision adapter ignores `response.usage`. `docs/design-choices.md` states pricing facts as prose that is now outdated.

**Images between 16 and 40 MP are rejected for embedding.** Ingestion accepts up to 40 MP (`ingestion/assets.py:16`). The multimodal embedder rejects anything above 16 MP (`adapters/voyage/visual.py:180-183`), the provider limit.

## 4. Design

### 4.1 Model registry

One packaged data file, `src/tychora/resources/models/registry.toml`, holds one entry per model. Typed contracts in `contracts/models.py` describe it, and `configuration/model_registry.py` loads it once per process. The loader validates it and caches it by file digest. The registry digest enters the resolved configuration's `index_fingerprint` and `query_fingerprint`, so an edited limit is visible as a configuration change.

Entries are a tagged union by `role`:

| Field | text / code | image | rerank | vision |
| --- | --- | --- | --- | --- |
| `id`, `provider`, `endpoint`, `status`, `released` | yes | yes | yes | yes |
| `asset_selection` (a `model-assets.json` selection) | yes | yes | yes | no |
| `contextualization` (`joint` or `independent`) | yes | `independent` | no | no |
| `embedding_space` (identifier of the vector space the model writes into) | yes | yes | no | no |
| `execution` (`hosted` or `local`) | yes | `hosted` | `hosted` | `hosted` |
| `dimensions` (tuple, provider default first) | `[1024, 2048, 512, 256]` | `[1024, 2048, 512, 256]` | no | no |
| `output_dtypes` | `float`, `int8`, `uint8`, `binary`, `ubinary` | `float` | no | no |
| Provider limits | group/request tokens | pixels, tokens, bytes, assets | query, pair, aggregate tokens, documents | long edge, visual tokens, bytes per image, request bytes |
| `request_options` | `enable_auto_chunking=false`, `truncation=false` | output dtype | `truncation=false` | `thinking`, `effort`, `max_tokens` |

`embedding_space` is the compatibility fact behind M8. The voyage-4 series (`voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-4-nano`) shares one space, `voyage-4`, which Voyage documents: "All embeddings created with the 4 series are compatible with each other." `voyage-context-4` and `voyage-code-4` each get their own space, because Voyage does not document them as sharing the voyage-4 space; their documentation embeds queries with the same model. `voyage-multimodal-3.5` has its own space. Two entries share a space only when a vendor page says so, and the entry cites it.

`status` takes one of three values:
- `qualified`: the entry passed every check in section 6 and may be a default.
- `candidate`: the entry is registered but not yet evaluated. It can be selected only explicitly, and `inspect settings` shows a "not qualified" warning.
- `retired`: the provider has announced retirement. New workspaces reject it, and existing workspaces get a health warning that names the replacement.

Tokenizer revisions and sha256 values stay only in `model-assets.json`; the registry references the selection. This avoids a second copy of each hash. A unit test checks that every referenced selection exists and contains exactly one `tokenizer.json`.

Example rerank entry:

```toml
[[model]]
role = "rerank"
id = "rerank-3"
provider = "voyage"
endpoint = "https://api.voyageai.com/v1/rerank"
status = "qualified"
released = 2026-09-30
asset_selection = "rerank-3"
limits = { query_tokens = 8000, pair_tokens = 32000, aggregate_tokens = 600000, documents = 1000 }
request_options = { truncation = false }
```

**Code that changes.**
- `qualify_voyage_rerank`, `qualify_voyage_capabilities` and `qualify_voyage_visual_capabilities` take a registry entry plus the verified counter. The model checks disappear from their bodies.
- `default_bindings.compose_default_bindings` resolves the entry with `registry.require(role_slot, model_id)` and passes it in.
- The duplicate check in `application/reranking.py:635` is removed. `create_reranker` uses the same `registry.require`.
- `load_voyage_tokenizer` accepts any selection that the registry references.
- `QUALIFIED_MODELS` in `settings_draft.py` becomes a query of `qualified` and `candidate` entries per slot.
- `RerankerSettings.model` and `VisionSettings.model` become `NonEmptyStr`, checked against the registry at configuration resolution.

Layering: adapters receive entries and never read the file.

**Errors.**
- A model id missing from the registry, or registered for another role, fails configuration resolution with `INVALID_REQUEST`, and the message names the slot and the qualified alternatives.
- A registered model whose assets or credential are missing fails at binding with `CAPABILITY_MISSING` (unchanged), and the message names the `manage setup` selection to run.

### 4.2 Defaults per tier

Defaults live in `src/tychora/resources/models/defaults.toml` as one table per tier. Presets (sub-project 5) may override a role, but only with a `qualified` entry.

| Role slot | `offline` tier | `online` tier | Preset overrides |
| --- | --- | --- | --- |
| `providers.text` | disabled | `voyage-context-4` (documents and queries, hosted) | any preset may opt into the asymmetric family of 4.9 |
| `providers.text` for code | disabled | `voyage-code-4` | none: code content uses it in every preset, including `software` |
| `providers.image` | disabled | `voyage-multimodal-3.5` | none: `visual` is a kind in every preset (`general`, `software`, `regulatory-normative`, `regulatory-administrative`, `knowledge`), and the role is required whenever a collection holds visual sources |
| `providers.rerank` | disabled | `rerank-3` | none |
| `providers.vision` | disabled | `claude-haiku-5-5` | none |

**Code default.** `voyage-code-4` (released 2026-08-13) is confirmed as the code default. Voyage reports NDCG@10 gains over `voyage-code-3` of 27.54% on agentic code retrieval (19 benchmarks) and 13.98% on traditional code search (28 datasets), and of 48.58% and 40.06% over OpenAI `text-embedding-3-large`. It costs $0.12 per million tokens against $0.18 for `voyage-code-3`, has a 32,000-token context and offers 2048, 1024 (default), 512 and 256 dimensions. There is no contextual code variant, so code families stay `independent`. The blog lists float, int8, uint8 and binary output, but the API documentation's `output_dtype` list omits `voyage-code-4`; the live qualification (4.4) therefore requests each data type once before the entry lists it in `output_dtypes`.

**Offline stays the base profile.** `ConfigurationValues.offline` defaults to `True` (`configuration/models.py:205`), and the offline tier keeps every remote role disabled. Enabling the online tier never grants upload consent. Each remote role still requires the collection's existing provider consent.

**Reranker choice.** The online default is `rerank-3`. `rerank-3-lite` is a qualified alternative, not an automatic fallback, because switching models without telling the user would break M6. The rule for the shipped default:
- If the benchmark run that qualifies a release shows query p95 ≥ 1 s with `rerank-3`, and the rerank stage is the largest contributor, the release changes the default to `rerank-3-lite`. That change is one data edit, recorded in the release notes.
- Vendor latency is unpublished. The research estimate (150–400 ms for 50 documents) must be measured, not assumed.

**Defaults are written into the workspace explicitly.** When the settings flow first activates the online tier, the chosen model ids are written into the workspace configuration as explicit values. A later release that edits `defaults.toml` therefore never changes an existing workspace (see 4.7).

### 4.3 Rerank input sizes

Chunking v2 fixes a hard chunk maximum of 2,000 tokens (validation rejects any preset above it). The rendered representation (`ports/reranking.py:44-57`) adds kind, title and heading lines, which chunking v2 caps at 400 tokens by dropping the deepest heading labels. Tychora's effective limits are derived from the entry, not hand-tuned:

- `max_representation_tokens = chunk_hard_max + 400`, giving 2,400. The 400-token allowance is checked by a test that renders the longest heading path allowed by the normalizers and confirms the header truncation. `retrieval/rerank.py:452-457` takes its budget from the bound capabilities instead of the literal 1200.
- `max_candidates = 64`, unchanged (`retrieval/rerank.py:180`).
- `max_query_tokens = min(entry.query_tokens, floor(entry.aggregate_tokens / max_candidates) − max_representation_tokens)`, which gives min(8,000, 9,375 − 2,400) = 6,975. A full pool of maximum-size inputs then always satisfies the aggregate check at `adapters/voyage/rerank.py:127-140`, so that check never fails at query time. A query longer than 6,975 tokens is reported as `INVALID_REQUEST` naming the limit (unchanged behaviour, higher limit).
- `max_pair_tokens = entry.pair_tokens` (32,000).

### 4.4 Tokenizer assets and verification

Add selections `rerank-3` and `rerank-3-lite` to `model-assets.json`. Each has one `tokenizer.json` asset pinned to the revisions above with sha256 `c0382117…87539`, and a notice entry citing the Apache-2.0 model card at the pinned revision. `manage setup rerank-3` provisions them offline from a local bundle or from the explicitly selected online source, like the existing selections (`docs/distribution.md`). `open`, `add` and `search` still never download.

Byte identity of the file is necessary but not sufficient. A live qualification test (`live` marker, opt-in) sends 20 synthetic documents of varied scripts (Latin, CJK, code, formulas) and compares the provider's `usage.total_tokens` with the local count of query × documents plus documents. The test passes only on exact equality. If it fails, the entry stays `candidate`, and the local cap is lowered by the measured worst-case ratio until a matching tokenizer is published. The same test runs for every Voyage entry, which also covers `voyage-context-4` and `voyage-code-4`.

### 4.5 Vision with Claude Haiku 5.5

Vision keeps the existing consent-gated path: `dispatch_captured_vision` (`adapters/vision/anthropic.py:211-247`), with authorization after queue admission and an epoch recheck. The registry entry `claude-haiku-5-5` records:
- provider limits: 8,000 × 8,000 px; 10 MB base64-encoded per image; 32 MB per request; long edge 2,576 px and 4,784 visual tokens (Claude 4.7-and-later high-resolution tier; visual tokens = ⌈w/28⌉·⌈h/28⌉);
- the many-image rule: when a request has more than 20 images, each side must be at most 2,000 px;
- request options.

**Downscaling.** A new pure function `fit_vision_region(image, entry) -> (bytes, transform_id)` runs before dispatch:
1. If the long edge is above 2,576 px or the visual-token count is above 4,784, scale down with Lanczos to the largest size that fits both, preserving aspect ratio.
2. Encode as PNG.
3. While the base64 size is above 10 MB, scale by 0.8, for at most five steps. If it still does not fit, return `LIMIT_EXCEEDED`.

The transform id (`fit-2576-4784-lanczos-png-v1`) goes into `region_pixel_transform`, so the observation cache key and provenance change deterministically. Downscaling locally rather than relying on server-side resizing keeps the pixels the model saw reproducible and keeps cost predictable.

**One image per request.** The adapter keeps sending one image per request, so the many-image rule does not apply. The rule is recorded so that a future multi-image builder can enforce it.

**Embedding guard.** The same fit function, with the image entry's 16 MP limit, downscales images between 16 and 40 MP for multimodal embedding instead of rejecting them. The 40 MP decode guard in `ingestion/assets.py:16` stays as the hard ceiling.

**Response shape.** Haiku 5.5 supports adaptive thinking. The entry sets `thinking = "disabled"` if the Models API reports `thinking.types.disabled.supported`; otherwise it sets `effort = "low"`. The parser then accepts zero or more thinking blocks followed by exactly one text block. It still requires `stop_reason == "end_turn"`. Thinking content is discarded and never retained as evidence. The live qualification records which case applies.

**Batch dispatch.** The background enrichment phase (programme contract; sub-project 4) dispatches through a `VisionDispatcher` port with two implementations:
- `interactive`: the current synchronous path, used for explicit user selections.
- `batch`: the Message Batches API at 50% of the synchronous price. It is the default for background enrichment jobs with at least 20 regions; smaller jobs use `interactive`, because batch turnaround is not worth it for a handful of images.

Rules for `batch`:
- Authorization runs per region at submission.
- When results arrive, the dispatcher repeats the epoch and withdrawal check before retaining each observation.
- A region whose source was withdrawn in the meantime is discarded unread.
- Batch identifiers are stored with the job so that a restart polls instead of resubmitting.

### 4.6 Usage ledger and prices

Every provider adapter returns a `ProviderUsage` value with its result:

```
ProviderUsage(provider, model, operation, units: {input_tokens?, output_tokens?, images?, pixels?, pages?}, latency_ms, outcome, batch: bool, units_source)
```

The owner-side caller hands this value to engine core's usage seams (`run_provider`, `run_query_provider`, `run_captured_provider`, `run_local_text`; engine core §4.10) together with the collection and job or query reference. Field names, the `operation` values (`embed_document`, `embed_query`, `embed_image`, `rerank`, `describe_image`, `ocr`) and the `batch` and `units_source` columns are those of engine core's `usage_events` table.

Sources of units:
- Voyage `usage.total_tokens` (recorded as `input_tokens`), and for multimodal requests also image pixels.
- Anthropic `usage.input_tokens` and `usage.output_tokens`.
- Local counts are used only when the provider omits usage, and the row then carries `units_source = "local"`.

Failed and cancelled calls record latency and `outcome`, with zero units unless the provider reported some.

**Price table.** Prices live in `src/tychora/resources/models/prices.toml`. Each row holds provider, model, operation, unit, USD per million units, `batch` flag, `effective_from`, `source_url` and `retrieved_on`. The table has a version and a digest, and becomes engine core's `builtin` price version (engine core §4.10). A workspace overrides rows by creating a `user` price version (`PUT /v1/prices`, `tychora manage prices set`); overrides are shown separately and never replace the packaged history. The ledger stores units only, and cost is computed at read time with the row effective at the call's timestamp, as the programme contract requires.

Initial rows, with research figures re-checked on the vendor pricing pages when the rows are entered:

| Model | Rows (USD per million) |
| --- | --- |
| `voyage-context-4`, `voyage-code-4`, `voyage-4-large` | tokens 0.12 |
| `voyage-multimodal-3.5` | text tokens 0.12; pixels 0.60 per billion |
| `rerank-3` | tokens 0.05 |
| `rerank-3-lite` | tokens 0.02 |
| `claude-haiku-5-5` | input 0.10, output 0.50 (prompts ≤ 100K tokens); batch rows at 50% |

`voyage-4-nano` runs locally and has no price row. Its encodings still write usage rows with `provider = 'local'` and `operation = 'embed_query'` (engine core §4.4), which cost nothing, and the query trace records their latency.

The stale pricing prose in `docs/design-choices.md:244-253` is replaced by a pointer to the table.

### 4.7 Upgrade policy

- A workspace's models are the explicit values in its configuration (4.2). A release that changes `defaults.toml` only adds an `inspect settings` notice: "newer default available for providers.rerank: rerank-3 (current rerank-2.5)".
- The user changes a model through the existing settings draft → preview → activate flow (`application/settings_draft.py`), from the CLI or the dashboard.
- The preview classifies the change:
  - **Query-only roles (rerank).** The change applies at activation; no stored data changes. Cache entries already carry the model identity, and the query trace records the model used.
  - **Vector roles (text, code, image).** The document model, query model, dimension and index data type are part of the vector family identity. The preview states "requires rebuilding the text channel" and gives an estimated token count and cost from the corpus aggregates and the price table. Activation schedules engine core's `manage rebuild index --channel text` (engine core §4.11). The old family serves queries until the new one publishes. The rescoring depth of a binary family (4.8) is query-only.
  - **Vision.** Existing observations keep their recorded model. Re-describing pictures is an explicit enrichment rerun (sub-project 4), never automatic.
- A `retired` model produces a health warning naming its replacement, and never forces a switch.

### 4.8 Dimensions and quantization per vector family

Every Voyage embedding entry in the registry supports Matryoshka output dimensions 256, 512, 1024 and 2048, and the text and code entries support `float`, `int8`, `uint8`, `binary` and `ubinary` output (`voyage-multimodal-3.5` is documented with float output only). Dimension and index data type are therefore options of each vector family (engine core's `VectorFamilySpec.dimension` and `index_dtype`, engine core §4.4), set in the family's settings and validated against the entry. Tychora always requests float output at the chosen dimension; the batch artifact keeps those float32 rows, and quantization happens in the vector index adapter, so the provider's quantized output types are recorded in the registry but not requested.

Three index data types are offered:

| `index_dtype` | Indexed | Search |
| --- | --- | --- |
| `float32` | float32 vectors | exact or approximate cosine |
| `int8` | int8 vectors, 4× smaller than float32 | cosine on int8, then rescoring of the top `rescore_top` with the float32 rows |
| `binary` | packed binary vectors (32× smaller than float32) | Hamming first pass, then full-precision rescoring of the top `rescore_top` (default 100) with the float32 rows of the batch artifact |

Voyage's figures guide the grid but do not choose the default. For `voyage-code-3`, averages were 92.28 at 1024 dimensions, 92.00 at 512 and 91.34 at 256, and binary search followed by full-precision rescoring of about the top 100 was up to 4.25% better than binary alone. Voyage publishes no quantization tables for the 4 generation, so the defaults come from our evaluation:

- **Grid.** For each vector family (text, code, image), every supported combination of dimension {256, 512, 1024, 2048} and `index_dtype` {float32, int8, binary}, run by engine core's `--suite families` harness on the benchmark corpus and the evaluation set, hybrid + rerank stage.
- **Recorded per configuration.** Bytes per vector and per corpus, ingestion tokens, vector-stage and total query p50/p95, nDCG@10 and recall@10.
- **Selection rule.** The configuration with the smallest stored bytes whose nDCG@10 and recall@10 are non-inferior to float at 1024 dimensions (lower bound of the paired-bootstrap 95% interval of the difference ≥ −0.01) and whose query p95 keeps M3. Ties go to the higher dimension.
- **Until the evaluation runs**, each family ships with 1024 dimensions and `float32`, the provider default.

The chosen values are data in `defaults.toml`, the report is committed under `docs/evaluation/`, and a later change follows the upgrade policy (4.7). For the `binary` index type, the adapter's packing (sign bit, most significant bit first) is part of the adapter version recorded with the family, so indexed bits are never reinterpreted.

### 4.9 Optional asymmetric text family ("fast/offline query")

The default text family stays `voyage-context-4` for documents and queries, embedded by the hosted API. A preset or workspace may instead select the asymmetric family:

- **Documents:** `voyage-4-large`, hosted, `independent` contextualization, under the collection's existing upload consent.
- **Queries:** `voyage-4-nano`, run locally. Voyage endorses this setup: the voyage-4 series shares one space, and its announcement recommends embedding documents with `voyage-4-large` and queries with a smaller model.
- **Asset.** `voyage-4-nano` is open weights under Apache-2.0 (about 0.34B parameters, BF16, 32K context). The `voyage-4-nano` selection in `model-assets.json` pins the Hugging Face revision and the sha256 of the weights, `tokenizer.json` and configuration files, with a notice entry citing the licence. `manage setup voyage-4-nano` provisions it like the other selections; `search` never downloads. A converted build (ONNX or int8) may be used only if it is pinned by sha256 and a qualification test shows cosine ≥ 0.999 against vectors from the reference build on 200 synthetic queries.
- **Dimension.** Both sides use the same Matryoshka dimension, recorded in the family identity; the family is rejected otherwise.

Trade-off against the default family. Voyage's results (unlabelled metric, read from a partly loading results sheet, so approximate) give 77.87 for `voyage-4-nano` queries against `voyage-4-large` documents, against 76.41 for nano on both sides, 79.91 for `voyage-4` queries and about 81 for `voyage-4-large` on both sides. `voyage-context-4` scores 0.45% above `voyage-4-large` overall and 7.11% above single-vector embedding on LongEmbed. The asymmetric family therefore gives up both contextual chunk embedding and roughly 3 points of query quality. In return, queries need no network round trip (typically 100–300 ms, an unmeasured estimate), no credential and no per-query cost, query text never leaves the machine, and vector search keeps working offline after ingestion. Its local query cost must be measured on the benchmark machine and reported with M3.

**Never mixing spaces.** A family names one document entry and one query entry, and the loader requires their `embedding_space` to be equal (M8). Querying a `voyage-context-4` or `voyage-code-4` family with `voyage-4-nano`, or switching a family between the default and the asymmetric setup, is a vector-role change with a full rebuild (4.7). Code families never use the asymmetric setup.

## 5. Migration and compatibility

The programme rule applies: new workspace format, rebuild from retained originals, no dual code paths.

- Workspaces created before v2 are rebuilt by engine core. During rebuild, their resolved provider settings are rewritten as explicit model ids, keeping their current choices: `rerank-2.5` stays `rerank-2.5`, still `qualified`.
- `rerank-2.5` and `voyage-code-3` remain registry entries, because existing users have chosen them. They are not defaults.
- The `RerankerSettings.model` literal is removed, and no alias table is kept.
- `TYCHORA_MODEL_CACHE` bundles gain the two new selections. CI setup adds `rerank-3` to the explicitly fetched selections (`docs/distribution.md`).

## 6. Testing and verification

| Layer | Test | Proves |
| --- | --- | --- |
| Unit | Registry loader rejects duplicate ids, unknown roles, missing asset selections and `candidate` defaults; property test that derived rerank limits always satisfy the aggregate constraint | M2, 4.3 |
| Unit | Loader rejects a family whose document and query entries have different `embedding_space`, or different dimensions, and a dimension or data type the entry does not list | M8, 4.8, 4.9 |
| Unit | Binary packing round trip; `binary` with rescoring returns the same top 10 as `float32` search on fixed vectors when `rescore_top` covers the corpus | 4.8 |
| Qualification | `voyage-4-nano` local encodings match reference vectors (cosine ≥ 0.999) for the pinned build | 4.9 |
| Benchmark and evaluation | Dimension × mode grid per vector family, and the asymmetric family against the default, with storage, latency and quality recorded | M7, 4.9 |
| Unit | `fit_vision_region` on synthetic images (8K × 8K, 40 MP, high-entropy noise for the byte loop, tiny); transform id stable | 4.5 |
| Contract (default suite, mocked) | One parametrized test per registry entry: request body shape, `truncation=false`, response mapping, `ProviderUsage` extraction from recorded fixture responses (successful, missing `usage`, provider error) | M2, M5 |
| Contract | Haiku response with thinking blocks + one text block accepted; two text blocks rejected | 4.5 |
| Contract | Batch dispatcher: withdrawal between submit and result discards the observation; restart polls an existing batch id | 4.5 consent |
| Integration | Settings preview for a text model change reports rebuild + cost; rerank change does not; a release-default change leaves an existing workspace's resolved config byte-identical | M6 |
| Live (`live` marker, `TYCHORA_RUN_VOYAGE_LIVE=1` / `TYCHORA_RUN_ANTHROPIC_LIVE=1`) | Tokenizer-count equality (4.4) per Voyage entry; Haiku structured-output round trip on a synthetic diagram; thinking-disable capability recorded | Qualification |
| Benchmark | Engine core harness, online profile, warm: per-stage p50/p95 for query embed and rerank with `rerank-2.5`, `rerank-3`, `rerank-3-lite` at pool sizes 32 and 64 | M3, default choice |
| Evaluation | Existing `manage evaluate` on the public evaluation set: nDCG@10, recall@8, MRR for no-rerank, 2.5, 3, 3-lite; report committed under `docs/evaluation/` | M4 |

The live tests remain opt-in and synthetic (`tests/qualification/test_voyage_live.py` pattern); they never run in the default suite. An entry becomes `qualified` only when its contract test, live test and evaluation row all pass. The status change is a reviewed data commit that links the report.

## 7. Code-health ratchet

- **Duplication.** Seven copies of model facts collapse into the registry. A CI check (`rg` over `src/tychora --type py`) fails on string literals matching `rerank-[0-9]|voyage-(4|context|code|multimodal)-|claude-` anywhere in Python source under `src/`; tests and recorded fixtures are exempt.
- **Function size.** `compose_default_bindings` (215 lines, `default_bindings.py:209-423`) loses its three model branches. Each provider binding becomes a helper of at most 40 lines (`_bind_text`, `_bind_image`, `_bind_rerank`, `_bind_vision`), each keyed by the registry entry.
- **Layering.** Adapters receive entries and never read resources. The import-linter contract from sub-project 0 forbids `adapters` importing `configuration.model_registry`.
- **Dead code.** Remove the second rerank qualification in `application/reranking.py:633-643` and the hard-coded `_visual_limits` body.
- **Comments.** Docstrings state what each qualification function checks, without plan task numbers (flow review §3.4).
- **Tests.** New contract tests use recorded fixtures, with no network access.

## 8. Risks and open questions

1. **Server-side token counts for rerank-3 differ from local counts.** Default: keep the entry `candidate` and ship `rerank-2.5` as the online default until the counts match. M1 is then reported as partially met.
2. **`rerank-3` makes query p95 exceed 1 s.** Default: switch the shipped default to `rerank-3-lite` (4.2), then drop the pool to 32 if lite still exceeds it. Each step is a data change, and both are measured.
3. **Batch dispatch keeps inputs at the provider while the batch is pending, and results stay retrievable for a period.** Default: the collection consent text for the `anthropic` provider names batch processing explicitly. The dashboard consent screen (sub-project 6) shows it, and no new consent scope is added. If the product owner wants batch to require separate consent, it becomes an extra flag on the existing consent record.
4. **The Haiku 5.5 price "from $0.10 / $0.50" may vary by prompt length.** Default: price rows carry the ≤ 100K-token band only, because a single-region prompt stays far below it. A row for a higher band is added if the ledger ever records a larger prompt.
5. **The vector store cannot do a binary first pass with rescoring.** Default: the adapter refuses the family (engine core §4.4, `family.dtype_unsupported`), so `binary` stays out of the evaluation grid for that store and the report says so; `float32` remains, and `int8` where the store supports it.
6. **Local `voyage-4-nano` query encoding is too slow on CPU.** No CPU timings are published. Default: the asymmetric family stays optional and is offered only if its measured query p95 keeps M3 on the benchmark machine.
7. **Conflict check with the programme.** None found. The programme asks for the current multimodal model; no 4-generation multimodal model exists, so this spec uses `voyage-multimodal-3.5` and makes a future swap a registry entry.

## 9. Flow-review findings

Closed: none fully. This spec contributes the policy half of **D8**: model changes are explicit, previewed and followed by a rebuild. Engine core v2 supplies the rebuild mechanics.

Deferred:
- **C5** ("configuration-derived family identity, family-less vector record identifiers"): engine core v2 owns family identity. This spec only guarantees that the model id and registry digest are inputs to it.
- **D7** (configuration hashing on hot paths): engine core. The registry is cached by digest and adds no hot-path serialization.
