# Engine Core v2, Phase 5: Trace, Usage, Aggregates and Dashboard Endpoints Implementation Plan

> **Status: task granularity, to be expanded before execution.** Files, interfaces and tests are fixed at task level. Before executing, expand each task with the superpowers:writing-plans skill into bite-sized TDD steps with real code and exact commands, against the code as it stands after phase 4.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every query run stores a scope-safe trace, every provider call writes a usage row priced from a versioned table, dashboards read bounded aggregates, documents become searchable before their enrichment phases, and the endpoints dashboard v2 needs exist with contract tests and recorded fixtures.

**Architecture:** A `TraceRecorder` wraps each query stage of phase 4 and writes `query_traces` plus `query_stage_timings` in the run's writer task. Each provider adapter returns a `ProviderUsage` with its result; the four owner seams write `usage_events` through a buffered interactive writer task. Publication maintains `collection_aggregates` as a delta; job transitions maintain `document_status`. `extraction_layers` let enrichment phases publish layer batches into the member's page. HTTP endpoints read repositories on the read pool, page with `Cursor` and apply scope.

**Tech Stack:** Python 3.12, `decimal.Decimal` for costs, SQLite, FastAPI streaming responses for byte ranges, MCP 2.2.0, Typer, pytest, `httpx` test client.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.10 "Query trace v1", "Usage ledger", "Aggregates", "Background phases"; §4.13 "Dead code"; §4.15; §6 rows "Trace", "Usage", "Dashboard endpoints". Consumers: `docs/superpowers/specs/2026-10-15-dashboard-v2-design.md` §5 table, `docs/superpowers/specs/2026-10-15-model-defaults-design.md` §4.6. Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`.

## Global Constraints

All constraints of the index apply. Phase-specific:

- Trace JSON is at most 64 KiB; over that, per-result detail beyond rank 20 is dropped and `truncated: true` is set. `detail` keys come from a closed list (`eligible_revisions`, `excluded_units`, `matching_windows`, `distinct_candidates`, `dropped_dependency`, `rescored`, `provider`, `model`, `usage_id`). Counts are post-scope only.
- Trace read: the reader's narrowed scope digest equals the run's, or the reader holds whole-workspace authority; otherwise `FORBIDDEN_SCOPE`. HTTP, MCP and CLI return the same object.
- Usage writes are queued at interactive priority and flushed every 250 ms or at the end of the job or run. Cost is never stored; it is computed at read time from the latest price version whose `effective_from` ≤ `occurred_at`.
- Chunk histogram bins (tokens): `[0,64,128,256,512,1024,1536,2048,∞)`.
- Endpoint limits: documents `limit` 1..100 default 50; usage `limit` 1..500 default 100, at most two grouping keys, range ≤ 400 days; metrics read at most 100,000 runs; hubs live in phase 6.
- Byte ranges: one range only; `416` with `INVALID_REQUEST` reasons `range.unsatisfiable` or `range.multiple`; content verified at write and by `manage health`, not per ranged read.
- Provider connection test: fixed synthetic input `"tychora connection test"` or a 1×1 image, 10 s timeout, provider lane, one usage row, failures reported in `error` with HTTP 200; unknown role is `NOT_FOUND`.

## Review Focus

1. **A run made under a narrowed scope is read back by a caller with a different narrowed scope.** Expect `FORBIDDEN_SCOPE`, and no count in any trace that reveals hidden sources; pinned in Task 2.
2. **A provider call that fails or is denied by upload authorization.** Expect exactly one usage row with `outcome` `failed` or `denied` and no tokens; pinned in Task 3.
3. **A price version added later with an earlier `effective_from`.** Expect costs of earlier calls to change to the new version only where its `effective_from` ≤ `occurred_at`, and `price_version` in the response to name the version used; pinned in Task 4.
4. **A title with accents and mixed case ("Évaluation Générale") searched as "evaluation generale".** Expect a match through `title_fold`; pinned in Task 6.
5. **A `Range` header beyond the end, or `bytes=0-0,5-9`.** Expect `416` with the right reason, never a 500 or a full-body read into memory; pinned in Task 9.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/tychora/contracts/trace.py` (create) | `QueryTrace`, stage and result entries, `TraceStageName` |
| `src/tychora/retrieval/trace.py` (create) | `TraceRecorder`, size bounding, scope-safe counts |
| `src/tychora/contracts/usage.py` (create) | `ProviderUsage`, `UsageRow`, `PriceDocument`, `PriceEntry`, `cost_usd` |
| `src/tychora/resources/models/prices.toml` (create; extended by sub-project 3) | Packaged price rows |
| `src/tychora/adapters/sqlite/repositories/{traces,usage,prices,aggregates,history}.py` (create; `history` moves from `adapters/sqlite/history.py`) | Repositories |
| `src/tychora/application/owner.py` (modify seams `:513`, `:580`, `:621`, `:667`) | Usage rows from `ProviderUsage` |
| `src/tychora/adapters/voyage/{text,query,rerank,visual}.py`, `src/tychora/adapters/vision/{anthropic,local}.py` | Return `ProviderUsage` |
| `src/tychora/application/query.py` (`_deliver` `:1352-1388`) | Populate `trace` |
| `src/tychora/application/layers.py` (create) | Background phase registry and runner |
| `src/tychora/interfaces/http/routes.py`, `src/tychora/interfaces/http/ranges.py` (create), `src/tychora/interfaces/mcp/tools.py`, `src/tychora/interfaces/commands/manage.py`, `src/tychora/interfaces/commands/inspect.py` | Endpoints, MCP and CLI surfaces |
| `src/tychora/application/maintenance.py` (`:90-315`) | Purge dispatch; daily retention task |
| `dashboard/contracts/*.json`, `dashboard/scripts/generate_contracts.py` | Recorded fixtures for the new endpoints |

