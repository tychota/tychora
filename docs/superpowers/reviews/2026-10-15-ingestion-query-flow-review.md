# Tychora: ingestion and query flow review

Date: 2026-10-15
Reviewed: `main` at `700df65`. No source file under `src/` has changed since `e7de140` except `adapters/sqlite/migrations.py` and the model-asset manifest, so every defect listed in `docs/review-assessment.md` is still live.
Against: the engine specification, the dashboard specification, `HANDOFF.md`, the 2026-10-08 specification review and the current assessment.

Method: both specifications and the prior reviews were read in full. The ingestion path was then read end to end (`application/sources.py`, `ingestion/*`, `indexing/*`, `adapters/sqlite/corpus.py`, `adapters/sqlite/publication.py`, `adapters/files/cas.py`, `application/owner.py`, `application/runtime.py`) and the query path likewise (`application/query.py`, `retrieval/*`, `adapters/sqlite/eligibility.py`, `adapters/sqlite/lexical.py`, `adapters/chroma/index.py`, `adapters/ladybug/traversal.py`, `application/reranking.py`, `formatting/*`). Claims about cost were then measured with a synthetic lexical-only workspace through the public Python API, and a focused subset of the test suite was run. No project file other than this review was modified.

This review deliberately does not repeat the requirement-by-requirement audit of 2026-10-08. It asks two questions about the two flows: is the design doing the right thing for the product, and is the implementation correct and maintainable.

## 1. Verdict

The engine does what the specification promised at the level of semantics. Identity, immutability, exact locators, atomic publication, eligibility before candidate selection, upload authorization with epoch rechecks, rank fusion, reserved admission and budget fitting are all present and are implemented with unusual discipline. The security posture of ingestion is strong. The focused test subset passes apart from asset-dependent tests.

It is not doing the right thing at the level of cost, and that is a design problem rather than a tuning problem. Both flows do work proportional to the whole corpus on every operation:

- Every search re-reads and re-parses the snapshot manifest three times, rebuilds the eligibility facts for every member of the corpus from SQLite with per-unit queries and per-document quadratic validation, and re-fetches every eligible vector record's presence from Chroma. The eligibility cache the specification requires exists as a key function with no callers.
- Every add re-verifies every batch of the whole corpus, including re-hashing every retained embedding file from disk, rebuilds a full graph generation, and writes a snapshot manifest that embeds the full lexical text of every window of every member. Snapshot manifests therefore grow with the corpus and accumulate quadratically.
- All canonical work, including tokenization, image decoding and the quadratic validation above, runs on one owner thread, so ingestion and queries serialize behind each other.

Measured on small synthetic documents (12 paragraphs, about 4.6 KB each) with no vector or graph channel enabled: search latency rises from 136 ms at 5 documents to 346 ms at 100 and 1.5 s at 200; median add latency rises from 700 ms to 992 ms; after 200 adds the artifact directory holds 159 MiB for 925 KB of source text. A 700-byte add into a 10-document workspace hashes 9.4 MB of retained files and writes 1.2 MB. At real document sizes the same code paths scale with total units and total windows.

Separately, the default profile fails ordinary queries on small corpora: with 20 synthetic documents the two-word query "verification outcome" returns `INDEX_INCOMPLETE` because more than 80 owners match and the profile is strict. This is the known lexical-cap defect, now reproduced through the public API at a corpus size where it cannot be dismissed.

Maintainability is the second concern. The code is consistent and heavily tested, but it is organized around defensive re-validation at every boundary rather than around validation at write time. That is why read paths carry the cost above, why functions of 200 to 450 lines are common, why 189 raw SQL statements live in the application layer, and why the "ports" are nominal for the corpus store. The layering described in the specification is not enforced: 14 adapter modules import application, retrieval, indexing or ingestion code.

## 2. What to keep

These parts are right and should survive any redesign:

