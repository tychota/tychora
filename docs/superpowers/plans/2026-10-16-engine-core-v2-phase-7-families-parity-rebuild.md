# Engine Core v2, Phase 7: Vector Families, Parity and Rebuild Implementation Plan

> **Status: task granularity, to be expanded before execution.** Files, interfaces and tests are fixed at task level. Before executing, expand each task with the superpowers:writing-plans skill into bite-sized TDD steps with real code and exact commands, against the code as it stands after phase 6.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Carry the vector family options sub-project 3 needs (dimension, index dtype, local query encoder), give a profile edit a path forward (`manage rebuild index`), deliver the remaining engine-core parity items (connectors with version history, bulk catalog import, `max_upload_rank`), split the ingestion pipeline into stages with its SQL in repositories, and verify E1–E11 and the code-health ratchet.

**Architecture:** `VectorFamilySpec` becomes the single description of a vector family; every field but `role` and `rescore_top` enters `config_sha` and therefore `family_id`. A `LocalQueryEncoder` port with an ONNX adapter runs on the CPU lane. `manage rebuild index` creates fresh-nonce families, re-windows from canonical units, reuses vectors by `(space_id, input_sha)`, and publishes one snapshot as a resumable job. A `SourceConnector` port turns external version histories into revisions; the Git-history connector is the reference. `execute_accepted` splits into five stages over `SourceRegistrationRepository`.

**Tech Stack:** Python 3.12, ONNX Runtime (new optional extra `local-encoder`, exact pin), HF `tokenizers`, `git` CLI through the existing acquisition helpers, SQLite, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.4 "Family options", §4.11 `index`, §4.14 rows "Source connectors with version history", "Bulk catalog import", §4.13 (repository for `sources`), §7, §1 (E1, E2, E8, E9); programme resolved decisions 1 and 4. Consumer: `docs/superpowers/specs/2026-10-15-model-defaults-design.md`. Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`.

## Global Constraints

All constraints of the index apply. Phase-specific:

- Space rule: a family is valid only when its document model and query encoder share a `space_id` from the model registry; otherwise `PROFILE_INCOMPATIBLE` reason `family.space_mismatch`. `voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-4-nano` share `voyage-4`; `voyage-context-4` and `voyage-code-4` each have their own.
- An adapter refuses an `index_dtype` it does not support natively with `PROFILE_INCOMPATIBLE` reason `family.dtype_unsupported`; Chroma declares `float32` only. Matryoshka truncation is requested from the provider (`output_dimension`), never done locally.
- Until a `families` report is accepted, families use `dimension = 1024` and `index_dtype = "float32"`; `rescore_top` defaults to 100 and is query-only (programme decision 4).
- The local query encoder never falls back to a hosted model; a missing or unverified asset makes the text channel `unavailable` with `CAPABILITY_MISSING` reason `model_asset.absent` naming `tychora manage setup`; a digest mismatch is `INTEGRITY_ERROR` reason `model_asset.mismatch`.
- Rebuild index: per-member checkpoints; adds continue against the old families; at the end, members added meanwhile are re-batched before the switch (bounded rebase loop, 3 attempts); every embedding batch is authorised under current consent.
- Catalog import: at most 100,000 lines; one idempotent add per line keyed by `digest(catalog_sha, line_no)`.
- `max_upload_rank`: documents whose confidentiality rank (effective access floor) is 2 or higher are refused in collections with provider upload consent (programme decision 1).

## Review Focus

1. **A profile edit that changes the text model, followed by an add.** Expect `PROFILE_INCOMPATIBLE` reason `index.rebuild_required` naming the command, then a successful rebuild, then the add succeeds; pinned in Task 4.
2. **An add arriving while `rebuild index` runs.** Expect the add to publish against the old families and be re-batched before the switch; pinned in Task 4.
3. **A Git-history connector over a path renamed in history.** Expect each commit touching the path (following the rename) to become a revision with its commit date; pinned in Task 6.
4. **A catalog file with a malformed line in the middle and a duplicate line.** Expect a per-line outcome report (one failure, one idempotent duplicate) and every other line added; pinned in Task 7.
5. **A local encoder asset replaced on disk after setup.** Expect `INTEGRITY_ERROR` reason `model_asset.mismatch` at load and the text channel unavailable, never a hosted call; pinned in Task 2.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/tychora/contracts/families.py` (create) | `VectorFamilySpec`, `QueryEncoder` |
| `src/tychora/configuration/models.py` (`EmbeddingSettings` `:74-89`, `IndexSettings` `:143`) | Family options in configuration |
| `src/tychora/ports/embeddings.py` (modify) | `LocalQueryEncoder` port |
| `src/tychora/adapters/onnx/query_encoder.py` (create) | ONNX `voyage-4-nano` encoder |
| `src/tychora/adapters/chroma/index.py` | `supported_dtypes = frozenset({"float32"})` |
| `src/tychora/evaluation/bench/families.py` (create) | `--suite families` |
| `src/tychora/application/rebuild_index.py` (create) | `manage rebuild index` job |
| `src/tychora/indexing/coordinator.py` (`finalize`) | `index.rebuild_required` instead of `INDEX_INCOMPLETE` |
| `src/tychora/ports/connectors.py`, `src/tychora/adapters/connectors/git_history.py`, `src/tychora/application/connectors.py` (create) | Connectors |
| `src/tychora/application/catalog_import.py` (create) | Bulk import |
| `src/tychora/contracts/collections.py` (`CollectionPolicyV2` `:11-16`) | `max_upload_rank` |
| `src/tychora/application/sources.py` (`execute_accepted` `:587-841`, `_registration` `:1321-1601`) | Five stages; SQL into `SourceRegistrationRepository` |
| `src/tychora/adapters/sqlite/repositories/registration.py` (create) | `SourceRegistrationRepository` |

