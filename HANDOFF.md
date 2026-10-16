# Tychora handoff

Updated: 2026-10-15. Audience: the next maintainer or agent continuing the engine and dashboard work.

## Start here: implemented dashboard and engine follow-ups

The bundled local dashboard implements named Collections, Documents/Add documents, Search, Explore, Models, Jobs, Evaluations, exact-scope Activity and Settings through authenticated services. Read [dashboard usage and delivery](docs/dashboard.md). It preserves one workspace with several stable collection identities and memory-only browser credentials/content/drafts. Node is build-only. Existing custom/restricted grants never broaden; whole-workspace maintenance requires complete authority.

Add documents now streams chosen PDFs as raw File bodies through private prepare/upload/analyse receipts, explicit confirmation of server-issued analysis/policy hashes and the existing durable source-job scheduler. Ordinary JSON remains 1 MiB; binary PDF intake is at most 100 MiB with lower configured limits. Model/setup navigation retains File/title/text/destination/kind and renews stale receipts under current authority, while cleanup keeps the exact original receipt owner. Queued/accepted and retained page-processing progress are not indexed success. OCR remains derived, noncitable and review-required with physical page locators and retained input/derivation identities; selected coverage and registered search modes expose gaps and exclusion honestly. Original inspection remains separately bounded at 16 MiB payload/24 MiB encoded response; large retained PDFs can exceed browser inspection without being removed.

Qualified browser verification uses generated synthetic content, actual disposable HTTP, installed pinned local OCR/tokenizers and explicit opt-in. Synthetic transient/partial failures are identified as test seams; simulated remote-provider journeys do not establish connectivity or quality. Thin CI does not provision model assets and explicitly skips those qualified scenarios. Current PDF intake evidence is recorded in its verification report; no claim follows about private full-document OCR accuracy/capacity or hosted CI. The controller owns private corpus, backup, runtime and preview work.

Remaining engine boundaries include bounded relations/inventory, metadata-only history without replay, unavailable trace/history-cache purge enforcement, no PDF overlays, no global workspace discovery, evaluation label editing/aggregate analytics and automatic settings rebuild planning. Earlier upstream qualification/advisory blockers below remain open; collection lifecycle and local named collection administration are implemented.

## Product decisions to preserve

- Tychora is a reusable local evidence engine with a programmatic sync/async Python API, CLI, HTTP and MCP. It returns structured, formatted evidence; the calling agent writes the answer or review.
- Workflows belong in companion skills and the dashboard. Keep the Python interface suitable for future framework adapters such as an Agno KnowledgeBase integration; no completed Agno adapter is claimed.
- Domain behavior belongs in configurable profiles, kinds, schemas, facets and relations. Use generic public examples such as standards, feedback, government templates, regulations and code. Do not introduce private customer context, corpora or conversation exports into the public project.
- SQLite holds canonical structured evidence; Chroma and Ladybug provide derived vector and graph projections. Keep replaceable interfaces, but do not claim every storage operation is already abstracted.
- Original bytes, exact locators, revision identity and provenance remain distinct from generated descriptions, embeddings and search windows. Changing a parser or model must not silently reinterpret old data.
- An enabled remote provider is not upload authorization. Owner/collection consent and the caller's narrower scope remain enforced. No live provider upload or credential publication is authorized merely by this handoff.

## Current repository and implementation state

The engine branch was fast-forwarded into `main` at `e7de14081073c4a9020d5e595cb0f360c33aa52e`. Documentation, branding and the static site followed in `8c2f8cba26ce53238401a9abc8606d491b0e70cf`. The initial handoff was committed in `620df92`; the hosted build fixes follow that baseline. The durable engine worktree was retained; do not delete worktrees or local verification records as routine cleanup.

The package is an **alpha**, not a fully qualified production release. Implemented areas include versioned capture and extraction, typed evidence and locators, SQLite/FTS5, optional Chroma/Ladybug, document and code adapters, provider boundaries, hybrid retrieval, deterministic formatting, API/CLI/HTTP/MCP, evaluation machinery, and lifecycle operations. See the architecture for concrete code boundaries rather than inferring completion from this list.

The bundled dashboard is implemented and served by the local HTTP owner at /dashboard/. The Material for MkDocs site is documentation, not that dashboard. The public site excludes the historical planning archive; those specifications remain available in Git.