- The identity scheme (`contracts/identity.py`) and the separation of source, revision, extraction, unit, chunk and snapshot. It matches the specification exactly and is the reason old citations stay valid.
- Acquisition (`ingestion/acquire.py`): root containment with directory file descriptors and `O_NOFOLLOW`, moving-source detection, size ceilings, partitioned capture manifests. This is careful and correct.
- The content-addressed store (`adapters/files/cas.py`): staged write, hash verification on put and open, per-hash guards, payload pins.
- Parser isolation: Docling and Tree-sitter run in spawned processes on private copies (`application/owner.py:304-398`, `application/runtime.py:208-234`).
- Lossless windowing (`indexing/windows.py`): exact adjacent codepoint ranges, no overlap, boundaries preferred, token-counted.
- Publication as one `BEGIN IMMEDIATE` transaction with base-snapshot comparison and receipt verification before the switch (`adapters/sqlite/publication.py:456-585`).
- The retrieval contract: typed owners, window collapse before fusion, dense text-family merge, exact and graph reservations, lane reservations, the finite omission order in `retrieval/response.py:359-485`.
- Upload authorization that re-reads policy at a fresh epoch before each provider dispatch (`application/owner.py:834-1415`).
- The bounded FTS expression builder (`retrieval/lexical_query.py:108-131`): user text never becomes MATCH syntax.

## 3. Ingestion flow

### 3.1 What actually happens on `sources.add`

1. `SourceService._accept` narrows scope, serializes the entire resolved configuration (registry, kinds, schemas) to canonical JSON and writes it to the CAS (`application/sources.py:157-172`), then creates the job row under the idempotency key.
2. `execute_accepted` (`application/sources.py:455-685`, 230 lines) serializes the configuration to the CAS a second time, canonicalizes metadata, captures bytes, registers the revision (`_registration`, 270 lines), reads the active snapshot and decision watermark, looks for a completed plan checkpoint, tries to reuse an existing published revision, tries to replay a retained extraction, and otherwise extracts.
3. Extraction: native Markdown/text/JSON in-process (`ingestion/native.py`), Docling in a child process, code via a syntax cache plus child-process parsing.
4. `_bind_source_metadata` projects registered facets; then up to three attempts of: `ProjectionCoordinator.prepare` (windows, canonical commit, membership from the base snapshot, family resolution, context groups, image decoding), provider embedding, `finalize` (lexical batches, vector batches with one CAS file per vector, full graph generation), checkpoint, `publish_snapshot`.
5. `publish_snapshot` re-reads the snapshot manifest and every batch input manifest, re-verifies every vector batch of every member against its retained bytes and against Chroma, re-seals the graph generation, then inserts bindings and switches the active snapshot.

### 3.2 Design findings

**D1. The snapshot manifest embeds the corpus.** `PublicationBatch.lexical_records` carries `body`, `title`, `headings`, `context` and `identifiers` for every window (`contracts/publication.py:41-47,79-89`). `publication_manifest` dumps the whole plan, slots and batches included (`adapters/sqlite/publication.py:55-86`). When a new snapshot is planned, earlier batches are reused as whole objects from the resolved previous manifest (`indexing/coordinator.py:1034-1052`), so each new manifest contains every earlier member's lexical text again. Consequences: manifest size is proportional to corpus text; total manifest storage after N adds is quadratic; `resolve_snapshot` has to parse corpus-sized JSON (section 4). In the probe the manifest became the largest artifact at 100 documents (752 KiB) and reached 1.5 MiB at 200; by then the artifact directory held 1,003 files and 159 MiB for 925 KB of source text, 163 of those files being snapshot manifests over 300 KiB. The specification's binding is `snapshot_batches` plus receipts; lexical bodies already live in `lexical_records`. The manifest should carry identifiers, counts and digests only.

**D2. Publication re-verifies the whole corpus on every add.** `_verify_batches` iterates every slot and every batch, including batches that were published many snapshots ago. For every vector batch it opens every embedding cache file, hashes it and unpacks it (`adapters/sqlite/publication.py:232-240`), then calls `index.verify_batch` which fetches every record with its embedding from Chroma (`adapters/chroma/index.py:266-304`). For the graph slot it calls `seal_generation` again. The specification says activation verifies each newly written batch and that large audits are health jobs. As implemented an add is O(total vectors) in disk reads, hashing and Chroma traffic. Measured at 10 lexical-only documents: 75 verified opens hashing 9.4 MB for one 700-byte add; with text embeddings enabled the same loop would additionally read every vector file in the workspace.

