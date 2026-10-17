# Engine Core v2, Phase 4: Query Path Implementation Plan

> **Status: task granularity, to be expanded before execution.** Files, interfaces and tests are fixed at task level. Before executing, expand each task with the superpowers:writing-plans skill into bite-sized TDD steps with real code and exact commands, against the code as it stands after phases 2 and 3.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a warm query's local work cost O(revisions + candidates), not O(corpus), and stop queries from queueing behind ingestion.

**Architecture:** The owner gets three lanes (writer with two priorities, a WAL read pool, a CPU lane). Queries resolve the snapshot once from a SQL-only catalog, compile a revision-level `EligibilityView` (cached by snapshot, epoch, scope, policy and request digests) on the read pool, and push eligibility into store-side joins. Candidates that survive fusion are checked against dependencies persisted at write time (phase 3). Reranking and segments read per-candidate rows; the result cache stores canonical JSON. Strict mode distinguishes truncation from incompleteness; stop words and edition selection are fixed.

**Tech Stack:** Python 3.12 (`concurrent.futures`, `asyncio`), SQLite WAL with `mode=ro` connections, FTS5, Chroma 1.5.9 metadata filters, Pydantic 2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.6, §4.7, §4.8 (read side), §4.9, §4.14 rows "Per-query channels and rerank", "Per-source generation policy", "Review gating per request", "Edition defects"; §6 rows "Eligibility equivalence", "Epoch invariant", "Concurrency", "Strict regression", "Editions", "Cache". Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`.

## Global Constraints

All constraints of the index apply. Phase-specific:

- Queue bounds: interactive 256, background 64, read pool 128; `WORKSPACE_BUSY` reason `queue.full` only when a bound is exceeded. Read pool size `min(4, os.cpu_count())`, CPU lane 2 threads.
- Read tasks open a deferred `BEGIN`, see one WAL snapshot and end with `ROLLBACK`. Read connections are `mode=ro` and `PRAGMA query_only=ON`.
- Eligibility cache: LRU of 64 views keyed by `(snapshot_id, policy_epoch, scope_digest, policy_digest, request_digest)`; snapshot catalog LRU of 32.
- Lexical: `LIMIT 4000` matching windows, grouped to 80 owners per kind; hitting the limit sets `truncated = true` and never `complete = false`.
- Vector: `revision_id $in` filter when 512 or fewer eligible revisions; otherwise over-fetch `4 × limit`, at most two doublings, cap 1,024. Survivor check reads dependencies for at most 200 candidates in one query.
- Chroma upserts are partitioned so that each call carries at most 2 MB of float32 vectors (spec: 512 records, reduced by risk 3 so the partition stays at or below 2 MB; 488 records at 1,024 dimensions) and each partition runs as a separate background writer task.
- Result cache entries are canonical JSON no larger than `max_response_bytes + 65536`; no pickle.
- Every write that changes an eligibility input increments `policy_epoch` in the same transaction.

## Review Focus

1. **A reviewer rejects a unit while a query holding a cached view runs.** Expect the next query (new epoch) to exclude it and the running one to finish on its snapshot; pinned in Task 4 (epoch invariant) and Task 5 (cache key).
2. **A corpus with more than 512 eligible revisions and a narrow scope** (one small collection among many). Expect over-fetch to still return `limit` results when they exist, or report `truncated`; pinned in Task 7.
3. **A query made only of stop words ("the of and").** Expect the original terms to be kept, not an empty FTS expression or an error; pinned in Task 9.
4. **Two editions in force on `as_of` whose intervals overlap, plus one undated edition under `unknown_dates="permissive"`.** Expect exactly one decision for the overlap and a separate `undated_candidates` decision; pinned in Task 11.
5. **A read task raises an unexpected Python exception** (malformed retained row). Expect `INTEGRITY_ERROR` reason `invariant.unexpected` with a diagnostic ID, and the owner keeps serving; pinned in Task 2.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/tychora/application/runtime.py` (modify `BoundedWorker` `:27-60`) | Two-priority writer queue, read pool executor, CPU executor |
| `src/tychora/application/owner.py` (modify `:119-320`) | `run_write`, `run_read`, `run_cpu`; read connection pool; `run_blocking` renamed to `run_write` at every call site |
| `src/tychora/adapters/sqlite/connection.py` (modify) | `open_read_connection(path)` (`mode=ro`, `query_only`) |
| `src/tychora/adapters/sqlite/repositories/__init__.py` (modify) | `SqliteUnitOfWork.read()` on read-pool connections |
| `src/tychora/retrieval/catalog.py` (create) | `SnapshotCatalog` |
| `src/tychora/contracts/eligibility.py` (create; remove `EligibilityView` from `contracts/graph.py:188-238`) | `RevisionGrant`, `GraphBinding`, `EligibilityView` v2 |
| `src/tychora/retrieval/selection.py`, `retrieval/compiler.py`, `retrieval/attempt.py` (create from `retrieval/eligibility.py`, which is deleted) | Editions, compiler, pinned attempt |
| `src/tychora/adapters/sqlite/repositories/eligibility.py` (create; replaces `adapters/sqlite/eligibility.py`) | `EligibilityRepository` |
| `src/tychora/ports/search.py` (create) | `RankedOwner`, `RankedOwners`, `LexicalSearch`, `VectorSearch` |
| `src/tychora/adapters/sqlite/lexical.py` (modify `:17-186`) | Join-restricted FTS and exact search |
| `src/tychora/adapters/chroma/index.py` (modify `:306-449`) | Filtered or over-fetched search; partitioned upserts |
| `src/tychora/application/query.py` (modify `_prepare` `:681-1035`, delete `_candidate_lanes` `:1036`, `_vector_owners` `:1169`, `QueryExecutionPlan` `:110-129`) | Stage functions, `QueryBindings` |
| `src/tychora/application/reranking.py` (modify `:115-330`, `:719-831`) | Per-candidate rerank facts |
| `src/tychora/retrieval/rerank.py`, `retrieval/anchors.py`, `retrieval/visuals.py`, `retrieval/segments.py` | `view.allows`; per-candidate rows; no `prepare_extraction` |
| `src/tychora/retrieval/cache.py` (modify `:110-125`) | `CachedSelection` |
| `src/tychora/resources/stopwords/en@1.txt`, `fr@1.txt` (create) | Stop-word lists |
| `src/tychora/contracts/requests.py` (modify `QueryRequest` `:356-380`) | `channels`, `rerank`, `review_states`, `revision_selector="applicable"` |
| `src/tychora/resources/migrations/0001.sql` | `review_current`, `sources.allowed_uses_override` |