---

### Task 1: Vector family specification and validation

**Files:** Create `src/tychora/contracts/families.py` (`VectorFamilySpec(role, space_id, document_model, query_encoder, dimension: Literal[256,512,1024,2048], index_dtype: Literal["float32","int8","binary"], rescore_top: int | None, contextual: bool)`, `QueryEncoder(kind: Literal["hosted","local"], model, asset_sha256)` with `asset_sha256` required for `local`); `configuration/models.py` (`EmbeddingSettings` gains `dimension`, `index_dtype`, `query_encoder`; `dtype: Literal["float32"]` at `:79` is replaced by `index_dtype`); family `config_sha` input excludes `role` and `rescore_top`; `VectorIndex.supported_dtypes` on the port; Chroma declares float32; quantized-family rescoring hook in `VectorSearch` (phase 4) reads float32 rows from batch artifacts through the read pool (`detail["rescored"]`).

Tests (`tests/contracts/test_families.py`, `tests/configuration/test_family_options.py`): `test_space_mismatch_refused`; `test_dtype_unsupported_refused_by_chroma`; `test_rescore_top_change_keeps_family_id`; `test_dimension_change_creates_new_family`; `test_output_dimension_requested_from_provider` (fake Voyage asserts the request field).

Commit `feat: describe vector families with dimension, dtype and query encoder`.

### Task 2: Local query encoder

**Files:** `pyproject.toml` new extra `local-encoder = ["onnxruntime==1.22.0"]` and `uv.lock`; `src/tychora/ports/embeddings.py` (`LocalQueryEncoder.encode(text) -> tuple[float, ...]`, `asset_sha256`); `src/tychora/adapters/onnx/query_encoder.py` (load once per owner, verify the asset digest, run on `run_cpu`); `src/tychora/resources/model-assets.json` entry for `voyage-4-nano` (Apache-2.0) with digest; `run_local_text` usage rows `provider='local'`, `operation='embed_query'`.