**D3. Full graph generation per add reads every unit of every member.** `_graph_slot` selects all unit identifiers for every member (`indexing/coordinator.py:822-828`), `read_graph_build` loads every unit and every assertion artifact, and Ladybug receives a complete new generation. The specification accepts a full generation for v1 but asks for measurement before optimizing. Combined with D1 and D2 it makes every add O(corpus) three times over. The graph channel is disabled in the default profile, so the probe did not pay this cost.

**D4. One owner thread for all canonical work.** `BoundedWorker()` defaults to one worker and a queue of 32 (`application/runtime.py:26`); `WorkspaceOwner._worker` uses it (`application/owner.py:113`). `run_blocking` sends there not only SQLite access but tokenization of whole documents (`coordinator.prepare` via `_windows`), PIL decoding of every asset (`_visual_inputs`, `indexing/coordinator.py:616-675`), the quadratic ownership validation of `prepare_extraction`, and every query's eligibility compilation. The specification asks for a single writer and single native-handle owner. It does not ask for a single reader. SQLite in WAL mode supports concurrent readers; queries could use a read connection pool and leave the owner thread for writes and native handles. As built, one large PDF being windowed stalls every concurrent search, and 33 concurrent callers trip `WORKSPACE_BUSY`.

**D5. Boundary keys defeat the chunking and context design.** Native, Docling and record normalizers set `boundary_key` to the unit's own native reference (`ingestion/normalize.py:100`, `ingestion/documents.py:98`, `ingestion/document_records.py:366`). Only code uses the file as boundary (`adapters/code/normalize.py:283`). Everything downstream keys on `boundary_key`: context groups (`indexing/windows.py:99`), joint contextual embedding groups (`indexing/groups.py:179-183`), neighbour context (`retrieval/segments.py:397-404`), and merging adjacent matched units (`retrieval/segments.py:667-671`). For documents each paragraph is therefore its own group and its own boundary: contextual embedding sends one capsule plus one window per group, neighbours are never attached and adjacent matches are never merged. The specification describes the boundary as a section, article, table or file. The handoff's stated next goal, semantic chunks with contextual indexing, cannot be reached by changing window sizes while this is the case. This is the most consequential single-line design choice in ingestion.

**D6. One CAS file per vector with two fsyncs each.** `_text_batch` writes each embedding as a separate artifact (`indexing/coordinator.py:494-499`); `_put_stream` fsyncs the file and the directory (`adapters/files/cas.py:192-211`). A 1,000-window document creates 1,000 files and 2,000 fsyncs, and every later publication re-opens and re-hashes all of them (D2). On macOS without `F_FULLFSYNC` the fsync cost was negligible in the probe; on Linux with real durability it will not be. A per-batch cache artifact would keep the spec's cache semantics at a fraction of the cost.

**D7. The resolved configuration is serialized and hashed on hot paths.** The 561 KiB registry artifact is dumped and written twice per add (`application/sources.py:447,468`), dumped and hashed once per query in `_compile` (`retrieval/eligibility.py:387-395`) and again in `_validate_provider_families` (`application/query.py:331-339`), re-read and JSON-parsed from disk on every eligibility read only to check its type (`adapters/sqlite/corpus.py:108-136`), and dumped once per selected segment for lane and role lookup (`retrieval/segments.py:56-79`). `ResolvedConfig.config_sha` already exists and is used elsewhere; the registry already exposes lanes and kinds as mappings.

**D8. Rebase after a configuration change has no path forward.** When the lexical family changes because the retained configuration is not runtime-compatible (`configuration/indexed_representation.py:23-57` treats any change outside credentials, offline tightening and root narrowing as incompatible), `finalize` raises `INDEX_INCOMPLETE` for any add because earlier members have no batch in the new family (`indexing/coordinator.py:1039-1047`, repeated for text and image). No maintenance operation re-batches earlier members into the new family; `application/recovery.py` and `application/maintenance.py` do not mention lexical rebuild. A workspace whose profile is edited therefore cannot accept new sources until it is recreated.

### 3.3 Correctness findings

**C1. Windowing is quadratic in unit length.** `_split_interval` counts tokens of `text[cursor:boundary]` for every sentence boundary in the remaining interval (`indexing/windows.py:65-68`), so a unit with B boundaries and L characters tokenizes about B·L/2 characters per window. `numbered_provisions` and `rule_records` produce units spanning whole sections (`ingestion/normalize.py:422-432,546-558`), which is exactly where this bites. Counting once and mapping token offsets to boundaries is linear. `_validate_windows` and `_make_group` then re-count every window and every capsule (`indexing/groups.py:64-72,92`).

