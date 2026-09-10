# Tychora — application design

Date: 2026-09-10
Status: product direction accepted; detailed engineering design under review
Related specifications: [Kinds/relations/output configuration](2026-09-10-tychora-configuration-design.md), [Distribution](2026-09-10-tychora-distribution-design.md), [Code retrieval profile](2026-09-10-tychora-code-retrieval-design.md), [Dashboard](2026-09-10-tychora-dashboard-design.md), [Engineering](2026-09-10-tychora-engineering-design.md), [Storage schema](2026-09-10-tychora-storage-schema.md), [Algorithms](2026-09-10-tychora-pipeline-algorithms.md), [Lifecycle and recovery](2026-09-10-tychora-lifecycle-design.md)

## 1. Product

Tychora is a local application for turning documents and code into discoverable, versioned evidence. People and agents can register sources, ingest them, search across text and images, inspect exact passages and source files, follow documented relationships, and measure retrieval quality.

The application has one set of services exposed through a Python API, CLI, MCP, and HTTP. A browser dashboard is specified now and implemented after those services. It is a corpus and retrieval workbench with saved query runs, rather than a required conversational interface.

Search returns structured evidence and deterministic JSON, Markdown, or agent-oriented formatting. The calling agent writes the final answer or review. Tychora does not presently call a generative model to write answers, produce reviews, or promote proposed rules. Selective vision during ingestion is a separate, explicit capability; its outputs remain labelled derived material.

Public code, documentation, tests, screenshots, examples, package metadata, and Git history use generic terminology, synthetic fixtures, and redistributable public references. Imported code must be reviewed for embedded data and retain applicable license notices. Local corpora, credentials, conversation exports, and machine-specific paths are excluded from distributions through explicit allowlists. Proposed project license: MIT.

### Representative uses

- Search a software standard together with public regulations and internal implementation guidance, retaining their different roles.
- Retrieve a government template instruction and its corresponding diagram with exact source locators.
- Find reviewer feedback related to a passage, retaining the quoted target, status, and provenance.
- Find a code symbol, inspect its definition at an indexed revision, and explore callers or imports.
- Compare retrieval configurations using a labelled evaluation set and inspect failures.

## 2. Product commitments

1. A small daily command surface; registering a source runs the required configured extraction and indexing stages.
2. Discoverable kinds, filters, collections, available capabilities, corpus coverage, and model configuration.
3. Original source bytes, source revisions, extraction artifacts, normalized evidence, and model-derived material remain distinguishable.
4. Exact identifiers, lexical search, text vectors, image vectors, reranking, and coherent source-context reconstruction coexist.
5. Source access, dates, review states, and usage restrictions apply to every retrieval channel and to exact inspection and relationship traversal.
6. Expensive work is resumable and cached; failed reindexing does not replace a working snapshot with partial results.
7. Graph traversal and vector search use dedicated engines behind replaceable interfaces.
8. Evaluation, observable jobs, and useful failure messages are part of the initial application, not dashboard-only additions.
9. Code retrieval preserves bounded parent/sibling context, language-specific extraction contracts, file-qualified symbol identity and code-specific quality/cache benchmarks.

## 3. Components and dependencies

Transport adapters depend on application services. Services depend on domain contracts and storage/provider interfaces. Concrete adapters implement those interfaces. Domain contracts do not import database clients, HTTP frameworks, CLI libraries, or model SDKs.

| Component | Responsibility |
| --- | --- |
| `contracts` | Source identities, revisions, evidence units, assets, assertions, policies, query contracts |
| `application` | Add/sync sources, execute queries, inspect evidence, traverse relations, evaluate, maintain workspaces |
| `ingestion` | Acquisition, extraction, normalization, locator preservation, quality diagnostics |
| `indexing` | Context groups, exact/lexical/vector/graph projections, checkpoints, snapshot activation |
| `retrieval` | Policy compilation, candidate generation, fusion, reranking, context assembly, response formatting |
| `adapters` | SQLite, Chroma, graph database, Docling, code parsers, Voyage and optional vision providers |
| `interfaces` | Python API, CLI, MCP, HTTP; no separate business logic |
| `evaluation` | Gold sets, replay, metrics, ablations, comparison reports |
| `observability` | Job events, query traces, retention and aggregation |
| `profiles` | Declarative document kinds, role descriptions, metadata fields and query defaults |