---

### Task 1: Owner lanes and queue priorities

**Files:** Modify `src/tychora/application/runtime.py:27-60` (`BoundedWorker` gains `PriorityWorker` with `interactive` and `background` queues on one thread; new `ReadPool` and `CpuLane` executors), `src/tychora/application/owner.py` (add `run_write(call, ctx, priority)`, `run_cpu(call, ctx)`; rename `run_blocking` and `run_blocking_sync` to `run_write`/`run_write_sync` at all 165 call sites listed by `grep -rn "run_blocking" src/tychora`). Test `tests/runtime/test_lanes.py`.

**Interfaces:** Produces `Priority`, `WorkspaceOwner.run_write`, `WorkspaceOwner.run_cpu` (index). `run_read` arrives in Task 2.

Tests: `test_interactive_task_runs_before_queued_background_tasks`; `test_queue_bounds_raise_queue_full` (257th interactive, 65th background → `WORKSPACE_BUSY` reason `queue.full`, `retryable=True`); `test_cpu_lane_runs_two_tasks_concurrently`; `test_native_handles_stay_on_writer_thread` (Chroma and Ladybug calls assert `threading.current_thread().name` starts with the writer prefix).

Acceptance: full default suite green (mechanical rename). Commit `refactor: split the owner worker into writer and CPU lanes`.

### Task 2: Read pool and read unit of work