**C2. Ownership validation is quadratic per document and is re-run on read paths.** `_locator_intervals` compares each unit against every other unit's locators (`indexing/representations.py:661-731`) and `_owners_by_locator` is an explicit double loop (`adapters/sqlite/eligibility.py:56-70`). `prepare_extraction` is called at ingestion (correct), at every query for every member (`adapters/sqlite/eligibility.py:375`), at every query for every admitted candidate's extraction (`application/reranking.py:760`), for visual candidates (`retrieval/visuals.py:292`), on inspection (`application/inspection.py:375`), and through `project_code_excerpt_locator` once per code citation (`retrieval/segments.py:90` and `indexing/representations.py:619`). A code result with eight citations re-validates its whole file eight times.

**C3. `read_eligibility_facts` can raise a bare `ValueError`.** `prepare_extraction` raises `ValueError` on any retained inconsistency, and the call at `adapters/sqlite/eligibility.py:375` is not wrapped. A single malformed historical extraction would make every search fail with an unhandled Python exception rather than a typed `INTEGRITY_ERROR`, unlike the same call in `application/reranking.py:757-763`, which is wrapped.

**C4. Lexical stop-words do not exist.** `LexicalIndex` takes a stop-word set but `QueryService` constructs it without one (`application/query.py:238`) and nothing in the configuration carries one. Combined with OR semantics, 32 terms and the 80-owner cap, any query containing common words over-matches and, in the default strict profile, fails. The specification calls for a versioned profile stop-word list.

**C5. Known defects persist unchanged.** Lexical cap reported as incompleteness (`adapters/sqlite/lexical.py:115` and `application/query.py:707-708`), spurious edition decisions (`retrieval/eligibility.py:104-107` and `application/query.py:988-995`), dead purge commands, configuration-derived family identity and family-less vector record identifiers, the busy-owner `INTEGRITY_ERROR`, and the never-populated trace (`application/query.py:1327-1361` builds `ResponseContext` without `trace`). The lexical cap was reproduced here at 20 documents; see section 5.

### 3.4 Maintainability findings

- `execute_accepted` is a 230-line method that performs validation, capture, registration, replay, reuse, extraction, embedding, finalization, a three-attempt rebase loop and three error-recovery branches, almost entirely through `run_blocking(lambda: ...)` hops. It cannot be unit-tested in pieces and its control flow is invisible in a stack trace. `_registration` is 270 lines of inline SQL and integrity checks.
- The application layer issues 189 raw SQL statements (`grep -c "db.execute("`), the indexing layer 15, evaluation 7. The `CorpusStore` port (`ports/corpus.py`) declares five methods; the concrete class and every service bypass it. The handoff's estimate that PostgreSQL support is "3 to 6 months" is, on this evidence, generous.
- The same four-column `ArtifactRef(sha256=row[0], ...)` reconstruction appears in about a dozen places; the decision watermark `SELECT COALESCE(MAX(decision_id),0)` in four; the policy-epoch read in six. None has a helper on the store.
- `INTEGRITY_ERROR` is raised from 335 sites with unique prose messages and no sub-code. It covers genuine corruption, programming invariants, configuration drift and provider mismatch alike, so a client cannot distinguish "reinstall the model" from "the database is damaged".
- 180 imports are placed inside function bodies. Most exist to break cycles: `adapters/sqlite/eligibility.py` imports `retrieval.eligibility` and `indexing.representations`; `retrieval/rerank.py` and `retrieval/visuals.py` import `application.reranking`; `adapters/docling/parser.py` imports `application.owner`; `indexing/coordinator.py` imports `adapters.sqlite` directly; `application/owner.py` imports `evaluation.runner`. Fourteen adapter modules import from layers above them. The specification's dependency direction is documented but not enforced, and an import-linter rule would fail today.
- Twenty-five functions exceed 180 lines; the largest is a 453-line closure inside `adapters/code/normalize.py:258`, followed by `read_eligibility_facts` (391), `ResponseAssembler.assemble` (372), `QueryService._prepare` (355) and `EligibilityCompiler._compile` (330).
- Docstrings refer to plan task numbers ("Task 7 typed retention header", "Task19's canonical ownership", "Task 30's bounded retained-facts loader") and the prose style is declarative contract-speak ("Detach one same-epoch policy observation; release the read before dispatch"). Neither tells a new maintainer what the function does.
- Dead code the prior review found (`eligible_cache_key`, `vector_record_id`, `new_family_nonce`) is still present and still unused.