---

### Task 1: Trace contract and recorder

**Files:** Create `src/tychora/contracts/trace.py`, `src/tychora/retrieval/trace.py`; test `tests/retrieval/test_trace.py`.

**Interfaces:** Produces `QueryTrace` (schema `tychora.query-trace`, version 1, fields of spec §4.10), `TraceStageName`, `TraceRecorder(run_id, snapshot_id, policy_epoch, clock)` with `stage(name) -> AbstractContextManager[StageHandle]` (`StageHandle.set(in_=, out=, status=, **detail)` accepting only closed detail keys), `record_result(key, channels, fused, rerank, admitted_by, omitted)`, `finish(total_ms) -> QueryTrace` (bounded to 64 KiB).

Tests: `test_detail_keys_are_closed`; `test_trace_over_64_kib_drops_results_beyond_rank_20_and_sets_truncated`; `test_stage_order_and_offsets_are_monotonic`; `test_trace_json_round_trips`.

Commit `feat: add the query trace contract and recorder`.

### Task 2: Wire the trace through the query stages and store it

**Files:** `0001.sql` (`query_traces(run_id TEXT PRIMARY KEY REFERENCES query_runs(run_id), schema_version INTEGER NOT NULL, trace_json TEXT NOT NULL CHECK(json_valid(trace_json)), size_bytes INTEGER NOT NULL) STRICT`, `query_stage_timings(run_id TEXT NOT NULL REFERENCES query_runs(run_id), stage TEXT NOT NULL, duration_ms INTEGER NOT NULL, status TEXT NOT NULL, occurred_at TEXT NOT NULL, PRIMARY KEY(run_id, stage)) STRICT`, `query_runs.total_ms INTEGER`), checksum; `src/tychora/application/query.py` (each phase-4 stage function runs inside `recorder.stage(...)`; `_deliver` sets `trace` when `request.trace`; history record writes trace and timings in the same writer task); `HistoryRepository` and `TraceRepository`; HTTP `GET /v1/query-runs/{run_id}/trace`, MCP `inspect` subject `trace`, CLI `tychora inspect trace <run_id>`.