**Files:** Modify `src/tychora/adapters/sqlite/connection.py` (add `open_read_connection(path: Path) -> sqlite3.Connection`), `src/tychora/adapters/sqlite/repositories/__init__.py` (`SqliteUnitOfWork(writer_db, read_connections)`: `read()` takes a pool connection, `BEGIN` deferred, `ROLLBACK` on exit), `src/tychora/application/owner.py` (`run_read(call: Callable[[ReadTransaction], T], ctx)`; boundary guard converting non-`TychoraError` exceptions to `INTEGRITY_ERROR` reason `invariant.unexpected` with `diagnostic_id`, logged with the traceback under that ID). Test `tests/runtime/test_read_pool.py`.

Tests: `test_read_sees_one_wal_snapshot_while_writer_commits`; `test_read_connection_refuses_writes` (`sqlite3.OperationalError` → mapped `INTEGRITY_ERROR` `invariant.unexpected`); `test_unexpected_exception_becomes_typed_error_with_diagnostic` (Review Focus 5); `test_read_pool_bound_128`.

Commit `feat: add a WAL read pool behind run_read`.

### Task 3: Snapshot catalog, single resolution per query, lease-only pins

**Files:** Create `src/tychora/retrieval/catalog.py` (`SnapshotCatalog(uow)`, LRU 32, `get(snapshot_id) -> SnapshotView` built by `SnapshotRepository.view` without members); modify `contracts/publication.py` `SnapshotView` (drop `members`, index note) and every consumer listed by `grep -rn "\.members" src/tychora` (move member reads to `SnapshotRepository` paged queries; delete phase 2's interim `RetainedSlot`, `RetainedBatch`, `CorpusStore.snapshot_slots` and `adapters/sqlite/retained_slots.py`, whose callers now read `BatchRepository` and `RepresentationRepository`); modify `adapters/sqlite/corpus.py` `pin_snapshot` (lease on `snapshot_id` only). Test `tests/retrieval/test_catalog.py`.

Tests: `test_catalog_builds_view_from_sql_without_artifacts` (spy on `open_verified`: zero calls); `test_query_resolves_snapshot_once` (spy on `SnapshotRepository.view`: one call per uncached query, zero when cached); `test_gc_keeps_leased_snapshot`.

Commit `feat: resolve snapshots once from a SQL-only catalog`.

### Task 4: Review state table, generation-policy override and the epoch invariant

**Files:** `0001.sql` (`CREATE TABLE review_current(target_kind TEXT NOT NULL CHECK(target_kind IN ('revision','unit','asset')), target_id TEXT NOT NULL, state TEXT NOT NULL, review_event_id INTEGER NOT NULL, PRIMARY KEY(target_kind,target_id)) STRICT;` and `ALTER`-free column `sources.allowed_uses_override TEXT CHECK(allowed_uses_override IS NULL OR json_valid(allowed_uses_override))` in the `sources` definition), `migrations.py` checksum; `adapters/sqlite/policy.py:84-100` (`record_review` upserts `review_current` in its transaction); new repository write `SourceRepository.set_allowed_uses_override(source_id, uses)` bumping `policy_epoch` (values `never_generate`, `reference_only`, or a subset of the kind's `allowed_uses`; anything else `INVALID_REQUEST` reason `filter.unknown_field`). Test `tests/storage/test_epoch_invariant.py`.

Tests: `test_every_eligibility_write_bumps_policy_epoch` — enumerates the write methods of every repository whose name is in `ELIGIBILITY_INPUT_WRITES` (review, withdrawal, collection policy, floors, facet bindings, consent, usage override) and asserts each bumps the epoch in the same transaction; a meta-test fails when a repository gains a write method that touches an eligibility table without being listed. `test_review_current_tracks_latest_event`. `test_allowed_uses_override_only_narrows`.

Commit `feat: maintain current review state and per-source use overrides`.

### Task 5: Eligibility v2 compiler, repository and cache (Q1, Q2, C3)

**Files:** Create `src/tychora/contracts/eligibility.py`, `src/tychora/retrieval/selection.py` (moves `select_temporal_revisions` unchanged here; rewritten in Task 11), `src/tychora/retrieval/compiler.py` (`EligibilityCompiler.compile(snapshot, scope, request, tx) -> EligibilityView` in stage functions under 80 lines: `_member_rows`, `_revision_grants`, `_exclusions`, `_unit_filter`, `_selected_batches`), `src/tychora/retrieval/attempt.py` (`run_pinned_eligibility`), `src/tychora/adapters/sqlite/repositories/eligibility.py` (the queries of spec §4.7 steps 1, 3, 4: one member join over `snapshot_members ⨝ revisions ⨝ sources ⨝ collections ⨝ kinds ⨝ review_current`, one exclusions query over `review_current` and `evidence_units.citable`, one indexed `facet_values` query per filter leaf), `EligibilityCache` in `src/tychora/retrieval/cache.py` using `eligible_cache_key` (moved to `retrieval/compiler.py`). Delete `src/tychora/retrieval/eligibility.py` and `src/tychora/adapters/sqlite/eligibility.py`. Kind envelopes digest-verified at registration and cached by `(kind_id, definition_hash)`. Copy the v1 compiler and fact reader into `tests/helpers/eligibility_v1_oracle.py` as the equivalence oracle.

**Interfaces:** Produces `EligibilityView` v2, `RevisionGrant`, `GraphBinding`, `EligibilityCache.get_or_compile`, `EligibilityRepository` (index). `view.allows(dependency_ids, kinds)` replaces the six "allowed" unions in `retrieval/rerank.py`, `retrieval/anchors.py`, `retrieval/visuals.py`.

Tests (`tests/retrieval/test_eligibility_v2.py`): `test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures` (scopes, access floors, reviews, filters, editions, overrides; admission compared after the survivor check of Task 7); `test_view_size_is_revisions_plus_exclusions`; `test_cache_hit_across_different_questions_same_filters`; `test_cache_miss_after_epoch_bump` (Review Focus 1); `test_compiler_never_calls_prepare_extraction` (spy).

Commit `feat: compile revision-level eligibility with a cache`.

### Task 6: Lexical and exact search through joins (C5 exact cap, Q6)

**Files:** Create `src/tychora/ports/search.py`; modify `src/tychora/adapters/sqlite/lexical.py:17-186` to implement `LexicalSearch` (FTS restricted to `snapshot_batches` for the pinned snapshot's lexical slot and to a per-connection temp table `elig_ext(extraction_id)` filled once per cache key per connection, skipped when every member is eligible; exclusions by `NOT EXISTS`; `ORDER BY bm25 LIMIT 4000`; grouped to 80 owners per kind) and exact search (same restriction; `complete=True`, `truncated=len(hits) > limit`). Delete `src/tychora/ports/lexical.py`.

Tests (`tests/retrieval/test_lexical.py`, extend): `test_33_identifier_hits_are_complete_and_truncated`; `test_verification_outcome_succeeds_strict_on_20_documents`; `test_window_limit_sets_truncated_not_incomplete`; `test_temp_table_filled_once_per_cache_key`; `test_lexical_ranks_survive_owner_grouping`.

Commit `feat: restrict lexical and exact search with store-side joins`.

### Task 7: Vector search, survivor check and lanes from the view

**Files:** Modify `src/tychora/adapters/chroma/index.py:306-449` (implement `VectorSearch`: `where={"owner_kind": k}` plus `revision_id $in` at ≤ 512 eligible revisions, else over-fetch with doublings and cap; remove `_present`; quantized-family hook reports `detail["rescored"] = 0` for float32; `upsert_batch` splits records into partitions of `max(1, 2_000_000 // (4 * dimension))` records, each submitted as its own background writer task so an interactive query waits at most one partition), `src/tychora/application/query.py` (delete `_vector_owners` `:1169-1197` and `_candidate_lanes` `:1036-1062`; owners mapped in one SQL statement over `vector_records` through `BatchRepository.owners(record_ids)`; survivor check reads `RepresentationRepository.dependencies` for at most 200 candidates and drops failures as `dropped_dependency`; lanes from `view.revisions[...].lane`).

Tests (`tests/retrieval/test_vector_selection.py`): `test_filtered_search_below_512_revisions`; `test_overfetch_returns_limit_or_truncated_above_512` (Review Focus 2); `test_owner_mapping_is_one_statement` (statement counter); `test_survivor_check_drops_ineligible_dependency`; `test_lanes_need_no_sql`; `test_upsert_partitions_stay_under_2_mb` (1,024 dimensions → 488 records per call); `test_interactive_query_waits_at_most_one_partition`.

Commit `feat: filter vector search by eligibility and check survivors once`.

### Task 8: Rerank facts, segments and inspection from persisted rows (C2, Q3, Q5)

**Files:** Modify `src/tychora/application/reranking.py:115-330` (`load_retained_representation` reads `RepresentationRepository` per candidate), `:719-831` (`prepare_rerank_facts` without `prepare_extraction` and without `PRAGMA table_info`), `src/tychora/retrieval/rerank.py:58-65,394-411` (`RerankFacts` holds per-candidate rows; no per-window chunk re-derivation), `src/tychora/retrieval/visuals.py:292`, `src/tychora/application/inspection.py:411`, `src/tychora/application/retained_content.py:148`, `src/tychora/retrieval/segments.py:90` (`project_code_excerpt_locator` from intervals); `src/tychora/retrieval/cache.py` (`CachedSelection`, canonical JSON, size bound). Cheap revalidation kept: each fetched unit's `text_sha`, policy, epoch.

Tests: `test_no_read_path_calls_prepare_extraction` (AST test over `application/`, `retrieval/`: only `indexing/coordinator.py` and `application/maintenance.py` may call it); `test_code_result_with_eight_citations_validates_file_zero_times`; `test_cache_entry_under_bound_for_300_page_result`; `test_cached_selection_round_trips_without_pickle`.

Commit `perf: read rerank and segment facts per candidate`.

### Task 9: Stop words (C4)

**Files:** Create `src/tychora/resources/stopwords/en@1.txt` and `fr@1.txt` (one word per line, lowercase, sorted; sources: the Snowball English and French stop-word lists, BSD licence, recorded in the file header comment and `THIRD-PARTY-NOTICES`), modify the lexical profile settings in `src/tychora/configuration/models.py` (`RetrievalSettings`: `languages: tuple[str, ...] = ("en", "fr")`, `stopwords: str = "builtin@1"`), `src/tychora/application/query.py:238` (construct `LexicalIndex` with the loaded set), query fingerprint (`_projection_fingerprint` `:257`) includes the list digests; `src/tychora/retrieval/lexical_query.py:108-131` keeps the original terms when removal empties the query. Ship both files in the wheel (`tests/release/test_resources.py`).

Tests: `test_stopwords_removed_from_fts_expression`; `test_only_stopwords_keeps_original_terms` (Review Focus 3); `test_stopword_digest_enters_query_fingerprint`; `test_changing_lists_needs_no_rebuild`.

Commit `feat: apply versioned stop-word lists to lexical queries`.

### Task 10: Strict mode and coverage semantics (Q6)

**Files:** Modify `src/tychora/application/query.py:981-985` (graph budget truncation sets `truncated`, not `partial`), coverage contract in `src/tychora/contracts/evidence.py` (add `truncated_channels: tuple[str, ...]`), strict rule: a channel is `incomplete` only when its index is unavailable, a required batch is missing, or the deadline is exceeded.

Tests (`tests/retrieval/test_degraded.py`, extend): `test_truncated_channel_is_not_incomplete_in_strict_mode`; `test_missing_required_batch_is_incomplete` (`INDEX_INCOMPLETE` reason `batch.missing`); `test_graph_budget_truncation_keeps_coverage_complete`.

Commit `fix: separate truncation from incompleteness in strict mode`.

### Task 11: Editions v2 (C5)

**Files:** Rewrite `select_temporal_revisions` in `src/tychora/retrieval/selection.py` (order by `effective_from` with `as_of`, by `published_on` with only `cutoff`; ties on `(published_on, edition_key)`; warning and `DecisionRequirement` only for a tie on the key or overlapping `[effective_from, effective_until)` among winners; permissive unknown-date editions returned as `undated_candidates` with their own decision; `revision_selector="applicable"` returns every edition in force at `as_of` with one decision); `src/tychora/contracts/requests.py` (`revision_selector: Literal["current","all","applicable"]`); `src/tychora/application/query.py:1012-1020` (decisions from the new result type).

Tests (`tests/retrieval/test_temporal.py`, extend): `test_unique_winner_yields_no_decision`; `test_effective_versus_publication_ordering`; `test_tie_and_overlap_yield_one_decision`; `test_undated_candidates_are_separate` (Review Focus 4); `test_applicable_selector_lists_editions_in_force`.

Commit `fix: select editions by the applicable date and report real ambiguities`.

### Task 12: Per-request narrowing and `QueryBindings`

**Files:** `src/tychora/contracts/requests.py` (`ChannelSelection`, `channels`, `rerank`, `review_states`, all in the eligibility request digest where relevant), `src/tychora/application/query.py` (replace `QueryExecutionPlan` `:110-129` by narrowing of the configured plan; a channel turned off is `skipped`; `QueryBindings` value object grouping the six optional providers, validated once in `build_services`), `src/tychora/application/services.py`, `src/tychora/evaluation/runner.py` (ablation variants use `channels` and `rerank`), MCP `search` tool and HTTP `POST /v1/queries` accept the fields.

Tests: `test_channels_can_only_narrow_configuration` (`INVALID_REQUEST` when enabling a disabled channel); `test_rerank_off_skips_rerank_stage`; `test_review_states_narrow_mode_states`; `test_review_states_change_request_digest`; `test_generation_policy_never_generate_excluded_from_compose`.

Commit `feat: let a query narrow channels, reranking and review states`.

### Task 13: Query stages, configuration dumps and SQL-free query modules (D7, ratchet)

**Files:** Split `QueryService._prepare` (`application/query.py:681`) into stage functions under 80 lines (`_channels`, `_fuse`, `_admit`, `_rerank`, `_assemble`, `_coverage`); `_validate_provider_families` (`:298`) compares `retained_sha` strings and family IDs only; remove registry dumps in query and compiler; append `application/query.py`, `application/reranking.py`, `application/visual.py`, `application/inspection.py`, `application/owner.py`, `application/removal.py`, `application/restore.py`, `retrieval/visuals.py` to `SQL_FREE_MODULES` (paths relative to `src/tychora`, as phase 1 defines the list) (their SQL moves to repositories; removal and restore keep retention and visual SQL after phase 2, which moves into `SnapshotRepository` and a `RetentionRepository` here).

Tests: `tests/architecture/test_sql_free_modules.py` green; `test_query_never_dumps_configuration` (spy on `ResolvedConfig.model_dump`).

Commit `refactor: run queries as typed stages without configuration dumps`.

### Task 14: Gate and ratchet

Steps: concurrency test `tests/acceptance/test_concurrency.py::test_add_of_300_pages_with_20_queries` (query p95 ≤ 1.5× idle; 33 concurrent callers succeed); strict regression `tests/acceptance/test_strict_scale.py` ("verification outcome" at 20, 1,000, 10,000 synthetic documents, the 10,000 case `@pytest.mark.slow`); evaluation versus the phase-1 baseline per stage (`python -m tychora.interfaces.bench run --corpus bench-small --suite query --profile offline` plus the evaluation command recorded in phase 1); phase-4 gate from the index (E1 local, E4, E5, E6); `uv run --locked python -m tools.health check` and `update`. Commit `chore: record phase 4 benchmark and health baseline`.

## Self-review (outline level)

- Spec coverage: §4.6 → Tasks 1, 2 (writer priorities and queue bounds), Task 7 (Chroma upsert partitions); §4.7 snapshot cache → Task 3; `EligibilityView` v2, compilation, `review_current`, epoch invariant → Tasks 4, 5; cache → Task 5; candidate selection lexical/exact/vector/survivor/lanes → Tasks 6, 7; typed errors → Task 2; §4.8 read side and `CachedSelection` → Task 8; §4.9 → Tasks 9, 10, 11; §4.14 per-query channels, review gating, generation policy, edition defects → Tasks 4, 11, 12; D7 query half → Task 13.
- Names match the index's "Produced by phase 4" block.