## 4. Query flow

### 4.1 What actually happens on `search`

1. `QueryService.search` validates the request, resolves the mode, then enters `run_pinned_eligibility` (`retrieval/eligibility.py:789-872`).
2. `pin_snapshot` calls `resolve_snapshot` twice (`adapters/sqlite/corpus.py:194,225`); each call opens the manifest with hash verification, parses it, re-canonicalizes it, validates `PublicationPlan`, re-dumps it for comparison and cross-checks three tables (`adapters/sqlite/publication.py:588-681`).
3. `compile_attempt` calls `read_eligibility_facts` (`adapters/sqlite/eligibility.py:215-603`): for every member of the snapshot it loads all units, locators, assets, links, proxies, records and identifiers, runs `prepare_extraction`, computes the quadratic overlap map, and issues per-unit queries for review state (two queries each) and facets. It then builds Python sets of eligible revisions, units, chunks, assets and vector records for the whole corpus (`retrieval/eligibility.py:368-696`).
4. `_validate_provider_families` resolves the snapshot a third time and dumps the configuration again.
5. Channels: exact identifiers; FTS after inserting every eligible chunk identifier and batch identifier into temp tables (`adapters/sqlite/lexical.py:53-78`); text ANN after `_vector_owners` issues one SQL query per eligible vector record (`application/query.py:1144-1172`) and Chroma `_present` fetches metadata for every eligible record (`adapters/chroma/index.py:336-350`); visual likewise; graph expansion in a thread.
6. Fusion, lane lookup with one SQL query per fused candidate (`application/query.py:1011-1037`), admission, rerank facts (full extraction reload plus `prepare_extraction` per admitted extraction, and `PRAGMA table_info(chunks)` once per matched window at `application/reranking.py:801-804`), reranking, segment assembly, visuals, records, relation checks, coverage.
7. `ResponseAssembler.assemble` renders and measures the full payload after every omission step; the result is recorded to `query_runs` on the owner thread.

### 4.2 Design findings

**Q1. Eligibility is a corpus scan per query and the cache was never wired.** The specification says eligibility "is not a persisted global permission table" and should be cached by snapshot, policy epoch, scope digest, policy digest and request digest. `eligible_cache_key` implements that key (`retrieval/eligibility.py:8-16`) and has no callers; `compile_attempt` always reads the facts afresh. Even with the cache, the design of materializing whole-corpus ID sets in Python and shipping them into FTS temp tables and Chroma `get` calls is O(corpus) per cache miss and O(corpus) memory per cached view. The spec's own text leans this way ("materializes separate context-eligible and matched-candidate unit sets"), so this is partly a specification problem. The sustainable shape is: revision-level eligibility as a small set (it is per source, not per unit), unit-level exclusions as a small set (withdrawn, rejected, blocked), SQL joins against `snapshot_batches` and `revisions` for candidate selection, and per-candidate dependency checks on the handful of candidates that survive, not on the corpus.

**Q2. The snapshot manifest is parsed three times per query.** Because of D1 it is corpus-sized. Instrumented on a 10-document workspace: 3 `resolve_snapshot` calls, 3 manifest parses, 4 verified CAS opens hashing 694 KB per search, of which 561 KiB is the registry artifact read only to check its header. The CAS hash verification on each open means every query pays SHA-256 over roughly the corpus text plus the configuration.

**Q3. Read-time validation of write-time invariants.** `prepare_extraction`, `attest_ranking_input`, `_span_dependencies`, the receipt and manifest equality checks in `resolve_snapshot`, and the per-window chunk re-derivation in `matches_from_channels` (`retrieval/rerank.py:394-411`) all re-prove at query time facts that `commit_extraction` and `publish_snapshot` already proved under `validate_bundle` and `_verify_batches`. The specification asks for revalidation of policy (withdrawal, access, epoch) before dispatch and delivery, which is cheap and correct. It does not ask for re-proving structural invariants. This is where most of the query-time CPU and most of the 4,300 retrieval lines go.