Tests: `test_every_run_stores_trace_and_stage_timings`; `test_trace_returned_inline_when_requested`; `test_hidden_source_fixture_leaks_no_counts` (Review Focus 1: two collections, reader scoped to one; every count in the trace equals the scoped-only workspace's trace); `test_trace_reader_with_other_scope_is_forbidden`; `test_same_trace_over_http_mcp_cli`; `test_trace_expires_with_run` (purge in Task 11).

Commit `feat: record a scope-safe trace for every query run`.

### Task 3: Usage contract, adapters and owner seams

**Files:** Create `src/tychora/contracts/usage.py`; `0001.sql` `usage_events` exactly as spec §4.10 (with `batch` and `units_source` columns and the three indexes); create `UsageRepository` (`append_many(rows)`, `grouped(query) -> UsageReport`); modify every provider adapter to return `(result, ProviderUsage)` through a small wrapper type `Metered[T]` (Voyage `usage.total_tokens` → `input_tokens`; Anthropic input/output tokens; pixels computed locally); modify the seams `run_provider` (`application/owner.py:513`), `run_local_text` (`:580`, `provider='local'`), `run_captured_provider` (`:621`), `run_query_provider` (`:667`) to append a row on success, failure, cancellation and denial; buffered writer flush every 250 ms or at job/run end.

Tests (`tests/providers/test_usage_rows.py`): `test_each_seam_writes_exactly_one_row` (fake providers, parametrized over the four seams); `test_failed_and_denied_calls_write_rows` (Review Focus 2); `test_rows_carry_run_or_job_never_both`; `test_local_encoder_rows_use_provider_local`; `test_flush_on_run_end_without_waiting_250_ms`.

Commit `feat: write a usage row for every provider call`.

### Task 4: Price tables and cost computation

**Files:** Create `src/tychora/resources/models/prices.toml` (rows for the models the current defaults use, each with `source_url` and `retrieved_on`; sub-project 3 extends it), `PriceRepository` (`latest_at(occurred_at)`, `versions()`, `add_user_version(doc)`), `0001.sql` `price_tables` as spec; builtin version inserted at workspace creation from the packaged file digest; `cost_usd` with `Decimal`; HTTP `GET /v1/prices`, `PUT /v1/prices`; CLI `tychora manage prices show|set <file.toml>`.

Tests (`tests/contracts/test_prices.py`, `tests/transports/test_prices_http.py`): `test_cost_uses_version_effective_at_call_time` (Review Focus 3); `test_user_version_never_replaces_builtin_history`; `test_missing_price_yields_null_cost`; `test_batch_rows_use_batch_price`.

Commit `feat: price usage from versioned price tables`.

### Task 5: Usage endpoint

**Files:** `src/tychora/interfaces/http/routes.py` (`GET /v1/usage` with the full query string and response of spec §4.15), `UsageRepository.grouped` (SQL group-by on at most two keys; `run` and `job` groupings ordered by first `occurred_at`, newest first; nearest-rank p50/p95 of latency); MCP and CLI parity (`tychora manage usage`).

Tests (`tests/transports/test_usage_http.py`): one per grouping key; `test_group_by_run_and_operation`; `test_range_over_400_days_rejected`; `test_cursor_paging_is_stable`; `test_invalid_cursor_reason`; `test_scope_filters_collections`; fixture recording to `dashboard/contracts/usage-*.json`.

Commit `feat: serve usage grouped by run and operation`.

### Task 6: Aggregates and the documents endpoint

**Files:** `0001.sql` `document_status`, `collection_aggregates` (spec §4.10, with `title_fold` and both indexes); `AggregateRepository` (`apply_publication_delta(base_snapshot_id, snapshot_id, replaced_stats, new_stats)` inside the activation transaction of `publish_snapshot` (phase 2), `set_document_state(...)` on job transitions, `documents(query) -> Page[DocumentStatusRow]`, `collection_summary(collection_id, snapshot_id)`); member stats computed when the batch is written (units, chunks, assets, tokens, pages, page kinds, chunk histogram); `title` set at registration (metadata title, else first heading, else file name) and updated at publication; `title_fold` = NFKD, combining marks stripped, `casefold()`. HTTP `GET /v1/documents` (filter, sort, order, cursor, limit) replacing `routes.py:189`, `GET /v1/collections/{id}/summary`.

Tests (`tests/transports/test_documents_http.py`, `tests/storage/test_aggregates.py`): `test_publication_delta_equals_full_recount` (randomized adds, replacements, removals); `test_title_filter_folds_case_and_accents` (Review Focus 4); `test_each_sort_is_stable_under_paging`; `test_needs_attention_reasons` (partial coverage, OCR review-required, failed enrichment phase); `test_dashboard_reads_touch_no_corpus_table` (statement spy: only `document_status`, `collection_aggregates`).

Commit `feat: maintain document and collection aggregates at publication`.

### Task 7: Query latency metrics endpoint

**Files:** `routes.py` `GET /v1/metrics/queries` (buckets hour/day, `mode`, `cache=hit|miss|any`; nearest-rank percentiles over `query_runs.total_ms` and `query_stage_timings`; newest 100,000 runs with `truncated`; `budgets` echoes `QUERY_STAGE_BUDGETS_MS` from `tychora.evaluation.bench.budgets`), `HistoryRepository.latency(query)`.

Tests: `test_percentiles_nearest_rank`; `test_truncated_above_100000_runs` (inserted rows, no queries run); `test_budgets_echo_spec_values`; fixture recording.

Commit `feat: serve query latency percentiles per stage`.

### Task 8: Background phases (extraction layers)

**Files:** `0001.sql` `extraction_layers` (spec §4.10), `evidence_units.layer_id`, `assets.layer_id`, `index_batches.layer_id` with `UNIQUE(family_id, extraction_id, layer_id)` replacing `UNIQUE(family_id, extraction_id)`; `src/tychora/contracts/identity.py` adds `derived_unit_id(layer_id, native_ref, ordinal, text_sha) -> str` = `digest("derived-unit-v1", ...)` and `layer_id(extraction_id, phase, enricher_fingerprint)` = `digest("layer-v1", ...)`; create `src/tychora/application/layers.py` (`PhaseRegistry.register(name, runner)`, low-priority job per extraction and phase, upload authorization like any provider call; phase publication rewrites only the member's page adding layer batches via phase 2's `SnapshotRepository`; if the extraction is no longer selected at publish time the layer stays retained and nothing publishes); `document_status.phase` transitions `core → enriching → complete`.

Tests (`tests/indexing/test_layers.py`): `test_document_searchable_after_core_phase`; `test_layer_publication_rewrites_only_member_page`; `test_derived_units_keep_original_identities`; `test_unselected_extraction_layer_is_retained_not_published`; `test_phase_job_requires_upload_authorization`; `test_picture_descriptions_after_consent_revocation_stay_searchable` (programme resolved decision 8; nothing new is sent).

Commit `feat: publish enrichment phases as extraction layers`.

### Task 9: Byte-range content endpoints

**Files:** Create `src/tychora/interfaces/http/ranges.py` (`parse_range(header, size) -> ByteRange | None` raising `TychoraError("INVALID_REQUEST", ..., reason="range.unsatisfiable"|"range.multiple")` whose registry entries carry `http_status=416`); `routes.py:534-547` (`GET /v1/assets/{id}/content`) and new `GET /v1/sources/{id}/revisions/{revision_id}/original` stream from `LocalArtifacts` with `Accept-Ranges`, `ETag: "<sha256>"`, `Content-Length`, media type, `If-Range`; `application/inspection.py:48` stops loading payloads into memory (returns an opened stream handle with scope and withdrawal checked per request).

Tests (`tests/transports/test_ranges.py`): `test_single_range_returns_206`; `test_suffix_and_open_ranges`; `test_unsatisfiable_and_multiple_ranges_return_416` (Review Focus 5); `test_if_range_mismatch_returns_full_body`; `test_asset_over_16_mib_served_by_ranges` (memory spy: peak below 2 MiB); `test_withdrawn_source_is_refused`.

Commit `feat: stream originals and assets with byte ranges`.

### Task 10: Source index facts and provider connection test

**Files:** `src/tychora/contracts/acquisition.py:48-57` (code capture records `branch` and per-language counts and skipped counts), `src/tychora/ingestion/acquire.py` (git branch from `HEAD` symbolic ref, `null` when detached), `src/tychora/adapters/code/*` language counts; `routes.py:356` (`GET /v1/sources/{id}` gains `index_facts`); `POST /v1/settings/providers/{role}/test` in `routes.py` with `application/settings.py` (`draft` flag selects the settings draft or active settings; checks `credential`, `endpoint`, `model`, `dimension`; local encoder role loads and verifies the asset).

Tests: `test_index_facts_for_git_capture_and_null_for_documents`; `test_detached_head_branch_is_null`; parametrized `test_connection_test_reports_each_failure_reason` (`provider.auth`, `provider.unreachable`, `provider.rate_limited`, `provider.timeout`) with HTTP 200; `test_connection_test_writes_one_usage_row`; `test_connection_test_sends_no_corpus_content`; `test_unknown_role_is_not_found`.

Commit `feat: expose repository index facts and provider connection tests`.

### Task 11: Purge commands and daily retention (C5 dead code)

**Files:** `src/tychora/application/maintenance.py:90-315` (`history-purge` → `HistoryRepository.purge(before)` deleting `query_traces` and `query_stage_timings` too; `cache-purge` → `QueryCaches.purge`, `EligibilityCache.clear`, `SnapshotCatalog.clear`, `JobRepository.expire_diagnostics`), owner daily retention task on the writer (background priority) calling both; `application/recovery.py` joins `SQL_FREE_MODULES`.

Tests (`tests/operations/test_history.py`, extend): `test_history_purge_removes_runs_traces_and_timings`; `test_cache_purge_clears_every_cache`; `test_daily_retention_runs_once_per_day` (fake clock).

Commit `feat: wire history and cache purge with a daily retention task`.

### Task 12: Dashboard fixtures, harness switch, gate and ratchet

Steps: record one fixture per endpoint into `dashboard/contracts/` via `dashboard/scripts/generate_contracts.py` (documents, collection summary, usage, prices, metrics, trace, index facts, connection test success and each failure); add `TraceStageSource` in `src/tychora/evaluation/bench/stages.py` and make it the default for `query` and `ingest` suites; run the phase-5 gate (`python -m tychora.interfaces.bench run --corpus bench-small --suite query --profile offline`: every run has a trace and stage timings; usage rows equal provider calls); `uv run --locked python -m tools.health check` and `update`. Commit `chore: record phase 5 fixtures, benchmark and health baseline`.

## Self-review (outline level)

- Spec coverage: trace v1 → Tasks 1, 2; usage ledger (seams, flush, batch flag, units source) → Task 3; prices → Task 4; `GET /v1/usage` → Task 5; aggregates and `GET /v1/documents`, collection summary → Task 6; query latency → Task 7; background phases → Task 8; byte ranges → Task 9; index facts, connection test → Task 10; purge and retention → Task 11; E7, E10 → Task 12. Hubs are phase 6.
- Spec gap carried: the trace path `GET /v1/query-runs/{id}/trace` differs from the existing `/v1/queries/{run_id}` family; this plan implements the spec path only (index gap 7).
- Names match the index's "Produced by phase 5" block.