Use `uv` for environments, dependency resolution, locking and builds; Ruff for lint/format; `ty` for type checking; pytest for tests. Typer with Rich supplies completion, readable help and progress. These tools have separate roles: Astral's tools do not supply the CLI framework. [uv](https://docs.astral.sh/uv/), [Ruff](https://docs.astral.sh/ruff/), [ty](https://docs.astral.sh/ty/), [Typer](https://typer.tiangolo.com/).

Use Pydantic for validated transport/configuration contracts and JSON Schema, FastAPI for HTTP, and the official MCP Python SDK for transport compliance. Use standard-library SQLite and TOML parsing where sufficient. Heavy parser/provider/storage integrations are packaged as explicit extras with a recommended full installation. Command help and configuration inspection must not load model weights, initialize databases, or construct external clients.

Target Python 3.12 initially. Additional Python/platform support is advertised only after clean installation and integration checks pass. Pin tested provider/model identities and record them with each index build; never silently float an existing index to a newer model.

## 4. Storage decision and interfaces

### Recommended split

| Interface | Initial implementation | Future adapter |
| --- | --- | --- |
| `CorpusStore` | SQLite: sources, revisions, normalized units, policies, manifests, jobs and activation receipts | PostgreSQL |
| `LexicalIndex` | SQLite FTS5 and exact identifier lookup | PostgreSQL full-text or another lexical index |
| `VectorIndex` | Embedded persistent Chroma; separate collections per compatible embedding family, containing immutable extraction batches | Qdrant or another vector service |
| `GraphStore` | LadybugDB, conditional on the acceptance checks below | Neo4j |
| `ArtifactStore` | Local content-addressed files | Object storage |

SQLite is not the graph traversal engine or a brute-force vector backend in the production profile. GraphStore owns indexed nodes/edges and relationship queries. Original relationship assertions and their derivation are retained in extraction/event artifacts, allowing graph projections to be rebuilt without inferring relationships again. Model vector caches, when enabled, are content-addressed artifacts rather than a second query implementation in CorpusStore.