**Q4. The single owner thread serializes queries with ingestion** (D4 again, seen from the query side). Eligibility compilation, rerank-fact loading and history recording all run on it, so query throughput is bounded by that one thread even on a many-core machine.

**Q5. The result cache pickles entire documents.** `_PreparedResult` includes `RankedEvidence`, whose items hold `RerankFacts` with the complete `RetainedRepresentationRows` and `PreparedExtraction` of each candidate's document (`retrieval/rerank.py:58-65`); `QueryCaches.put_result` pickles that (`retrieval/cache.py:114-115`). A result over large documents is tens of megabytes per cache entry and is silently dropped above the 64 MiB ceiling, so the cache is least effective exactly when it would matter.

**Q6. Default profile is strict and the lexical channel over-matches** (C4, C5). The combination means that out of the box, on a corpus of twenty short documents, a two-word query fails. The degraded profile is a configuration edit away, but a product whose first run fails on "verification outcome" over twenty manuals is not doing the right thing by its users.

### 4.3 Correctness findings

- `_vector_owners` is called separately for the text and visual channels and issues one SELECT per eligible vector record each time; both calls run on the owner thread and both run before the ANN call, so vector queries pay two corpus-sized SQL loops before Chroma is touched (`application/query.py:1074,1210`).
- `_candidate_lanes` issues one SELECT per fused candidate with a three-table join (`application/query.py:1011-1037`); the kind is already in the eligibility facts the attempt holds.
- `linked_visual_context` appends unit candidates to the visual vote list with ranks restarting at one (`application/query.py:788`), so a source unit linked to the weakest image can outrank the image itself in the visual channel. Already noted in the assessment; unchanged.
- `inspect_relations` treats `expansion_inspected_rows == max_rows` as a truncated check without consulting the graph (`retrieval/response.py:525-526`), which is correct, but `coverage.status` becomes `partial` whenever graph expansion is merely budget-truncated (`application/query.py:981-985`). The specification distinguishes candidate truncation from channel incompleteness.
- `ResponseAssembler.assemble` re-renders every profile and re-tokenizes the entire payload after each of up to dozens of omission steps (`retrieval/response.py:345-357`). Correct, bounded, and avoidable with a single incremental measure.
- The graph traversal is correctly serialized under the store lock (`adapters/ladybug/traversal.py:516,636,797`); the earlier suspicion that `asyncio.to_thread` races Ladybug does not hold.

### 4.4 Maintainability findings

- The "allowed" set `view.revision_ids | view.context_unit_ids | view.structural_unit_ids | view.asset_ids` is recomputed in six places across `retrieval/rerank.py`, `retrieval/anchors.py` and `retrieval/visuals.py`. It belongs on `EligibilityView`.
- `_prepare` is 355 lines and interleaves channel execution, degraded-mode policy, fusion, graph expansion, lane lookup, admission, reranking, path validation, segment assembly, visual selection, record preparation, relation checks and coverage. Each of those is a stage with a typed input and output in the specification and would be testable as such.
- `retrieval/eligibility.py` is three modules in one file: the key function and `RevisionFact` at the top, the compiler in the middle with a second import block at line 114, and the attempt runner with a third import block at line 714.
- `segments._lane` and `_role` look up lanes by dumping the whole configuration to JSON per call; the registry object already in scope has `lane_bindings` and `kinds`.
- `QueryService` carries six optional providers, two counters, a reranker, an error placeholder, caches, history, profiles and an ablation plan as constructor arguments, and `build_services` has to reconcile which cache object each provider shares. A small `QueryBindings` value object with validation would remove the identity checks sprinkled through `_text_candidates`, `_visual_candidates` and `_validate_provider_families`.

## 5. Measurements

Synthetic workspace, lexical-only default profile, offline, synthetic character-count token counter, documents of 12 paragraphs of about 380 characters (about 4.6 KB each). Hardware: the reviewer's macOS development machine; absolute numbers are only comparable within the table.