Distribution supports a Python wheel and source archive. Standalone executable build/qualification tooling also exists, with platform and model-asset limits documented in [distribution](docs/distribution.md). Model weights and tokenizer assets are external. Do not equate a wheel, a local frozen build, and qualification on all supported operating systems.

### Hosted build repair and publication follow-up

The public repository is [tychota/tychora](https://github.com/tychota/tychora); the documentation is deployed at [tychota.github.io/tychora](https://tychota.github.io/tychora/). The first Pages deployment passed, and the homepage, architecture page, search index and hero image were checked over HTTP.

The hosted build repair addresses these portability and qualification issues:

- Some Python SQLite builds omit `enable_load_extension`. Import verification now disables extension loading when the control exists; a regression test also verifies that SQL extension loading is denied when the method is unavailable. Schema and integrity checks remain enforced.
- Linux wheel metadata lists compressed filename platform tags separately. Qualification checks the actual required tag set.
- Docling can merge adjacent PDF paragraphs on Linux. Qualification checks exact passage retention and physical-page provenance without requiring identical paragraph grouping across operating systems. Table, figure and retained-artifact checks remain in place.
- The split-table provenance scenario now requests enough output budget for both the table and row. Its synthetic counter counts characters as tokens; the original 6,000 cap correctly omitted one owner from a 6,170-character response. Both IDs are now required, while separate tight-budget omission tests remain intact.

Six artifact-dependent tests now carry the release marker so the Linux generic-check filter excludes macOS `sandbox-exec` builds while the dedicated distribution workflow still runs them. The CLI delegation test waits for a published HTTP authority rather than the earlier ownership record. The 23-page vision fixture uses an explicit 180-second job wait instead of the SDK's 30-second default; its provider remains mocked and all source/context assertions remain enforced.

The lock upgrades OAuthlib to 4.0.0, Docling/Docling Slim to 2.132.0, Docling Core to 2.101.1, Docling Parse to 7.22.3 and multidict to 6.9.1. The Docling model-asset owner labels were updated; asset bytes, hashes and revisions are unchanged.

Verification during this repair: the full local source run completed with 1,926 passes, one skip and one failure in the split-table budget scenario. After correcting that test, the 15-test table/graph/output-budget follow-up passed. Eight synthetic CI asset tests passed: provisioning now retries an explicitly retryable provider download failure up to three attempts, while integrity failures stop immediately and size/hash verification remains mandatory. A final focused follow-up with real local parser assets and mocked providers passed 23 tests (six release-only tests deselected), covering CLI readiness, the 23-page vision scenario and portable release tooling. Ruff, formatting and type checks passed. The [installed distribution run](https://github.com/tychota/tychora/actions/runs/38475452978) and [macOS executable job](https://github.com/tychota/tychora/actions/runs/38475453144/job/115564894868) passed. These observations do not imply all hosted jobs are green.

**Linux native qualification still blocked:** with Docling 2.132.0, the synthetic PDF's exact figure-caption text and page geometry are retained, but Linux labels it as body text and emits no picture-caption reference. macOS emits that reference. A controlled comparison of closer spacing, italic and bold captions reproduced the discrepancy; those temporary experiments were removed. The strict assertion and production requirement to bind asset captions to retained Docling references remain unchanged. The latest experiment produced 43 native passes and one caption-association failure. This belongs in the planned Docling improvement work: distinguish explicit parser relations from inferred candidates and evaluate platform behavior before changing the provenance contract.

**Security gate:** the fresh installed-stack advisory audit reports five rows covering four unique Chroma 1.5.9 advisories, with no fixed release published. Each is listed in the time-limited waiver register [`quality/advisories.toml`](quality/advisories.toml) (product owner decision, 30 days, reviewed monthly); gate G19 fails on an unlisted or expired advisory and on an entry no longer reported. See [distribution](docs/distribution.md) for identifiers. This is distinct from package and executable correctness; do not describe the full CI suite as green.

### Recorded verification from the preceding documentation update

These are dated observations, not results automatically inherited by later changes:

- Engine source suite at `e7de140`: **1,922 passed, 5 skipped, 4 deselected**, with release tests excluded and live-provider tests deselected.
- Four skips were local OCR cases missing their asset environment variable. A follow-up covering documentation examples, release artifact auditing and all four OCR cases completed with **29 passed**. The remaining research-tokenizer skip was not closed.
- A strict MkDocs build passed; desktop/mobile layouts and Mermaid diagrams were inspected.
- Wheel/source-archive audits passed, including a build with the original checkout, Git directory and a synthetic private directory denied to the build subprocess.
- The outgoing-history audit from `c914cb2` through `8c2f8cb` passed across 506 paths. Publication of this handoff requires a fresh outgoing check.
- Logs and preview images were saved under the ignored local `.superpowers/docs-refresh-2026-10-13/` directory. They are not a public release artifact.

On 2026-10-15, 2,385 tests were collected; the default selection is now green on a clean machine (code-health baseline).

The full source run does not establish live embedding/reranking quality, gold-set adequacy, a Linux or minimum-macOS qualification result, or closure of the behavior defects below. Historical dependency advisories remain disclosed in the distribution guide; rerun the current advisory audit rather than treating that older result as a waiver. Hosted CI outcomes must be checked after pushing.

### Local state (2026-10-15)

Local worktrees live under `.worktrees/` of the main checkout, one per branch listed by `git worktree list`: `engine` (`feat/evidence-engine`, locked, retained), `dashboard` (`feat/dashboard`), `ci-fixes` (`fix/hosted-builds`), `docling-formula` (`fix/docling-formula-spans`), `launch-link` (`feat/dashboard-launch-link`), `neighbour-fix` (`fix/docling-neighbour-context`), `program-specs` (`docs/program-specs`) and `code-health` (`feat/code-health`). The ignored `.superpowers/` directory of the code-health worktree held 1.6 MB of local records. `git worktree prune --dry-run -v` reports one prunable entry: a former `feat/evidence-engine` worktree under the system temporary directory whose directory no longer exists. It has not been pruned; ask the product owner first, because worktrees are not deleted routinely.

## Review findings and needed fixes

The earlier review was checked against `e7de140`. Preserve the distinctions below when turning it into issues. The [full assessment](docs/review-assessment.md) has exact source references and the remaining review inventory.

| Priority area            | Current finding                                                                                                                | Next useful acceptance test                                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Strict lexical retrieval | More than the 80-per-kind cap marks the channel incomplete; strict queries can report `INDEX_INCOMPLETE` for normal truncation | More than 80 eligible matches remains a valid bounded search; genuinely unavailable/incomplete indexes still fail appropriately                    |
| Historical editions      | A unique selected edition can still produce an overlap warning and a human-decision requirement                                | Unique dated winner produces no spurious conflict; tied/unknown/overlapping cases retain explicit behavior                                         |
| Retention and purge      | Advertised history/cache purge commands return `CAPABILITY_MISSING`; expiry is recorded but not enforced                       | Working scoped purge, expiry enforcement, retained-payload/replay behavior and job-diagnostic cleanup                                              |
| Vector identities        | Family creation identity is configuration-derived; record IDs omit family identity                                             | Independent families with identical configuration remain isolated across rebuild, restore and deletion; plan migration before changing durable IDs |
| Workspace ownership      | A second client encountering a standalone Python owner can receive `INTEGRITY_ERROR`                                           | Two-process case returns a retryable ownership error without treating an ordinary live owner as corrupt                                            |
| Query traces             | The trace request exists, but the result trace stays null                                                                      | Scoped trace with bounded timings/counts/reasons and no hidden-source leakage, exposed consistently across transports                              |

Synthetic probes reproduced lexical cap classification, the spurious edition warning, and missing purge dispatch. Their end-to-end consequences and the other findings were assessed through code inspection; do not describe all six as full end-to-end reproductions.

Dashboard-relevant gaps also include global workspace discovery, missing CLI relation-assertion inspection by ID, code context excerpts without their own locator/reason, and full request-history retention not exposed through normal configuration. Metadata-only history cannot provide exact replay. Do not display unavailable trace, retention or replay features as operational.

Some findings were already overtaken: missing-revision original access was corrected in `e7de140`; parse reuse, read scope, vector candidate/window separation and reranker asset setup were finalized in earlier commits. Historical failing-suite counts and claims about workflows never running are not current facts.

Further review items need targeted reproduction: pinned-generation GC, publication/receipt atomicity, startup integrity coverage, restore receipts, archive limits, mixed-scope import behavior, configuration/CLI error contracts, and code resolution provenance. The existing assessment records these without promoting them to newly verified defects. Passing tests do not close a test gap or a test that asserts the wrong contract.

## Answers to the architecture questions

### SHA-256 versus BLAKE3

SHA-256 provides standard-library availability, checksum interoperability and a stable durable identity scheme without another native dependency. BLAKE3 may improve hashing throughput, but no current benchmark shows hashing is the bottleneck. It is not a claim that SHA-256 is faster. Changing public identities requires versioning and a migration/dual-read strategy across artifacts, references, backups and indexes. An internal cache could use a different hash without replacing retained public IDs. See [design choices](docs/design-choices.md).

### Multiple independent RAGs and their locations

One workspace directory owns one corpus: `corpus.sqlite3`, `artifacts/`, and the configured `indexes/text/`, `indexes/image/`, and `indexes/graph/` projections. Pass its path to Python, CLI or a one-workspace server. The CLI defaults to the current directory; there is no implicit home-directory corpus, global workspace catalog or federated query. Choose directories in a project or under a user-managed home directory. `TYCHORA_MODEL_CACHE` can share model assets, not corpus data. Collections partition a workspace logically but do not create separate database directories, and named collection HTTP lifecycle and scoped discovery are available; the redesigned collection/document UI remains subsequent work. See [workspaces](docs/workspaces.md).

### Document kinds and configurable relations

Versioned data-only packs define kinds, bounded schemas, roles, lanes, facets, relationship predicates and policy. They can reuse existing normalizers; new extraction semantics need Python code and fingerprints. Defining a predicate constrains valid assertions but does not discover or create links automatically. Preserve historical schema identities or provide explicit mappings. Current built-in kinds and exact mechanisms are in [architecture](docs/architecture.md) and [extensions](docs/extensions.md).

### Better Docling ingestion

The current adapter retains structured extraction, geometry and visual assets, but the standard pipeline disables built-in Docling OCR. Separate selected-region OCR and optional vision exist. Priorities are explicit OCR policy, heading hierarchy, table cell matching/accuracy, layout and rendering options, resource limits, and selective formula/code/picture enrichment. Full-page VLM extraction is a separate path to evaluate, not the existing crop-vision adapter. Pin settings/assets into extraction identity and evaluate exact locators as well as text quality. Audio/video needs media handling, timestamp/frame provenance and transcription policies. All six requested upstream guides are mapped in the [ingestion roadmap](docs/ingestion-roadmap.md).

### Code language and resolution coverage

Dedicated adapters currently cover Python, JavaScript/JSX and TypeScript/TSX. Other suffixes are not equivalent to supported semantic code extraction. New language support includes grammar/dialect handling, semantic units, declarations, references, exact byte/line ranges, resolution, diagnostics, cache identity, tests and packaging. Syntax-only support and compiler-aware resolution must be advertised separately.

Current resolution is static and bounded, not a compiler or runtime call graph. Non-explicit imports, package rules, overloads, inheritance, extensions, macros, generated code and build context need language-specific work. LSP/compiler/semantic-index ingestion is a candidate extension, not an existing capability. Preserve revision alignment, ambiguity and provenance; never label a plausible target as verified solely because its name matches. See [extensions](docs/extensions.md).

### The 512-token target and 800-token soft ceiling

These are initial constants in `src/tychora/indexing/windows.py`, not an empirically established optimum or normal TOML controls. The indexer partitions prepared evidence into exact adjacent windows with no overlap, preferring natural boundaries. A remaining 700-token interval can stay intact. Provider request limits are separate.

The owner wants **semantic chunks and, ideally, contextual indexing**. Do not reduce this request to changing two numbers. First inspect how normalized semantic units become windows, contextual groups, provider inputs and returned segments. Contextual provider support already exists; distinguish the existence of a model path from its activation, grouping quality and evaluation. Preserve authoritative article/rule/table/symbol boundaries, exact spans and immutable identities. Specify oversized-unit handling and retrieval-time context reconstruction. Compare fixed-window, structural/semantic and contextual variants on the same labeled corpus and budget before selecting defaults.

### Optional reranking and the new Voyage model

Reranking is optional because the base profile is offline/provider-free; a remote reranker adds upload authorization, latency and cost. An explicitly online quality profile should evaluate and normally enable it. As checked on 2026-10-13, Voyage announced `rerank-3` and `rerank-3-lite` on [2026-09-30](https://blog.voyageai.com/2026/09/30/rerank-3/).

The current configuration accepts `rerank-3`, but runtime tokenizer/capability qualification only supports `rerank-2.5`; `rerank-3-lite` is not an exposed choice. Complete tokenizer/asset identity, limits, response mapping, cache identity and actual evaluation before claiming support. Embedding support includes contextual text, code and multimodal paths; it is not proof of live reranking correctness. See [design choices](docs/design-choices.md).

### Qdrant, Neo4j and PostgreSQL effort

Vector and graph ports provide a starting boundary for Qdrant and Neo4j. Parity also needs eligible-ID filtering, bounded retrieval/traversal, generation/family isolation, receipts, publication, recovery, deletion and contract tests. PostgreSQL is larger: SQLite-specific SQL, migration and FTS assumptions appear across services, and the small `CorpusStore` protocol does not abstract all persistence.

Earlier engineering estimates were 4–8 weeks for Qdrant parity, 6–12 for Neo4j, and 3–6 months for PostgreSQL canonical-store parity. At an illustrative €800/day, that is €16–32k, €24–48k and €48–96k respectively; narrow prototypes are smaller. These are uncertain human-effort estimates, not quotes, model-usage costs or commitments. Shared refactoring changes the combined estimate. See [extension boundaries](docs/extensions.md) before planning each implementation.

## Owner's remaining engine work after dashboard delivery

The following work is explicitly desired but not implemented by this handoff. The order after dashboard delivery is not fixed:

1. Polish the product and documentation.
2. Understand and redesign chunking around semantic units, with contextual indexing and measured retrieval behavior.
3. Improve Docling table/layout handling and the related extraction options described above.
4. Exercise the complete reranking path, including the new model qualification gap and live quality evaluation on deliberately selected authorized data.
5. Add **Java, Kotlin, Ruby, Elixir, Rust, Swift, C, C++, C#, Go**, and further code languages. Include resolution rules, source ranges, references and possibly LSP/compiler data; grammar installation alone is insufficient.
6. Implement **Neo4j, Qdrant and PostgreSQL** adapters, including the necessary persistence refactoring for PostgreSQL.

Use distinct acceptance criteria for syntax extraction, semantic resolution, retrieval quality and backend durability. Preserve the programmatic API, citations and policy behavior while introducing new implementations.

## Documentation and publication

The README links the tutorial, architecture and current limitations. The repository includes a logo and hero image. The searchable site uses Material for MkDocs with a separate locked docs environment:

```bash
uv run --project packaging/docs --locked mkdocs build --strict
uv run --project packaging/docs --locked mkdocs serve
```

See [documentation maintenance and publication](docs/documentation.md). The docs workflow builds on pushes/PRs. GitHub Pages publication is a separate manual workflow action, with Pages configured to use GitHub Actions. Pushing the repository does not publish a package or deliberately deploy Pages. Runtime, native qualification and executable workflows also run on relevant pushes; inspect their actual results and do not weaken failing gates merely to obtain a green badge.

Keep examples runnable, prerequisites explicit, current limits next to affected features, and tutorials/how-to/reference/explanation distinct. The local documentation previews are not a dashboard prototype. Any future handoff should update this file's baseline, remaining defects and verification evidence together.

## Named collection backend (2026-10-14)

CollectionService and HTTP collection/document endpoints now provide stable UUIDs, managed-owner-only creation, idempotent archive/reactivate, strict v1/v2 policy projection and lifecycle-preserving consent. Static grants never expand. Managed future-call grants refresh in the attached workspace, job facade and explicit LocalAuth slot; accepted job scopes stay immutable. Archive gates new source/PDF/provider/publication work and ordinary retrieval while retaining authorized exact inspection and history.

Inventory is bounded and conservative: selected readable publication metadata and up to 400 canonical channel observations, with null unit/window counts and unknown latest-job state because current exact ownership/job lineage cannot be established inside the summary budget. No corpus artifacts are decoded for summaries. Actual synthetic HTTP fixtures are in `dashboard/contracts/collections-*.json`. Session refresh supports these backend operations; this delivery does not claim redesigned pages, guided settings, OCR or explicit PDF-policy execution.