Tests (`tests/providers/test_local_encoder.py`, with a tiny ONNX fixture model generated by a committed script): `test_encoder_loaded_once_and_kept_warm`; `test_wrong_digest_is_model_asset_mismatch` (Review Focus 5); `test_missing_asset_makes_text_channel_unavailable_without_hosted_call` (network-deny fixture plus provider spy); `test_local_encoding_writes_usage_row`.

Commit `feat: run the fast offline query encoder locally`.

### Task 3: Families benchmark suite

**Files:** `src/tychora/evaluation/bench/families.py`; `src/tychora/interfaces/bench.py` dispatches `--suite families` to it (phase 1 refused that suite); for each candidate `(dimension, index_dtype, rescore_top)` record storage bytes per vector, text ANN p50/p95, nDCG@10 and recall@50 on the evaluation set; local encoder latency p95 for queries ≤ 64 tokens against the 60 ms budget.

Tests: `test_families_suite_reports_each_candidate` (offline, deterministic provider, two candidates); `test_unsupported_dtype_candidate_reported_as_refused`.

Commit `feat: compare vector family options in the benchmark`.

### Task 4: `manage rebuild index` (D8)

**Files:** Create `src/tychora/application/rebuild_index.py` (resumable job: fresh nonces for the selected channels via `FamilyRepository.create`; re-window from canonical units; lexical batches; graph contributions into a new live graph for a new graph family; re-embed with per-batch authorisation; reuse vectors from batch artifacts when `(space_id, input_sha)` matches; per-member checkpoints in `job_steps`; one snapshot publication; bounded rebase loop of 3 for members added meanwhile; old live graph dropped by compaction when no retained snapshot uses its family); `src/tychora/indexing/coordinator.py` `finalize` (`:1039-1172`) returns `PROFILE_INCOMPATIBLE` reason `index.rebuild_required` naming `tychora manage rebuild index`; `src/tychora/contracts/operations.py` `RebuildMaintenance` gains `scope: Literal["native","index"]` and `channels`; CLI `tychora manage rebuild index [--channel …]`; `src/tychora/application/settings.py` activation offers the rebuild.

Tests (`tests/operations/test_rebuild_index.py`): `test_profile_edit_then_add_requires_rebuild_then_succeeds` (Review Focus 1); `test_vectors_reused_when_space_unchanged` (provider call counter zero); `test_add_during_rebuild_is_rebatched_before_switch` (Review Focus 2); `test_rebuild_resumes_from_member_checkpoint_after_crash`; `test_rebuild_reauthorises_each_embedding_batch`.

Commit `feat: rebuild index families after a profile change`.

### Task 5: `max_upload_rank`

**Files:** `src/tychora/contracts/collections.py` (`CollectionPolicyV2.max_upload_rank: NonNegativeInt = 1`), owner upload authorization (`application/owner.py:834-1415` region, the policy re-read before dispatch) refuses dispatch for sources whose effective access floor ≥ 2 when the collection has provider upload consent, with `FORBIDDEN_SCOPE`; registration refuses adding such a document to a consenting collection with the same error and an operator message; dashboard-facing collection contract exposes the field.

Tests (`tests/security/test_upload_consent.py`, extend): `test_rank_two_document_refused_in_consenting_collection`; `test_rank_two_document_accepted_without_consent_and_never_uploaded`; `test_policy_change_bumps_epoch`.

Commit `feat: refuse confidential documents in collections that upload`.

### Task 6: Source connectors and the Git-history connector

**Files:** Create `src/tychora/ports/connectors.py` (`ConnectorItem(external_id, edition_key, published_on, effective_from, effective_until, url, metadata)`, `SourceConnector.list(cursor: Cursor | None) -> ConnectorPage`, `fetch(item) -> bytes`), `src/tychora/adapters/connectors/git_history.py` (each commit touching a path is an edition dated by the commit; follows renames with `git log --follow`; runs through the acquisition root containment of `ingestion/acquire.py`), `src/tychora/application/connectors.py` (`manage connector sync <name>` resumable job with a cursor checkpoint; `external_id` → `source_key`; each version → a revision with `edition_key`), configuration section `[connectors.<name>]`, CLI.