| Documents | Median add | Search, unique term | Search, repeated | Query "verification outcome" | Largest artifact | SQLite |
| ---: | ---: | ---: | ---: | --- | ---: | ---: |
| 5 | 700 ms | 136 ms | 120 ms | ok | 561 KiB (registry) | 0.5 MiB |
| 20 | 765 ms | 188 ms | 155 ms | `INDEX_INCOMPLETE` | 561 KiB (registry) | 1.7 MiB |
| 50 | 809 ms | 222 ms | 207 ms | `INDEX_INCOMPLETE` | 561 KiB (registry) | 4.4 MiB |
| 100 | 851 ms | 346 ms | 338 ms | `INDEX_INCOMPLETE` | 752 KiB (snapshot manifest) | 10.8 MiB |
| 200 | 992 ms | 1,502 ms | 803 ms | `INDEX_INCOMPLETE` | 1.5 MiB (snapshot manifest) | 31.9 MiB |

After 200 adds the artifact directory held 1,003 files totalling 159 MiB for 925 KB of source text. The "repeated" column is the same question asked again inside the five-minute result cache, so the remaining time is the cost paid before the cache is consulted: snapshot resolution, eligibility compilation and family validation. At 200 documents that floor is 803 ms for under 1 MB of text, with no provider call anywhere. The growth between 100 and 200 documents is super-linear in every column.

Instrumented single operations on a 10-document workspace:

| Operation | Wall time | Verified CAS opens | Bytes hashed | CAS writes | Snapshot resolutions |
| --- | ---: | ---: | ---: | ---: | ---: |
| One add of a 700-byte document | 716 ms | 75 | 9.4 MB | 9 (1.2 MB) | several |
| One lexical search | 222 ms | 4 | 694 KB | 0 | 3 |

Twenty-two fsyncs occurred during the add and cost 1 ms in total on this machine, so fsync is not the explanation for the add latency; re-reading, re-hashing and re-parsing retained JSON is.

Focused test run (`tests/retrieval tests/indexing tests/jobs tests/storage tests/configuration tests/contracts tests/application tests/formatting`, main sources, the `ci-fixes` virtual environment): 901 passed, 18 failed, 6 skipped in 2 min 44 s. All 18 failures raise `KeyError: 'TYCHORA_TEST_MODEL_CACHE'`; none is a regression. The qualification, live and release suites were not run.

## 6. Where the specification itself should change

- Eligibility as materialized ID sets (section 4 of the specification, "Query-critical indexes and eligibility") should be replaced by revision-level sets plus SQL-side batch joins, with per-candidate dependency checks. The cache the specification requires is necessary but not sufficient.
- The snapshot manifest should be specified as a binding document (identifiers, counts, receipts, digests), with lexical and vector content living only in their tables and artifacts.
- "Activation verifies each newly written batch" should be made explicit as *only* newly written batches, with whole-corpus verification reserved for `manage health`.
- Boundary keys for documents should be defined as the enclosing section or table, matching the text the specification already uses, and the normalizers should be required to emit them.
- A stop-word list should be a required part of the lexical profile, and the strict-mode rule should distinguish candidate truncation from index unavailability (this is already on the assessment list).
- The owner model should distinguish the single writer and native-handle owner from read connections, and should state which CPU-bound stages may run on the owner thread.

## 7. Recommended order of work

1. Fix the strict lexical classification and add a stop-word list. Without this the default profile fails on ordinary corpora, and no scaling work is observable through the public API.
2. Change `boundary_key` for document normalizers to the enclosing section or table before touching chunking. This single change unlocks neighbour context, adjacent merging and real contextual groups.
3. Slim the snapshot manifest to bindings and digests; read lexical content from `lexical_records`. This removes the quadratic storage growth and most of the per-query parse cost at once.
4. Verify only newly written batches at activation; move whole-corpus verification into `manage health`.
5. Introduce the eligibility cache and, in the same step, replace unit-level ID sets with revision-level sets and SQL joins for candidate selection. Keep per-candidate dependency checks.
6. Stop re-running `prepare_extraction` and configuration dumps on read paths; trust `commit_extraction` and `publish_snapshot`, which already validate.
7. Separate read connections from the owner thread; keep writes, native handles and provider dispatch on the owner.
8. Then address the maintainability list: an import-direction check, a repository layer that absorbs the raw SQL in `application/`, helper functions for the repeated row-to-`ArtifactRef` and epoch reads, and splitting the five 300-plus-line functions along the stage boundaries the specification already names.