Chroma provides a persistent embedded Python client. [Client documentation](https://docs.trychroma.com/docs/run-chroma/clients).

LadybugDB is the proposed embedded property graph engine. The original Kuzu repository is archived, so it is not a new dependency candidate. Ladybug has Python distribution and ongoing releases, but those facts do not establish fitness for this application. [Kuzu archive](https://github.com/kuzudb/kuzu), [Ladybug releases](https://github.com/LadybugDB/ladybug/releases), [Python distributions](https://pypi.org/project/ladybug/).

Before adopting Ladybug, require binary installation on the declared OS/Python matrix, persist/reopen, idempotent upsert/delete, bounded traversal, transaction rollback, concurrent service requests, cancellation, forced-termination recovery, and export/rebuild tests. Check exact wheel tags, including minimum macOS versions. If these fail, report the failed acceptance criterion and revise the graph adapter choice; do not substitute SQLite traversal and describe it as equivalent.

A combined graph/vector engine remains an alternative, but is not selected merely because it implements both features. It would have to pass the same independent retrieval, filtering, durability, packaging and migration checks.

### Portability without pretending every database is identical

Interfaces expose operations such as writing a revision, selecting eligible evidence, writing lexical/vector batches and sealing graph generations, finding ranked candidates, retrieving bounded neighbours and traversing typed paths. They do not expose raw SQL or promise arbitrary Cypher portability. Adapters report supported predicates, transactional guarantees, limits and health. An unsupported required filter fails clearly; it is never silently omitted. Common adapter contract suites enforce result identity, eligibility, lifecycle and errors.

Identifiers use a persisted workspace UUID and stable source/revision/evidence IDs, independent of absolute filesystem location. Embedding-space identity includes provider, model, dimension, input representation version and metric. Code-specific embeddings may occupy a separate space and contribute through rank fusion.

### Ownership, transactions and index visibility

There is no distributed transaction across SQLite, Chroma and Ladybug. Ingestion commits source material and a manifest, then writes immutable lexical/vector batches within compatible families and builds a full graph generation. Only after readiness checks does one CorpusStore transaction publish the membership and exact slot/batch receipts. Queries pin those bindings. Leases survive client deadlines until all associated backend work exits. Shared FTS statistics are recorded separately: a snapshot fixes evidence membership, not identical BM25 rankings.

Updates reuse unchanged extraction/embedding artifacts and compatible batches; graph v1 builds a full generation for the selected membership. This does not require reembedding the entire corpus. A deleted or withdrawn source becomes ineligible immediately in CorpusStore; physical index cleanup can follow. Reusing stale vector results must never bypass current eligibility.

Ladybug requires shared connections to a single read/write database object rather than independent readers beside a writing process. [Concurrency](https://docs.ladybugdb.com/concurrency/). Tychora therefore has one owner for an open workspace. A standalone command can own it for the command's lifetime. `serve` owns it while running; other commands delegate to that verified local service. A second standalone owner gets an actionable busy error. Parsing may run in bounded workers, but database writes pass through the owner. No automatic background daemon is required.

Read-only corpus operations do not mutate sources or index state. Optional operational history writes are a separately configured capability. Basic help does not acquire the workspace.

## 5. Corpus and discovery model

Core objects are Workspace, Collection, Source, Revision, Extraction, EvidenceUnit, SearchChunk, Asset, RelationshipAssertion, IndexFamily, IndexBatch, PublishedSnapshot, Job and QueryRun. IndexGeneration specifically describes the graph projection.

A Source is a logical item. A Revision identifies original bytes and declared edition or code snapshot, with publication/effective dates where supplied. Identical bytes can have different declared editions. A new revision does not overwrite older evidence or weaken its access classification. Derived extraction versions are recorded independently from source revisions.

An EvidenceUnit has a type, parent, ordering, original or derived text, identifiers and a locator. A document locator can contain heading path, physical page, printed-page label, bounding box and native object reference. A code locator includes repository identity, revision or working-tree snapshot, relative path, line/byte range and qualified symbol. Unknown locators remain unknown; no invented PDF-style pages for a DOCX without a layout-preserving renderer.

Assets contain immutable hashes, MIME type, artifact references and source-region links. Relationships carry typed endpoints, direction, provenance, confidence, assertion status and revision scope. Native links, parser-inferred calls and model-suggested relations are different derivation classes.

### Semantic kinds are separate from file formats

A PDF may contain a law, standard, manual, template or feedback. A kind definition declares its stable key, label, description, evidence role, required/optional metadata, normalization hints, permitted uses, available relations and representative queries. An adapter declares formats and extraction capabilities separately.

Ship generic presets for documents, standards/regulations, feedback and code. Built-in kinds include law, regulation, standard, guidance, template, manual, feedback, example, research note, code file and architecture decision. Presets are declarative and editable; arbitrary metadata fields do not automatically become filterable. The configuration specification defines versioned role/lane/mode registries, concrete kind/relation TOML examples, structured normalization, global applicability versus missing values, and a deterministic hierarchical output profile. Register typed facets explicitly.

`inspect kinds` returns descriptions, examples and requirements, including zero-count configured kinds. `inspect sources` reports accessible source counts, versions, ingestion status and coverage. Discovery also exposes supported filters/operators, relation types, installed adapters, enabled models, limitations and structured request schemas. Counts and diagnostics obey the same access scope as search.

### Access and dates

Collection scope, source classification, usage policy, review state and allowed provider upload scope are separate concepts. A caller cannot elevate server permissions by supplying a higher clearance in query JSON. Standalone operation uses the configured local principal; server operations intersect request filters with server-granted capabilities.

Publication cutoff and effective-date selection are distinct. Queries choose an unknown-date policy: allow with a warning or exclude; strict temporal presets exclude unknowns. Multiple editions are reported as editions, not automatically labelled contradictions. A contradiction requires an explicit assertion or separately justified detection.

## 6. Ingestion and indexing

The flow is register source → acquire/hash → extract → normalize → validate → commit source revision → build projections → verify → activate snapshot. Each step records its version, inputs, outputs and diagnostics. Retrying a job reuses completed artifacts keyed by hashes and configuration fingerprints. A failed optional enrichment leaves the original evidence intact and marks that capability incomplete.

### Documents and visual content

Use native Markdown/text parsing for inexpensive faithful structure. Use Docling for rich document extraction, especially PDF and office documents, retaining its structured JSON alongside originals and normalized evidence. Docling documents PDF, DOCX, Markdown and image inputs and JSON serialization. [Supported formats](https://docling-project.github.io/docling/usage/supported_formats/).

Preserve headings, lists, tables, figures, captions, source objects and available geometry. Tables retain physical fragments and per-row provenance; proposed cross-page joins retain their evidence and status. Repeated column labels enrich searchable rows without changing the original source quotation.

Visual assets support original image embeddings and a separate textual retrieval representation. Distinguish caption, OCR and generated description. Selective vision is explicit per profile/provider, cached, and constrained to the smallest useful region. Derived values and inferred annotation targets remain unverified until reviewed; an image description is not an authoritative quotation.

Text and image vectors live in separate embedding spaces. Text-only questions can retrieve linked images; image queries use the matching multimodal provider. Evidence assembly returns the original asset and textual context with source locators. Visual relevance must be evaluated beyond image self-matching.

### Code

Use Tree-sitter adapters with initially tested Python and TypeScript/JavaScript support; expand the advertised language list with fixtures. Preserve symbol boundaries, decorators, parent scope, imports, comments and exact ranges. Respect Git ignore rules plus workspace excludes; avoid secrets, generated/vendor content and symlink escapes. Git operations inspect source state and never execute repository hooks or code.

Store an explicit indexed snapshot. Dirty working-tree bytes must not be attributed solely to HEAD. Unchanged files reuse extraction/embedding artifacts; deletion removes them from the active snapshot. Historical inspection is revision-qualified. Calls/imports/inheritance are typed, and heuristic name resolution is labelled as such. The [Code retrieval profile](2026-09-10-tychora-code-retrieval-design.md) makes structural context, a LanguageAdapter port, resolution diagnostics, file/symbol evaluation and four cache benchmark states first-release requirements. Sparse parent/adjacency assertions replace a quadratic set of sibling links. More elaborate graph propagation and relevance/penalty segment scoring remain explicit experiments.

### Retrieval-related indexing

Build exact identifier, contextual lexical, contextual text-vector, image-vector and graph projections independently. Persist the exact context representation and grouping hash used for embeddings. Protect provider token/batch limits and validate response mapping, dimensions, finiteness and model identity before saving. Model activation requires expected coverage, not merely one existing vector.

## 7. Retrieval and response contract

1. Validate the request and pin a corpus/index snapshot.
2. Resolve caller policy, collection scope, semantic kinds, dates and typed filters.
3. Retrieve eligible exact, lexical and vector candidates; optional graph expansion uses the same policy.
4. Fuse ranks from distinct channels without adding incomparable model scores.
5. Rerank a candidate pool larger than the final output.
6. Reconstruct bounded source context with exact matched units distinguished from included neighbours.
7. Attach linked images and typed relation paths; enforce diversity and a shared response budget.
8. Revalidate returned evidence against current withdrawal/access restrictions and format it.

Each graph path requires eligible endpoints, intermediate nodes, assertion grounding and every required supporting unit. Optional semantic search augmentation uses one-hop traversal; the code preset also supports named parent/sibling structural queries. They share five seeds, at most 32 new units and a 2,000-row work budget; they do not each receive a separate allowance. Explicit relationship inspection is bounded to two hops and 100 edges. Requests can lower configured limits. Relation provenance and confidence remain visible; graph rank expansion is evaluated rather than assumed to improve ranking.

A QueryRequest contains a discriminated question/passage/image input, collection/profile selection, typed filters, requested modalities, context budget and optional previously selected evidence IDs. Advanced candidate counts and model tuning live in configuration or structured request files. The tool accepts passage input directly; hidden generative rewriting and a conversation-memory engine are not part of this release.

A QueryResult contains schema version, run ID, snapshot ID, applied scope, separately ranked `segments` and `visuals`, configured lane IDs, permitted structured-record attachments, exact citations, relevant relations, coverage, typed coverage gaps/decision requirements, warnings and an optional detailed trace with policy and lexical-statistics epochs. Coverage reports what was retrieved and which channels completed; it does not claim that finding a passage proves an answer correct. No matches, partial channel coverage, multiple versions and missing required kinds are distinct states.

Optional profile-defined lane reservations occur during candidate admission and final selection, before formatting; grouping results afterward cannot restore an omitted lane. The pure hierarchical renderer preserves authorized records/citations and supports configured labels and field aliases. Output compatibility and retrieval equivalence have separate fixture gates.

Provider failure preserves successful channel results only if the profile allows degraded retrieval, with an explicit partial result. Strict profiles fail with a typed error. Timeouts, cancellation, retries and provider usage are traceable. External providers receive only authorized inputs; offline mode disables every remote model invocation and never silently switches to a remote fallback.

## 8. CLI and configuration

Five everyday verbs:

| Command | Purpose |
| --- | --- |
| `tychora init` | Create a workspace using documented presets and an explicit processing/provider choice |
| `tychora add PATH` | Register files/folders/repositories and run or resume the configured pipeline |
| `tychora search "question"` | Retrieve evidence; accept a passage/request file or image when needed |
| `tychora inspect SUBJECT` | Discover kinds, sources, capabilities, configuration and runs; open exact evidence or relationships |
| `tychora serve` | Expose the same workspace services through MCP/HTTP |

`inspect` subjects are discoverable nouns, not another set of pipeline-management steps. Typical examples: `inspect kinds`, `inspect sources`, `inspect evidence ID`, `inspect relations ID`, `inspect run ID`.

Less frequent lifecycle operations belong under `tychora manage`: sync registered sources, remove a source, rebuild selected projections, export/import a workspace, check health and run evaluations. Removing sources first withdraws their eligibility. Original-payload removal uses a separate explicit plan and per-revision tombstones; shared files remain while required elsewhere. It does not erase derived evidence, history or external backups. Canonical export copies SQLite and all required retained payloads under leases; import into an empty destination reports omitted native indexes and never calls providers implicitly. [Lifecycle and recovery](2026-09-10-tychora-lifecycle-design.md) defines these operations. This group does not recreate manual configure-vector/index-text/index-images activation chores: ordinary `add` owns those stages.

Use positional inputs, shell completion and concise help with examples. Human output is readable; `--json` emits only the versioned machine response to stdout, with progress/errors on stderr. Structured error codes and process exit statuses are consistent. Common flags cover workspace, profile, output format and offline execution. Advanced ranking knobs stay out of the default help.

Configuration lives in `tychora.toml`, with source metadata in sidecar manifests or the registry. Precedence is validated defaults → selected preset → explicit versioned pack dependencies → selected pack → workspace config → allowlisted request/CLI overrides; the configuration specification defines merge and conflict rules. Credentials are environment/secret references only. `inspect config` shows effective values and their origins with secrets redacted. Configuration is strict: unknown keys or contradictory provider/index settings fail with a useful explanation. Profile upgrades must explicitly map retained historical kind versions or fail with a scoped incompatibility error. Eligibility-cache identity includes the full resolved policy and eligibility request. Embedding changes create a new compatible family; graph representation changes require a new graph generation.

## 9. Programmatic, agent and HTTP interfaces

### Python API is a primary product interface

The installed `tychora` package exports stable typed clients and contracts. A Python application can open a local workspace or connect to the running workspace service without invoking the CLI, parsing formatted strings, starting an HTTP server unnecessarily, or importing private storage modules. CLI, MCP and HTTP are clients of the same application operations, not the API's implementation.

Provide `Tychora` and `AsyncTychora` clients with equivalent request/result semantics. Both support explicit context-manager lifecycle, a configured principal, injected configuration/providers for embedding and tests, and no global mutable workspace or request scope. Local ownership follows the storage rules above; a framework can retain one client across agent requests.

| Public operation | Typed result/behavior |
| --- | --- |
| `discover` | Kinds, filters, collections, capabilities and usage descriptions |
| `sources.add` / `sources.sync` | Source registration and resumable ingestion job handle |
| `search` | Complete structured QueryResult; formatting is separate |
| `evidence.get` / `assets.get` | Exact source evidence and registered original assets |
| `relations.traverse` | Bounded, policy-filtered paths with assertion provenance |
| `jobs.get` / `jobs.wait` / `jobs.cancel` | Typed job state, progress/events and cooperative cancellation |
| `evaluations.run` | Evaluation job and comparable structured metrics |
| `format_result` | Pure JSON, Markdown or agent-envelope formatting |

SourceInput accepts registered local paths and in-memory text/bytes with explicit source identity and metadata; a framework does not need temporary files just to load a document. Byte/format limits apply before extraction. An external-URL acquisition capability, if enabled, uses configured allowed schemes/hosts, bounded downloads and redirect validation rather than unrestricted server-side fetching.

Requests accept validated models and documented primitive mappings. Responses expose typed fields and lossless schema-versioned serialization. Errors distinguish invalid input, denied scope, not found, busy workspace, missing capability, incomplete index, provider failure, cancellation and timeout. No public API method prints, exits the process, mutates logging configuration, or returns an undocumented SDK object.

Async calls must not block the event loop on parser, embedded database or provider work. Use native async clients where suitable and bounded executors/owner queues for synchronous libraries. Sync entry points do not call `asyncio.run` inside an existing event loop. Timeouts and cancellation propagate to cancellable providers and job checkpoints; completed source/index transactions remain valid when cancellation arrives. Streaming events use the same event schema as HTTP.

RequestScope is immutable per call and intersects the client's granted scope; framework-supplied metadata or filters cannot elevate it. Limits count returned evidence segments, with context expansion accounted for inside each segment's budget. Final ResponseAssembler fitting measures every requested delivered representation, including record/diagnostic overhead; a pure later formatter fails explicitly if its new rendering exceeds the declared cap. Adapters can therefore map framework document limits predictably.

### Framework adapter contract

A future Agno knowledge integration must be implementable as a small external adapter over the public Python API. It translates a framework query/filter/limit into QueryRequest, converts evidence segments into framework document objects, and retains evidence/source/revision IDs, exact locators, usage role, derived-status flags, asset references and retrieval run ID in metadata. It must not rechunk or reembed Tychora results, rerun a second retriever, parse CLI output, or reach into SQLite/Chroma/graph internals.

Discovery can supply agent instructions and tool descriptions; exact evidence and relationship inspection remain accessible through additional tools. Native image evidence remains an asset reference when a framework document cannot carry image bytes. Unsupported mandatory filters produce a structured failure rather than disappearing during conversion. Framework write methods delegate only when the client has source-write capability; deletion and load completion preserve Tychora's job/snapshot semantics.

Pin the chosen framework version when implementing an adapter instead of assuming one permanent `KnowledgeBase` superclass. Agno's current source exposes `KnowledgeProtocol`, with context/tool discovery and synchronous/asynchronous retrieval hooks. The eventual adapter must implement the interface for the supported Agno version; no Agno runtime dependency belongs in the core package. [Agno protocol source](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/knowledge/protocol.py).

Initial release acceptance includes public API documentation, a framework-neutral adapter example, sync/async parity, event-loop responsiveness, scope isolation, and tests using public exports exclusively. Shipping the Agno adapter itself is a separate integration task.

### MCP and HTTP

Use a small capability-based MCP surface: `discover`, `search`, `inspect`, `add_sources` and `manage_job`. Exact-source and graph inspection are typed inspect requests; ingestion tools are exposed only when the server principal has write capability. MCP resources expose schemas, kind definitions and evidence/assets through stable URIs. Tool descriptions include concrete input examples and source-use semantics. Implement with the official SDK, including real transport tests. [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools), [resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources).

HTTP has versioned routes for capabilities, collections, kinds, sources/revisions, evidence/assets, relations, query runs, jobs/events, evaluations and settings. The dashboard uses only these routes. Long tasks return job IDs; progress uses reconnectable server-sent events with durable sequence numbers. Idempotency keys bind canonical caller intent: identical retries reuse the captured job, while changed requests under the same key fail with a conflict. A new key captures changes at the same live path.

`serve` exposes HTTP and MCP Streamable HTTP by default. `serve --stdio` exposes MCP over stdio while retaining a private authenticated loopback control endpoint for sibling local clients; stdout remains protocol-only. Both modes share one workspace owner and publish its verified identity/endpoint in a protected runtime file. Stale runtime files are not sufficient evidence that an owner is alive.

The local server binds to loopback and uses a per-install local credential with scoped read/write capabilities and strict origin checks. Remote network exposure is outside the initial deployment profile. Asset endpoints resolve registered artifact IDs, never arbitrary filesystem paths. CLI delegation verifies workspace identity and service credentials.

Companion skills teach configuration, adding documents/code, discovering available kinds/filters, making queries and inspecting sources. They consume discovery schemas rather than maintaining a duplicated hard-coded taxonomy. Skills explain original versus derived content and preserve citations; they do not include private domain workflows.

## 10. History, diagnostics and dashboard dependencies

Persist job progress, adapter health, source/index coverage, query timing, selected IDs, policy/lexical-statistics epochs, filter/model/configuration fingerprints and evaluation reports from the first backend release. Query text, passages, result excerpts and provider request bodies are not retained by default. Default query-run retention is 30 days; explicitly enabled full local history has a separately visible retention setting. Job diagnostic retention is 30 days; source provenance and activation manifests persist for the lifetime of the corpus. Users can disable query history entirely and purge optional operational payloads without deleting sources. Minimal idempotency bindings, pinned acquisition/configuration and terminal job receipts persist so diagnostic expiration cannot cause duplicate writes.

A run with metadata-only history can be inspected diagnostically but cannot reproduce the missing original query. The dashboard labels that limitation. Replay requires the retained request, current authorization, snapshot membership and available/rebuildable projection dependencies, plus supported provider/configuration capabilities. A retained manifest alone is insufficient after graph GC. Missing dependencies are explained before offering a new run. Shared lexical statistics and remote provider revisions can change scores; historical exact-rank replay requires a frozen database/provider environment.

See the dashboard specification for screens, journeys, accessibility, state handling, events and acceptance criteria.

## 11. Verification and release gates

Use synthetic public fixtures and independently reviewed public-source test material. Version labels, identifiers, feedback states, document dates, source roles, code snapshots and graph provenance must remain meaningful during tests. Avoid evaluation leakage by splitting related sources/reviews and generated derivatives together rather than randomly by chunk.

Required tests cover:

- Cross-interface contract parity, pure help/discovery startup, structured errors and no unexpected network calls.
- Immutable revisions, stable IDs across relocation, withdrawals, access/temporal filters across every channel and intermediate graph node.
- Real graph/vector persist/reopen and crash recovery, retries, cancellation and activation races.
- Real Docling extraction on a small PDF/DOCX fixture set plus native Markdown; tables, figures and source locators checked against originals.
- Real code parser fixtures, ignored files, dirty revisions, deleted files and heuristic relation precision.
- Embedding response validation, cache identity, failed-reindex preservation and independent text/image spaces.
- Actual MCP/HTTP requests, scoped writes, path containment, malformed inputs, resource limits and prompt-like source content treated as data.
- Build/install from wheel and source distribution, freeze/qualify final standalone executables, dependency checks, license notices and public-artifact content review; see the distribution specification.

Evaluation separates deterministic integrity checks from semantic quality. Exact-ID and forbidden-source cases require zero failures. Report node/document recall, nDCG, citation/locator accuracy, temporal precision, context completeness, relevant-image recall and relationship precision. Compare lexical, hybrid, reranked and graph-expanded variants on the same dataset/split, snapshot, policy epoch and lexical-statistics epoch; report any differences in these controls. Golden assertions identify expected and forbidden source/revision/evidence IDs.

Capture p50/p95 latency, stage durations, peak memory, ingestion throughput, cache reuse and provider usage under declared hardware/corpus/concurrency conditions. No universal speed or semantic quality target is claimed without measurements. Release reports include regressions, missing capabilities and reproducible commands; synthetic hash embeddings never stand in for semantic-quality evidence.

One `tychora` wheel ships the API/CLI/resources with documented extras. Standalone executable targets bundle the supported runtime stack per platform; system Git and explicitly provisioned model weights remain external requirements. The distribution specification defines build tools, resource hooks, candidate platforms, artifact contents and clean-machine acceptance; no built binary is claimed yet.

## 12. Delivery sequence

The application is designed as a whole; implementation is divided into testable subprojects with explicit interfaces.

1. **Foundation and storage qualification:** package/tooling, contracts, configuration/kind registry, adapter acceptance probes, workspace ownership, snapshots and policy invariants.
2. **Ingestion and retrieval:** rich documents/visuals, code snapshots, resumable projections, hybrid retrieval, exact inspection and typed graph traversal.
3. **User and agent operation:** documented sync/async Python API, complete CLI, MCP/HTTP, skill guidance, jobs, source lifecycle and diagnostics.
4. **Release quality:** evaluation datasets/harness, real integrations, performance/security review, documentation and distributable package.
5. **Dashboard implementation:** implement the already specified screens against the established API/events; no second retrieval implementation.

Subagent implementation tasks follow these boundaries only after the written design is approved and a detailed implementation plan is prepared. Graph qualification precedes parallel implementation of graph-dependent work. LLM answer generation and more advanced query planning are future additions evaluated through the same contracts, rather than prerequisites for this release.