Tests (`tests/application/test_connectors.py`, offline fixture repository built in `tests/code/conftest.py` style): `test_each_commit_becomes_a_revision_with_commit_date`; `test_rename_followed` (Review Focus 3); `test_sync_resumes_from_cursor_checkpoint`; `test_resync_is_idempotent`.

Commit `feat: sync version histories through source connectors`.

### Task 7: Bulk catalog import

**Files:** Create `src/tychora/application/catalog_import.py` (`manage import-catalog <file.jsonl>`; line schema `{source: {path|url}, source_key, collection, kind, edition_key?, metadata}`; one idempotent add per line keyed by `digest(catalog_sha, line_no)`; catalog job reporting per-line outcomes; at most 100,000 lines, otherwise `LIMIT_EXCEEDED`), CLI.

Tests (`tests/application/test_catalog_import.py`): `test_each_line_added_once`; `test_malformed_and_duplicate_lines_reported_per_line` (Review Focus 4); `test_reimport_is_idempotent`; `test_over_100000_lines_refused`.

Commit `feat: import catalogs of sources in bulk`.

### Task 8: Ingestion pipeline stages and registration repository

**Files:** Create `src/tychora/adapters/sqlite/repositories/registration.py` (`SourceRegistrationRepository` with the SQL of `_registration` `:1321-1601` and of `execute_accepted`); split `execute_accepted` (`application/sources.py:587-841`) into `capture`, `register`, `reuse_or_extract`, `project`, `publish`, each ≤ 60 lines; append `application/sources.py` and `indexing/coordinator.py` (its remaining extraction and visual SQL moves to `RepresentationRepository` and `BatchRepository`) to `SQL_FREE_MODULES`; enable the import-linter contract "rewritten modules import no `sqlite3`" for the eleven modules of spec §4.13.

Tests: existing `tests/jobs`, `tests/indexing`, `tests/application` suites green; `tests/architecture/test_sql_free_modules.py` green; `lint-imports` green; `test_each_stage_is_individually_testable` (one test per stage with fakes).

Commit `refactor: run source ingestion as five stages over repositories`.

### Task 9: Final verification

Steps:
- Parity: one passing test per §4.14 row (neighbours, impact: phase 6; per-query channels, generation policy, review gating, edition defects: phase 4; stable gold labels: phase 1; connectors, catalog import: this phase; edge weights: phase 6). Add `tests/acceptance/test_parity.py` that imports and runs a representative test for each row so E8 is one command.
- Online benchmark (weekly workflow or manual with credentials): `python -m tychora.interfaces.bench run --corpus bench-v1 --suite query --profile online`, `--suite ingest --profile online`, `--suite families --profile online`; E1 total p95 < 1 s, E2 native PDF ≤ 100 pages p90 < 30 s, per-page budgets reported.
- Scale: E3, E4, E11 re-run on `bench-small --suite scale`.
- Evaluation: E6 per stage ≥ the phase-1 baseline.
- Ratchet (E9): every deletion of spec §7 confirmed by `grep` (listed in the expanded plan); no function > 120 lines in touched modules; 0 reasonless `INTEGRITY_ERROR` in touched modules; mutation score on `adapters/sqlite/repositories/` and `retrieval/compiler.py` ≥ baseline (nightly job result); `uv run --locked python -m tools.health check` green and `update` committed.
- Commit `chore: verify engine core v2 success criteria`.

## Self-review (outline level)

- Spec coverage: §4.4 family options, space rule, local encoder, dimension and quantization, choosing defaults → Tasks 1–3; §4.11 `index` and D8 → Task 4; programme decision 1 → Task 5; §4.14 connectors and catalog import → Tasks 6, 7; §4.13 `sources` repository and the import-linter rule, §7 `execute_accepted` split → Task 8; E1, E2, E8, E9 → Task 9.
- Names match the index's "Produced by phase 7" line.