## 8. Implications for the handoff plan

`HANDOFF.md` fixes the next task as dashboard specification and planning, and lists six later work items. Read against the findings above:

**Dashboard planning.** The handoff asks for an endpoint and capability matrix before any UI work. Three findings change what that matrix can honestly say. The query trace is still never populated, so the "explain a query" panel in the dashboard specification has no data source. Query history is metadata-only and purge is dead, so the Activity view can list runs but cannot replay or purge them. Search latency grows with corpus size (section 5), so the workbench's interactive search over a few hundred documents will feel slow before any provider is involved. The matrix should mark trace, replay, purge and large-corpus search as "missing" or "available with limitations", and the owner should decide whether to fix Q1, Q2 and D1 before the dashboard makes those costs visible to users.

**Item 2, semantic chunks and contextual indexing.** The handoff is right that this is not "changing two numbers". Finding D5 is the blocker: with per-paragraph boundary keys, contextual groups contain one window, neighbours never attach and adjacent matches never merge. Changing the normalizers to emit section or table boundary keys is the prerequisite; the window constants in `indexing/windows.py` are secondary. C1 (quadratic token counting on long units) will surface as soon as units become section-sized.

**Item 3, Docling improvements.** Unaffected by the flow findings except D2 and D6: richer extraction means more assets and more vectors, which makes per-add re-verification and per-vector files more expensive. Fix those before enlarging extraction output.

**Item 4, reranking.** The rerank path reloads and re-validates the full extraction of every admitted candidate per query (`application/reranking.py:719-831`) and issues `PRAGMA table_info` per matched window. Live qualification of `rerank-3` should include latency measurement of this preparation step, which today exceeds the provider round trip for long documents.

**Item 5, more languages.** The language adapter work is independent of the flow findings, with one caveat: `project_code_excerpt_locator` re-validates the whole file per citation (C2), so larger repositories will make code citations slower. Fix the read-path validation before adding languages that produce more symbols per file.

**Item 6, Neo4j, Qdrant and PostgreSQL.** The handoff's effort estimates assume the ports are the boundary. For vectors and graph that is roughly true. For PostgreSQL it is not: the corpus port has five methods and the application layer issues 189 raw SQL statements, the eligibility compiler reads SQLite directly, and the snapshot manifest design (D1) and whole-corpus eligibility sets (Q1) would have to be redesigned for a networked database anyway. Items 3 to 6 of section 7 are the PostgreSQL prerequisites and should be budgeted as such.

**Product decisions to preserve.** The handoff's six preserved decisions all hold in the code and none of the recommendations in section 7 weakens them. In particular, slimming the manifest and caching eligibility do not touch original bytes, locators, revision identity or the upload-consent boundary.

**Handoff items this review did not examine.** Linux Docling caption association, the Chroma advisories, the standalone executable, incomplete collection management and the missing workspace directory registry were out of scope here and remain as the handoff describes them.

## Appendix: reproduction

The probe used for section 5 is a stand-alone script; it needs only a Python environment with the base package installed and no model assets:

```bash
PYTHONPATH=src .worktrees/ci-fixes/.venv/bin/python <probe.py> 5 20 50 100 200
```

Core of the probe: open `Tychora` with a counter whose `count` returns `len(text)`, add N `text` sources of kind `manual@1`, then time `kb.search({"question": ..., "modalities": ["lexical"]})` for a term unique to the last document and for the common query `"verification outcome"`.

Per-operation instrumentation wrapped `os.fsync`, `LocalArtifacts.put_stream`, `LocalArtifacts.open_verified`, `publication.resolve_snapshot`, `publication._read_json` and `eligibility.read_eligibility_facts` with counters before one add and one search on a 10-document workspace.

Focused tests:

```bash
PYTHONPATH=src .worktrees/ci-fixes/.venv/bin/python -m pytest \
  tests/retrieval tests/indexing tests/jobs tests/storage tests/configuration \
  tests/contracts tests/application tests/formatting -q -p no:cacheprovider \
  -o addopts="--import-mode=importlib"
```
