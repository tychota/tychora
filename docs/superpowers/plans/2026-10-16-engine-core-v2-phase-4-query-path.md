# Engine Core v2, Phase 4: Query Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a warm query's local work cost O(revisions + candidates), not O(corpus), and stop queries from queueing behind ingestion.

**Architecture:** The owner gets three lanes: a writer with an interactive and a background queue, a WAL read pool of `mode=ro` connections, and a CPU lane. A query resolves its snapshot once from a SQL-only catalog, compiles a revision-level `EligibilityView` on the read pool (cached by snapshot, policy epoch, scope, policy and request digests), and pushes eligibility into store-side joins (`json_each` bindings, Chroma `where` filters or over-fetch). Candidates that survive fusion are checked against the dependency sets phase 3 persisted at write time; reranking, segments, visuals and inspection read per-candidate slices of persisted rows; the result cache stores canonical JSON. Strict mode separates truncation from incompleteness, stop words apply at query time, editions are selected by the applicable date, and a request can narrow channels, reranking and review states. The unit-level view is migrated reader by reader and deleted once nothing reads it.

**Tech Stack:** Python 3.12 (`concurrent.futures`, `asyncio`), SQLite WAL with `mode=ro` connections and `json_each`, FTS5, Chroma 1.5.9 metadata filters, Pydantic 2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.6, §4.7, §4.8 (read side), §4.9, §4.12, §4.13 (repositories, error reasons), §4.14 rows "Per-query channels and rerank", "Per-source generation policy", "Review gating per request", "Edition defects"; §6 rows "Eligibility equivalence", "Epoch invariant", "Concurrency", "Strict regression", "Editions", "Cache"; §7. Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` ("Produced by phase 3" is what this plan consumes; Task 21 records "Produced by phase 4"). Flow review: D4/Q4, D7 (query half), C2 (read side), C3, C4, C5 (exact cap, editions), Q1, Q2, Q3, Q5, Q6.

**Base:** `origin/main` at `505e8ff` (phases 1, 2 and 3 merged). Every `file:line` below was re-read at that commit; a reference into code an earlier task of this plan rewrites names the place as it stands after that task.

## Global Constraints

All constraints of the index apply. Phase-specific, verbatim from the outline:

- Queue bounds: interactive 256, background 64, read pool 128; `WORKSPACE_BUSY` reason `queue.full` only when a bound is exceeded. Read pool size `min(4, os.cpu_count())`, CPU lane 2 threads.
- Read tasks open a deferred `BEGIN`, see one WAL snapshot and end with `ROLLBACK`. Read connections are `mode=ro` and `PRAGMA query_only=ON`.
- Eligibility cache: LRU of 64 views keyed by `(snapshot_id, policy_epoch, scope_digest, policy_digest, request_digest)`; snapshot catalog LRU of 32.
- Lexical: `LIMIT 4000` matching windows, grouped to 80 owners per kind; hitting the limit sets `truncated = true` and never `complete = false`.
- Vector: `revision_id $in` filter when 512 or fewer eligible revisions; otherwise over-fetch `4 × limit`, at most two doublings, cap 1,024. Survivor check reads dependencies for at most 200 candidates in one query.
- Chroma upserts are partitioned so that each call carries at most 2 MB of float32 vectors (spec: 512 records, reduced by risk 3 so the partition stays at or below 2 MB; 488 records at 1,024 dimensions) and each partition runs as a separate background writer task.
- Result cache entries are canonical JSON no larger than `max_response_bytes + 65536`; no pickle.
- Every write that changes an eligibility input increments `policy_epoch` in the same transaction.

Also binding here (index and earlier-phase rulings):

- Every new `TychoraError` site passes `reason=` from `src/tychora/contracts/error_reasons.py`, with its entry in `tests/contracts/test_error_reasons.py`. This phase registers `canonical.facts_missing` (Task 5), `policy.use_unknown`, `source.absent`, `scope.capability_missing` (Task 4), `request.limit_exceeded`, `mode.unavailable`, `profile.snapshot_mismatch`, `kind.historical_unavailable`, `facet.binding_unverified`, `policy.epoch_changed` (Task 7), `identifier.namespace_unknown` (Task 8), `scope.dependency_ineligible` (Task 10), `edition.as_of_required` (Task 16), `channel.not_configured` (Task 17), and classifies `queue.full`, `invariant.unexpected`, `batch.missing`, `channel.unavailable`, `index.rebuild_required`, `payload.missing` and `graph.disabled` sites it touches.
- Phase 4 changes the schema (Task 4: `review_current`, `sources.allowed_uses_override`, `vector_records.group_id`, two indexes). `SCHEMA_CHECKSUM` moves once and the phase-3 checksum `be4274cd…` joins `EARLIER_FORMAT_TWO_CHECKSUMS`, so a workspace created on `505e8ff` opens with `PROFILE_INCOMPATIBLE` reason `workspace.format_old` naming `tychora manage rebuild workspace --from`, like phases 2 and 3. Only phase-3 workspaces open before this phase, so no read path keeps a fallback for missing persisted facts: a selected extraction without them is `INTEGRITY_ERROR` reason `canonical.facts_missing`.
- Adapters import no upper layer (import-linter contract `adapters-implement-ports`), retrieval imports no adapter (`storage-through-ports`), and no `ignore_imports` entry is added; this phase removes twelve (Tasks 5, 7, 11, 12).
- Dead code is a hard gate (G12, vulture; the whitelist may only shrink): every new public name has a caller in `src` (for example `set_allowed_uses_override` gets the client method of Task 4, `RepresentationRepository.neighbours` arrives with its caller in Task 10).
- No comment or docstring cites a plan task, plan file or review ID (G15). New functions ≤ 80 lines, cognitive complexity ≤ 15, no new function-body imports.
- Commands: `uv run --locked pytest <path> -v`; full suite `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`; imports `uv run --locked lint-imports --no-cache`; health `uv run --locked python -m tools.health check --test-reports without`. To run anything outside the worktree's own environment, set `UV_PROJECT_ENVIRONMENT` to a dedicated virtual environment.
- macOS (development machines): benchmark runs and the slow acceptance tests are informative and their `bench-results/` files are never committed; G16–G18 and test durations are never recorded (`tools.health update` runs with `--test-reports without`). Gate verdicts and committed results come from the reference runner class `github-ubuntu-22.04` (`benchmarks/reference-machine.toml`), through the `Benchmarks` workflow that the owner dispatches.

## Review Focus

1. **A reviewer rejects a unit while a query holding a cached view runs.** Expect the next query (new epoch) to exclude it, and the running one to deliver only if its epoch still holds, otherwise to retry once on the same snapshot with a fresh view. Pinned by `tests/retrieval/test_eligibility_v2.py::test_cache_miss_after_epoch_bump` (Task 7), the epoch-invariant tests of Task 4 (`tests/storage/test_epoch_invariant.py`), and the owner-backed retry tests of `tests/retrieval/test_eligibility.py` moved to the new `run_pinned_eligibility` in Task 7.
2. **A corpus with more than 512 eligible revisions and a narrow scope** (one small collection among many). Expect over-fetch to still return `limit` owners when they exist, or report `truncated`. Pinned by `tests/retrieval/test_vector_selection.py::test_overfetch_returns_limit_or_truncated_above_512` (Task 9).
3. **A query made only of stop words ("the of and").** Expect the original terms to be kept, not an empty FTS expression or an error. Pinned by `tests/retrieval/test_stopwords.py::test_only_stopwords_keeps_original_terms` (Task 14).
4. **Two editions in force on `as_of` whose intervals overlap, plus one undated edition under `unknown_dates="permissive"`.** Expect exactly one decision for the overlap and a separate undated decision. Pinned by `tests/retrieval/test_temporal.py::test_tie_and_overlap_yield_one_decision` and `::test_undated_candidates_are_separate` (Task 16).
5. **A read task raises an unexpected Python exception** (malformed retained row). Expect `INTEGRITY_ERROR` reason `invariant.unexpected` with a diagnostic ID, and the owner keeps serving. Pinned by `tests/runtime/test_read_pool.py::test_unexpected_exception_becomes_typed_error_with_diagnostic` (Task 2).

## Changes from the outline

- **Fourteen tasks become twenty-one.** The outline's Task 5 swapped a unit-level view that 18 modules read for a revision-level one in one commit; Task 8 rewrote reranking, segments, visuals, inspection and the result cache together. Neither fits one implementer and reviewer cycle. The swap now runs as a strangler: Task 6 adds the v2 contracts and store reads and renames the old view `LegacyEligibilityView`; Task 7 compiles and caches v2 views and hands both views to the query path (`attempt.view` is v2, `attempt.legacy` the old one); Tasks 8–11 move each reader; Task 12 derives graph scopes and deletes the legacy view, compiler and fact reader, keeping a copy as the equivalence oracle in `tests/helpers/eligibility_v1_oracle.py`. Between Tasks 7 and 12 a query compiles both views; the gate runs after Task 12. The outline's Task 7 survivor check moves into Task 8 (lexical candidates need it first); its Task 8 becomes Tasks 10 (rerank facts, segments), 11 (visuals, inspection, retained content, discovery) and 13 (`CachedSelection`); its Task 13 becomes Tasks 19 (query stages, the query modules) and 20 (`owner`, `removal`, `restore`, which hold 67 statements off the query path). Two tasks are new: Task 5 (persisted facts replace query-time derivation, a carried decision) and Task 18 (trimmed code records keep their `definition_key`, which the index gives phase 4 for the code gold `definition_recall`).
- **Pins are already lease-only.** `pin_snapshot` leases the snapshot ID only (`adapters/sqlite/corpus.py:194-199`, phase 2) and `test_leased_snapshot_is_retained` pins it; Task 3 does not touch leases.
- **One unit of work per read connection.** `SqliteUnitOfWork` keeps its one-connection shape (`adapters/sqlite/repositories/unit_of_work.py:80-113`, not `repositories/__init__.py`); `run_read` opens one over the calling pool thread's connection. The outline's `SqliteUnitOfWork(writer_db, read_connections)` is not needed.
- **No temp tables.** `query_only=ON` refuses writes to `temp` too (checked on SQLite 3.53) and every read task ends with `ROLLBACK`, which drops a temp table created inside it. The SQLite adapters bind ID lists with `IN (SELECT value FROM json_each(?))` (the convention phase 3 used), skip the extraction restriction when every member is eligible, and cache each view's JSON binding (Task 8). `test_temp_table_filled_once_per_cache_key` becomes `test_eligible_extraction_binding_is_built_once_per_view`.
- **Index signatures corrected.** `EligibilityView.allows(dependency_ids)` takes no `kinds` (each persisted set names exactly one revision, and exclusions are closed under dependency); `SnapshotCatalog.get(tx, snapshot_id)` reads inside the caller's transaction; `EligibilityCache` holds `CompiledEligibility` (a hit must return edition warnings and historical kinds too); `GraphBinding` carries the generation receipt until phase 6; `RankedOwner` carries `window_ids`; `LexicalSearch` gains `exact` and a `stopwords` keyword; `ChannelSelection` gains `graph_strategies`; `EligibilityView` gains `all_members_eligible`; `CachedSelection` lives in `retrieval/cached_selection.py` (importing `retrieval.segments` from `retrieval/cache.py` would close an import cycle through `adapters/voyage/runtime.py`). Task 21 writes these into the index.
- **Schema additions beyond the outline.** `vector_records.group_id` (a text record's persisted context-group set is otherwise not addressable from SQL), `representation_dependencies(dependency_id, owner_id)` (exclusion closures read dependents) and `evidence_units(declared_review_state, extraction_id)` (exclusion seeds read only units in excluded states). All DDL lands in Task 4, so the checksum moves once.
- **Use overrides.** There is no `SourceRepository`; `set_allowed_uses_override` is a `CorpusStore` policy write beside `withdraw_source`, with a public caller (`kb.sources.set_allowed_uses`) so the dead-code gate passes. An unknown use is `policy.use_unknown`, not `filter.unknown_field`.
- **Retained slots.** `RetainedSlot`, `CorpusStore.snapshot_slots`, `RetainedBatchCache` and `load_retained_slots` are deleted (Task 5); `RetainedBatch` and `iter_snapshot_batches` stay for native recovery, which verifies one batch at a time.
- **Chroma placement.** Chroma and Ladybug calls of the query path ran on `asyncio.to_thread` threads (`application/query.py:828`, `:985`, `:1105`, `:1239`); they move to the writer, interactive (Task 1). Partitioned upserts are issued by `SourceService` before `finalize` from `ProjectionCoordinator.native_batches` (Task 9); `finalize` then finds every record present and builds the receipt.
- **Inspection and discovery stay on the writer thread** with a v2 view compiled inside `store.uow.read()`; moving them to the read pool is phase 5's endpoint work (spec §4.15).
- **Two configuration helpers move down a layer.** `application/collection_policy.py` and `resolve_graph_traversal_policy` move to `configuration/` (Task 7) so the compiler, a retrieval module, imports neither the application layer nor an adapter; six ignore entries disappear with them.
- **File and line references re-verified** at `505e8ff`: `BoundedWorker` is `runtime.py:28-82` (not `:27-60`); there are 167 `run_blocking` sites in `src` (not 165); `QueryRequest` is `contracts/requests.py:352-380`; `Coverage` is `contracts/evidence.py:194-198`; Chroma `search` is `adapters/chroma/index.py:326-492` (not `:306-449`); `load_retained_representation` is `application/reranking.py:115-343` and `prepare_rerank_facts` `:716-828`; `inspection.py`'s `prepare_extraction` call is `:409`; `QueryCaches.get_result`/`put_result` are `retrieval/cache.py:110-115`; graph truncation coverage is `query.py:1003-1012` and edition decisions `:1013-1020`; `record_review` is `adapters/sqlite/policy.py:85-118`. Every other reference was re-checked; see each task's **Files**.

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/tychora/application/runtime.py` | `PriorityWorker`, `ReadPool`, lane constants, `queue_full` | 1, 2 |
| `src/tychora/application/owner.py` | `run_write`, `run_write_sync`, `run_read`, `run_cpu`, `read_boundary`, `catalog`, `eligibility_cache`; SQL-free | 1, 2, 3, 7, 20 |
| `src/tychora/adapters/sqlite/connection.py` | `open_read_connection` | 2 |
| `src/tychora/retrieval/catalog.py` (create) | `SnapshotCatalog` | 3 |
| `src/tychora/contracts/publication.py` | `SnapshotView` without members; `RetainedSlot` removed | 3, 5 |
| `src/tychora/adapters/sqlite/repositories/snapshots.py`, `ports/repositories.py` | `member`, `members`; every new repository port | 3–20 |
| `src/tychora/resources/migrations/0001.sql`, `adapters/sqlite/migrations.py` | review state, use override, group binding, two indexes; checksum | 4 |
| `src/tychora/adapters/sqlite/policy.py`, `ports/corpus.py`, `application/sources.py`, `clients/sync.py`, `clients/async_.py` | `review_current`; `set_allowed_uses_override`; public setter | 4 |
| `src/tychora/adapters/sqlite/eligibility.py` (deleted in 12) | persisted-fact reader, then removed | 5, 12 |
| `src/tychora/adapters/sqlite/retained_slots.py` | one-batch loader for native recovery only | 5 |
| `src/tychora/contracts/eligibility.py` (create) | `RevisionGrant`, `GraphBinding`, `EligibilityView` | 6 |
| `src/tychora/adapters/sqlite/repositories/eligibility.py` (create) | `SqliteEligibilityRepository` | 6, 9, 12 |
| `src/tychora/retrieval/selection.py` (create) | edition selection | 6, 16 |
| `src/tychora/configuration/collection_policy.py`, `configuration/graph_policy.py` (moved) | collection policy parsing; traversal permissions | 7 |
| `src/tychora/retrieval/compiler.py` (create) | `EligibilityCompiler`, keys, digests, `filter_matches` | 7, 11, 16, 17 |
| `src/tychora/retrieval/attempt.py` (create) | `EligibilityAttempt`, `run_pinned_eligibility`, admission predicates | 7, 11, 12 |
| `src/tychora/retrieval/cache.py` | `EligibilityCache`; byte-valued result pool | 7, 12, 13 |
| `src/tychora/retrieval/eligibility.py` (deleted in 12) | legacy compiler during the migration | 6, 7, 12 |
| `src/tychora/ports/search.py` (create), `adapters/sqlite/lexical.py`, `retrieval/lexical_query.py` | ranked-owner ports; joined FTS and exact search | 8, 14 |
| `src/tychora/retrieval/survivors.py` (create) | survivor check and lanes | 8 |
| `src/tychora/adapters/chroma/index.py`, `ports/vectors.py`, `contracts/vectors.py`, `indexing/coordinator.py` | view-filtered search; partitions; `native_batches` | 9 |
| `src/tychora/retrieval/vectors.py` (create) | canonical vector owners; linked visual context | 9 |
| `src/tychora/contracts/corpus.py`, `adapters/sqlite/retained_bindings.py`, `adapters/sqlite/repositories/slices.py` (create), `repositories/representations.py` | `RepresentationSlice`; scopes; `neighbours`, `slice` | 10, 11 |
| `src/tychora/application/reranking.py`, `retrieval/rerank.py`, `retrieval/anchors.py`, `retrieval/segments.py`, `retrieval/response.py` | readers on v2 and slices; `budget_stub` | 10, 18 |
| `src/tychora/retrieval/visuals.py`, `application/inspection.py`, `application/retained_content.py`, `application/discovery.py` | readers on v2 and slices | 11, 12, 19 |
| `src/tychora/contracts/graph.py`, `retrieval/graph_scope.py` (create), `adapters/ladybug/*`, `ports/graph.py`, `retrieval/graph.py` | `GraphScope` | 12 |
| `src/tychora/retrieval/cached_selection.py` (create) | `CachedSelection` | 13 |
| `src/tychora/resources/stopwords/*` (create), `configuration/stopwords.py` (create), `configuration/models.py`, `configuration/resolve.py`, `configuration/indexed_representation.py` | stop-word lists, profile, fingerprint | 14 |
| `src/tychora/contracts/evidence.py`, `retrieval/coverage.py` (create) | truncated and skipped channels; coverage | 15, 16, 17 |
| `src/tychora/contracts/requests.py`, `application/query_plan.py` (create), `evaluation/runner.py`, `interfaces/mcp/tools.py` | request narrowing; `QueryBindings` | 16, 17 |
| `src/tychora/application/query.py`, `application/services.py`, `application/visual.py` | stages; SQL-free | 1–19 |
| `src/tychora/adapters/sqlite/repositories/{assets,inspection,upload_policy,removal}.py`, `adapters/sqlite/bootstrap.py`, `adapters/sqlite/restore_rows.py` (create) | statements moved out of application modules | 19, 20 |
| `src/tychora/contracts/error_reasons.py` | fourteen reasons | 4, 5, 7, 8, 10, 16, 17 |
| `src/tychora/evaluation/bench/harness.py` | E4 assertion | 21 |
| Tests: `tests/runtime/test_lanes.py`, `test_read_pool.py`, `tests/retrieval/test_catalog.py`, `test_persisted_facts.py`, `test_eligibility_v2.py`, `test_eligibility_cache.py`, `test_survivors.py`, `test_vector_selection.py`, `test_graph_scope.py`, `test_result_cache.py`, `test_stopwords.py`, `test_query_plan.py`, `test_query_stages.py`, `tests/storage/test_epoch_invariant.py`, `test_eligibility_repository.py`, `test_representation_slices.py`, `tests/contracts/test_eligibility_view.py`, `tests/formatting/test_budget_stub.py`, `tests/acceptance/test_concurrency.py`, `test_strict_scale.py`, `tests/helpers/eligibility_v1_oracle.py` (create); `tests/retrieval/test_lexical.py`, `test_exact.py`, `test_temporal.py`, `test_degraded.py`, `test_segments.py`, `tests/architecture/test_sql_free_modules.py`, `tests/helpers/{eligibility,publication,corpus}.py` (extend) | tests | 1–21 |

---

### Task 1: Owner lanes and queue priorities

**Files:**
- Modify: `src/tychora/application/runtime.py:28-82` (`BoundedWorker` gains a thread name and the `queue.full` reason; new `PriorityWorker`, `Priority`, lane constants)
- Modify: `src/tychora/application/owner.py:119-331` (`_worker` becomes a `PriorityWorker`; new `_cpu` lane; `run_write`, `run_write_sync`, `run_cpu`; `_blocking_future` becomes `_write_future`), `:526-723` (query authorizations run interactive)
- Modify: every `run_blocking` / `run_blocking_sync` call site (`grep -rn "run_blocking" src/tychora tests` lists 167 sites in `src` and about 600 in `tests`; the rename is mechanical)
- Modify: `src/tychora/application/query.py:561`, `:828`, `:985`, `:1105`, `:1239` (no `asyncio.to_thread`: Chroma and Ladybug calls go to the writer, interactive; the counter preflight goes to the CPU lane)
- Test: `tests/runtime/test_lanes.py` (new); `tests/runtime/test_owner.py:94-121` (extend the bound test with the reason)

**Interfaces:**
- Consumes: `drain_result`, `_sync_result`, `BoundedWorker` (`runtime.py:28-82`), `TychoraError(..., reason=)` (phase 1).
- Produces:
  - `runtime.py`: `type Priority = Literal["interactive", "background"]`; `WRITER_THREAD_PREFIX = "tychora-writer"`, `CPU_THREAD_PREFIX = "tychora-cpu"`, `INTERACTIVE_BOUND = 256`, `BACKGROUND_BOUND = 64`, `CPU_WORKERS = 2`; `queue_full() -> TychoraError`; `BoundedWorker(workers=1, queued=32, *, name="tychora")`; `PriorityWorker(*, interactive=256, background=64)` with `submit(call, priority="background") -> Future[T]`.
  - `owner.py`: `Priority` re-exported; `WorkspaceOwner.run_write(call, ctx, priority="background") -> T` (async), `WorkspaceOwner.run_write_sync(call, ctx, priority="background") -> T`, `WorkspaceOwner.run_cpu(call, ctx) -> T` (async). `run_blocking` and `run_blocking_sync` no longer exist.

- [ ] **Step 1: Write the failing tests**

```python
# tests/runtime/test_lanes.py
"""Writer priorities, queue bounds and the CPU lane of the workspace owner."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from tychora.application.owner import WorkspaceOwner
from tychora.application.runtime import (
    BACKGROUND_BOUND,
    CPU_THREAD_PREFIX,
    INTERACTIVE_BOUND,
    WRITER_THREAD_PREFIX,
    PriorityWorker,
    drain_result,
)
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


def _hold(worker: PriorityWorker):
    started, release = threading.Event(), threading.Event()

    def held():
        started.set()
        assert release.wait(5)

    future = worker.submit(held, "background")
    assert started.wait(1)
    return future, release


def test_interactive_task_runs_before_queued_background_tasks():
    worker = PriorityWorker()
    order: list[str] = []
    held, release = _hold(worker)
    try:
        background = [
            worker.submit(lambda index=index: order.append(f"b{index}"), "background")
            for index in range(3)
        ]
        interactive = worker.submit(lambda: order.append("i"), "interactive")
    finally:
        release.set()
    held.result(5)
    interactive.result(5)
    for future in background:
        future.result(5)
    worker.close()
    assert order == ["i", "b0", "b1", "b2"]


@pytest.mark.parametrize(
    ("priority", "bound"),
    [("interactive", INTERACTIVE_BOUND), ("background", BACKGROUND_BOUND)],
)
def test_queue_bounds_raise_queue_full(priority, bound):
    worker = PriorityWorker()
    held, release = _hold(worker)
    queued = []
    try:
        queued = [worker.submit(lambda: 1, priority) for _ in range(bound)]
        with pytest.raises(TychoraError) as error:
            worker.submit(lambda: 2, priority)
        assert (error.value.code, error.value.reason) == ("WORKSPACE_BUSY", "queue.full")
        assert error.value.retryable
        other = "background" if priority == "interactive" else "interactive"
        queued.append(worker.submit(lambda: 1, other))
    finally:
        release.set()
    held.result(5)
    assert [future.result(5) for future in queued] == [1] * len(queued)
    worker.close()


@pytest.mark.asyncio
async def test_cpu_lane_runs_two_tasks_concurrently(tmp_path, settings):
    barrier = threading.Barrier(2, timeout=5)

    def meet():
        barrier.wait()
        return threading.current_thread().name

    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as owner:
        ctx = ExecutionContext()
        names = await asyncio.gather(owner.run_cpu(meet, ctx), owner.run_cpu(meet, ctx))
    assert all(name.startswith(CPU_THREAD_PREFIX) for name in names)


@pytest.mark.asyncio
async def test_writes_run_on_the_writer_thread(tmp_path, settings):
    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as owner:
        ctx = ExecutionContext()
        names = {
            await owner.run_write(lambda: threading.current_thread().name, ctx),
            await owner.run_write(
                lambda: threading.current_thread().name, ctx, "interactive"
            ),
        }
    assert len(names) == 1
    assert names.pop().startswith(WRITER_THREAD_PREFIX)


def test_native_handles_stay_on_writer_thread():
    """The query service never hands Chroma or Ladybug work to an unowned thread."""
    tree = ast.parse((SOURCE / "application/query.py").read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "to_thread"
    ]
    assert offenders == []


def test_run_blocking_is_gone():
    hits = [
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*.py")
        if "run_blocking" in path.read_text()
    ]
    assert hits == []


@pytest.mark.asyncio
async def test_drained_priority_future_keeps_cancellation_semantics():
    worker = PriorityWorker()
    try:
        assert await drain_result(worker.submit(lambda: 7, "interactive"), ExecutionContext()) == 7
    finally:
        worker.close()
```

In `tests/runtime/test_owner.py:94-121` (`test_queue_bound_rejects_excess_work`), after `assert exc.value.retryable` add:

```python
        assert exc.value.reason == "queue.full"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/runtime/test_lanes.py tests/runtime/test_owner.py::test_queue_bound_rejects_excess_work -v`
Expected: FAIL at collection with `ImportError: cannot import name 'BACKGROUND_BOUND' from 'tychora.application.runtime'`; after Step 3 alone, `test_run_blocking_is_gone` and `test_native_handles_stay_on_writer_thread` still fail until Steps 5 and 6.

- [ ] **Step 3: Add the lanes to `runtime.py`**

At the top of `src/tychora/application/runtime.py`, add `from collections import deque` and `from typing import Any, Literal, cast` (replacing the existing `from typing import Any, cast`). Replace `class BoundedWorker` (`:28-82`) with:

```python
type Priority = Literal["interactive", "background"]

WRITER_THREAD_PREFIX = "tychora-writer"
CPU_THREAD_PREFIX = "tychora-cpu"
INTERACTIVE_BOUND = 256
BACKGROUND_BOUND = 64
CPU_WORKERS = 2


def queue_full() -> TychoraError:
    """The refusal for work beyond a lane's queue bound or after it closed."""
    return TychoraError(
        "WORKSPACE_BUSY",
        "Workspace queue is full or closing; retry shortly",
        retryable=True,
        reason="queue.full",
    )


class BoundedWorker:
    """A finite submission queue in front of a fixed-size thread pool."""

    def __init__(
        self, workers: int = 1, queued: int = 32, *, name: str = "tychora"
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        # The garbage collector clears this reference before it finalizes an
        # abandoned owner; by then the pool's threads exit without new work.
        self._pool_alive = weakref.ref(self._pool)
        self._bound = workers + queued
        self._pending = 0
        self._closed = False
        self._lock = Lock()

    def submit[T](self, call: Callable[[], T]) -> Future[T]:
        with self._lock:
            if self._closed or self._pending >= self._bound:
                raise queue_full()
            self._pending += 1
            try:
                future = self._pool.submit(call)
            except BaseException:
                self._pending -= 1
                raise
        future.add_done_callback(self._finished)
        return future

    def _finished(self, future: Future) -> None:
        with self._lock:
            self._pending -= 1

    def stop_admission(self) -> None:
        with self._lock:
            self._closed = True

    def close(self, cleanup: Callable[[], None] | None = None) -> None:
        # Internal cleanup goes behind all accepted work, bypassing admission.
        with self._lock:
            self._closed = True
            if self._pool_alive() is None:
                # Collected pool: no thread runs submissions, so clean up inline.
                self._pool.shutdown(wait=False)
                if cleanup is not None:
                    cleanup()
                return
            final = self._pool.submit(cleanup) if cleanup is not None else None
        try:
            if final is not None:
                final.result()
        finally:
            self._pool.shutdown(wait=True)


class PriorityWorker(BoundedWorker):
    """One thread that runs queued interactive calls before queued background calls.

    Each submission queues its call under its priority and schedules one pool
    slot; a slot runs the oldest interactive call, else the oldest background
    call, so the order is decided when the thread is free, not at submission.
    A running call is never preempted.
    """

    def __init__(
        self, *, interactive: int = INTERACTIVE_BOUND, background: int = BACKGROUND_BOUND
    ) -> None:
        super().__init__(workers=1, queued=0, name=WRITER_THREAD_PREFIX)
        self._bounds: dict[str, int] = {
            "interactive": interactive,
            "background": background,
        }
        self._queues: dict[str, deque[tuple[Future[Any], Callable[[], Any]]]] = {
            "interactive": deque(),
            "background": deque(),
        }

    def submit[T](
        self, call: Callable[[], T], priority: Priority = "background"
    ) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            queue = self._queues[priority]
            if self._closed or len(queue) >= self._bounds[priority]:
                raise queue_full()
            queue.append((future, call))
            try:
                self._pool.submit(self._run_next)
            except BaseException:
                queue.pop()
                raise
        return future

    def _run_next(self) -> None:
        with self._lock:
            queue = self._queues["interactive"] or self._queues["background"]
            future, call = queue.popleft()
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = call()
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)
```

- [ ] **Step 4: Give the owner its lanes**

In `src/tychora/application/owner.py`:

1. Import `CPU_THREAD_PREFIX`, `CPU_WORKERS`, `Priority` and `PriorityWorker` from `.runtime` next to `BoundedWorker`. `Priority` is used in the signatures below, so `from tychora.application.owner import Priority` works for callers (the index names it there).
2. In `WorkspaceOwner.__init__` (`:128`), replace `self._worker = BoundedWorker()` with:

```python
        self._worker = PriorityWorker()
        self._cpu = BoundedWorker(workers=CPU_WORKERS, queued=64, name=CPU_THREAD_PREFIX)
```

3. In `open` (`:153-170`), stop and close the CPU lane with the others:

```python
                with owner._admission:
                    owner._closing = True
                    owner._worker.stop_admission()
                    owner._cpu.stop_admission()
                    owner._parsers.stop_admission()
                    for lane in owner._providers.values():
                        lane.stop_admission()
                    if owner._local_text is not None:
                        owner._local_text.stop_admission()
                with ExitStack() as cleanup:
                    cleanup.callback(owner._worker.close, owner._close_storage)
                    cleanup.callback(owner._cpu.close)
                    cleanup.callback(owner._parsers.close)
                    for lane in owner._providers.values():
                        cleanup.callback(lane.close)
                    if owner._local_text is not None:
                        cleanup.callback(owner._local_text.close)
```

4. Replace `_blocking_future`, `run_blocking` and `run_blocking_sync` (`:315-331`) with:

```python
    def _write_future[T](
        self, call: Callable[[], T], ctx: ExecutionContext, priority: Priority
    ) -> Future[T]:
        ctx.checkpoint()

        def checked() -> T:
            ctx.checkpoint()
            return call()

        return self._worker.submit(checked, priority)

    async def run_write[T](
        self,
        call: Callable[[], T],
        ctx: ExecutionContext,
        priority: Priority = "background",
    ) -> T:
        """Run ``call`` on the writer thread, which owns the connection and native handles."""
        return await drain_result(self._write_future(call, ctx, priority), ctx)

    def run_write_sync[T](
        self,
        call: Callable[[], T],
        ctx: ExecutionContext,
        priority: Priority = "background",
    ) -> T:
        """Run ``call`` on the writer thread from a thread with no event loop."""
        self._sync_thread()
        return _sync_result(self._write_future(call, ctx, priority), ctx)

    async def run_cpu[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T:
        """Run pure CPU work (tokenising, measuring, decoding) off the writer."""
        ctx.checkpoint()

        def checked() -> T:
            ctx.checkpoint()
            return call()

        return await drain_result(self._cpu.submit(checked), ctx)
```

5. In `run_query_provider` (`:701-723`), authorize on the interactive queue:

```python
        def invoke() -> T:
            ctx.checkpoint()
            self._worker.submit(
                lambda: self.upload_policy.authorize_query(
                    scope, provider, authorization
                ),
                "interactive",
            ).result()
            ctx.checkpoint()
            return call()
```

- [ ] **Step 5: Rename every call site**

Run:

```bash
grep -rl "run_blocking" src/tychora tests | xargs sed -i '' -e 's/run_blocking_sync/run_write_sync/g' -e 's/run_blocking/run_write/g'
```

(On Linux use `sed -i` without the empty suffix.) Then `grep -rn "run_blocking" src/tychora tests` must print nothing. The rename keeps every call on the background queue. Then mark the query-path writer calls interactive, since they run while a user waits: in `src/tychora/application/query.py` add `, "interactive"` as the third argument of each `self.owner.run_write(...)` call (`:548`, `:585`, `:588`, `:651`, `:655`, `:700`, `:724`, `:793`, `:845`, `:1099`, `:1117`, `:1224`, `:1235`); in `src/tychora/retrieval/eligibility.py` (`:823`, `:859`, `:865`, `:878`, `:902`), `src/tychora/retrieval/visuals.py:314`, `src/tychora/application/reranking.py` (`:507`, `:828`) and `src/tychora/application/inspection.py` (`:762`, `:774`, `:786`, `:793`) likewise. Ingestion, maintenance and settings calls stay background.

- [ ] **Step 6: Move Chroma, Ladybug and the counter preflight off ad-hoc threads**

In `src/tychora/application/query.py`:

- `:559-562` (counter preflight) becomes:

```python
        preflight = getattr(self.counter, "ensure_available", None)
        if preflight is not None:
            await self.owner.run_cpu(preflight, ctx)
```

- `:828-836` (`augment`) becomes:

```python
            graph_candidates = await self.owner.run_write(
                lambda: augment(
                    view,
                    fused,
                    self.owner.config,
                    self.graph,
                    ctx,
                    plan.graph_strategies if plan is not None else None,
                ),
                ctx,
                "interactive",
            )
```

- `:985-1000` (`inspect_relations`) becomes the same shape: `checks = await self.owner.run_write(lambda: inspect_relations(<the same arguments>), ctx, "interactive")`, keeping every keyword argument.
- `:1105-1112` and `:1239-1246` (`index.search`) become:

```python
        page = await self.owner.run_write(
            lambda: index.search(
                payload, mapping, 80, WorkLimits(deadline=ctx.deadline), ctx
            ),
            ctx,
            "interactive",
        )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/runtime -v`
Expected: PASS.

- [ ] **Step 8: Run the full default suite (mechanical rename)**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS. A failure that names `run_blocking` means a missed site; a deadlock in a test that called `run_write_sync` from the writer thread means that test nested a writer call (rewrite it to call the function directly, as the code it tests does).

- [ ] **Step 9: Commit**

```bash
git add -A src/tychora tests
git commit -m "refactor: split the owner worker into writer and CPU lanes"
```

---

### Task 2: Read pool, read transactions and the boundary guard

**Files:**
- Modify: `src/tychora/adapters/sqlite/connection.py` (add `open_read_connection`)
- Modify: `src/tychora/application/runtime.py` (add `ReadPool`, `READ_THREAD_PREFIX`, `READ_BOUND`)
- Modify: `src/tychora/application/owner.py` (`_initialize` opens the pool; `open` closes it before the writer; `run_read`; `read_boundary`)
- Test: `tests/runtime/test_read_pool.py` (new)

**Interfaces:**
- Consumes: Task 1 `BoundedWorker(..., name=)`, `queue_full`; phase 1 `SqliteUnitOfWork(db).read()` (`adapters/sqlite/repositories/unit_of_work.py:93-104`, deferred `BEGIN`, rollback on exit), `typed_storage_errors()` (`unit_of_work.py:37-55`), `ReadTransaction` (`ports/repositories.py:340-348`).
- Produces:
  - `connection.py`: `open_read_connection(path: Path) -> sqlite3.Connection` (URI `mode=ro`, `PRAGMA query_only=ON`, `busy_timeout=2000`; refuses a database not in WAL mode with `INTEGRITY_ERROR` reason `invariant.unexpected`).
  - `runtime.py`: `READ_THREAD_PREFIX = "tychora-read"`, `READ_BOUND = 128`, `class ReadPool(BoundedWorker)` with `ReadPool(connect: Callable[[], Any], *, workers: int, queued: int = READ_BOUND)`, `connection() -> Any` (the calling pool thread's connection, opened on first use) and `close(cleanup=None)` (closes every connection after the threads exit).
  - `owner.py`: `read_boundary() -> AbstractContextManager[None]`; `WorkspaceOwner.run_read(call: Callable[[ReadTransaction], T], ctx) -> T` (async). A read task sees one WAL snapshot; any exception that is not a `TychoraError` leaves as `INTEGRITY_ERROR` reason `invariant.unexpected` with a `diagnostic_id`, logged with its traceback under that ID.

Read connections stay `mode=ro` and `query_only=ON` for their whole life. `query_only` also refuses writes to `temp` tables (checked on SQLite 3.53: `CREATE TEMP TABLE` fails with "attempt to write a readonly database"), and a read task's `ROLLBACK` would drop a temp table created inside it anyway, so no read path uses temp tables: ID lists are bound with `json_each(?)` under `IN (SELECT value FROM json_each(?))`, which SQLite materialises once per statement (Spec gaps 2).

- [ ] **Step 1: Write the failing tests**

```python
# tests/runtime/test_read_pool.py
"""The read pool: WAL snapshots, read-only connections and the boundary guard."""

import asyncio
import logging
import sqlite3
import threading

import pytest

from tychora.application.owner import WorkspaceOwner
from tychora.application.runtime import READ_BOUND, READ_THREAD_PREFIX, ReadPool
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext


@pytest.fixture
def owner(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    settings = resolve_config(path, "test", {})
    with WorkspaceOwner.open(
        tmp_path / "data", settings, RequestScope(principal_id="owner")
    ) as opened:
        yield opened


def _bump(owner):
    owner.db.execute("UPDATE workspaces SET policy_epoch=policy_epoch+1")


@pytest.mark.asyncio
async def test_read_sees_one_wal_snapshot_while_writer_commits(owner):
    ctx = ExecutionContext()

    def read(tx):
        first = tx.workspace.policy_epoch()
        owner.run_write_sync(lambda: _bump(owner), ExecutionContext())
        return first, tx.workspace.policy_epoch(), threading.current_thread().name

    first, second, thread = await owner.run_read(read, ctx)
    assert first == second
    assert thread.startswith(READ_THREAD_PREFIX)
    assert await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ctx) == first + 1


@pytest.mark.asyncio
async def test_read_connection_refuses_writes(owner):
    def write(tx):
        tx.workspace._db.execute("UPDATE workspaces SET policy_epoch=policy_epoch+1")

    with pytest.raises(TychoraError) as error:
        await owner.run_read(write, ExecutionContext())
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "invariant.unexpected",
    )
    assert await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ExecutionContext()) == 0


@pytest.mark.asyncio
async def test_unexpected_exception_becomes_typed_error_with_diagnostic(owner, caplog):
    def malformed(tx):
        return {}["missing column"]

    with caplog.at_level(logging.ERROR), pytest.raises(TychoraError) as error:
        await owner.run_read(malformed, ExecutionContext())
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "invariant.unexpected",
    )
    assert error.value.diagnostic_id
    assert error.value.diagnostic_id in caplog.text
    assert "KeyError" in caplog.text
    # The owner keeps serving reads and writes.
    assert await owner.run_read(lambda tx: tx.workspace.workspace_id(), ExecutionContext())
    assert await owner.run_write(lambda: 1, ExecutionContext()) == 1


@pytest.mark.asyncio
async def test_typed_errors_cross_the_boundary_unchanged(owner):
    def refuse(tx):
        raise TychoraError("NOT_FOUND", "absent", reason="snapshot.absent")

    with pytest.raises(TychoraError) as error:
        await owner.run_read(refuse, ExecutionContext())
    assert (error.value.reason, error.value.diagnostic_id) == ("snapshot.absent", None)


def test_read_pool_bound_128():
    started = threading.Barrier(3, timeout=5)
    release = threading.Event()
    pool = ReadPool(lambda: sqlite3.connect(":memory:", check_same_thread=False), workers=2)

    def held():
        started.wait()
        assert release.wait(5)

    running = [pool.submit(held) for _ in range(2)]
    started.wait()
    queued = [pool.submit(lambda: 1) for _ in range(READ_BOUND)]
    try:
        with pytest.raises(TychoraError) as error:
            pool.submit(lambda: 2)
        assert error.value.reason == "queue.full"
    finally:
        release.set()
    for future in (*running, *queued):
        future.result(5)
    pool.close()


def test_each_pool_thread_holds_its_own_connection():
    pool = ReadPool(lambda: sqlite3.connect(":memory:", check_same_thread=False), workers=2)
    barrier = threading.Barrier(2, timeout=5)

    def identity():
        barrier.wait()
        return id(pool.connection()), id(pool.connection())

    first, second = (pool.submit(identity) for _ in range(2))
    a, b = first.result(5), second.result(5)
    pool.close()
    assert a[0] == a[1] and b[0] == b[1] and a[0] != b[0]


@pytest.mark.asyncio
async def test_reads_run_concurrently_with_a_long_write(owner):
    entered, release = threading.Event(), threading.Event()

    def long_write():
        entered.set()
        assert release.wait(5)

    write = asyncio.create_task(owner.run_write(long_write, ExecutionContext()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert await owner.run_read(lambda tx: tx.workspace.policy_epoch(), ExecutionContext()) == 0
    finally:
        release.set()
        await write
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/runtime/test_read_pool.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'READ_BOUND' from 'tychora.application.runtime'`.

- [ ] **Step 3: Add `open_read_connection`**

Append to `src/tychora/adapters/sqlite/connection.py`:

```python
def open_read_connection(path: Path) -> sqlite3.Connection:
    """Open a read-only WAL connection for one read-pool thread.

    The URI's ``mode=ro`` refuses writes to the database file and
    ``query_only`` refuses every write, including to ``temp``. The writer has
    already verified the schema, so only the journal mode is checked here.
    The pool closes the connection from its closing thread after its worker
    exits, hence ``check_same_thread=False``.
    """
    db = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=2000")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Read connections require a WAL database",
                reason="invariant.unexpected",
            )
        return db
    except BaseException:
        db.close()
        raise
```

- [ ] **Step 4: Add `ReadPool` to `runtime.py`**

Add `import threading` to the imports and, after `PriorityWorker`:

```python
READ_THREAD_PREFIX = "tychora-read"
READ_BOUND = 128


class ReadPool(BoundedWorker):
    """Read workers that each hold one connection opened on first use.

    A connection belongs to the thread that opened it; ``close`` closes them
    all once every worker has exited.
    """

    def __init__(
        self, connect: Callable[[], Any], *, workers: int, queued: int = READ_BOUND
    ) -> None:
        super().__init__(workers=workers, queued=queued, name=READ_THREAD_PREFIX)
        self._connect = connect
        self._local = threading.local()
        self._connections: list[Any] = []

    def connection(self) -> Any:
        """Return the calling pool thread's connection, opening it once."""
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._connect()
            with self._lock:
                self._connections.append(db)
            self._local.db = db
        return db

    def close(self, cleanup: Callable[[], None] | None = None) -> None:
        try:
            super().close(cleanup)
        finally:
            with self._lock:
                connections, self._connections = self._connections, []
            for db in connections:
                db.close()
```

- [ ] **Step 5: Open the pool and add `run_read` to the owner**

In `src/tychora/application/owner.py`:

1. Imports: `import logging`; `from contextlib import AbstractContextManager` (with the existing `ExitStack, contextmanager, suppress`); `open_read_connection` from `tychora.adapters.sqlite.connection`; `SqliteUnitOfWork, typed_storage_errors` from `tychora.adapters.sqlite.repositories`; `ReadTransaction` from `tychora.ports.repositories`; `ReadPool` from `.runtime`. Add `_LOG = logging.getLogger(__name__)` under `MAX_PARSER_OUTPUT_BYTES`.
2. Add the guard after `_sync_result`:

```python
@contextmanager
def read_boundary() -> Iterator[None]:
    """Let only typed errors leave a read task.

    SQLite failures become their typed storage errors; any other exception is
    a defect, logged with its traceback under a fresh diagnostic ID that the
    caller receives with ``INTEGRITY_ERROR`` reason ``invariant.unexpected``.
    """
    try:
        with typed_storage_errors():
            yield
    except TychoraError:
        raise
    except Exception as error:
        diagnostic = uuid4().hex
        _LOG.error("Unexpected read failure [%s]", diagnostic, exc_info=error)
        raise TychoraError(
            "INTEGRITY_ERROR",
            f"Unexpected read failure; report diagnostic {diagnostic}.",
            reason="invariant.unexpected",
            diagnostic_id=diagnostic,
        ) from None
```

3. In `__init__`, add `self._reads: ReadPool | None = None`.
4. At the end of `_initialize` (after `self.store = CorpusStore(...)`):

```python
        self._reads = ReadPool(
            lambda: open_read_connection(database),
            workers=max(1, min(4, os.cpu_count() or 1)),
        )
```

5. In `open`'s `finally`, stop admission with the others (`if owner._reads is not None: owner._reads.stop_admission()`) and, inside the `ExitStack`, register `cleanup.callback(owner._reads.close)` right after `cleanup.callback(owner._worker.close, owner._close_storage)` when `owner._reads is not None`, so read connections close before the writer's.
6. Add after `run_cpu`:

```python
    async def run_read[T](
        self, call: Callable[[ReadTransaction], T], ctx: ExecutionContext
    ) -> T:
        """Run ``call`` in one deferred read transaction on a read-pool connection.

        The transaction sees one WAL snapshot and rolls back on exit.
        """
        ctx.checkpoint()
        reads = self._reads
        if reads is None:
            raise queue_full()

        def task() -> T:
            ctx.checkpoint()
            with read_boundary(), SqliteUnitOfWork(reads.connection()).read() as tx:
                return call(tx)

        return await drain_result(reads.submit(task), ctx)
```

(`queue_full` is imported from `.runtime`.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/runtime -v`
Expected: PASS.

- [ ] **Step 7: Run the storage and runtime suites**

Run: `uv run --locked pytest tests/runtime tests/storage tests/operations -q`
Expected: PASS (the pool opens lazily per thread, so workspaces that never read through it are unaffected).

- [ ] **Step 8: Commit**

```bash
git add src/tychora/adapters/sqlite/connection.py src/tychora/application/runtime.py src/tychora/application/owner.py tests/runtime/test_read_pool.py
git commit -m "feat: add a WAL read pool behind run_read"
```

---

### Task 3: Snapshot catalog and members out of `SnapshotView` (Q2)

`pin_snapshot` already leases only the snapshot ID (`adapters/sqlite/corpus.py:194-199`, phase 2) and `tests/operations/test_snapshot_gc.py::test_leased_snapshot_is_retained` pins it, so this task does not touch leases. It removes the corpus-sized `members` tuple from every `SnapshotView`, caches views by ID, and makes a query resolve its snapshot once.

**Files:**
- Create: `src/tychora/retrieval/catalog.py`
- Modify: `src/tychora/contracts/publication.py:312-327` (`SnapshotView` loses `members` and `lexical_statistics_epoch`)
- Modify: `src/tychora/ports/repositories.py:268-319` (`SnapshotRepository.member`, `SnapshotRepository.members`), `src/tychora/adapters/sqlite/repositories/snapshots.py:93-154` (`view` without members; `member`, `members`)
- Modify: `src/tychora/application/owner.py` (`WorkspaceOwner.catalog`), `src/tychora/application/snapshot_gc.py:66-76` (discard collected views)
- Modify: `src/tychora/retrieval/eligibility.py:197-209` (`EligibilityReader.read_eligibility_facts(snapshot, members=None)`), `:369-410` (`compile_attempt(..., members=None)`), `:793-912` (`EligibilityAttempt.snapshot`; `run_pinned_eligibility` resolves through the catalog)
- Modify: `src/tychora/adapters/sqlite/corpus.py:114-137` and `src/tychora/adapters/sqlite/eligibility.py:362-381` (members passed or paged)
- Modify: `src/tychora/application/query.py:298-304` (`_validate_provider_families(snapshot: SnapshotView)`), `:583-586`
- Modify: `src/tychora/application/inspection.py:80-114`, `:212-219`, `:368-386`, `:576-592`, `:634-697`, `:740-751`; `src/tychora/application/retained_content.py:99-107`; `src/tychora/application/discovery.py:1026-1030`; `src/tychora/evaluation/provenance.py:97-101`; `src/tychora/indexing/coordinator.py:1563-1586`
- Test: `tests/retrieval/test_catalog.py` (new); `tests/helpers/owner.py` (new); `tests/helpers/publication.py` (add `members_of`); every test that reads `<view>.members` (`grep -rn "\.members\b" tests | grep -v "page\.\|pages\[\|new_pages"` lists them: `tests/evaluation/test_temporal_scenarios.py`, `tests/evaluation/test_provenance.py`, `tests/indexing/test_sources.py`) and `tests/retrieval/test_temporal.py:126` (constructs a `SnapshotView`)

**Interfaces:**
- Consumes: Task 2 `run_read`; phase 2 `SnapshotRepository.view`, `active_id`, `Cursor`.
- Produces:
  - `SnapshotView(snapshot_id, workspace_id, parent_snapshot_id, config_sha, membership_sha, member_count, page_ids, families)` with `family(slot)`; no `members`, no `lexical_statistics_epoch`.
  - `SnapshotRepository.member(snapshot_id: str, extraction_id: str) -> SnapshotMember | None`; `SnapshotRepository.members(snapshot_id: str, *, after: Cursor | None = None, limit: int = 1000) -> tuple[tuple[SnapshotMember, ...], Cursor | None]` (keyset on `(source_id, revision_id, extraction_id)`; the cursor is `None` after the last page).
  - `retrieval/catalog.py`: `CATALOG_SIZE = 32`; `class SnapshotCatalog` with `get(tx: ReadTransaction, snapshot_id: str) -> SnapshotView` and `discard(snapshot_ids: Iterable[str]) -> None`. (The index writes `get(snapshot_id)`; a view is read inside the caller's read transaction, so `tx` is the first parameter. Task 21 updates the index.)
  - `WorkspaceOwner.catalog: SnapshotCatalog`.
  - `EligibilityAttempt.snapshot: SnapshotView | None`; `EligibilityCompiler.compile_attempt(snapshot, scope, request, config, *, members: tuple[SnapshotMember, ...] | None = None, _retained_inspection=False)`.
  - `tests/helpers/publication.py`: `members_of(store, view) -> tuple[SnapshotMember, ...]` (call it on the owner's writer thread).
  - `tests/helpers/owner.py`: `on_owner(owner, call) -> T`, `owner_rows(owner, sql, args=()) -> list[tuple]`, `owner_row(owner, sql, args=()) -> tuple | None`. The owner's connection is bound to its writer thread, so a test never executes on `owner.db` or opens `owner.store.uow` from the test thread; it goes through these helpers (later tasks' tests use them).

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_catalog.py
"""Snapshot views come from SQL once per snapshot and carry no members."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner
from tests.helpers.publication import members_of
from tychora import Tychora
from tychora.adapters.files.cas import LocalArtifacts
from tychora.adapters.sqlite.repositories import SqliteSnapshotRepository
from tychora.contracts.publication import SnapshotView
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.catalog import CATALOG_SIZE, SnapshotCatalog


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        _add(opened, "a.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        _add(opened, "b.md", "# Valves\n\nReplace worn valves yearly.\n")
        yield opened


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_snapshot_view_has_no_members():
    assert "members" not in SnapshotView.model_fields
    assert "lexical_statistics_epoch" not in SnapshotView.model_fields


def test_catalog_builds_view_from_sql_without_artifacts(kb, monkeypatch):
    owner = kb._workspace.owner

    def refuse(self, ref):
        raise AssertionError("the catalog opened an artifact")

    monkeypatch.setattr(LocalArtifacts, "open_verified", refuse)
    catalog = SnapshotCatalog()

    def read(tx):
        return catalog.get(tx, tx.snapshots.active_id())

    view = _read(kb, read)
    assert view.member_count == 2
    assert len(on_owner(owner, lambda: members_of(owner.store, view))) == 2


def test_catalog_reuses_views_and_evicts_least_recent(kb, monkeypatch):
    calls = []
    original = SqliteSnapshotRepository.view

    def counted(self, snapshot_id):
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SqliteSnapshotRepository, "view", counted)
    catalog = SnapshotCatalog(capacity=1)

    def read(tx):
        active = tx.snapshots.active_id()
        first = catalog.get(tx, active)
        again = catalog.get(tx, active)
        return first, again

    first, again = _read(kb, read)
    assert first is again
    assert len(calls) == 1
    catalog.discard((first.snapshot_id,))
    _read(kb, read)
    assert len(calls) == 2
    assert CATALOG_SIZE == 32


def test_query_resolves_snapshot_once(kb, monkeypatch):
    calls = []
    original = SqliteSnapshotRepository.view

    def counted(self, snapshot_id):
        calls.append(snapshot_id)
        return original(self, snapshot_id)

    monkeypatch.setattr(SqliteSnapshotRepository, "view", counted)
    kb.search({"question": "pump seal"})
    assert len(calls) == 1
    kb.search({"question": "worn valves"})
    assert len(calls) == 1


def test_members_page_by_keyset(kb):
    owner = kb._workspace.owner

    def read():
        with owner.store.uow.read() as tx:
            active = tx.snapshots.active_id()
            first, cursor = tx.snapshots.members(active, limit=1)
            second, end = tx.snapshots.members(active, after=cursor, limit=1)
            member = tx.snapshots.member(active, second[0].extraction_id)
            missing = tx.snapshots.member(active, "missing")
        return first, cursor, second, end, member, missing

    first, cursor, second, end, member, missing = on_owner(owner, read)
    assert cursor is not None and end is None
    assert missing is None
    assert len(first) == len(second) == 1
    assert first[0] != second[0]
    assert member == second[0]
```

Create `tests/helpers/owner.py`:

```python
"""Run calls and statements on the workspace owner's writer thread from a test."""

from collections.abc import Callable, Sequence
from typing import Any

from tychora.application.owner import WorkspaceOwner
from tychora.ports.execution import ExecutionContext


def on_owner[T](owner: WorkspaceOwner, call: Callable[[], T]) -> T:
    """Run `call` on the writer thread, which owns the connection."""
    return owner.run_write_sync(call, ExecutionContext())


def owner_rows(owner: WorkspaceOwner, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
    """Every row of one statement, executed on the writer thread."""
    return on_owner(owner, lambda: [tuple(row) for row in owner.db.execute(sql, args)])


def owner_row(owner: WorkspaceOwner, sql: str, args: Sequence[Any] = ()) -> tuple | None:
    """The first row of one statement, executed on the writer thread."""
    rows = owner_rows(owner, sql, args)
    return rows[0] if rows else None
```

In `tests/helpers/publication.py`, add (with `from tychora.contracts.publication import SnapshotMember, SnapshotView` and `from tychora.ports.repositories import Cursor`):

```python
def members_of(store: CorpusStore, view: SnapshotView) -> tuple[SnapshotMember, ...]:
    """Every member of a published snapshot, read page by page."""
    found: list[SnapshotMember] = []
    cursor: Cursor | None = None
    with store.uow.read() as tx:
        while True:
            page, cursor = tx.snapshots.members(view.snapshot_id, after=cursor)
            found.extend(page)
            if cursor is None:
                return tuple(found)
```

In the test files that read `<view>.members`, replace each `X.members` with `members_of(<the store at hand>, X)` (for example `owner.store.resolve_snapshot(result.snapshot_id).members[0]` becomes `members_of(owner.store, owner.store.resolve_snapshot(result.snapshot_id))[0]`; in `tests/indexing/test_sources.py` the store is `service.owner.store` or `owner.store` as each test names it). In `tests/retrieval/test_temporal.py:126`, drop the `members=` argument from `SnapshotView(...)` and pass the members to the reader call that follows as `members=(...)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_catalog.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'tychora.retrieval.catalog'`.

- [ ] **Step 3: Remove members from `SnapshotView`**

In `src/tychora/contracts/publication.py`, replace `class SnapshotView` (`:313-328`) with:

```python
class SnapshotView(Contract):
    """A published snapshot as read back from SQL rows, without its members.

    Members are read in pages through ``SnapshotRepository.members``.
    """

    snapshot_id: Identifier
    workspace_id: Identifier
    parent_snapshot_id: Identifier | None = None
    config_sha: Sha256
    membership_sha: Sha256
    member_count: NonNegativeInt
    page_ids: tuple[Identifier, ...]
    families: tuple[SlotFamily, ...]

    def family(self, slot: str) -> SlotFamily | None:
        """Return the family serving ``slot``, or ``None`` when the slot is absent."""
        return next((item for item in self.families if item.slot == slot), None)
```

- [ ] **Step 4: Page members through the repository**

In `src/tychora/ports/repositories.py`, import `SnapshotMember` from `tychora.contracts.publication` and add to `SnapshotRepository` after `view`:

```python
    def member(self, snapshot_id: str, extraction_id: str) -> SnapshotMember | None:
        """Return the member that selects this extraction, or ``None``."""
        ...

    def members(
        self, snapshot_id: str, *, after: Cursor | None = None, limit: int = 1000
    ) -> tuple[tuple[SnapshotMember, ...], Cursor | None]:
        """Return one page of members in source, revision and extraction order.

        The returned cursor continues after the page; it is ``None`` after the last.
        """
        ...
```

In `src/tychora/adapters/sqlite/repositories/snapshots.py`, import `Cursor` from `tychora.ports.repositories`, replace `view` and `_members` (`:93-154`) with:

```python
    def view(self, snapshot_id: str) -> SnapshotView:
        """Read a published snapshot from its rows; no artifact is opened."""
        row = self._db.execute(_SNAPSHOT_ROW, (snapshot_id,)).fetchone()
        if row is None:
            raise absent_snapshot()
        page_ids = tuple(
            r[0]
            for r in self._db.execute(
                "SELECT page_id FROM snapshot_pages WHERE snapshot_id=? ORDER BY bucket",
                (snapshot_id,),
            )
        )
        (count,) = self._db.execute(
            "SELECT COUNT(*) FROM snapshot_members WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if membership_sha(page_ids) != row[2] or count != row[4]:
            raise _integrity("Snapshot rows differ from their membership digest")
        return SnapshotView(
            snapshot_id=snapshot_id,
            workspace_id=row[0],
            parent_snapshot_id=row[1],
            config_sha=row[3],
            membership_sha=row[2],
            member_count=row[4],
            page_ids=page_ids,
            families=self._families(snapshot_id),
        )

    def member(self, snapshot_id: str, extraction_id: str) -> SnapshotMember | None:
        """Return the member that selects this extraction, or ``None``."""
        row = self._db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members "
            "WHERE snapshot_id=? AND extraction_id=?",
            (snapshot_id, extraction_id),
        ).fetchone()
        return None if row is None else _member(row)

    def members(
        self, snapshot_id: str, *, after: Cursor | None = None, limit: int = 1000
    ) -> tuple[tuple[SnapshotMember, ...], Cursor | None]:
        """Return one page of members in source, revision and extraction order."""
        if not 1 <= limit <= 10_000:
            raise TychoraError(
                "INVALID_REQUEST", "Member page size must be 1..10000", reason="cursor.invalid"
            )
        start = ("", "", "") if after is None else (*map(str, after.sort_key), after.last_id)
        if len(start) != 3:
            raise TychoraError("INVALID_REQUEST", "Cursor is invalid.", reason="cursor.invalid")
        rows = self._db.execute(
            "SELECT source_id,revision_id,extraction_id,is_default FROM snapshot_members "
            "WHERE snapshot_id=? AND (source_id,revision_id,extraction_id)>(?,?,?) "
            "ORDER BY source_id,revision_id,extraction_id LIMIT ?",
            (snapshot_id, *start, limit + 1),
        ).fetchall()
        page = tuple(_member(row) for row in rows[:limit])
        if len(rows) <= limit:
            return page, None
        last = page[-1]
        return page, Cursor(
            sort_key=(last.source_id, last.revision_id), last_id=last.extraction_id
        )
```

and add the module-level helper next to `absent_snapshot`:

```python
def _member(row: tuple) -> SnapshotMember:
    return SnapshotMember(
        source_id=row[0], revision_id=row[1], extraction_id=row[2], is_default=bool(row[3])
    )
```

- [ ] **Step 5: Add the catalog and give it to the owner**

```python
# src/tychora/retrieval/catalog.py
"""Published snapshot views by ID, read from SQL once while they stay cached."""

from collections import OrderedDict
from collections.abc import Iterable
from threading import Lock

from tychora.contracts.publication import SnapshotView
from tychora.ports.repositories import ReadTransaction

CATALOG_SIZE = 32


class SnapshotCatalog:
    """A thread-safe LRU of snapshot views; a published snapshot never changes.

    Collection removes a snapshot's rows, so the collector discards its view.
    """

    def __init__(self, capacity: int = CATALOG_SIZE) -> None:
        self._capacity = capacity
        self._views: OrderedDict[str, SnapshotView] = OrderedDict()
        self._lock = Lock()

    def get(self, tx: ReadTransaction, snapshot_id: str) -> SnapshotView:
        """Return the cached view, or read it from SQL in ``tx`` and cache it."""
        with self._lock:
            view = self._views.get(snapshot_id)
            if view is not None:
                self._views.move_to_end(snapshot_id)
                return view
        view = tx.snapshots.view(snapshot_id)
        with self._lock:
            self._views[snapshot_id] = view
            self._views.move_to_end(snapshot_id)
            while len(self._views) > self._capacity:
                self._views.popitem(last=False)
        return view

    def discard(self, snapshot_ids: Iterable[str]) -> None:
        """Forget the views of collected snapshots; unknown IDs are ignored."""
        with self._lock:
            for snapshot_id in snapshot_ids:
                self._views.pop(snapshot_id, None)
```

In `WorkspaceOwner.__init__`, add `self.catalog = SnapshotCatalog()` (import from `tychora.retrieval.catalog`). In `src/tychora/application/snapshot_gc.py:66-73`, after the `with store.uow.write() as tx:` block that produces `report`, add `self.owner.catalog.discard(report.snapshots)`.

- [ ] **Step 6: Pass members explicitly to the eligibility reader**

In `src/tychora/retrieval/eligibility.py`:

- `EligibilityReader.read_eligibility_facts` (`:203`) becomes `def read_eligibility_facts(self, snapshot: SnapshotView, members: tuple[SnapshotMember, ...] | None = None) -> EligibilityFacts: ...` (import `SnapshotMember` with `SnapshotView`).
- `compile_eligibility` and `compile_attempt` gain a keyword `members: tuple[SnapshotMember, ...] | None = None` and call `self.reader.read_eligibility_facts(snapshot, members)`.
- `EligibilityAttempt` gains `snapshot: SnapshotView | None = None` (after `ctx`).
- `run_pinned_eligibility` (`:829-912`) resolves through the catalog when an owner is given. Replace the body from `pin = compiler.reader.pin_snapshot(selector)` to the end with:

```python
    if owner is not None and ctx is not None:
        catalog = owner.catalog

        def resolve(tx) -> SnapshotView:
            snapshot_id = selector or tx.snapshots.active_id()
            if snapshot_id is None:
                raise TychoraError(
                    "NOT_FOUND",
                    "No published snapshot is available",
                    reason="snapshot.absent",
                )
            return catalog.get(tx, snapshot_id)

        snapshot = await owner.run_read(resolve, ctx)
        pin = owner.store.leases.pin(frozenset({snapshot.snapshot_id}))
    else:
        pin = compiler.reader.pin_snapshot(selector)
        snapshot = None
    with pin as pinned:
        snapshot = snapshot or pinned
        for attempt_number in range(2):
            compiled = (
                await owner.run_write(
                    lambda: compiler.compile_attempt(snapshot, scope, request, config),
                    ctx,
                    "interactive",
                )
                if owner is not None and ctx is not None
                else compiler.compile_attempt(snapshot, scope, request, config)
            )
            attempt = replace(compiled, owner=owner, ctx=ctx, snapshot=snapshot)
            completed_operation = False
            try:
                result = await operation(attempt)
                completed_operation = True
                if owner is not None and ctx is not None:
                    await owner.run_write(
                        lambda view=attempt.view: compiler.revalidate_epoch(
                            view, "delivery"
                        ),
                        ctx,
                        "interactive",
                    )
                else:
                    compiler.revalidate_epoch(attempt.view, "delivery")
            except TychoraError as error:
                if (
                    error.code == "SCOPE_CHANGED"
                    and completed_operation
                    and on_invalidated is not None
                ):
                    await on_invalidated(result)
                if error.code != "SCOPE_CHANGED" or attempt_number:
                    raise
                continue
            return result
        raise AssertionError("Unreachable eligibility retry state")
```

(The lease is an in-memory `LeaseSet` entry, safe to take and release on the event-loop thread; GC reads `leases.pinned()` on the writer. Between the read that resolved the ID and the pin, retention keeps the newest 16 snapshots, so the resolved snapshot cannot be collected.)

In `src/tychora/adapters/sqlite/corpus.py:114`, `read_eligibility_facts(self, snapshot, members=None)` passes `members` through. In `src/tychora/adapters/sqlite/eligibility.py`, `read_eligibility_facts(store, snapshot, members=None)` replaces `for member in snapshot.members:` (`:381`) with `for member in members if members is not None else _all_members(store, snapshot.snapshot_id):` and gains:

```python
def _all_members(store: CorpusStore, snapshot_id: str) -> Iterator[SnapshotMember]:
    """Every member of the snapshot, page by page, inside the open read."""
    cursor = None
    with store.uow.read() as tx:
        while True:
            page, cursor = tx.snapshots.members(snapshot_id, after=cursor)
            yield from page
            if cursor is None:
                return
```

(`store.uow.read()` joins the transaction `read_eligibility_facts` opened.)

- [ ] **Step 7: Resolve once in the query service**

In `src/tychora/application/query.py`:

- `_validate_provider_families(self, snapshot_id: str)` (`:298-304`) becomes `_validate_provider_families(self, snapshot: SnapshotView)`; delete `snapshot = self.owner.store.resolve_snapshot(snapshot_id)` and the function-body `from collections.abc import Mapping` (import `Mapping` at module level with `Awaitable, Callable`).
- In `operation` (`:584-586`): `await self.owner.run_write(lambda: self._validate_provider_families(attempt.snapshot), ctx, "interactive")`.

- [ ] **Step 8: Move the other member readers**

- `src/tychora/application/inspection.py`: add at module level

```python
@dataclass(frozen=True)
class RetainedPin:
    """A pinned active snapshot narrowed to the retained members one read covers."""

    view: SnapshotView
    members: tuple[SnapshotMember, ...]
```

`_pin_retained` (`:690-697`) yields `RetainedPin(view=active.model_copy(update={"snapshot_id": membership, "membership_sha": membership, "member_count": len(members), "page_ids": (), "families": ()}), members=members)`. `_attempt(self, snapshot, scope, members=None)` passes `members=members` to `compile_attempt`. In `_inspect_sync` (`:740-751`) the block becomes `with self._pin_retained(request) as pin:` with `attempt = self._attempt(pin.view, granted, pin.members)` and each `_inspect_*` receiving `pin` instead of `snapshot`; inside them `snapshot.members` becomes `pin.members` (`:214`, `:381`, `:589`). `_relations_sync` (`:803-805`) keeps `with self.owner.store.pin_snapshot(None) as snapshot:` and `self._attempt(snapshot, granted)`.
- `src/tychora/application/retained_content.py:99-107`: `with service._pin_retained(pin_request) as pin:`, `member = pin.members[0]`, `attempt = service._attempt(pin.view, granted, pin.members)`.
- `src/tychora/application/discovery.py:1026-1027`:

```python
        with owner.store.pin_snapshot(None) as snapshot:
            with owner.store.uow.read() as tx:
                selected = tx.snapshots.member(snapshot.snapshot_id, extraction)
            if selected is None:
```

- `src/tychora/evaluation/provenance.py:97-102`: drop `self.members`; `_load` reads

```python
        with self.owner.store.uow.read() as tx:
            member = tx.snapshots.member(self.snapshot_id, extraction_id)
```

(and `__init__` no longer sets `self.members`).
- `src/tychora/indexing/coordinator.py:1563-1586`, `_snapshot_members(base, page)` reads the base's members in pages:

```python
    def _snapshot_members(
        self, base: SnapshotView | None, page: MembershipPage
    ) -> tuple[SnapshotMember, ...]:
        """The members of the planned snapshot: the base outside the page's bucket."""
        kept: list[SnapshotMember] = []
        if base is not None:
            cursor = None
            with self.store.uow.read() as tx:
                while True:
                    rows, cursor = tx.snapshots.members(base.snapshot_id, after=cursor)
                    kept.extend(m for m in rows if bucket_of(m.source_id) != page.bucket)
                    if cursor is None:
                        break
        added = [
            SnapshotMember(
                source_id=m.source_id,
                revision_id=m.revision_id,
                extraction_id=m.extraction_id,
                is_default=m.is_default,
            )
            for m in page.members
        ]
        return tuple(
            sorted(kept + added, key=lambda m: (m.source_id, m.revision_id, m.extraction_id))
        )
```

(it stops being a `@staticmethod`; the call at `:1473` is unchanged).

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_catalog.py tests/retrieval/test_temporal.py tests/evaluation/test_temporal_scenarios.py tests/evaluation/test_provenance.py tests/indexing/test_sources.py tests/application/test_inspection.py tests/operations/test_snapshot_gc.py -q`
Expected: PASS.

- [ ] **Step 10: Confirm no reader of members is left**

Run: `rg -n "\.members\b" src/tychora | rg -v "page\.members|base_page\.members|kept\.members|p\.members|plan\.new_pages|pin\.members|self\.members for|for m in page\.members"`
Expected: only `contracts/snapshots.py` (page validation) and `contracts/publication.py:190` (`_page_batches`).

- [ ] **Step 11: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add -A src/tychora tests
git commit -m "feat: resolve snapshots once from a SQL-only catalog"
```

---

### Task 4: Review state, use overrides, group bindings and the epoch invariant

This task holds every DDL change of the phase, so `SCHEMA_CHECKSUM` moves once. Phase 4 changes the schema: workspaces created on `main` at `505e8ff` (checksum `be4274cd…`, phase 3) open with `PROFILE_INCOMPATIBLE` reason `workspace.format_old` and are rebuilt with `tychora manage rebuild workspace --from`, as for phases 2 and 3.

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (`sources` `:37-46`, after `representation_dependencies` `:196-200`, after `review_events` `:246-259`, `vector_records` `:305-316`, the index block `:510-516`)
- Modify: `src/tychora/adapters/sqlite/migrations.py:12-24` (`SCHEMA_CHECKSUM`, `EARLIER_FORMAT_TWO_CHECKSUMS`)
- Modify: `src/tychora/adapters/sqlite/policy.py:66-118` (`_bump` through the workspace repository; `review_state` from `review_current`; `record_review` upserts it), add `set_allowed_uses_override`
- Modify: `src/tychora/ports/repositories.py` (`WorkspaceRepository.bump_policy_epoch`; `VectorRecordRow.group_id`), `src/tychora/adapters/sqlite/repositories/workspace.py` (`bump_policy_epoch`), `src/tychora/adapters/sqlite/repositories/batches.py:37`, `:215-228` (`group_id` column)
- Modify: `src/tychora/ports/corpus.py:13-40` (`set_allowed_uses_override`), `src/tychora/indexing/coordinator.py:643-652` (`group_id=b.group_id`)
- Modify: `src/tychora/application/sources.py` (`SourceService.set_allowed_uses`), `src/tychora/clients/sync.py:57-135` and `src/tychora/clients/async_.py:43-130` (`sources.set_allowed_uses`)
- Modify: `src/tychora/contracts/error_reasons.py`, `tests/contracts/test_error_reasons.py` (reasons `policy.use_unknown`, `source.absent`, `scope.capability_missing`)
- Test: `tests/storage/test_epoch_invariant.py` (new); `tests/storage/test_schema.py:497-505` (append the phase-3 checksum); `tests/storage/test_batch_repository.py:86-93`, `:219-225`, `:301-307` (rows gain `group_id`)

**Interfaces:**
- Consumes: phase 1 `SqliteWorkspaceRepository`, `require_write`; phase 3 `VectorRecordBinding.group_id` (`contracts/publication.py:61`).
- Produces:
  - DDL: `review_current(target_kind, target_id, state, review_event_id)`; `sources.allowed_uses_override` (JSON array of uses or `NULL`); `vector_records.group_id` (the context group of a text record, `NULL` for an image record); indexes `representation_dependencies_dependency(dependency_id, owner_id)` and `units_declared_review(declared_review_state, extraction_id)`.
  - `WorkspaceRepository.bump_policy_epoch() -> int`.
  - `CorpusStore.set_allowed_uses_override(source_id: str, override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None) -> None` (stores the narrowed use list, bumps `policy_epoch` in the same transaction).
  - `VectorRecordRow.group_id: Identifier | None = None` (required for chunk rows by a `CHECK`).
  - `SourceService.set_allowed_uses(source_id, override, scope, ctx) -> None` (async; `manage` on the source's collection) and `kb.sources.set_allowed_uses(source_id, override)` on the sync and async clients: the public caller of the store method.
  - Reasons `INVALID_REQUEST` `policy.use_unknown` (the outline named `filter.unknown_field`, which describes filters; a use name is not a filter field), `NOT_FOUND` `source.absent`, `FORBIDDEN_SCOPE` `scope.capability_missing`.
  - `tests/storage/test_epoch_invariant.py`: `ELIGIBILITY_TABLES`, `ELIGIBILITY_INPUT_WRITES` (function → how it keeps the invariant).

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_epoch_invariant.py
"""Every write to an eligibility input moves the policy epoch in its transaction."""

import ast
import re
from pathlib import Path

import pytest

from tests.helpers.corpus import reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import (
    ChunkSpan,
    prepare_extraction,
    representation_facts,
)
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"
ELIGIBILITY_TABLES = frozenset(
    {
        "review_events",
        "review_current",
        "collections",
        "sources",
        "revisions",
        "facet_values",
        "assertion_decisions",
        "kinds",
        "evidence_units",
        "assets",
    }
)
_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+\"?(\w+)",
    re.IGNORECASE,
)
BUMPS = "bumps policy_epoch in the same transaction"
NEW_ROWS = "inserts rows no published snapshot selects yet"
ELIGIBILITY_INPUT_WRITES = {
    "adapters/sqlite/policy.py:PolicyRepository.record_review": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.raise_access_floor": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.withdraw_source": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.record_assertion_decision": BUMPS,
    "adapters/sqlite/policy.py:PolicyRepository.set_allowed_uses_override": BUMPS,
    "adapters/sqlite/workspace_inventory.py:_seed_one": BUMPS + " (seed_collections)",
    "adapters/sqlite/workspace_inventory.py:_restore_source": BUMPS + " (restore_policy)",
    "adapters/sqlite/workspace_inventory.py:_restore_revision": BUMPS + " (restore_policy)",
    "application/collections.py:CollectionService.create.create": BUMPS + " (_mutate)",
    "application/collections.py:CollectionService.edit.edit": BUMPS + " (_mutate)",
    "application/collections.py:CollectionService.set_archived.archive": BUMPS + " (_mutate)",
    "application/consent.py:apply_upload_consent": BUMPS,
    "adapters/sqlite/corpus.py:CorpusStore.commit_extraction": BUMPS
    + " when it registers a collection or source; " + NEW_ROWS,
    "adapters/sqlite/corpus.py:CorpusStore._insert_facets": NEW_ROWS,
    "application/owner.py:CanonicalUploadPolicy.register_captured_source": NEW_ROWS,
    "api.py:LocalWorkspace._ensure_default_collection": "creates the default collection of an empty workspace",
}


def _writers(path: Path) -> set[str]:
    """Qualified names of the functions in ``path`` that write an eligibility table."""
    found: set[str] = set()

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*stack, child.name])
                continue
            texts: list[str] = []
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                texts.append(child.value)
            if isinstance(child, ast.JoinedStr):
                texts.append(
                    "".join(v.value for v in child.values if isinstance(v, ast.Constant))
                )
            if any(
                match.group(1).lower() in ELIGIBILITY_TABLES
                for text in texts
                for match in _WRITE.finditer(text)
            ):
                found.add(".".join(stack))
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"_insert", "_register"}
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and child.args[0].value in ELIGIBILITY_TABLES
            ):
                found.add(".".join(stack))
            visit(child, stack)

    visit(ast.parse(path.read_text()), [])
    return found


def test_every_eligibility_write_is_listed():
    found = {
        f"{path.relative_to(SOURCE)}:{name}"
        for path in SOURCE.rglob("*.py")
        for name in _writers(path)
    }
    assert found == set(ELIGIBILITY_INPUT_WRITES)


def test_listing_guard_finds_a_new_writer(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "class Repo:\n    def lower(self, db):\n"
        "        db.execute('UPDATE sources SET access_floor=0')\n"
    )
    assert _writers(sample) == {"Repo.lower"}


def _facts(bundle):
    return representation_facts(
        bundle,
        prepare_extraction(bundle),
        (ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end) for c in bundle.chunks),
    )


@pytest.fixture
def store(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    corpus = CorpusStore(db, registry=registry(), artifacts=reader())
    yield corpus
    db.close()


@pytest.fixture
def bundle(store):
    seeded = seed_bundle("Seal checks are quarterly.")
    store.commit_extraction(seeded, _facts(seeded))
    return seeded


def _epoch(store):
    return store.db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0]


@pytest.mark.parametrize(
    "write",
    [
        lambda s, b: s.record_review(ReviewTarget(unit_id=b.units[0].unit_id), "accepted", "r"),
        lambda s, b: s.raise_access_floor(ReviewTarget(revision_id=b.revision.revision_id), 2),
        lambda s, b: s.withdraw_source(b.source.source_id),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, "never_generate"),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, ("lookup",)),
        lambda s, b: s.set_allowed_uses_override(b.source.source_id, None),
    ],
)
def test_every_eligibility_write_bumps_policy_epoch(store, bundle, write):
    before = _epoch(store)
    write(store, bundle)
    assert _epoch(store) == before + 1


def test_a_failed_policy_write_moves_nothing(store, bundle):
    before = _epoch(store)
    with pytest.raises(TychoraError):
        store.set_allowed_uses_override(bundle.source.source_id, ("teleport",))
    assert _epoch(store) == before
    row = store.db.execute(
        "SELECT allowed_uses_override FROM sources WHERE source_id=?",
        (bundle.source.source_id,),
    ).fetchone()
    assert row == (None,)


def test_review_current_tracks_latest_event(store, bundle):
    target = ReviewTarget(unit_id=bundle.units[0].unit_id)
    store.record_review(target, "needs_review", "r")
    latest = store.record_review(target, "accepted", "r")
    row = store.db.execute(
        "SELECT state,review_event_id FROM review_current "
        "WHERE target_kind='unit' AND target_id=?",
        (bundle.units[0].unit_id,),
    ).fetchone()
    assert row == ("accepted", latest)
    assert store.review_state(target) == "accepted"
    revision = ReviewTarget(revision_id=bundle.revision.revision_id)
    store.record_review(revision, "rejected", "r")
    assert store.review_state(target) == "rejected"


def test_allowed_uses_override_only_narrows(store, bundle):
    source = bundle.source.source_id

    def stored():
        return store.db.execute(
            "SELECT allowed_uses_override FROM sources WHERE source_id=?", (source,)
        ).fetchone()[0]

    store.set_allowed_uses_override(source, "never_generate")
    assert stored() == '["audit","lookup"]'
    store.set_allowed_uses_override(source, "reference_only")
    assert stored() == '["lookup"]'
    store.set_allowed_uses_override(source, ("audit",))
    assert stored() == '["audit"]'
    store.set_allowed_uses_override(source, None)
    assert stored() is None
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override(source, ("teleport",))
    assert (error.value.code, error.value.reason) == ("INVALID_REQUEST", "policy.use_unknown")
    with pytest.raises(TychoraError) as error:
        store.set_allowed_uses_override("missing", None)
    assert (error.value.code, error.value.reason) == ("NOT_FOUND", "source.absent")


def test_clients_narrow_a_source_with_manage_only(tmp_path):
    from tests.helpers.legacy import OutputCounter
    from tests.helpers.owner import owner_row
    from tychora import Tychora

    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "a.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# A\n\nInspect the seal.\n",
                "metadata": {"title": "A"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        owner = kb._workspace.owner
        (source_id,) = owner_row(owner, "SELECT source_id FROM sources")
        kb.sources.set_allowed_uses(source_id, "reference_only")
        assert owner_row(owner, "SELECT allowed_uses_override FROM sources") == (
            '["lookup"]',
        )
        reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
        with pytest.raises(TychoraError) as error:
            kb._workspace.bridge.call(
                kb._workspace.services.source_jobs.sources.set_allowed_uses(
                    source_id, None, reader, ExecutionContext()
                )
            )
    assert (error.value.code, error.value.reason) == (
        "FORBIDDEN_SCOPE",
        "scope.capability_missing",
    )
```

In `tests/storage/test_schema.py`, append to `MAIN_FORMAT_TWO_CHECKSUMS` (`:497-505`):

```python
    "be4274cd06d8e3fd0e9d566d99e6b78b7ac7c2a0dadb1e1677c2b671d8e32166",  # 4224cc1
```

In `tests/storage/test_batch_repository.py`, every `VectorRecordRow(...)` with a `chunk_id` gains `group_id="g" * 64` (`:88`, `:219`, `:301`).

In `tests/contracts/test_error_reasons.py`, add `"policy.use_unknown"` to `CLASSIFIED_REASONS["INVALID_REQUEST"]`, `"source.absent"` to `CLASSIFIED_REASONS["NOT_FOUND"]` and a new entry `"FORBIDDEN_SCOPE": {"scope.whole_workspace", "scope.capability_missing"}` (replacing the existing one-element set).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_epoch_invariant.py tests/storage/test_schema.py tests/contracts/test_error_reasons.py -v`
Expected: FAIL — `test_every_eligibility_write_is_listed` reports `set_allowed_uses_override` listed but absent; the override tests fail with `AttributeError: 'CorpusStore' object has no attribute 'set_allowed_uses_override'`; `test_every_earlier_main_checksum_reports_format_old[be4274cd…]` fails because the current checksum is still that value.

- [ ] **Step 3: Change the DDL**

In `src/tychora/resources/migrations/0001.sql`:

1. In `CREATE TABLE sources`, after `withdrawn_at TEXT,` add:

```sql
  allowed_uses_override TEXT CHECK(allowed_uses_override IS NULL OR json_valid(allowed_uses_override)),
```

2. After the `review_events` indexes (`:257-259`) add:

```sql
CREATE TABLE review_current (
  target_kind TEXT NOT NULL CHECK(target_kind IN ('revision','unit','asset')),
  target_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('unreviewed','needs_review','accepted','rejected')),
  review_event_id INTEGER NOT NULL REFERENCES review_events(review_event_id),
  PRIMARY KEY(target_kind,target_id)
) STRICT, WITHOUT ROWID;
```

3. In `CREATE TABLE vector_records`, after `row_index INTEGER NOT NULL CHECK(row_index>=0),` add `group_id TEXT,` and after `CHECK((chunk_id IS NOT NULL)+(asset_id IS NOT NULL)=1),` add `CHECK((chunk_id IS NULL)=(group_id IS NULL)),`.

4. In the index block (`:510-516`) add:

```sql
CREATE INDEX representation_dependencies_dependency ON representation_dependencies(dependency_id,owner_id);
CREATE INDEX units_declared_review ON evidence_units(declared_review_state,extraction_id);
```

Compute the new checksum from the raw file (`migration_sql()` refuses a mismatching file, so it cannot be used here):

Run: `uv run --locked python -c "from hashlib import sha256; print(sha256(open('src/tychora/resources/migrations/0001.sql','rb').read()).hexdigest())"`
Expected: one 64-character hex digest, different from `be4274cd…`.

In `src/tychora/adapters/sqlite/migrations.py`, set `SCHEMA_CHECKSUM` to that digest and append `"be4274cd06d8e3fd0e9d566d99e6b78b7ac7c2a0dadb1e1677c2b671d8e32166",` as the last entry of `EARLIER_FORMAT_TWO_CHECKSUMS`.

- [ ] **Step 4: Register the reasons**

In `src/tychora/contracts/error_reasons.py`, after the `filter.unknown_field` entry:

```python
    Reason(
        "INVALID_REQUEST",
        "policy.use_unknown",
        False,
        "Use only the uses the profile's modes define (compose, audit, lookup).",
    ),
```

after the `snapshot.absent` entry:

```python
    Reason(
        "NOT_FOUND",
        "source.absent",
        False,
        "List the collection's sources and use one of their identifiers.",
    ),
```

and after the `scope.whole_workspace` entry:

```python
    Reason(
        "FORBIDDEN_SCOPE",
        "scope.capability_missing",
        False,
        "Ask the workspace owner for the capability this operation needs.",
    ),
```

- [ ] **Step 5: Epoch bump through the workspace repository**

In `src/tychora/ports/repositories.py`, add to `WorkspaceRepository` after `bump_lexical_epoch`:

```python
    def bump_policy_epoch(self) -> int:
        """Advance the policy epoch once and return the new value."""
        ...
```

In `src/tychora/adapters/sqlite/repositories/workspace.py`, after `bump_lexical_epoch`:

```python
    def bump_policy_epoch(self) -> int:
        """Advance the policy epoch once and return the new value."""
        require_write(self._db, self._writable)
        self._row()
        return self._db.execute(
            "UPDATE workspaces SET policy_epoch=policy_epoch+1 RETURNING policy_epoch"
        ).fetchone()[0]
```

- [ ] **Step 6: Maintain `review_current` and add the override**

In `src/tychora/adapters/sqlite/policy.py`:

```python
_TARGET_KINDS = {"revision_id": "revision", "unit_id": "unit", "asset_id": "asset"}
_GENERATION_USE = "compose"
_REFERENCE_USE = "lookup"
```

`_bump` (`:66-70`) becomes:

```python
    def _bump(self, workspace_id: str) -> None:
        workspace = SqliteWorkspaceRepository(self.db, writable=True)
        if workspace.workspace_id() != workspace_id:
            fail("Policy subject belongs to another workspace")
        workspace.bump_policy_epoch()
```

(import `SqliteWorkspaceRepository` from `tychora.adapters.sqlite.repositories`).

In `review_state` (`:72-83`), replace the `review_events` query with:

```python
        current = self.db.execute(
            "SELECT state FROM review_current WHERE target_kind=? AND target_id=?",
            (_TARGET_KINDS[column], identifier),
        ).fetchone()
        return current[0] if current else declared
```

In `record_review`, after `event_id = cursor.lastrowid` and its `None` check, before `return event_id`:

```python
            self.db.execute(
                "INSERT INTO review_current(target_kind,target_id,state,review_event_id) "
                "VALUES (?,?,?,?) ON CONFLICT(target_kind,target_id) DO UPDATE SET "
                "state=excluded.state,review_event_id=excluded.review_event_id",
                (_TARGET_KINDS[column], identifier, state, event_id),
            )
```

(move `self._bump(workspace)` after this upsert so both share the transaction; it already does, since `_write()` wraps the block).

Add after `withdraw_source`:

```python
    def set_allowed_uses_override(
        self,
        source_id: str,
        override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None,
    ) -> None:
        """Narrow the uses a source's revisions grant; ``None`` removes the override.

        ``never_generate`` drops the generation use, ``reference_only`` keeps
        lookup only, and a tuple keeps those uses. The stored value is the
        narrowed list; eligibility intersects it with each revision's uses.
        """
        with self._write():
            row = self.db.execute(
                "SELECT c.workspace_id FROM sources s JOIN collections c USING(collection_id) "
                "WHERE s.source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                raise TychoraError(
                    "NOT_FOUND", "Source is unavailable", reason="source.absent"
                )
            known = self._known_uses()
            if override is None:
                stored = None
            elif override == "never_generate":
                stored = sorted(known - {_GENERATION_USE})
            elif override == "reference_only":
                stored = sorted(known & {_REFERENCE_USE})
            elif isinstance(override, tuple) and set(override) <= known:
                stored = sorted(set(override))
            else:
                raise TychoraError(
                    "INVALID_REQUEST",
                    "Allowed-use override names an unknown use",
                    reason="policy.use_unknown",
                )
            self.db.execute(
                "UPDATE sources SET allowed_uses_override=? WHERE source_id=?",
                (None if stored is None else json.dumps(stored, separators=(",", ":")), source_id),
            )
            self._bump(row[0])

    def _known_uses(self) -> frozenset[str]:
        """Every use a mode of the active registry defines."""
        return frozenset(
            use
            for mode in self.registry.modes.values()
            for use in cast(tuple[str, ...], mode["allowed_uses"])
        )
```

(imports: `import json`, `from typing import Literal, cast`; `PolicyRepository` gains the annotation `registry: Registry` next to `db` and `artifacts`, imported under `TYPE_CHECKING` from `tychora.configuration.registry`; `CorpusStore` already sets `self.registry`).

In `src/tychora/ports/corpus.py`, add to `CorpusStore` after `record_review`:

```python
    def set_allowed_uses_override(
        self,
        source_id: str,
        override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None,
    ) -> None: ...
```

- [ ] **Step 7: Give the override its public caller**

In `src/tychora/application/sources.py`, add to `SourceService`:

```python
    async def set_allowed_uses(
        self,
        source_id: str,
        override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None,
        scope: RequestScope,
        ctx: ExecutionContext,
    ) -> None:
        """Narrow the uses a source's revisions grant; ``None`` restores them."""
        granted = self.owner.scope.narrow(scope)
        if "manage" not in granted.capabilities:
            raise TychoraError(
                "FORBIDDEN_SCOPE",
                "Changing allowed uses requires the manage capability",
                reason="scope.capability_missing",
            )

        def write() -> None:
            row = self.owner.db.execute(
                "SELECT collection_id FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is None or row[0] not in granted.collection_ids:
                raise TychoraError(
                    "NOT_FOUND", "Source is unavailable", reason="source.absent"
                )
            self.owner.store.set_allowed_uses_override(source_id, override)

        await self.owner.run_write(write, ctx)
```

In `src/tychora/clients/sync.py`, add to `_Sources`:

```python
    @_admitted
    def set_allowed_uses(
        self,
        source_id: str,
        override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None,
    ) -> None:
        """Narrow the uses a source grants (``never_generate``, ``reference_only`` or a use list)."""
        ws = self.kb._open()
        ws.bridge.call(
            ws.services.source_jobs.sources.set_allowed_uses(
                source_id, override, ws.scope, _context()
            )
        )
```

and to `_AsyncSources` in `src/tychora/clients/async_.py`:

```python
    @_admitted
    async def set_allowed_uses(
        self,
        source_id: str,
        override: Literal["never_generate", "reference_only"] | tuple[str, ...] | None,
    ) -> None:
        """Narrow the uses a source grants (``never_generate``, ``reference_only`` or a use list)."""
        ws = self.kb._open()
        ctx = _context()
        await ws.bridge.acall(
            ws.services.source_jobs.sources.set_allowed_uses(
                source_id, override, ws.scope, ctx
            ),
            ctx,
        )
```

(import `Literal` from `typing` where missing).

- [ ] **Step 8: Bind text records to their context group**

In `src/tychora/ports/repositories.py`, `VectorRecordRow` gains `group_id: Identifier | None = None` after `asset_id`. In `src/tychora/adapters/sqlite/repositories/batches.py`, `_VECTOR_COLUMNS` (`:37`) becomes `"record_id,batch_id,chunk_id,asset_id,input_sha,row_index,group_id"` and `write_vectors` appends `r.group_id` to `values`. If `_insert_once` compares the stored row by these columns, it now also compares `group_id`, which is what a repeat must match. In `src/tychora/indexing/coordinator.py:644-651`, add `group_id=b.group_id,` to `VectorRecordRow(...)`.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/storage tests/contracts/test_error_reasons.py -q`
Expected: PASS.

- [ ] **Step 10: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS. Tests that open a workspace created by an earlier commit are already regenerated per test (temporary directories); none commits a database file.

- [ ] **Step 11: Commit**

```bash
git add src/tychora tests
git commit -m "feat: maintain current review state and per-source use overrides"
```

---

### Task 5: Persisted facts replace query-time derivation; retained slots leave the query path

`read_eligibility_facts` still rebuilds every selected extraction from its rows and runs `prepare_extraction` and `representation_facts` on it for every query (`adapters/sqlite/eligibility.py:476-484`), although phase 3 persists those sets at commit. This task reads the persisted sets instead and refuses a selected extraction that has none: `INTEGRITY_ERROR` reason `canonical.facts_missing` (registered here). Phase 3 workspaces are the only ones that open (older ones are refused with `workspace.format_old`) and every published member had its facts written by the coordinator (`indexing/coordinator.py:455`, `:509`), so there is no fallback: a missing set means damage, never an old workspace. The reader also stops opening the snapshot's configuration artifact on every query (`adapters/sqlite/corpus.py:114-137`) and stops loading whole batches (`snapshot_slots`), which removes the interim slot loader from `src` except for native recovery.

**Files:**
- Modify: `src/tychora/adapters/sqlite/eligibility.py:36-56` (imports), delete `:254-358` (`_Owners`, `_require_text_ownership`, `_group_vector_facts`, `_text_vector_facts`, `_image_vector_facts`, `_vector_facts`), rewrite `:361-595` (`read_eligibility_facts`)
- Modify: `src/tychora/retrieval/eligibility.py:184-195` (`EligibilityFacts`: `batches`, `lexical_chunks`, `graph_receipt` replace `slots`), `:628-692` (`_compile` reads them)
- Modify: `src/tychora/adapters/sqlite/corpus.py:41-45`, `:92`, `:114-137`, `:201-212` (no config artifact read; `batch_cache` and `snapshot_slots` deleted)
- Modify: `src/tychora/adapters/sqlite/retained_slots.py` (keep `_batch` and `iter_retained_batches`; delete `RetainedBatchCache`, `MAX_CACHED_RECORDS`, `_records`, `load_retained_slots`), `src/tychora/contracts/publication.py:170-185` (delete `RetainedSlot`)
- Modify: `src/tychora/application/snapshot_gc.py:74-76`, `src/tychora/application/default_bindings.py:243-300`, `src/tychora/application/recovery.py:561-600`
- Modify: `src/tychora/contracts/error_reasons.py` (reason `canonical.facts_missing`), `pyproject.toml:197` (drop the `adapters.sqlite.eligibility -> indexing.representations` ignore)
- Test: `tests/storage/test_representation_facts.py:228-250` (replace), `tests/retrieval/test_persisted_facts.py` (new), `tests/helpers/corpus.py` (`commit_with_facts`), `tests/helpers/publication.py` (`slots_of`); update the tests listed by `grep -rln "snapshot_slots\|RetainedSlot\|batch_cache\|RetainedBatchCache" tests`

**Interfaces:**
- Consumes: phase 3 `representation_dependencies` (owners: units, chunks, assets, context groups); Task 4 `vector_records.group_id`; phase 2 `snapshot_batches`, `BatchRepository.receipt`.
- Produces:
  - Reason `INTEGRITY_ERROR` `canonical.facts_missing` ("Run `tychora manage rebuild workspace --from` …").
  - `EligibilityFacts(..., batches: tuple[tuple[str, str, str | None], ...] = (), lexical_chunks: tuple[tuple[str, str], ...] = (), graph_receipt: GraphBatchReceipt | None = None)` — `(slot, batch_id, extraction_id)`, `(chunk_id, extraction_id)`; no `slots`.
  - `CorpusStore` without `snapshot_slots` and `batch_cache`; `iter_snapshot_batches` stays (native recovery only).
  - `tests/helpers/corpus.py`: `commit_with_facts(store, bundle) -> str`. `tests/helpers/publication.py`: `class Slot(NamedTuple): slot; family; required; batches` and `slots_of(store, view) -> tuple[Slot, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_persisted_facts.py
"""Eligibility reads persisted representation facts and never derives them."""

import ast
from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_eligibility_reader_imports_no_indexing():
    tree = ast.parse((SOURCE / "adapters/sqlite/eligibility.py").read_text())
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in modules if m.startswith("tychora.indexing")}


@pytest.mark.parametrize(
    "owners",
    [
        "SELECT unit_id FROM evidence_units",
        "SELECT chunk_id FROM chunks",
    ],
)
def test_selected_extraction_without_facts_is_refused(tmp_path, owners):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pumps.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        owner = kb._workspace.owner

        def drop():
            owner.db.execute(
                f"DELETE FROM representation_dependencies WHERE owner_id IN ({owners})"
            )

        owner.run_write_sync(drop, ExecutionContext())
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "pump seal"})
    assert (error.value.code, error.value.reason) == (
        "INTEGRITY_ERROR",
        "canonical.facts_missing",
    )


def test_eligibility_reads_no_artifact(tmp_path, monkeypatch):
    from tychora.adapters.files.cas import LocalArtifacts

    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "pumps.md", "# Pumps\n\nInspect the pump seal quarterly.\n")
        owner = kb._workspace.owner
        opened = []
        original = LocalArtifacts.open_verified

        def recording(self, ref):
            opened.append(ref.sha256)
            return original(self, ref)

        monkeypatch.setattr(LocalArtifacts, "open_verified", recording)

        def read():
            store = owner.store
            return store.read_eligibility_facts(store.resolve_snapshot(None))

        facts = owner.run_write_sync(read, ExecutionContext())
    assert facts.units and facts.chunks
    config_sha = owner.config.retained_sha
    assert config_sha not in opened
```

In `tests/storage/test_representation_facts.py`, delete `test_published_add_persists_what_eligibility_derives` (`:228-250`): the reader now returns the stored sets, so the comparison is an identity; `tests/retrieval/test_persisted_facts.py` covers the reader.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_persisted_facts.py -v`
Expected: FAIL — `test_eligibility_reader_imports_no_indexing` finds `tychora.indexing.representations`; the missing-facts cases fail with `ValueError: Registered reason ... canonical.facts_missing` (unregistered) or pass silently because the reader re-derives the sets; `test_eligibility_reads_no_artifact` finds the config artifact opened.

- [ ] **Step 3: Register the reason**

In `src/tychora/contracts/error_reasons.py`, after `canonical.binding_mismatch`:

```python
    Reason(
        "INTEGRITY_ERROR",
        "canonical.facts_missing",
        False,
        "Run `tychora --workspace NEW manage rebuild workspace --from OLD`; a "
        "published extraction has no persisted representation facts.",
    ),
```

and add `"canonical.facts_missing"` to `CLASSIFIED_REASONS["INTEGRITY_ERROR"]` in `tests/contracts/test_error_reasons.py`.

- [ ] **Step 4: Read persisted facts in the eligibility reader**

In `src/tychora/adapters/sqlite/eligibility.py`:

1. Imports: delete `from tychora.indexing.representations import (...)` and `from tychora.contracts.publication import RetainedBatch, RetainedSlot, SnapshotView`; add `from tychora.contracts.publication import SnapshotMember, SnapshotView`, `from tychora.contracts.graph import GraphBatchReceipt`, and `SqliteBatchRepository, SqliteRepresentationRepository` to the `tychora.adapters.sqlite.repositories` import.
2. Delete `_Owners` through `_vector_facts` (`:254-358`).
3. Add the persisted-fact helpers:

```python
def _missing_facts() -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR",
        "A selected extraction has no persisted representation facts.",
        reason="canonical.facts_missing",
    )


def _persisted(db, owner_ids: tuple[str, ...]) -> dict[str, frozenset[str]]:
    """The stored dependency set of every owner; any absent set is refused."""
    found = SqliteRepresentationRepository(db).dependencies(owner_ids)
    if len(found) != len(set(owner_ids)) or not all(found.values()):
        raise _missing_facts()
    return dict(found)


_SNAPSHOT_BATCHES = (
    "SELECT sb.slot,sb.batch_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
    "ORDER BY sb.slot,sb.batch_id"
)
_LEXICAL_CHUNKS = (
    "SELECT r.chunk_id,b.extraction_id FROM snapshot_batches sb "
    "JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN lexical_records r ON r.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot='lexical'"
)
_VECTOR_ROWS = (
    "SELECT v.record_id,v.chunk_id,v.asset_id,v.group_id,b.extraction_id "
    "FROM snapshot_batches sb JOIN index_batches b ON b.batch_id=sb.batch_id "
    "JOIN vector_records v ON v.batch_id=sb.batch_id "
    "WHERE sb.snapshot_id=? AND sb.slot IN ('text','image')"
)


def _vector_facts(
    db, snapshot_id: str, selected: set[str], assets: Mapping[str, AssetFact]
) -> tuple[VectorFact, ...]:
    """Each selected vector record with its context group's or asset's set."""
    rows = [row for row in db.execute(_VECTOR_ROWS, (snapshot_id,)) if row[4] in selected]
    groups = _persisted(db, tuple({row[3] for row in rows if row[3] is not None}))
    facts = []
    for record_id, chunk_id, asset_id, group_id, _ in rows:
        if group_id is not None:
            dependencies = groups[group_id]
        elif asset_id in assets:
            dependencies = assets[asset_id].dependency_ids
        else:
            raise _integrity("Image vector has no canonical asset")
        facts.append(
            VectorFact(
                record_id, chunk_id=chunk_id, asset_id=asset_id, dependency_ids=dependencies
            )
        )
    return tuple(facts)


def _graph_receipt(db, batches) -> GraphBatchReceipt | None:
    """The receipt of the snapshot's graph root batch, if it binds one."""
    graph = [batch for slot, batch, _ in batches if slot == "graph"]
    if not graph:
        return None
    if len(graph) != 1:
        raise _integrity("Snapshot binds more than one graph generation")
    receipt = SqliteBatchRepository(db, writable=False).receipt(graph[0])
    if not isinstance(receipt, GraphBatchReceipt):
        raise _integrity("Graph batch receipt is unavailable", "canonical.row_invalid")
    return receipt
```

4. In `read_eligibility_facts(store, snapshot, members=None)`, inside the member loop, replace the block from `unit_rows = db.execute(` (`:484`) to the end of the loop (`:566`) with:

```python
            unit_rows = db.execute(
                "SELECT u.unit_id,u.unit_type,u.citable,p.asset_id FROM evidence_units u "
                "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                "WHERE u.extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT chunk_id,unit_id,context_group_key FROM chunks WHERE extraction_id=?",
                (member.extraction_id,),
            ).fetchall()
            asset_ids = tuple(
                row[0]
                for row in db.execute(
                    "SELECT asset_id FROM assets WHERE extraction_id=?",
                    (member.extraction_id,),
                )
            )
            dependencies = _persisted(
                db,
                (
                    *(row[0] for row in unit_rows),
                    *(row[0] for row in chunk_rows),
                    *asset_ids,
                ),
            )
            for uid, unit_type, citable, proxy_asset in unit_rows:
                unit_facts.append(
                    UnitFact(
                        uid,
                        member.revision_id,
                        member.extraction_id,
                        unit_type,
                        store.review_state(ReviewTarget(unit_id=uid)),
                        bool(citable),
                        dependencies[uid],
                        proxy_asset_id=proxy_asset,
                        facets=_unit_facets(db, uid),
                    )
                )
            source_asset_owners: dict[str, set[str]] = defaultdict(set)
            for uid, aid in db.execute(
                "SELECT ua.unit_id,ua.asset_id FROM unit_assets ua JOIN evidence_units u USING(unit_id) "
                "WHERE u.extraction_id=? AND ua.role='source'",
                (member.extraction_id,),
            ):
                source_asset_owners[aid].add(uid)
            for aid in asset_ids:
                asset_facts.append(
                    AssetFact(
                        aid,
                        member.revision_id,
                        store.review_state(ReviewTarget(asset_id=aid)),
                        dependencies[aid],
                        source_owner_ids=frozenset(source_asset_owners.get(aid, ())),
                    )
                )
            chunk_facts.extend(
                ChunkFact(chunk_id, uid, dependencies[chunk_id], group)
                for chunk_id, uid, group in chunk_rows
            )
```

with the facet read moved into a helper next to `_rows`:

```python
def _unit_facets(db, unit_id: str) -> tuple[tuple[str, object], ...]:
    """A unit's facet values, each as its one typed value."""
    return tuple(
        (key, text if text is not None else boolean if boolean is not None else number)
        for key, text, number, boolean in db.execute(
            "SELECT facet_key,text_value,number_value,boolean_value FROM facet_values "
            "WHERE unit_id=?",
            (unit_id,),
        )
    )
```

and replace the tail after the loop (`:568-591`) with:

```python
        assets_by_id = {item.asset_id: item for item in asset_facts}
        batches = tuple(db.execute(_SNAPSHOT_BATCHES, (snapshot.snapshot_id,)))
        result = EligibilityFacts(
            policy_epoch=policy_epoch_value,
            revisions=tuple(revision_facts),
            units=tuple(unit_facts),
            chunks=tuple(chunk_facts),
            assets=tuple(asset_facts),
            vectors=_vector_facts(
                db, snapshot.snapshot_id, selected_extractions, assets_by_id
            ),
            assertion_decisions=tuple(
                db.execute(
                    "SELECT assertion_id,MAX(decision_id) FROM assertion_decisions "
                    "GROUP BY assertion_id"
                )
            ),
            revision_facets=tuple(revision_facets),
            historical_definitions=tuple(sorted(historical.items())),
            batches=batches,
            lexical_chunks=tuple(db.execute(_LEXICAL_CHUNKS, (snapshot.snapshot_id,))),
            graph_receipt=_graph_receipt(db, batches),
        )
```

Delete the now-unused `chunk_order` variable and the `unit_ordinals` query.

5. `CorpusStore.read_eligibility_facts` (`adapters/sqlite/corpus.py:114-137`) becomes:

```python
    def read_eligibility_facts(self, snapshot: SnapshotView, members=None):
        from .eligibility import read_eligibility_facts

        return read_eligibility_facts(self, snapshot, members)
```

(the configuration artifact was verified when the snapshot was published; a query does not reopen it).

- [ ] **Step 5: Compile from the new fact fields**

In `src/tychora/retrieval/eligibility.py`, `EligibilityFacts` (`:180-194`) replaces `slots: tuple[RetainedSlot, ...] = ()` with:

```python
    batches: tuple[tuple[str, str, str | None], ...] = ()
    lexical_chunks: tuple[tuple[str, str], ...] = ()
    graph_receipt: GraphBatchReceipt | None = None
```

(drop `RetainedSlot` from the `contracts.publication` import). In `_compile`, replace the `selected_chunks` and `selected_vectors` blocks (`:628-641`) with:

```python
        selected_chunks = {
            chunk for chunk, extraction in facts.lexical_chunks if extraction in extraction_ids
        }
        selected_vectors = {item.record_id for item in facts.vectors}
```

and the batch loop with the blocked set (`:664-692`) with:

```python
        graph_binding = facts.graph_receipt
        batch_ids = [
            batch
            for slot, batch, extraction in facts.batches
            if slot != "graph" and extraction in extraction_ids
        ]
        if graph_binding is not None:
            batch_ids.append(graph_binding.generation_id)
        # Every assertion decided after the generation's watermark is blocked;
        # an assertion outside the generation is never reached by traversal.
        blocked = frozenset(
            assertion
            for assertion, decision in facts.assertion_decisions
            if graph_binding is not None and decision > graph_binding.decision_watermark
        )
```

Remove the now-unused `graph_manifest_digest` import.

- [ ] **Step 6: Delete the slot loader from the query path**

1. `src/tychora/contracts/publication.py`: delete `class RetainedSlot` (`:170-185`). `RetainedBatch` stays: native recovery verifies one batch at a time.
2. `src/tychora/adapters/sqlite/retained_slots.py`: delete `MAX_CACHED_RECORDS`, `_records`, `RetainedBatchCache`, `load_retained_slots`; update the module docstring to "Rebuild one batch's contents from SQL rows and its input manifest for native recovery. Every row is checked against the batch's receipt and input manifest, so drift in SQL is refused rather than served." Drop the now-unused imports (`OrderedDict`, `RetainedSlot`).
3. `src/tychora/adapters/sqlite/corpus.py`: delete `self.batch_cache = RetainedBatchCache()` (`:92`) and `snapshot_slots` (`:201-212`); the `.retained_slots` import keeps `iter_retained_batches` only.
4. `src/tychora/application/snapshot_gc.py:74-76`: delete the `store.batch_cache.discard(...)` call.
5. `src/tychora/application/default_bindings.py:243-300`: read slot families from the view and graph generations from receipts:

```python
        if (active_id := owner.store.active_snapshot_id()) is not None:
            view = owner.store.resolve_snapshot(active_id)
            with owner.store.uow.read() as tx:
                generations = tuple(
                    receipt.generation_id
                    for row in tx.batches.for_snapshot(active_id, "graph")
                    if isinstance((receipt := tx.batches.receipt(row.batch_id)), GraphBatchReceipt)
                )
            for item in view.families:
                if item.slot not in unavailable:
                    continue
                if item.slot in {"text", "image"}:
                    owner.store.vector_indexes.pop(item.family.family_id, None)
                elif item.slot == "graph":
                    for generation in generations:
                        owner.store.graph_indexes.pop(generation, None)
            for item in view.families:
```

and in the second loop rename `slot` to `item` (`slot.slot` → `item.slot`, `slot.family` → `item.family`), with the graph branch:

```python
                elif graph is not None and item.slot == "graph":
                    for generation in generations:
                        owner.store.graph_indexes[generation] = graph
```

(import `GraphBatchReceipt` from `tychora.contracts.graph`).
6. `src/tychora/application/recovery.py:576-600`: replace `for slot in (slots := store.snapshot_slots(snapshot, fresh=True)):` with

```python
            loaded = tuple(
                store.iter_snapshot_batches(snapshot, slots=frozenset(channels))
            )
            for item in {pair[0].slot: pair[0] for pair in loaded}.values():
```

so the family checks run once per slot (`slot.slot` → `item.slot`, `slot.family` → `item.family`), then `for item, batch in loaded: verify_canonical_native_batch(store, item.family, batch)`; the second loop becomes `for item, batch in loaded:` with `slot.slot`/`slot.family` renamed the same way.

- [ ] **Step 7: Test helpers and test updates**

In `tests/helpers/corpus.py`:

```python
def commit_with_facts(store, bundle) -> str:
    """Commit a bundle with the representation facts the coordinator writes."""
    from tychora.indexing.representations import (
        ChunkSpan,
        prepare_extraction,
        representation_facts,
    )

    facts = representation_facts(
        bundle,
        prepare_extraction(bundle),
        (ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end) for c in bundle.chunks),
    )
    return store.commit_extraction(bundle, facts)
```

In `tests/helpers/publication.py`:

```python
class Slot(NamedTuple):
    """One slot of a published snapshot with its batch contents, for assertions."""

    slot: str
    family: IndexFamily
    required: bool
    batches: tuple[RetainedBatch, ...]


def slots_of(store: CorpusStore, view: SnapshotView) -> tuple[Slot, ...]:
    """Every slot of ``view`` with its batches, read fresh like native recovery."""
    loaded = tuple(store.iter_snapshot_batches(view))
    return tuple(
        Slot(
            item.slot,
            item.family,
            item.required,
            tuple(batch for owner, batch in loaded if owner.slot == item.slot),
        )
        for item in view.families
    )
```

Then:
- Replace every `<store>.snapshot_slots(<view>)` and `<store>.snapshot_slots(<view>, fresh=True)` in tests with `slots_of(<store>, <view>)` (`tests/acceptance/test_combined_channels.py`, `test_crash_matrix.py`, `combined_store_probe.py`, `tests/evaluation/test_image_scenario.py`, `test_graph_scenarios.py`, `tests/indexing/test_native_collection.py`, `test_sources.py`, `test_page_publication.py`, `test_publication.py`, `tests/operations/test_snapshot_gc.py:243`).
- Delete the cache tests: `tests/indexing/test_page_publication.py` `test_the_batch_cache_evicts_the_least_recently_used_batch_over_its_cap` and the assertions that compare a cached load with a fresh one (`:551-560`, `:576`); the `batch_cache` assertions in `tests/indexing/test_native_collection.py:74`, `:95` and `tests/operations/test_snapshot_gc.py:404`, `:414` (the epoch assertions of that test stay).
- `tests/retrieval/test_eligibility.py` builds `EligibilityFacts(..., fixture_slots)` from `RetainedSlot`s (`:110-237`, `:1399`): build `batches`, `lexical_chunks`, `vectors` and `graph_receipt` instead, from the same fixture rows (`batches=(("lexical", batch.batch_id, batch.extraction_id),)`, `lexical_chunks=tuple((r.chunk_id, batch.extraction_id) for r in batch.lexical_records)`, `graph_receipt=batch.receipt` for the graph fixture).
- `tests/indexing/test_page_publication.py:286-291` compares `facts.slots`; compare `facts.batches` and `facts.lexical_chunks` instead.
- Run the default suite; each test that now fails with `canonical.facts_missing` committed an extraction without facts and then compiled eligibility over it: commit it with `commit_with_facts(store, bundle)` instead of `store.commit_extraction(bundle)`.

- [ ] **Step 8: Drop the import-linter exception**

In `pyproject.toml`, delete the line `"tychora.adapters.sqlite.eligibility -> tychora.indexing.representations",  # …` (`:197`).

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_persisted_facts.py tests/retrieval/test_eligibility.py tests/storage tests/indexing tests/operations/test_snapshot_gc.py -q`
Expected: PASS.

- [ ] **Step 10: Full suite and imports**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'` then `uv run --locked lint-imports --no-cache`
Expected: PASS; `lint-imports` reports every contract kept.

- [ ] **Step 11: Commit**

```bash
git add -A src/tychora tests pyproject.toml
git commit -m "perf: read persisted representation facts in eligibility"
```

---

### Task 6: Eligibility v2 contracts and repository

The revision-level view replaces a unit-level one that every read consumer uses (`grep -rln "EligibilityView" src/tychora` lists 18 modules). Swapping it in one commit is too large for one review, so this phase migrates in steps: this task adds the v2 types and the store-side reads beside the old view, which it renames `LegacyEligibilityView`; Task 7 compiles and caches v2 views and hands both to the query path; Tasks 8 to 11 move each reader from `attempt.legacy` to `attempt.view`; Task 12 deletes the legacy view and compiler. Between Tasks 7 and 12 a query compiles both views (the legacy one only for readers not yet moved); the gate runs after Task 12.

**Files:**
- Create: `src/tychora/contracts/eligibility.py`, `src/tychora/adapters/sqlite/repositories/eligibility.py`, `src/tychora/retrieval/selection.py`
- Modify: `src/tychora/contracts/graph.py:188-238` (rename `EligibilityView` to `LegacyEligibilityView`) and every importer (`grep -rln "EligibilityView" src/tychora tests`)
- Modify: `src/tychora/retrieval/eligibility.py:22-115` (move `RevisionFact`, `_parsed_date`, `select_temporal_revisions` to `selection.py`)
- Modify: `src/tychora/ports/repositories.py` (`MemberRow`, `ReviewRow`, `UnitFlags`, `FacetValue`, `OwnerFact`, `EligibilityRepository`, `ReadTransaction.eligibility`), `src/tychora/adapters/sqlite/repositories/unit_of_work.py:58-76`, `repositories/__init__.py`
- Test: `tests/contracts/test_eligibility_view.py`, `tests/storage/test_eligibility_repository.py` (new); `tests/retrieval/test_temporal.py:8` (import from `selection`)

**Interfaces:**
- Consumes: Task 4 `review_current`, `vector_records.group_id`, index `representation_dependencies_dependency`, `units_declared_review`; Task 5 reason `canonical.facts_missing`; phase 3 `SqliteRepresentationRepository.dependencies`.
- Produces:
  - `contracts/eligibility.py`: `RevisionGrant(extraction_id, source_id, kind_id, lane, collection_id)`; `GraphBinding(family_id: Sha256, all_eligible: bool, receipt: GraphBatchReceipt)` (the index lists the first two fields; `receipt` carries the generation the snapshot binds until phase 6 replaces generations with contributions); `EligibilityView(snapshot_id, policy_epoch, scope_digest, eligibility_policy_digest, eligibility_request_digest, revisions: Mapping[str, RevisionGrant], excluded_unit_ids: frozenset[str], excluded_asset_ids: frozenset[str], unit_filter: frozenset[str] | None, selected_batches: Mapping[str, tuple[str, ...]], graph: GraphBinding | None, all_members_eligible: bool)` with `allows(dependency_ids: Iterable[str]) -> bool`, cached `revision_of_extraction: Mapping[str, str]`, `extraction_ids: frozenset[str]` and `batch_ids: frozenset[str]`. `all_members_eligible` (not in the index) tells a store that every snapshot member is an eligible revision, so it can skip the extraction restriction (spec §4.7 "skipped when every member is eligible").
  - `allows` takes no `kinds` argument (the index wrote `allows(dependency_ids, kinds)`): every persisted dependency set names exactly one revision, its owner's, plus units and assets of that revision's extraction (`indexing/representations.py:1172-1194`, `:1273-1281`), and exclusions are closed under dependency, so a set is readable exactly when its one revision is eligible and it names no excluded unit or asset. Task 21 updates the index.
  - `excluded_unit_ids` holds the units of eligible extractions that fail the mode's review states, the proxy policy or a unit-level (`all_units`) filter, closed under dependency; `unit_filter` holds the units that pass a `matched_candidates` filter (`None` without one). Non-citable units are not exclusions: they are structural context.
  - `ports/repositories.py`: `MemberRow`, `ReviewRow`, `UnitFlags`, `FacetValue` (`NamedTuple`s: bulk rows stay tuples so a 10,000-member compile does not validate 10,000 models), `OwnerFact` (a contract) and `EligibilityRepository` with the methods below; `ReadTransaction.eligibility`.
  - `retrieval/selection.py`: `RevisionFact`, `select_temporal_revisions` (unchanged here; Task 16 rewrites it).
  - `contracts/graph.py`: `LegacyEligibilityView` (deleted by Task 12).

- [ ] **Step 1: Rename the legacy view and move edition selection**

Run:

```bash
grep -rl "\bEligibilityView\b" src/tychora tests | xargs sed -i '' -E 's/\bEligibilityView\b/LegacyEligibilityView/g'
```

(on Linux: `sed -i -E`). Then create `src/tychora/retrieval/selection.py` with the module docstring `"""Edition selection over the retained revisions of each source."""`, the imports `from dataclasses import dataclass`, `from datetime import date`, `from typing import Literal`, and the definitions of `RevisionFact`, `_parsed_date` and `select_temporal_revisions` moved verbatim from `src/tychora/retrieval/eligibility.py:22-115`. In `retrieval/eligibility.py`, delete them and add `from tychora.retrieval.selection import RevisionFact, select_temporal_revisions`. In `tests/retrieval/test_temporal.py:8`, import both names from `tychora.retrieval.selection`.

Run: `uv run --locked pytest tests/retrieval -q`
Expected: PASS (pure rename and move).

- [ ] **Step 2: Write the failing tests**

```python
# tests/contracts/test_eligibility_view.py
"""The revision-level view answers readability from a persisted dependency set."""

import pytest

from tychora.contracts.eligibility import EligibilityView, RevisionGrant
from tychora.contracts.identity import digest


def _view(**changes):
    values = {
        "snapshot_id": "snapshot",
        "policy_epoch": 3,
        "scope_digest": digest("scope"),
        "eligibility_policy_digest": digest("policy"),
        "eligibility_request_digest": digest("request"),
        "revisions": {
            "rev-a": RevisionGrant(
                extraction_id="ext-a",
                source_id="src-a",
                kind_id="manual@1",
                lane="standard",
                collection_id="col",
            )
        },
        "excluded_unit_ids": frozenset({"unit-x"}),
        "excluded_asset_ids": frozenset({"asset-x"}),
        "selected_batches": {"lexical": ("batch-a",), "text": ("batch-t",)},
    }
    return EligibilityView(**(values | changes))


@pytest.mark.parametrize(
    ("dependencies", "expected"),
    [
        ({"rev-a", "unit-1"}, True),
        ({"rev-a", "unit-1", "unit-2", "asset-1"}, True),
        ({"rev-a", "unit-1", "unit-x"}, False),
        ({"rev-a", "asset-x"}, False),
        ({"rev-b", "unit-1"}, False),
        ({"unit-1"}, False),
        (set(), False),
    ],
)
def test_allows_checks_the_one_revision_and_the_exclusions(dependencies, expected):
    assert _view().allows(dependencies) is expected


def test_view_size_is_revisions_plus_exclusions():
    view = _view()
    assert len(view.revisions) + len(view.excluded_unit_ids) + len(
        view.excluded_asset_ids
    ) == 3
    assert view.unit_filter is None


def test_derived_lookups():
    view = _view()
    assert view.revision_of_extraction == {"ext-a": "rev-a"}
    assert view.extraction_ids == frozenset({"ext-a"})
    assert view.batch_ids == frozenset({"batch-a", "batch-t"})
    assert view.all_members_eligible is False
```

```python
# tests/storage/test_eligibility_repository.py
"""Store-side eligibility reads: one row per member, exclusions, closures, owner facts."""

import json

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext

_TEXT = "# Pumps\n\nInspect the pump seal quarterly.\n\n# Valves\n\nReplace worn valves.\n"


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        for key in ("a.md", "b.md"):
            job = opened.sources.add(
                {
                    "type": "text",
                    "source_key": key,
                    "collection_id": "default",
                    "kind": "document@1",
                    "format": "markdown",
                    "text": _TEXT,
                    "metadata": {"title": key},
                }
            )
            assert opened.jobs.wait(job.id).state == "succeeded"
        yield opened


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_member_rows_are_one_statement(kb):
    owner = kb._workspace.owner
    statements = []

    def read(tx):
        db = tx.eligibility._db
        db.set_trace_callback(statements.append)
        try:
            return tx.eligibility.members(tx.snapshots.active_id())
        finally:
            db.set_trace_callback(None)

    rows = _read(kb, read)
    assert len(rows) == 2
    assert len(statements) == 1
    assert {row.review_state for row in rows} == {"unreviewed"}
    assert all(row.workspace_id == owner.workspace_id for row in rows)


def test_review_rows_report_reviewed_and_declared_states(kb):
    owner = kb._workspace.owner
    unit_id, extraction_id = owner_row(
        owner, "SELECT unit_id,extraction_id FROM evidence_units ORDER BY unit_id LIMIT 1"
    )
    on_owner(
        owner,
        lambda: owner.store.record_review(ReviewTarget(unit_id=unit_id), "rejected", "r"),
    )
    rows = _read(
        kb,
        lambda tx: tx.eligibility.review_rows(
            (extraction_id,), ("needs_review", "rejected")
        ),
    )
    assert [(row.target_id, row.kind, row.state, row.proxy) for row in rows] == [
        (unit_id, "unit", "rejected", False)
    ]


def test_dependents_close_over_units_that_depend_on_a_seed(kb):
    owner = kb._workspace.owner
    owner_id, dependency = owner_row(
        owner,
        "SELECT d.owner_id,d.dependency_id FROM representation_dependencies d "
        "JOIN evidence_units u ON u.unit_id=d.owner_id "
        "WHERE d.dependency_id<>d.owner_id AND d.dependency_id IN "
        "(SELECT unit_id FROM evidence_units) LIMIT 1",
    ) or (None, None)
    if owner_id is None:
        pytest.skip("this fixture has no unit that depends on another unit")
    closure = _read(kb, lambda tx: tx.eligibility.dependents((dependency,)))
    assert closure[dependency] == "unit"
    assert closure[owner_id] == "unit"


def test_owner_facts_read_units_chunks_and_sets(kb):
    owner = kb._workspace.owner
    unit_id, chunk_id = owner_row(
        owner, "SELECT unit_id,chunk_id FROM chunks ORDER BY chunk_id LIMIT 1"
    )
    facts = {
        fact.owner_id: fact
        for fact in _read(kb, lambda tx: tx.eligibility.owner_facts((unit_id, chunk_id)))
    }
    assert facts[unit_id].kind == "unit" and facts[unit_id].citable in {True, False}
    assert facts[chunk_id].kind == "chunk" and facts[chunk_id].unit_id == unit_id
    assert unit_id in facts[chunk_id].dependency_ids


def test_owner_facts_refuse_a_missing_set(kb):
    owner = kb._workspace.owner
    (unit_id,) = owner_row(owner, "SELECT unit_id FROM evidence_units LIMIT 1")
    owner_rows(
        owner, "DELETE FROM representation_dependencies WHERE owner_id=?", (unit_id,)
    )
    with pytest.raises(TychoraError) as error:
        _read(kb, lambda tx: tx.eligibility.owner_facts((unit_id,)))
    assert error.value.reason == "canonical.facts_missing"


def test_batches_and_unit_flags(kb):
    def read(tx):
        snapshot = tx.snapshots.active_id()
        batches = tx.eligibility.batches(snapshot)
        extractions = tuple({row[2] for row in batches if row[2] is not None})
        return batches, tx.eligibility.unit_flags(extractions)

    batches, flags = _read(kb, read)
    assert {slot for slot, _, _ in batches} == {"lexical"}
    assert flags and all(not row.proxy for row in flags)


def test_kind_definitions_and_facets_bind_json_lists(kb):
    def read(tx):
        kinds = tx.eligibility.kind_definitions(("document@1",))
        return kinds, tx.eligibility.revision_facets((), ("scope.project",))

    kinds, facets = _read(kb, read)
    (kind_id, definition_hash, definition) = kinds[0]
    assert kind_id == "document@1" and len(definition_hash) == 64
    assert json.loads(definition)["kind"]
    assert facets == ()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_eligibility_view.py tests/storage/test_eligibility_repository.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'tychora.contracts.eligibility'`.

- [ ] **Step 4: Add the v2 contracts**

```python
# src/tychora/contracts/eligibility.py
"""Revision-level eligibility: what one pinned query may read, without its units."""

from collections.abc import Iterable, Mapping
from functools import cached_property

from pydantic import Field

from .base import Contract, Identifier, NonEmptyStr, NonNegativeInt, Sha256
from .graph import GraphBatchReceipt


class RevisionGrant(Contract):
    """One eligible revision: its selected extraction, kind, lane and collection."""

    extraction_id: Identifier
    source_id: Identifier
    kind_id: NonEmptyStr
    lane: NonEmptyStr
    collection_id: Identifier


class GraphBinding(Contract):
    """The graph a view reads and whether every snapshot member is eligible."""

    family_id: Sha256
    all_eligible: bool
    receipt: GraphBatchReceipt


class EligibilityView(Contract):
    """What one pinned query may read: O(revisions + exclusions), never O(units).

    A unit, chunk, asset or vector record is readable when its persisted
    dependency set is (``allows``); candidate rules add citability and the
    candidate filter on top.
    """

    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    scope_digest: Sha256
    eligibility_policy_digest: Sha256
    eligibility_request_digest: Sha256
    revisions: Mapping[Identifier, RevisionGrant]
    excluded_unit_ids: frozenset[Identifier] = frozenset()
    excluded_asset_ids: frozenset[Identifier] = frozenset()
    unit_filter: frozenset[Identifier] | None = None
    selected_batches: Mapping[NonEmptyStr, tuple[Identifier, ...]] = Field(
        default_factory=dict
    )
    graph: GraphBinding | None = None
    all_members_eligible: bool = False

    def allows(self, dependency_ids: Iterable[str]) -> bool:
        """Whether a persisted dependency set is readable under this view.

        A set names exactly one revision, its owner's, plus units and assets of
        that revision's extraction; exclusions are closed under dependency. The
        set is readable when its revision is eligible and it names no excluded
        unit or asset.
        """
        revision_seen = False
        for item in dependency_ids:
            if item in self.revisions:
                revision_seen = True
            elif item in self.excluded_unit_ids or item in self.excluded_asset_ids:
                return False
        return revision_seen

    @cached_property
    def revision_of_extraction(self) -> Mapping[str, str]:
        """The eligible revision of each selected extraction."""
        return {grant.extraction_id: rid for rid, grant in self.revisions.items()}

    @cached_property
    def extraction_ids(self) -> frozenset[str]:
        """Every selected extraction."""
        return frozenset(grant.extraction_id for grant in self.revisions.values())

    @cached_property
    def batch_ids(self) -> frozenset[str]:
        """Every selected batch, whatever its slot."""
        return frozenset(
            batch for batches in self.selected_batches.values() for batch in batches
        )
```

- [ ] **Step 5: Declare the repository port**

In `src/tychora/ports/repositories.py`, add `from typing import Literal, NamedTuple, Protocol` (extending the existing `typing` import) and, before `class ReadTransaction`:

```python
class MemberRow(NamedTuple):
    """One snapshot member and the policy facts its revision is judged on."""

    source_id: str
    revision_id: str
    extraction_id: str
    is_default: bool
    kind_id: str
    definition_hash: str
    collection_id: str
    collection_policy_json: str
    collection_floor: int
    source_floor: int
    withdrawn: bool
    allowed_uses_override: str | None
    access_rank: int
    allowed_uses_json: str
    review_state: str
    published_on: str | None
    effective_from: str | None
    effective_until: str | None
    edition_key: str
    workspace_id: str


class ReviewRow(NamedTuple):
    """A unit or asset whose review state may exclude it, with that state."""

    target_id: str
    kind: Literal["unit", "asset"]
    state: str
    proxy: bool


class UnitFlags(NamedTuple):
    """A unit's extraction, citability and proxy status."""

    unit_id: str
    extraction_id: str
    citable: bool
    proxy: bool


class FacetValue(NamedTuple):
    """One facet value of a revision or unit, as its typed value."""

    owner_id: str
    facet_key: str
    value: str | float | int


class OwnerFact(Contract):
    """One unit, asset, chunk or vector record with what admitting it reads."""

    owner_id: Identifier
    kind: Literal["unit", "asset", "chunk", "record"]
    extraction_id: Identifier
    dependency_ids: frozenset[Identifier]
    unit_id: Identifier | None = None
    chunk_id: Identifier | None = None
    asset_id: Identifier | None = None
    proxy: bool = False
    citable: bool = False
    batch_id: Identifier | None = None
    source_owner_ids: frozenset[Identifier] = frozenset()


class EligibilityRepository(Protocol):
    """The store-side reads that compile and apply a revision-level view."""

    def members(
        self, snapshot_id: str, extraction_ids: tuple[str, ...] = ()
    ) -> tuple[MemberRow, ...]:
        """One row per member of the snapshot; with ``extraction_ids``, per listed extraction instead."""
        ...

    def kind_definitions(
        self, kind_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(kind_id, definition_hash, definition_json)`` of each listed kind."""
        ...

    def batches(self, snapshot_id: str) -> tuple[tuple[str, str, str | None], ...]:
        """``(slot, batch_id, extraction_id)`` of every batch the snapshot serves."""
        ...

    def review_rows(
        self, extraction_ids: tuple[str, ...], excluded_states: tuple[str, ...]
    ) -> tuple[ReviewRow, ...]:
        """Reviewed units and assets, proxy units, and units or assets declared in ``excluded_states``."""
        ...

    def unit_flags(self, extraction_ids: tuple[str, ...]) -> tuple[UnitFlags, ...]:
        """Every unit of these extractions; read only for filters and graph scopes."""
        ...

    def dependents(self, owner_ids: tuple[str, ...]) -> Mapping[str, Literal["unit", "asset"]]:
        """The listed units and assets with every unit or asset depending on them, transitively."""
        ...

    def revision_facets(
        self, revision_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Revision-level values of the listed facets."""
        ...

    def unit_facets(
        self, extraction_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        """Unit-level values of the listed facets for units of these extractions."""
        ...

    def revision_metadata(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """``(revision_id, metadata_json, metadata_sha)`` of each listed revision."""
        ...

    def global_facet_markers(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, ArtifactRef | None], ...]:
        """``(revision_id, source_ref, artifact)`` of each global-facet binding row."""
        ...

    def owner_facts(self, owner_ids: tuple[str, ...]) -> tuple[OwnerFact, ...]:
        """Facts of the listed units, assets, chunks and vector records; unknown IDs are skipped.

        A known owner without a persisted dependency set is refused with
        ``INTEGRITY_ERROR`` reason ``canonical.facts_missing``.
        """
        ...
```

and add `eligibility: EligibilityRepository` to `ReadTransaction`.

- [ ] **Step 6: Implement the SQLite repository**

```python
# src/tychora/adapters/sqlite/repositories/eligibility.py
"""Store-side reads that compile a revision-level eligibility view and apply it."""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from typing import Literal

from tychora.configuration.global_facets import ROLE as GLOBAL_FACET_ROLE
from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import (
    FacetValue,
    MemberRow,
    OwnerFact,
    ReviewRow,
    UnitFlags,
)

from .artifacts import artifact_ref
from .representations import SqliteRepresentationRepository

_IN = "IN (SELECT value FROM json_each(?))"
_MEMBER_COLUMNS = (
    "m.source_id,m.revision_id,m.extraction_id,m.is_default,r.kind_id,k.definition_hash,"
    "c.collection_id,c.policy_json,c.access_floor,s.access_floor,"
    "s.withdrawn_at IS NOT NULL,s.allowed_uses_override,r.access_rank,"
    "r.allowed_uses_json,COALESCE(rc.state,r.declared_review_state),"
    "r.published_on,r.effective_from,r.effective_until,r.edition_key,c.workspace_id"
)
_MEMBER_JOINS = (
    "JOIN revisions r ON r.revision_id=m.revision_id "
    "JOIN sources s ON s.source_id=m.source_id "
    "JOIN collections c ON c.collection_id=s.collection_id "
    "JOIN kinds k ON k.kind_id=r.kind_id "
    "LEFT JOIN review_current rc ON rc.target_kind='revision' AND rc.target_id=r.revision_id "
)
_SNAPSHOT_MEMBERS = (
    f"SELECT {_MEMBER_COLUMNS} FROM snapshot_members m {_MEMBER_JOINS}"
    "WHERE m.snapshot_id=? ORDER BY m.source_id,m.revision_id"
)
_LISTED_MEMBERS = (
    f"SELECT {_MEMBER_COLUMNS} FROM (SELECT r.source_id,e.revision_id,e.extraction_id,"
    "1 AS is_default FROM extractions e JOIN revisions r USING(revision_id) "
    f"WHERE e.extraction_id {_IN}) m {_MEMBER_JOINS}"
    "ORDER BY m.source_id,m.revision_id"
)
_UNIT_REVIEWS = (
    "SELECT u.unit_id,'unit',rc.state,p.proxy_unit_id IS NOT NULL FROM review_current rc "
    "JOIN evidence_units u ON u.unit_id=rc.target_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
    f"WHERE rc.target_kind='unit' AND u.extraction_id {_IN} "
    "UNION SELECT u.unit_id,'unit',u.declared_review_state,1 FROM asset_proxies p "
    "JOIN evidence_units u ON u.unit_id=p.proxy_unit_id "
    f"WHERE u.extraction_id {_IN} AND NOT EXISTS (SELECT 1 FROM review_current rc "
    "WHERE rc.target_kind='unit' AND rc.target_id=u.unit_id) "
    "UNION SELECT u.unit_id,'unit',u.declared_review_state,0 FROM evidence_units u "
    f"WHERE u.declared_review_state {_IN} AND u.extraction_id {_IN} "
    "AND NOT EXISTS (SELECT 1 FROM asset_proxies p WHERE p.proxy_unit_id=u.unit_id) "
    "AND NOT EXISTS (SELECT 1 FROM review_current rc "
    "WHERE rc.target_kind='unit' AND rc.target_id=u.unit_id)"
)
_ASSET_REVIEWS = (
    "SELECT a.asset_id,'asset',COALESCE(rc.state,a.declared_review_state),0 FROM assets a "
    "LEFT JOIN review_current rc ON rc.target_kind='asset' AND rc.target_id=a.asset_id "
    f"WHERE a.extraction_id {_IN} AND (rc.target_id IS NOT NULL "
    f"OR a.declared_review_state {_IN})"
)
_DEPENDENTS = (
    "WITH RECURSIVE removed(id) AS (SELECT value FROM json_each(?) UNION "
    "SELECT d.owner_id FROM representation_dependencies d "
    "JOIN removed r ON d.dependency_id=r.id) "
    "SELECT r.id,CASE WHEN a.asset_id IS NOT NULL THEN 'asset' ELSE 'unit' END "
    "FROM removed r LEFT JOIN assets a ON a.asset_id=r.id "
    "WHERE a.asset_id IS NOT NULL OR EXISTS "
    "(SELECT 1 FROM evidence_units u WHERE u.unit_id=r.id)"
)
_OWNERS = (
    "SELECT 'unit',u.unit_id,u.extraction_id,u.unit_id,NULL,p.asset_id,u.citable,"
    "NULL,u.unit_id FROM evidence_units u "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
    f"WHERE u.unit_id {_IN} "
    "UNION ALL SELECT 'asset',a.asset_id,a.extraction_id,NULL,NULL,a.asset_id,0,"
    f"NULL,a.asset_id FROM assets a WHERE a.asset_id {_IN} "
    "UNION ALL SELECT 'chunk',c.chunk_id,c.extraction_id,c.unit_id,c.chunk_id,"
    "p.asset_id,u.citable,NULL,c.chunk_id FROM chunks c "
    "JOIN evidence_units u ON u.unit_id=c.unit_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    f"WHERE c.chunk_id {_IN} "
    "UNION ALL SELECT 'record',v.record_id,b.extraction_id,c.unit_id,v.chunk_id,"
    "v.asset_id,0,v.batch_id,COALESCE(v.group_id,v.asset_id) FROM vector_records v "
    "JOIN index_batches b ON b.batch_id=v.batch_id "
    f"LEFT JOIN chunks c ON c.chunk_id=v.chunk_id WHERE v.record_id {_IN}"
)


def _ids(values) -> str:
    return json.dumps(sorted(set(values)))


def _typed(text, number, boolean) -> str | float | int:
    """The one stored value of a facet row; booleans stay the stored 0 or 1."""
    if text is not None:
        return text
    if boolean is not None:
        return boolean
    return number


def facts_missing() -> TychoraError:
    """The refusal for a selected owner without a persisted dependency set."""
    return TychoraError(
        "INTEGRITY_ERROR",
        "A selected owner has no persisted representation facts.",
        reason="canonical.facts_missing",
    )


class SqliteEligibilityRepository:
    """Eligibility reads bound to one connection's current transaction."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db

    def members(
        self, snapshot_id: str, extraction_ids: tuple[str, ...] = ()
    ) -> tuple[MemberRow, ...]:
        if extraction_ids:
            rows = self._db.execute(_LISTED_MEMBERS, (_ids(extraction_ids),))
        else:
            rows = self._db.execute(_SNAPSHOT_MEMBERS, (snapshot_id,))
        return tuple(
            MemberRow(*row[:3], bool(row[3]), *row[4:10], bool(row[10]), *row[11:])
            for row in rows
        )

    def kind_definitions(
        self, kind_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            self._db.execute(
                "SELECT kind_id,definition_hash,definition_json FROM kinds "
                f"WHERE kind_id {_IN} ORDER BY kind_id",
                (_ids(kind_ids),),
            )
        )

    def batches(self, snapshot_id: str) -> tuple[tuple[str, str, str | None], ...]:
        return tuple(
            self._db.execute(
                "SELECT sb.slot,sb.batch_id,b.extraction_id FROM snapshot_batches sb "
                "JOIN index_batches b ON b.batch_id=sb.batch_id WHERE sb.snapshot_id=? "
                "ORDER BY sb.slot,sb.batch_id",
                (snapshot_id,),
            )
        )

    def review_rows(
        self, extraction_ids: tuple[str, ...], excluded_states: tuple[str, ...]
    ) -> tuple[ReviewRow, ...]:
        extractions, states = _ids(extraction_ids), _ids(excluded_states)
        units = self._db.execute(
            _UNIT_REVIEWS, (extractions, extractions, states, extractions)
        ).fetchall()
        assets = self._db.execute(_ASSET_REVIEWS, (extractions, states)).fetchall()
        return tuple(
            ReviewRow(row[0], row[1], row[2], bool(row[3]))
            for row in sorted((*units, *assets))
        )

    def unit_flags(self, extraction_ids: tuple[str, ...]) -> tuple[UnitFlags, ...]:
        return tuple(
            UnitFlags(row[0], row[1], bool(row[2]), bool(row[3]))
            for row in self._db.execute(
                "SELECT u.unit_id,u.extraction_id,u.citable,p.proxy_unit_id IS NOT NULL "
                "FROM evidence_units u LEFT JOIN asset_proxies p ON p.proxy_unit_id=u.unit_id "
                f"WHERE u.extraction_id {_IN} ORDER BY u.unit_id",
                (_ids(extraction_ids),),
            )
        )

    def dependents(
        self, owner_ids: tuple[str, ...]
    ) -> Mapping[str, Literal["unit", "asset"]]:
        if not owner_ids:
            return {}
        return dict(self._db.execute(_DEPENDENTS, (_ids(owner_ids),)))

    def revision_facets(
        self, revision_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        return tuple(
            FacetValue(owner, key, _typed(*values))
            for owner, key, *values in self._db.execute(
                "SELECT revision_id,facet_key,text_value,number_value,boolean_value "
                f"FROM facet_values WHERE facet_key {_IN} AND revision_id {_IN} "
                "ORDER BY revision_id,facet_key,ordinal",
                (_ids(facet_keys), _ids(revision_ids)),
            )
        )

    def unit_facets(
        self, extraction_ids: tuple[str, ...], facet_keys: tuple[str, ...]
    ) -> tuple[FacetValue, ...]:
        return tuple(
            FacetValue(owner, key, _typed(*values))
            for owner, key, *values in self._db.execute(
                "SELECT f.unit_id,f.facet_key,f.text_value,f.number_value,f.boolean_value "
                "FROM facet_values f JOIN evidence_units u ON u.unit_id=f.unit_id "
                f"WHERE f.facet_key {_IN} AND u.extraction_id {_IN} "
                "ORDER BY f.unit_id,f.facet_key,f.ordinal",
                (_ids(facet_keys), _ids(extraction_ids)),
            )
        )

    def revision_metadata(
        self, revision_ids: tuple[str, ...]
    ) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            self._db.execute(
                "SELECT revision_id,metadata_json,metadata_sha FROM revisions "
                f"WHERE revision_id {_IN} ORDER BY revision_id",
                (_ids(revision_ids),),
            )
        )

    def global_facet_markers(self, revision_ids: tuple[str, ...]):
        return tuple(
            (
                row[0],
                row[1],
                artifact_ref(row[2:6]) if row[2] is not None else None,
            )
            for row in self._db.execute(
                "SELECT ra.revision_id,ra.source_ref,a.artifact_sha,a.size_bytes,"
                "a.media_type,a.storage_key FROM revision_artifacts ra "
                "LEFT JOIN artifacts a USING(artifact_sha) "
                f"WHERE ra.role=? AND ra.revision_id {_IN} ORDER BY ra.revision_id",
                (GLOBAL_FACET_ROLE, _ids(revision_ids)),
            )
        )

    def owner_facts(self, owner_ids: tuple[str, ...]) -> tuple[OwnerFact, ...]:
        ids = _ids(owner_ids)
        rows = self._db.execute(_OWNERS, (ids, ids, ids, ids)).fetchall()
        sets = SqliteRepresentationRepository(self._db).dependencies(
            tuple({row[8] for row in rows if row[8] is not None})
        )
        sources: dict[str, set[str]] = defaultdict(set)
        for asset_id, unit_id in self._db.execute(
            "SELECT asset_id,unit_id FROM unit_assets "
            f"WHERE role='source' AND asset_id {_IN}",
            (ids,),
        ):
            sources[asset_id].add(unit_id)
        facts = []
        for kind, owner, extraction, unit, chunk, asset, citable, batch, set_owner in rows:
            dependencies = sets.get(set_owner) if set_owner is not None else None
            if not dependencies:
                raise facts_missing()
            facts.append(
                OwnerFact(
                    owner_id=owner,
                    kind=kind,
                    extraction_id=extraction,
                    dependency_ids=dependencies,
                    unit_id=unit,
                    chunk_id=chunk,
                    asset_id=asset,
                    proxy=kind in {"unit", "chunk"} and asset is not None,
                    citable=bool(citable),
                    batch_id=batch,
                    source_owner_ids=frozenset(sources.get(owner, ())),
                )
            )
        return tuple(facts)
```

(`adapters -> configuration` is an allowed edge: `adapters/sqlite/eligibility.py:12-20` already imports `tychora.configuration.global_facets`.)

Register it: in `repositories/unit_of_work.py`, `SqliteTransaction` gains `eligibility: EligibilityRepository` and `self.eligibility = SqliteEligibilityRepository(db, writable=writable)`; export `SqliteEligibilityRepository` from `repositories/__init__.py` (`__all__` too).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/contracts/test_eligibility_view.py tests/storage/test_eligibility_repository.py tests/architecture -v`
Expected: PASS (the port-neutrality test accepts the new protocol: no `sqlite3` type crosses it and no parameter is named `sql`, `where` or `match`).

- [ ] **Step 8: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -A src/tychora tests
git commit -m "feat: add revision-level eligibility contracts and store reads"
```

---

### Task 7: Eligibility v2 compiler, cache and pinned attempts (Q1, Q2, C3)

**Files:**
- Move: `src/tychora/application/collection_policy.py` → `src/tychora/configuration/collection_policy.py` (verbatim; importers updated); `resolve_graph_traversal_policy` from `src/tychora/adapters/ladybug/traversal.py:35-110` → `src/tychora/configuration/graph_policy.py` (verbatim, the function-body import of `GraphPredicatePolicy` lifted to the module top)
- Create: `src/tychora/retrieval/compiler.py`, `src/tychora/retrieval/attempt.py`
- Modify: `src/tychora/retrieval/cache.py` (add `EligibilityCache`)
- Modify: `src/tychora/retrieval/eligibility.py` (rename `EligibilityCompiler` → `LegacyEligibilityCompiler`, `EligibilityAttempt` → `LegacyAttempt` without `owner`, `ctx` and `owner_provider`; delete `run_pinned_eligibility`, `eligible_cache_key` and `_filter_matches`, which `compiler.py` now owns as `filter_matches`)
- Modify: `src/tychora/application/owner.py` (`eligibility_cache`), `src/tychora/application/query.py:220-226`, `:562-670` (compile through `run_pinned_eligibility` of `attempt.py`; legacy fields read as `attempt.legacy`)
- Modify: `src/tychora/application/reranking.py:716-828`, `src/tychora/retrieval/rerank.py:41`, `:73`, `:220-300`, `src/tychora/retrieval/visuals.py:19`, `:251-314`, `src/tychora/retrieval/response.py:43`, `:636-670` (type `EligibilityAttempt` from `attempt.py`; `attempt.view` → `attempt.legacy`)
- Modify: `src/tychora/application/inspection.py:42`, `:76`, `src/tychora/application/discovery.py:1022`, `:1065`, `src/tychora/application/retained_content.py:34`, `:114` (legacy names; `eligible_cache_key(attempt.view)` becomes `legacy_cache_key(attempt.view)`, a local tuple of the same five fields)
- Modify: `src/tychora/contracts/error_reasons.py` (six reasons), `pyproject.toml` (drop the ignores this task makes obsolete)
- Test: `tests/retrieval/test_eligibility_v2.py`, `tests/retrieval/test_eligibility_cache.py` (new); the tests importing the renamed legacy names (`grep -rln "EligibilityAttempt\|EligibilityCompiler\|run_pinned_eligibility\|_filter_matches\|eligible_cache_key" tests`)

**Interfaces:**
- Consumes: Task 6 `EligibilityView`, `RevisionGrant`, `GraphBinding`, `EligibilityRepository`, `OwnerFact`; Task 3 `SnapshotCatalog`; Task 2 `run_read`.
- Produces:
  - `retrieval/compiler.py`: `REVIEW_STATES`; `class EligibilityKey(NamedTuple): snapshot_id; policy_epoch; scope_digest; policy_digest; request_digest`; `eligible_cache_key(view: EligibilityView) -> EligibilityKey`; `@dataclass CompiledEligibility(view, warnings, historical_kind_roles, historical_kind_envelopes)`; `@dataclass ModePolicy(name, digest, allowed_lanes, allowed_uses, original_reviews, proxy_reviews, proxy_disabled)`; `check_request_limits(request, config)`; `request_digest(request, selected) -> str`; `filter_matches(expr, registry, source_values, child_values, *, include_global, global_facets) -> bool`; `class EligibilityCompiler(registry, artifacts)` with `mode(config, request, scope) -> ModePolicy`, `knows_compatible(snapshot, config) -> bool`, `mark_compatible(snapshot, config, retained: ResolvedConfig) -> None`, `key(tx, snapshot, scope, request, config) -> tuple[EligibilityKey, ModePolicy, ScopedFilter | None]`, `compile(tx, snapshot, scope, request, config, *, members=None) -> CompiledEligibility`, `compile_cached(tx, cache, snapshot, scope, request, config) -> CompiledEligibility`.
  - `retrieval/cache.py`: `ELIGIBILITY_CACHE_SIZE = 64`; `class EligibilityCache` with `get_or_compile(key: EligibilityKey, compile: Callable[[], CompiledEligibility]) -> CompiledEligibility` and `purge() -> int`. (The index types the value as `EligibilityView`; a cache hit must also return the edition warnings and historical kinds, so the entry is the `CompiledEligibility`. Task 21 updates the index.)
  - `retrieval/attempt.py`: `class AttemptOwner(Protocol)`; `@dataclass(frozen=True) EligibilityAttempt(view, snapshot, warnings, historical_kind_roles, historical_kind_envelopes, legacy, legacy_dependencies, owner, ctx)` with `dependencies_for(representation_id)` (legacy, until Task 12), `revalidate(stage)` and `owner_provider(call)`; `resolve_snapshot(catalog, tx, selector) -> SnapshotView`; `run_pinned_eligibility(compiler, legacy, selector, scope, request, config, operation, *, owner, ctx, on_invalidated=None) -> T`; admission predicates `is_eligible_unit`, `is_context_unit`, `is_structural_unit`, `is_source_unit`, `is_candidate_proxy`, `is_eligible_asset`, `is_candidate_asset`, `is_admitted_chunk`, `is_admitted_record` (each `(view, fact, facts=None) -> bool`).
  - `WorkspaceOwner.eligibility_cache: EligibilityCache`.
  - Reasons: `INVALID_REQUEST` `request.limit_exceeded`, `mode.unavailable`, `profile.snapshot_mismatch`; `PROFILE_INCOMPATIBLE` `kind.historical_unavailable`, `facet.binding_unverified`; `SCOPE_CHANGED` `policy.epoch_changed` (retryable).

The request digest covers only the eligibility-relevant request fields (compiled filters, explicit revisions, selector, `as_of`, `cutoff`, `unknown_dates`, `include_global`; Task 17 adds review states and channels), so two different questions with the same filters share a view. The policy digest is the v1 formula (`eligibility_fingerprint`, mode name and definition, traversal policy), cached per configuration and mode, so no configuration is dumped per query. Global facets are verified only when a request carries a filter, and only for the revisions it selects; v1 verified every member's binding artifact on every query.

- [ ] **Step 1: Move the two pure configuration helpers down a layer**

Run:

```bash
git mv src/tychora/application/collection_policy.py src/tychora/configuration/collection_policy.py
grep -rl "application.collection_policy\|from .collection_policy" src/tychora tests | xargs sed -i '' -e 's/tychora\.application\.collection_policy/tychora.configuration.collection_policy/g' -e 's/from \.collection_policy import/from tychora.configuration.collection_policy import/g'
```

Create `src/tychora/configuration/graph_policy.py` with the docstring `"""Native graph traversal permissions bound from a profile and its registry."""`, the imports the function needs (`from typing import cast`; `from tychora.configuration.models import ResolvedConfig`; `from tychora.configuration.registry import Registry`; `from tychora.contracts.base import FrozenJsonMap`; `from tychora.contracts.errors import TychoraError`; `from tychora.contracts.graph import GraphPredicatePolicy, GraphTraversalPolicy`), and `resolve_graph_traversal_policy` moved verbatim from `adapters/ladybug/traversal.py`. Update its importers (`adapters/ladybug/traversal.py` imports it back for its own use, `application/default_bindings.py:12`, `application/inspection.py:10`, `application/settings.py:265`, `retrieval/eligibility.py:119`, `tests/retrieval/test_graph_policy.py:9`) to `from tychora.configuration.graph_policy import resolve_graph_traversal_policy`.

In `pyproject.toml`, delete the now-obsolete ignores: `tychora.indexing.coordinator -> tychora.application.collection_policy` (both lists), `tychora.adapters.sqlite.corpus -> tychora.application.collection_policy`, `tychora.adapters.sqlite.eligibility -> tychora.application.collection_policy`, `tychora.adapters.sqlite.publication -> tychora.application.collection_policy`, `tychora.retrieval.eligibility -> tychora.adapters.ladybug.traversal`.

Run: `uv run --locked lint-imports --no-cache && uv run --locked pytest tests/retrieval/test_graph_policy.py tests/application/test_collections.py -q`
Expected: every contract kept; PASS.

- [ ] **Step 2: Rename the legacy compiler and attempt**

In `src/tychora/retrieval/eligibility.py`: `class EligibilityCompiler` → `class LegacyEligibilityCompiler`; `class EligibilityAttempt` → `class LegacyAttempt` with only the fields `view`, `compiler`, `dependencies`, `warnings`, `historical_kind_roles`, `historical_kind_envelopes` and the method `dependencies_for`; delete `owner_provider`, `run_pinned_eligibility` and `eligible_cache_key`; `compile_attempt` returns `LegacyAttempt`. Update the importers named in **Files**; the tests that build attempts for the query path move to the new `EligibilityAttempt` in Step 8.

- [ ] **Step 3: Write the failing tests**

```python
# tests/retrieval/test_eligibility_cache.py
"""Compiled views are cached by snapshot, epoch, scope, policy and request digests."""

import pytest

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.identity import digest
from tychora.retrieval.cache import ELIGIBILITY_CACHE_SIZE, EligibilityCache
from tychora.retrieval.compiler import (
    CompiledEligibility,
    EligibilityKey,
    eligible_cache_key,
)


def _compiled(epoch=0, request="request"):
    return CompiledEligibility(
        EligibilityView(
            snapshot_id="snapshot",
            policy_epoch=epoch,
            scope_digest=digest("scope"),
            eligibility_policy_digest=digest("policy"),
            eligibility_request_digest=digest(request),
            revisions={},
        ),
        warnings=("edition warning",),
    )


def test_hit_returns_the_cached_entry_without_compiling():
    cache = EligibilityCache()
    compiled = _compiled()
    key = eligible_cache_key(compiled.view)
    assert cache.get_or_compile(key, lambda: compiled) is compiled
    assert cache.get_or_compile(key, lambda: pytest.fail("compiled twice")) is compiled


def test_a_view_that_does_not_answer_its_key_is_refused():
    cache = EligibilityCache()
    key = EligibilityKey("snapshot", 1, digest("scope"), digest("policy"), digest("request"))
    with pytest.raises(TychoraError) as error:
        cache.get_or_compile(key, lambda: _compiled(epoch=0))
    assert error.value.reason == "invariant.unexpected"


def test_least_recent_entry_is_evicted():
    cache = EligibilityCache(capacity=1)
    first, second = _compiled(request="a"), _compiled(request="b")
    cache.get_or_compile(eligible_cache_key(first.view), lambda: first)
    cache.get_or_compile(eligible_cache_key(second.view), lambda: second)
    fresh = _compiled(request="a")
    assert cache.get_or_compile(eligible_cache_key(first.view), lambda: fresh) is fresh
    assert ELIGIBILITY_CACHE_SIZE == 64
    assert cache.purge() == 1
```

```python
# tests/retrieval/test_eligibility_v2.py
"""The revision-level view admits exactly what the unit-level compiler admitted."""

import random
from datetime import date

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.collections import CollectionCreate
from tychora.contracts.corpus import ReviewTarget
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_context_unit,
    is_eligible_asset,
    is_source_unit,
    is_structural_unit,
)
from tychora.retrieval.cache import EligibilityCache
from tychora.retrieval.compiler import EligibilityCompiler
from tychora.retrieval.eligibility import LegacyEligibilityCompiler

ITERATIONS = 500
MUTATE_EVERY = 25

_LAW = "Article 1\nThe operator inspects seals.\n\nArticle 2\nThe operator records results.\n"
_STANDARD = "STD-1 Seals are inspected quarterly.\nSTD-2 Results are recorded.\n"
_MANUAL = "# Pumps\n\nInspect the pump seal.\n\n## Valves\n\nReplace worn valves.\n"
_SOURCES = (
    # (collection, key, kind, text, metadata)
    ("default", "law.txt", "law@1", _LAW, {"edition": "1", "effective_start": "2020-01-01", "effective_end": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "law.txt", "law@1", _LAW + "Article 3\nNew duty.\n", {"edition": "2", "effective_start": "2023-01-01", "scope": {"project": "alpha"}}),
    ("default", "std.txt", "standard@1", _STANDARD, {"edition": "1", "publication_date": "2021-05-01", "approval": "accepted"}),
    ("default", "std.txt", "standard@1", _STANDARD + "STD-3 Added rule.\n", {"edition": "2", "publication_date": "2022-05-01"}),
    ("default", "manual.md", "manual@1", _MANUAL, {"scope": {"project": "beta"}}),
    ("default", "undated.md", "manual@1", _MANUAL.replace("Pumps", "Fans"), {"edition": "1"}),
    ("second", "notes.md", "rationale@1", "# Why\n\nSeals fail early.\n", {"scope": {"project": "alpha"}}),
    ("second", "guide.md", "guidance@1", "# Guide\n\nCheck seals.\n", {"approval": "rejected"}),
    ("second", "manual.md", "manual@1", _MANUAL.replace("Valves", "Hoses"), {"publication_date": "2024-02-01"}),
)


def _add(kb, collections, collection, key, kind, text, metadata):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": collections[collection],
            "kind": kind,
            "format": "markdown" if key.endswith(".md") else "text",
            "text": text,
            "metadata": {"title": key, **metadata},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    path = tmp_path_factory.mktemp("equivalence") / "ws"
    with Tychora.open(path, token_counter=OutputCounter()) as kb:
        second = kb._workspace.bridge.call(
            kb._workspace.services.collections.create(
                CollectionCreate(title="Second", slug="second"),
                kb.scope,
                ExecutionContext(),
                managed_owner_authorized=True,
            )
        )
        collections = {
            "default": str(kb._workspace.default_collection_id),
            "second": str(second.collection_id),
        }
        for row in _SOURCES:
            _add(kb, collections, *row)
        yield kb, collections


def _scope(rng, collections):
    chosen = frozenset(c for c in collections.values() if rng.random() < 0.8)
    return RequestScope(
        principal_id="equivalence",
        collection_ids=chosen or frozenset(collections.values()),
        access_ceiling=rng.randint(0, 3),
        uses=frozenset(u for u in ("compose", "audit", "lookup") if rng.random() < 0.7)
        or frozenset({"lookup"}),
        capabilities=frozenset({"read"}),
    )


def _request(rng, revision_ids):
    value = {"question": "seal inspection", "mode": rng.choice(["compose", "audit", "lookup"])}
    if rng.random() < 0.3:
        value["filters"] = rng.choice(
            [
                {"op": "eq", "field": "project", "value": "alpha"},
                {"op": "in", "field": "project", "values": ["alpha", "beta"]},
            ]
        )
        value["include_global"] = rng.random() < 0.5
    if rng.random() < 0.3:
        value["as_of"] = date(rng.randint(2019, 2025), rng.randint(1, 12), 1).isoformat()
    if rng.random() < 0.2:
        value["cutoff"] = date(rng.randint(2020, 2025), 6, 1).isoformat()
    if rng.random() < 0.2:
        value["revision_selector"] = "all"
    if rng.random() < 0.2:
        value["unknown_dates"] = "permissive"
    if rng.random() < 0.1 and revision_ids:
        value["revision_ids"] = rng.sample(sorted(revision_ids), 1)
    return QueryRequest.from_mapping(value)


def _mutate(rng, owner):
    store = owner.store
    if rng.random() < 0.5:
        unit_id = rng.choice([r[0] for r in owner.db.execute("SELECT unit_id FROM evidence_units")])
        target = ReviewTarget(unit_id=unit_id)
    else:
        revision = rng.choice([r[0] for r in owner.db.execute("SELECT revision_id FROM revisions")])
        target = ReviewTarget(revision_id=revision)
    choice = rng.random()
    if choice < 0.6:
        store.record_review(target, rng.choice(["accepted", "needs_review", "rejected"]), "eq")
    elif choice < 0.8 and target.revision_id is not None:
        store.raise_access_floor(target, rng.randint(1, 2))
    else:
        source = rng.choice([r[0] for r in owner.db.execute("SELECT source_id FROM sources")])
        store.set_allowed_uses_override(source, rng.choice(["never_generate", "reference_only", None]))


def _legacy(owner, legacy, snapshot, scope, request):
    return owner.run_write_sync(
        lambda: legacy.compile_eligibility(snapshot, scope, request, owner.config),
        ExecutionContext(),
    )


def test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures(corpus):
    kb, collections = corpus
    owner = kb._workspace.owner
    bridge = kb._workspace.bridge
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    legacy = LegacyEligibilityCompiler(owner.store)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))

    def facts(tx):
        units = tuple(r[0] for r in tx.eligibility._db.execute("SELECT unit_id FROM evidence_units"))
        chunks = tuple(
            r[0]
            for r in tx.eligibility._db.execute(
                "SELECT r.chunk_id FROM snapshot_batches sb JOIN lexical_records r "
                "ON r.batch_id=sb.batch_id WHERE sb.snapshot_id=? AND sb.slot='lexical'",
                (snapshot.snapshot_id,),
            )
        )
        return {f.owner_id: f for f in tx.eligibility.owner_facts(units + chunks)}

    by_id = bridge.call(owner.run_read(facts, ExecutionContext()))
    revision_ids = {r[0] for r in owner_rows(owner, "SELECT revision_id FROM revisions")}
    rng = random.Random(20261023)
    compared = 0
    for iteration in range(ITERATIONS):
        if iteration and iteration % MUTATE_EVERY == 0:
            owner.run_write_sync(lambda: _mutate(rng, owner), ExecutionContext())
        scope, request = _scope(rng, collections), _request(rng, revision_ids)
        try:
            v1 = _legacy(owner, legacy, snapshot, scope, request)
        except TychoraError as error:
            with pytest.raises(TychoraError) as v2_error:
                bridge.call(
                    owner.run_read(
                        lambda tx: compiler.compile(tx, snapshot, scope, request, owner.config),
                        ExecutionContext(),
                    )
                )
            assert v2_error.value.code == error.code
            continue
        v2 = bridge.call(
            owner.run_read(
                lambda tx: compiler.compile(tx, snapshot, scope, request, owner.config).view,
                ExecutionContext(),
            )
        )
        units = [f for f in by_id.values() if f.kind == "unit"]
        chunks = [f for f in by_id.values() if f.kind == "chunk"]
        assert set(v2.revisions) == v1.revision_ids, (iteration, request)
        assert {f.owner_id for f in units if is_source_unit(v2, f)} == v1.source_unit_ids
        assert {f.owner_id for f in units if is_context_unit(v2, f)} == v1.context_unit_ids
        assert {f.owner_id for f in units if is_structural_unit(v2, f)} == v1.structural_unit_ids
        assert {f.owner_id for f in chunks if is_admitted_chunk(v2, f, by_id)} == v1.chunk_ids
        assert {f.owner_id for f in by_id.values() if is_eligible_asset(v2, f)} == v1.asset_ids
        compared += 1
    assert compared > ITERATIONS // 2


def test_cache_hit_across_different_questions_same_filters(corpus):
    kb, _ = corpus
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    calls = []
    original = compiler._compile

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    compiler._compile = counted
    for question in ("seal inspection", "worn valves"):
        request = QueryRequest.from_mapping({"question": question})
        kb._workspace.bridge.call(
            owner.run_read(
                lambda tx, request=request: compiler.compile_cached(
                    tx, cache, snapshot, owner.scope, request, owner.config
                ),
                ExecutionContext(),
            )
        )
    assert len(calls) == 1


def test_cache_miss_after_epoch_bump(corpus):
    kb, _ = corpus
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    cache = EligibilityCache()
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping({"question": "seal"})

    def compiled():
        return kb._workspace.bridge.call(
            owner.run_read(
                lambda tx: compiler.compile_cached(
                    tx, cache, snapshot, owner.scope, request, owner.config
                ),
                ExecutionContext(),
            )
        )

    first = compiled()
    (unit_id,) = owner_row(owner, "SELECT unit_id FROM evidence_units LIMIT 1")
    on_owner(
        owner,
        lambda: owner.store.record_review(ReviewTarget(unit_id=unit_id), "rejected", "r"),
    )
    second = compiled()
    assert second.view.policy_epoch == first.view.policy_epoch + 1
    assert unit_id in second.view.excluded_unit_ids
    assert unit_id not in first.view.excluded_unit_ids


def test_compiler_never_calls_prepare_extraction(corpus, monkeypatch):
    import tychora.indexing.representations as representations

    monkeypatch.setattr(
        representations,
        "prepare_extraction",
        lambda *a, **k: pytest.fail("compile derived representation facts"),
    )
    kb, _ = corpus
    owner = kb._workspace.owner
    compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)
    snapshot = on_owner(owner, lambda: owner.store.resolve_snapshot(None))
    request = QueryRequest.from_mapping({"question": "seal"})
    view = kb._workspace.bridge.call(
        owner.run_read(
            lambda tx: compiler.compile(tx, snapshot, owner.scope, request, owner.config).view,
            ExecutionContext(),
        )
    )
    assert view.revisions
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_eligibility_cache.py tests/retrieval/test_eligibility_v2.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'tychora.retrieval.compiler'`.

- [ ] **Step 5: Register the reasons**

In `src/tychora/contracts/error_reasons.py`, add (each after the last entry of its code; `tests/contracts/test_error_reasons.py` `CLASSIFIED_REASONS` gains the same names under their codes, with a new `"SCOPE_CHANGED": {"policy.epoch_changed"}` entry):

```python
    Reason(
        "INVALID_REQUEST",
        "request.limit_exceeded",
        False,
        "Lower the request's limit or budgets to the profile's limits.",
    ),
    Reason(
        "INVALID_REQUEST",
        "mode.unavailable",
        False,
        "Use a retrieval mode the profile defines.",
    ),
    Reason(
        "INVALID_REQUEST",
        "profile.snapshot_mismatch",
        False,
        "Open the workspace with the profile that published its snapshots.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "kind.historical_unavailable",
        False,
        "Restore the retained kind definition or rebuild the workspace.",
    ),
    Reason(
        "PROFILE_INCOMPATIBLE",
        "facet.binding_unverified",
        False,
        "Publish a new edition of the source so its global facets are bound again.",
    ),
    Reason(
        "SCOPE_CHANGED",
        "policy.epoch_changed",
        True,
        "Retry; workspace policy changed while the query ran.",
    ),
```

- [ ] **Step 6: Write the compiler**

```python
# src/tychora/retrieval/compiler.py
"""Compile one pinned snapshot into a revision-level eligibility view."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from tychora.configuration.collection_policy import collection_is_active
from tychora.configuration.global_facets import SOURCE_REF as GLOBAL_FACET_SOURCE_REF
from tychora.configuration.global_facets import (
    binding_bytes as global_facet_binding_bytes,
)
from tychora.configuration.graph_policy import resolve_graph_traversal_policy
from tychora.configuration.indexed_representation import pinned_runtime_compatible
from tychora.configuration.models import ResolvedConfig
from tychora.configuration.registry import Registry, ScopedFilter, compile_filter
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView, GraphBinding, RevisionGrant
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import JsonValue, digest
from tychora.contracts.publication import SnapshotMember, SnapshotView
from tychora.contracts.requests import (
    AndFilter,
    EqFilter,
    FilterExpr,
    InFilter,
    OrFilter,
    QueryRequest,
    RangeFilter,
    RequestScope,
    canonical_scope_projection,
)
from tychora.ports.artifacts import ArtifactReader
from tychora.ports.repositories import FacetValue, MemberRow, ReadTransaction, ReviewRow
from tychora.retrieval.selection import RevisionFact, select_temporal_revisions

if TYPE_CHECKING:
    from tychora.retrieval.cache import EligibilityCache

REVIEW_STATES = ("unreviewed", "needs_review", "accepted", "rejected")
_GLOBAL_BINDING_LIMIT = 65536


class EligibilityKey(NamedTuple):
    """What a compiled view depends on; equal keys share one view."""

    snapshot_id: str
    policy_epoch: int
    scope_digest: str
    policy_digest: str
    request_digest: str


def eligible_cache_key(view: EligibilityView) -> EligibilityKey:
    """The key a compiled view answers."""
    return EligibilityKey(
        view.snapshot_id,
        view.policy_epoch,
        view.scope_digest,
        view.eligibility_policy_digest,
        view.eligibility_request_digest,
    )


@dataclass(frozen=True)
class CompiledEligibility:
    """A view and what compiling it decided about editions and historical kinds."""

    view: EligibilityView
    warnings: tuple[str, ...] = ()
    historical_kind_roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class ModePolicy:
    """A retrieval mode bound to the caller's uses."""

    name: str
    digest: str
    allowed_lanes: frozenset[str]
    allowed_uses: frozenset[str]
    original_reviews: frozenset[str]
    proxy_reviews: frozenset[str]
    proxy_disabled: bool


@dataclass(frozen=True)
class _Kind:
    definition: JsonValue
    envelope: FrozenJsonMap
    allowed_uses: frozenset[str]
    role: str | None


def _invalid(message: str, reason: str) -> TychoraError:
    return TychoraError("INVALID_REQUEST", message, reason=reason)


def _differs(message: str) -> TychoraError:
    return TychoraError(
        "INTEGRITY_ERROR", message, reason="canonical.binding_mismatch"
    )


def check_request_limits(request: QueryRequest, config: ResolvedConfig) -> None:
    """Refuse a request beyond the profile's limits."""
    limits = config.values.limits
    if (
        request.limit > limits.limit
        or request.budgets.max_tokens > limits.max_tokens
        or request.budgets.max_response_bytes > limits.max_response_bytes
    ):
        raise _invalid(
            "Request exceeds resolved profile limits", "request.limit_exceeded"
        )


def request_digest(request: QueryRequest, selected: ScopedFilter | None) -> str:
    """Digest of the request fields that change what a query may read."""
    return digest(
        "eligibility-request-v2",
        selected.model_dump(mode="json") if selected is not None else None,
        list(request.revision_ids),
        request.revision_selector,
        request.as_of.isoformat() if request.as_of else None,
        request.cutoff.isoformat() if request.cutoff else None,
        request.unknown_dates,
        request.include_global,
    )
```

Then move `_filter_matches` verbatim from `retrieval/eligibility.py:211-297` under the name `filter_matches` (it recurses through `filter_matches`), and add the stage functions:

```python
def _filter_keys(expr: FilterExpr) -> set[str]:
    if isinstance(expr, (AndFilter, OrFilter)):
        return set().union(*(_filter_keys(child) for child in expr.children))
    return {expr.field}


def _grouped(rows: Iterable[FacetValue]) -> dict[str, dict[str, object]]:
    found: dict[str, dict[str, object]] = defaultdict(dict)
    for row in rows:
        found[row.owner_id][row.facet_key] = row.value
    return found


def _member_rows(
    tx: ReadTransaction,
    snapshot: SnapshotView,
    members: tuple[SnapshotMember, ...] | None,
) -> tuple[MemberRow, ...]:
    """One row per member, with its revision, source, collection and kind facts."""
    if members is not None:
        if not members:
            return ()
        defaults = {m.extraction_id: m.is_default for m in members}
        rows = tuple(
            row._replace(is_default=defaults[row.extraction_id])
            for row in tx.eligibility.members(snapshot.snapshot_id, tuple(defaults))
        )
    else:
        rows = tx.eligibility.members(snapshot.snapshot_id)
        if len(rows) != snapshot.member_count:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Snapshot members lack canonical rows",
                reason="canonical.row_invalid",
            )
    if any(row.workspace_id != snapshot.workspace_id for row in rows):
        raise _differs("Selected extraction belongs to another workspace")
    return rows


def _readable(
    row: MemberRow, kind: _Kind, scope: RequestScope, mode: ModePolicy, active: bool
) -> bool:
    """Revision-level policy: scope, floors, withdrawal, archive, review and uses."""
    uses = frozenset(json.loads(row.allowed_uses_json))
    if row.allowed_uses_override is not None:
        uses &= frozenset(json.loads(row.allowed_uses_override))
    return (
        row.collection_id in scope.collection_ids
        and not row.withdrawn
        and active
        and max(row.access_rank, row.source_floor, row.collection_floor)
        <= scope.access_ceiling
        and row.review_state in mode.original_reviews
        and bool(uses & mode.allowed_uses)
        and bool(kind.allowed_uses & mode.allowed_uses)
    )


def _admitted_members(
    rows: tuple[MemberRow, ...],
    kinds: Mapping[str, _Kind],
    scope: RequestScope,
    mode: ModePolicy,
    registry: Registry,
) -> list[MemberRow]:
    """Members whose revision passes policy and whose kind's lane the mode allows."""
    active: dict[str, bool] = {}
    readable = []
    for row in rows:
        if row.collection_id not in active:
            active[row.collection_id] = collection_is_active(row.collection_policy_json)
        if _readable(row, kinds[row.kind_id], scope, mode, active[row.collection_id]):
            readable.append(row)
    readable_kinds = frozenset(row.kind_id for row in readable)
    registry.validate_activation(
        readable_kinds, {kind: kinds[kind].definition for kind in readable_kinds}
    )
    return [
        row
        for row in readable
        if registry.lane_bindings.get(row.kind_id) in mode.allowed_lanes
    ]


def _revision_fact(
    row: MemberRow, kind: _Kind, global_facets: Mapping[str, frozenset[str]]
) -> RevisionFact:
    return RevisionFact(
        source_id=row.source_id,
        revision_id=row.revision_id,
        extraction_id=row.extraction_id,
        collection_id=row.collection_id,
        kind_id=row.kind_id,
        access_rank=row.access_rank,
        source_floor=row.source_floor,
        collection_floor=row.collection_floor,
        allowed_uses=frozenset(json.loads(row.allowed_uses_json)),
        review_state=row.review_state,
        withdrawn=row.withdrawn,
        published_on=row.published_on,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        is_default=row.is_default,
        kind_allowed_uses=kind.allowed_uses,
        global_facets=global_facets.get(row.revision_id, frozenset()),
    )


def _filter_sets(
    tx: ReadTransaction,
    registry: Registry,
    selected: ScopedFilter | None,
    request: QueryRequest,
    chosen: list[MemberRow],
    global_facets: Mapping[str, frozenset[str]],
) -> tuple[frozenset[str], frozenset[str] | None]:
    """Units failing the all-units filter, and units passing the candidate filter."""
    if selected is None or (
        selected.all_units is None and selected.matched_candidates is None
    ):
        return frozenset(), None
    revision_of = {row.extraction_id: row.revision_id for row in chosen}
    keys: set[str] = set()
    for expr in (selected.all_units, selected.matched_candidates):
        if expr is not None:
            keys |= _filter_keys(expr)
    facet_keys = tuple(sorted(keys))
    extractions = tuple(sorted(revision_of))
    units = tx.eligibility.unit_flags(extractions)
    by_unit = _grouped(tx.eligibility.unit_facets(extractions, facet_keys))
    by_revision = _grouped(
        tx.eligibility.revision_facets(tuple(sorted(revision_of.values())), facet_keys)
    )

    def matches(expr: FilterExpr, unit_id: str, extraction_id: str) -> bool:
        revision = revision_of[extraction_id]
        return filter_matches(
            expr,
            registry,
            by_revision.get(revision, {}),
            by_unit.get(unit_id, {}),
            include_global=request.include_global,
            global_facets=global_facets.get(revision, frozenset()),
        )

    failing = frozenset(
        u.unit_id
        for u in units
        if selected.all_units is not None
        and not matches(selected.all_units, u.unit_id, u.extraction_id)
    )
    if selected.matched_candidates is None:
        return failing, None
    return failing, frozenset(
        u.unit_id
        for u in units
        if matches(selected.matched_candidates, u.unit_id, u.extraction_id)
    )


def _excluded(row: ReviewRow, mode: ModePolicy) -> bool:
    if row.kind == "asset":
        return row.state not in mode.original_reviews
    if row.proxy:
        return mode.proxy_disabled or row.state not in mode.proxy_reviews
    return row.state not in mode.original_reviews


def _exclusions(
    tx: ReadTransaction,
    chosen: list[MemberRow],
    mode: ModePolicy,
    failing: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Units and assets the mode excludes, closed under dependency."""
    extractions = tuple(sorted(row.extraction_id for row in chosen))
    excluded_states = tuple(s for s in REVIEW_STATES if s not in mode.original_reviews)
    reviews = {
        row.target_id: row
        for row in tx.eligibility.review_rows(extractions, excluded_states)
    }
    rejected = tuple(
        sorted(row.extraction_id for row in chosen if row.review_state == "rejected")
    )
    if rejected:
        # A rejected revision's units and assets are rejected whatever their own reviews.
        for row in tx.eligibility.review_rows(rejected, REVIEW_STATES):
            reviews[row.target_id] = row._replace(state="rejected")
    seeds = set(failing) | {
        target for target, row in reviews.items() if _excluded(row, mode)
    }
    closure = tx.eligibility.dependents(tuple(sorted(seeds)))
    return (
        frozenset(i for i, kind in closure.items() if kind == "unit"),
        frozenset(i for i, kind in closure.items() if kind == "asset"),
    )


def _selected_batches(
    rows: Iterable[tuple[str, str, str | None]], extraction_ids: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = defaultdict(list)
    for slot, batch, extraction in rows:
        if slot == "graph" or extraction in extraction_ids:
            found[slot].append(batch)
    return {slot: tuple(batches) for slot, batches in found.items()}


def _graph_binding(
    tx: ReadTransaction, batches: Mapping[str, tuple[str, ...]], all_eligible: bool
) -> GraphBinding | None:
    graph = batches.get("graph", ())
    if not graph:
        return None
    if len(graph) != 1:
        raise _differs("Snapshot binds more than one graph generation")
    receipt = tx.batches.receipt(graph[0])
    if not isinstance(receipt, GraphBatchReceipt):
        raise TychoraError(
            "INTEGRITY_ERROR",
            "Graph batch receipt is unavailable",
            reason="canonical.row_invalid",
        )
    return GraphBinding(
        family_id=receipt.family_id, all_eligible=all_eligible, receipt=receipt
    )


def _historical(
    registry: Registry, kinds: Mapping[str, _Kind], chosen: list[MemberRow]
) -> tuple[Mapping[str, str], Mapping[str, FrozenJsonMap]]:
    """Roles of selected kinds the current registry lacks, and every selected envelope."""
    selected = {row.kind_id for row in chosen}
    roles = {}
    for kind_id in sorted(selected - set(registry.kinds)):
        role = kinds[kind_id].role
        if not role:
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Selected historical kind role is unavailable",
                reason="kind.historical_unavailable",
            )
        roles[kind_id] = role
    return (
        MappingProxyType(roles),
        MappingProxyType({kind: kinds[kind].envelope for kind in selected}),
    )
```

and the compiler class:

```python
class EligibilityCompiler:
    """Compiles revision-level views; kind envelopes and mode policies are cached."""

    def __init__(self, registry: Registry, artifacts: ArtifactReader) -> None:
        self.registry = registry
        self.artifacts = artifacts
        self._kinds: dict[tuple[str, str], _Kind] = {}
        self._modes: dict[tuple[str, str, frozenset[str]], ModePolicy] = {}
        self._compatible: set[tuple[str, str]] = set()
        self._lock = Lock()

    def mode(
        self, config: ResolvedConfig, request: QueryRequest, scope: RequestScope
    ) -> ModePolicy:
        """The request's mode bound to the scope's uses, with its policy digest."""
        name = request.mode or config.values.mode or config.values.default_mode
        if name is None or name not in self.registry.modes:
            raise _invalid("Retrieval mode is unavailable", "mode.unavailable")
        key = (config.config_sha, name, scope.uses)
        with self._lock:
            cached = self._modes.get(key)
        if cached is not None:
            return cached
        definition = cast(Mapping[str, Any], self.registry.modes[name])
        policy = resolve_graph_traversal_policy(config, self.registry)
        proxy_reviews = frozenset(definition.get("derived_proxy_review_states", ()))
        if definition["derived_proxy_policy"] == "accepted_only":
            proxy_reviews &= {"accepted"}
        mode = ModePolicy(
            name=name,
            digest=digest(
                "eligibility-policy-v1",
                config.eligibility_fingerprint,
                name,
                definition,
                policy.model_dump(mode="json"),
            ),
            allowed_lanes=frozenset(definition["allowed_lanes"]),
            allowed_uses=frozenset(definition["allowed_uses"]) & scope.uses,
            original_reviews=frozenset(definition["original_review_states"]),
            proxy_reviews=proxy_reviews,
            proxy_disabled=definition["derived_proxy_policy"] == "disabled"
            or not definition.get("allow_derived_proxies", True),
        )
        with self._lock:
            self._modes[key] = mode
        return mode

    def knows_compatible(self, snapshot: SnapshotView, config: ResolvedConfig) -> bool:
        """Whether the snapshot's retained configuration is known to serve ``config``."""
        with self._lock:
            return snapshot.config_sha == config.retained_sha or (
                snapshot.config_sha,
                config.config_sha,
            ) in self._compatible

    def mark_compatible(
        self, snapshot: SnapshotView, config: ResolvedConfig, retained: ResolvedConfig
    ) -> None:
        """Record that the retained configuration serves ``config``, or refuse it."""
        if not pinned_runtime_compatible(retained, config):
            raise TychoraError(
                "PROFILE_INCOMPATIBLE",
                "Pinned snapshot requires publication under current settings.",
                reason="index.rebuild_required",
            )
        with self._lock:
            self._compatible.add((snapshot.config_sha, config.config_sha))

    def key(
        self,
        tx: ReadTransaction,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
    ) -> tuple[EligibilityKey, ModePolicy, ScopedFilter | None]:
        """The cache key of this request, with its mode and compiled filter."""
        check_request_limits(request, config)
        if self.registry.registry_digest != config.registry_digest:
            raise _invalid(
                "Snapshot and resolved profile differ", "profile.snapshot_mismatch"
            )
        if not self.knows_compatible(snapshot, config):
            raise _invalid(
                "Snapshot configuration was not checked against the profile",
                "profile.snapshot_mismatch",
            )
        mode = self.mode(config, request, scope)
        selected = (
            compile_filter(request.filters, self.registry) if request.filters else None
        )
        return (
            EligibilityKey(
                snapshot.snapshot_id,
                tx.workspace.policy_epoch(),
                digest("eligibility-scope-v2", canonical_scope_projection(scope)),
                mode.digest,
                request_digest(request, selected),
            ),
            mode,
            selected,
        )

    def compile(
        self,
        tx: ReadTransaction,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
        *,
        members: tuple[SnapshotMember, ...] | None = None,
    ) -> CompiledEligibility:
        """Compile without the cache (explicit retained members are never cached)."""
        key, mode, selected = self.key(tx, snapshot, scope, request, config)
        return self._compile(tx, key, snapshot, scope, request, mode, selected, members)

    def compile_cached(
        self,
        tx: ReadTransaction,
        cache: EligibilityCache,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        config: ResolvedConfig,
    ) -> CompiledEligibility:
        """Return the cached view for this key, compiling it once on a miss."""
        key, mode, selected = self.key(tx, snapshot, scope, request, config)
        return cache.get_or_compile(
            key,
            lambda: self._compile(
                tx, key, snapshot, scope, request, mode, selected, None
            ),
        )

    def _compile(
        self,
        tx: ReadTransaction,
        key: EligibilityKey,
        snapshot: SnapshotView,
        scope: RequestScope,
        request: QueryRequest,
        mode: ModePolicy,
        selected: ScopedFilter | None,
        members: tuple[SnapshotMember, ...] | None,
    ) -> CompiledEligibility:
        rows = _member_rows(tx, snapshot, members)
        kinds = self._kind_envelopes(tx, rows)
        admitted = _admitted_members(rows, kinds, scope, mode, self.registry)
        facets = self._global_facets(tx, admitted) if selected is not None else {}
        selected_ids, warnings = select_temporal_revisions(
            tuple(_revision_fact(r, kinds[r.kind_id], facets) for r in admitted),
            explicit_ids=request.revision_ids,
            selector=request.revision_selector,
            as_of=request.as_of,
            cutoff=request.cutoff,
            unknown_dates=request.unknown_dates,
        )
        chosen = [row for row in admitted if row.revision_id in selected_ids]
        grants = {
            row.revision_id: RevisionGrant(
                extraction_id=row.extraction_id,
                source_id=row.source_id,
                kind_id=row.kind_id,
                lane=cast(str, self.registry.lane_bindings[row.kind_id]),
                collection_id=row.collection_id,
            )
            for row in chosen
        }
        failing, unit_filter = _filter_sets(
            tx, self.registry, selected, request, chosen, facets
        )
        units, assets = _exclusions(tx, chosen, mode, failing)
        batches = _selected_batches(
            tx.eligibility.batches(snapshot.snapshot_id),
            frozenset(g.extraction_id for g in grants.values()),
        )
        complete = (
            len(grants) == snapshot.member_count
            and not units
            and not assets
            and unit_filter is None
        )
        view = EligibilityView(
            snapshot_id=key.snapshot_id,
            policy_epoch=key.policy_epoch,
            scope_digest=key.scope_digest,
            eligibility_policy_digest=key.policy_digest,
            eligibility_request_digest=key.request_digest,
            revisions=grants,
            excluded_unit_ids=units,
            excluded_asset_ids=assets,
            unit_filter=unit_filter,
            selected_batches=batches,
            graph=_graph_binding(tx, batches, complete),
            all_members_eligible=len(grants) == snapshot.member_count,
        )
        roles, envelopes = _historical(self.registry, kinds, chosen)
        return CompiledEligibility(view, warnings, roles, envelopes)

    def _kind_envelopes(
        self, tx: ReadTransaction, rows: Iterable[MemberRow]
    ) -> dict[str, _Kind]:
        """Each member kind's verified envelope, cached by kind and definition hash."""
        needed = {(row.kind_id, row.definition_hash) for row in rows}
        with self._lock:
            missing = sorted({kind for kind, h in needed if (kind, h) not in self._kinds})
        for kind_id, definition_hash, raw in tx.eligibility.kind_definitions(
            tuple(missing)
        ):
            envelope = json.loads(raw)
            recorded = envelope.pop("envelope_digest", None)
            if recorded != definition_hash or digest("historical-kind-v1", envelope) != recorded:
                raise _differs("Original kind envelope differs from canonical binding")
            definition = {**envelope, "envelope_digest": recorded}
            body = envelope.get("kind") or {}
            with self._lock:
                self._kinds[(kind_id, definition_hash)] = _Kind(
                    definition=cast(JsonValue, definition),
                    envelope=FrozenJsonMap(definition),
                    allowed_uses=frozenset(body.get("allowed_uses", ())),
                    role=body.get("role"),
                )
        with self._lock:
            return {kind: self._kinds[(kind, h)] for kind, h in needed}

    def _global_facets(
        self, tx: ReadTransaction, admitted: list[MemberRow]
    ) -> dict[str, frozenset[str]]:
        """Verified global facets of the admitted revisions; read only for filters."""
        revisions = tuple(sorted(row.revision_id for row in admitted))
        markers: dict[str, list] = defaultdict(list)
        for revision, source_ref, ref in tx.eligibility.global_facet_markers(revisions):
            markers[revision].append((source_ref, ref))
        found: dict[str, frozenset[str]] = {}
        for revision, raw, metadata_sha in tx.eligibility.revision_metadata(revisions):
            metadata = FrozenJsonMap(json.loads(raw))
            if digest("metadata-v1", metadata) != metadata_sha:
                raise _differs("Selected metadata differs from immutable digest")
            facets, binding = global_facet_binding_bytes(
                metadata, revision, metadata_sha, self.registry
            )
            self._verify_binding(binding, markers.get(revision, []))
            found[revision] = facets
        return found

    def _verify_binding(self, binding: bytes | None, rows: list) -> None:
        unverified = TychoraError(
            "PROFILE_INCOMPATIBLE",
            "Retained global facet lacks a verified binding; publish a new edition",
            reason="facet.binding_unverified",
        )
        if binding is None:
            if rows:
                raise _differs("Undeclared global facet binding exists")
            return
        if (
            len(rows) != 1
            or rows[0][0] != GLOBAL_FACET_SOURCE_REF
            or rows[0][1] is None
            or rows[0][1].size_bytes > _GLOBAL_BINDING_LIMIT
        ):
            raise unverified
        try:
            with self.artifacts.open_verified(rows[0][1]) as stream:
                if stream.read(_GLOBAL_BINDING_LIMIT + 1) != binding:
                    raise unverified
        except OSError as error:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Global facet binding payload is unavailable",
                reason="payload.missing",
            ) from error
```

- [ ] **Step 7: Add the cache**

In `src/tychora/retrieval/cache.py` (`OrderedDict`, `RLock` are already imported; add `from collections.abc import Callable` and `from tychora.contracts.errors import TychoraError`, and import `CompiledEligibility`, `EligibilityKey`, `eligible_cache_key` from `tychora.retrieval.compiler`):

```python
ELIGIBILITY_CACHE_SIZE = 64


class EligibilityCache:
    """LRU of compiled views keyed by snapshot, policy epoch, scope, policy and request.

    Two concurrent misses on one key may both compile; the second result
    replaces the first, which is equal.
    """

    def __init__(self, capacity: int = ELIGIBILITY_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[EligibilityKey, CompiledEligibility] = OrderedDict()
        self._lock = RLock()

    def get_or_compile(
        self, key: EligibilityKey, compile: Callable[[], CompiledEligibility]
    ) -> CompiledEligibility:
        """Return the cached entry for ``key``, or compile, check and keep one."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry
        entry = compile()
        if eligible_cache_key(entry.view) != key:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Compiled view does not answer its cache key",
                reason="invariant.unexpected",
            )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return entry

    def purge(self) -> int:
        """Drop every entry; return how many there were."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count
```

In `WorkspaceOwner.__init__`, add `self.eligibility_cache = EligibilityCache()` (import from `tychora.retrieval.cache`).

- [ ] **Step 8: Pinned attempts and admission predicates**

```python
# src/tychora/retrieval/attempt.py
"""One pinned query attempt: its snapshot, its view and the epoch checks around it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.contracts.graph import LegacyEligibilityView
from tychora.contracts.publication import SnapshotView
from tychora.contracts.requests import QueryRequest, RequestScope
from tychora.ports.execution import ExecutionContext
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.retrieval.catalog import SnapshotCatalog
from tychora.retrieval.compiler import EligibilityCompiler


class AttemptOwner(Protocol):
    """The owner operations a pinned attempt runs on."""

    scope: RequestScope
    catalog: SnapshotCatalog
    eligibility_cache: Any
    store: Any

    async def run_read(self, call: Callable[[ReadTransaction], Any], ctx: ExecutionContext) -> Any: ...

    async def run_write(
        self, call: Callable[[], Any], ctx: ExecutionContext, priority: Literal["interactive", "background"] = "background"
    ) -> Any: ...


def _changed() -> TychoraError:
    return TychoraError(
        "SCOPE_CHANGED",
        "Workspace policy changed during retrieval",
        reason="policy.epoch_changed",
    )


def resolve_snapshot(
    catalog: SnapshotCatalog, tx: ReadTransaction, selector: str | None
) -> SnapshotView:
    """The selected or active snapshot's view, through the catalog."""
    snapshot_id = selector or tx.snapshots.active_id()
    if snapshot_id is None:
        raise TychoraError(
            "NOT_FOUND", "No published snapshot is available", reason="snapshot.absent"
        )
    return catalog.get(tx, snapshot_id)


@dataclass(frozen=True)
class EligibilityAttempt:
    """One compiled attempt; provider calls and delivery recheck its policy epoch."""

    view: EligibilityView
    snapshot: SnapshotView
    warnings: tuple[str, ...] = ()
    historical_kind_roles: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    historical_kind_envelopes: Mapping[str, FrozenJsonMap] = field(
        default_factory=lambda: MappingProxyType({})
    )
    legacy: LegacyEligibilityView | None = None
    legacy_dependencies: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    owner: AttemptOwner | None = None
    ctx: ExecutionContext | None = None

    def dependencies_for(self, representation_id: str) -> frozenset[str]:
        """The legacy dependency set of an eligible representation."""
        try:
            return self.legacy_dependencies[representation_id]
        except KeyError:
            raise TychoraError(
                "FORBIDDEN_SCOPE", "Representation is not eligible"
            ) from None

    async def revalidate(self, stage: Literal["provider", "delivery"]) -> None:
        """Refuse when the policy epoch moved since the view was compiled."""
        if self.owner is None or self.ctx is None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                f"An unowned attempt cannot be revalidated at {stage}",
                reason="invariant.unexpected",
            )
        epoch = await self.owner.run_read(
            lambda tx: tx.workspace.policy_epoch(), self.ctx
        )
        if epoch != self.view.policy_epoch:
            raise _changed()

    async def owner_provider[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Recheck the epoch before an owner-authorized provider operation."""
        await self.revalidate("provider")
        return await call()


async def run_pinned_eligibility[T](
    compiler: EligibilityCompiler,
    legacy: Any,
    selector: str | None,
    scope: RequestScope,
    request: QueryRequest,
    config: ResolvedConfig,
    operation: Callable[[EligibilityAttempt], Awaitable[T]],
    *,
    owner: AttemptOwner,
    ctx: ExecutionContext,
    on_invalidated: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Hold one snapshot lease through both attempts, provider calls and delivery."""
    scope = owner.scope.narrow(scope)
    snapshot = await owner.run_read(
        lambda tx: resolve_snapshot(owner.catalog, tx, selector), ctx
    )
    with owner.store.leases.pin(frozenset({snapshot.snapshot_id})):
        if not compiler.knows_compatible(snapshot, config):
            retained = await owner.run_write(
                lambda: owner.store.retained_config(snapshot), ctx, "interactive"
            )
            compiler.mark_compatible(snapshot, config, retained)
        for attempt_number in range(2):
            attempt = await _attempt(compiler, legacy, owner, snapshot, scope, request, config, ctx)
            if attempt is None:
                continue
            try:
                result = await operation(attempt)
            except TychoraError as error:
                if error.code != "SCOPE_CHANGED" or attempt_number:
                    raise
                continue
            try:
                await attempt.revalidate("delivery")
            except TychoraError as error:
                if error.code == "SCOPE_CHANGED" and on_invalidated is not None:
                    await on_invalidated(result)
                if error.code != "SCOPE_CHANGED" or attempt_number:
                    raise
                continue
            return result
        raise _changed()


async def _attempt(compiler, legacy, owner, snapshot, scope, request, config, ctx):
    """Compile the view (cached, read pool) and, until every reader moved, the legacy one."""
    compiled = await owner.run_read(
        lambda tx: compiler.compile_cached(
            tx, owner.eligibility_cache, snapshot, scope, request, config
        ),
        ctx,
    )
    old = await owner.run_write(
        lambda: legacy.compile_attempt(snapshot, scope, request, config),
        ctx,
        "interactive",
    )
    if old.view.policy_epoch != compiled.view.policy_epoch:
        return None
    return EligibilityAttempt(
        view=compiled.view,
        snapshot=snapshot,
        warnings=compiled.warnings,
        historical_kind_roles=compiled.historical_kind_roles,
        historical_kind_envelopes=compiled.historical_kind_envelopes,
        legacy=old.view,
        legacy_dependencies=old.dependencies,
        owner=owner,
        ctx=ctx,
    )


def _candidate_filter(view: EligibilityView, unit_id: str) -> bool:
    return view.unit_filter is None or unit_id in view.unit_filter


def is_eligible_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """A unit whose dependency set is readable: context or structural."""
    return fact.kind == "unit" and view.allows(fact.dependency_ids)


def is_context_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible citable original or an eligible proxy."""
    return is_eligible_unit(view, fact) and (fact.proxy or fact.citable)


def is_structural_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible non-citable original (headings, file roots)."""
    return is_eligible_unit(view, fact) and not fact.proxy and not fact.citable


def is_source_unit(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible citable original that passes the candidate filter."""
    return (
        is_eligible_unit(view, fact)
        and not fact.proxy
        and fact.citable
        and _candidate_filter(view, fact.owner_id)
    )


def is_candidate_proxy(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An eligible proxy unit that passes the candidate filter."""
    return (
        is_eligible_unit(view, fact)
        and fact.proxy
        and _candidate_filter(view, fact.owner_id)
    )


def is_eligible_asset(view: EligibilityView, fact: OwnerFact, facts=None) -> bool:
    """An asset whose dependency set is readable."""
    return fact.kind == "asset" and view.allows(fact.dependency_ids)


def is_candidate_asset(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """An eligible asset; with a candidate filter, one of its source units passes it."""
    if not is_eligible_asset(view, fact):
        return False
    if view.unit_filter is None:
        return True
    return any(
        owner in fact.dependency_ids
        and (unit := facts.get(owner)) is not None
        and is_eligible_unit(view, unit)
        and not unit.proxy
        and _candidate_filter(view, owner)
        for owner in fact.source_owner_ids
    )


def is_admitted_chunk(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """A window of a source unit or candidate proxy whose own set is readable."""
    unit = facts.get(fact.unit_id or "")
    return (
        fact.kind == "chunk"
        and unit is not None
        and (is_source_unit(view, unit) or is_candidate_proxy(view, unit))
        and view.allows(fact.dependency_ids)
    )


def is_admitted_record(
    view: EligibilityView, fact: OwnerFact, facts: Mapping[str, OwnerFact]
) -> bool:
    """A selected vector record over an admitted window or a candidate asset."""
    if (
        fact.kind != "record"
        or fact.batch_id not in view.batch_ids
        or not view.allows(fact.dependency_ids)
    ):
        return False
    if fact.chunk_id is not None:
        chunk = facts.get(fact.chunk_id)
        return chunk is not None and is_admitted_chunk(view, chunk, facts)
    asset = facts.get(fact.asset_id or "")
    return asset is not None and is_candidate_asset(view, asset, facts)
```

Then wire the query path:
- `src/tychora/application/query.py`: `self.compiler = EligibilityCompiler(self.registry, owner.artifacts)` and `self.legacy_compiler = LegacyEligibilityCompiler(owner.store)` (`:222`); `search` calls `run_pinned_eligibility(self.compiler, self.legacy_compiler, None, granted, request, self.owner.config, operation, owner=self.owner, ctx=ctx, on_invalidated=supersede)` imported from `tychora.retrieval.attempt`; `EligibilityAttempt` is imported from there too. Inside `_prepare`, `_text_candidates`, `_visual_candidates`, `_registered_image_query`, `_text_window_candidates`, `_vector_owners`, `_candidate_lanes` and `_deliver`, every `attempt.view` becomes `attempt.legacy` and every `view = attempt.view` becomes `view = attempt.legacy`; `attempt.view.snapshot_id`, `.policy_epoch` and `.scope_digest` in `operation` and `_deliver` stay on `attempt.view` (both views carry them).
- `src/tychora/application/reranking.py` (`prepare_rerank_facts`), `src/tychora/retrieval/rerank.py`, `src/tychora/retrieval/visuals.py` (`prepare_visual_candidates`), `src/tychora/retrieval/response.py` (`prepare_records`): import `EligibilityAttempt` from `tychora.retrieval.attempt` under `TYPE_CHECKING`; `attempt.view` → `attempt.legacy`, `ctx.attempt.view` → `ctx.attempt.legacy`.
- Tests that construct attempts for these functions (`tests/retrieval/test_degraded.py:85-90`, `test_query.py`, `test_visual_selection.py`, `tests/formatting/test_record_attachment.py`, `test_code_record_binding.py`) set `legacy=<the view they built>` instead of `view=`; `SimpleNamespace` attempts gain `legacy=view`.
- `tests/retrieval/test_eligibility.py` tests of the removed `run_pinned_eligibility` without an owner are deleted; the owner-backed ones call the new function with `LegacyEligibilityCompiler(owner.store)` as `legacy`.
- `tests/configuration/test_global_facets.py` imports `filter_matches` from `tychora.retrieval.compiler`; `tests/retrieval/test_policy_cache.py` builds keys with `eligible_cache_key` from `tychora.retrieval.compiler` over v2 views compiled for the two modes.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_eligibility_cache.py tests/retrieval/test_eligibility_v2.py -v`
Expected: PASS. A mismatch in `test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures` prints the iteration and request; fix the v2 rule (not the assertion). If the test takes more than 30 s locally, mark a 500-iteration copy `@pytest.mark.slow` and keep `ITERATIONS = 100` in the default suite.

- [ ] **Step 10: Full suite, imports and size**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`, then `uv run --locked lint-imports --no-cache`, then `uv run --locked python -m tools.health check --test-reports without`
Expected: PASS; contracts kept; the health check may report the new functions' sizes — every function in `compiler.py` and `attempt.py` is under 80 lines and cognitive complexity 15 (split a stage further if not).

- [ ] **Step 11: Commit**

```bash
git add -A src/tychora tests pyproject.toml
git commit -m "feat: compile revision-level eligibility with a cache"
```

---

### Task 8: Lexical and exact search through joins, survivor check and lanes (C5 exact cap, Q6)

**Files:**
- Create: `src/tychora/ports/search.py`, `src/tychora/retrieval/survivors.py`
- Modify: `src/tychora/adapters/sqlite/lexical.py` (rewrite: `SqliteLexicalSearch`), `src/tychora/adapters/sqlite/repositories/unit_of_work.py` (`SqliteTransaction.lexical`), `src/tychora/ports/repositories.py` (`ReadTransaction.lexical`)
- Modify: `src/tychora/retrieval/lexical_query.py:77-105` (`retrieve_exact_lexical(tx, view, request, registry, limit, ctx)`), `src/tychora/contracts/candidates.py:17-26` (`ChannelCandidates.truncated`)
- Modify: `src/tychora/application/query.py:220-238` (no `LexicalIndex`), `:666-675` (`_lexical_epoch` through the workspace repository), `:699-727` (channels on the read pool), `:845-847` and `:1036-1062` (lanes from survivors; `_candidate_lanes` deleted)
- Delete: `src/tychora/ports/lexical.py`; `CorpusStore.lookup_identifiers` (`ports/corpus.py:34-36`, `adapters/sqlite/corpus.py:175-180`)
- Modify: `src/tychora/contracts/error_reasons.py` (reason `identifier.namespace_unknown`)
- Test: `tests/retrieval/test_lexical.py` (rewrite the adapter tests), `tests/retrieval/test_exact.py` (rewrite the adapter tests), `tests/retrieval/test_survivors.py` (new), `tests/helpers/eligibility.py` (`revision_view`)

**Interfaces:**
- Consumes: Task 7 `EligibilityAttempt.view`, admission predicates, `OwnerFact`; Task 6 `EligibilityView.extraction_ids`, `all_members_eligible`.
- Produces:
  - `ports/search.py`: `RankedOwner(key: CandidateKey, rank: PositiveInt, score: float, window_ids: tuple[str, ...] = ())` (the index lists the first three fields; `window_ids` carries the matching windows reranking and segments read); `RankedOwners(owners: tuple[RankedOwner, ...], truncated: bool, detail: Mapping[str, int])`; `LexicalSearch(Protocol)` with `search(view, query, limit, ctx) -> RankedOwners` and `exact(view, identifiers: tuple[str, ...], limit, ctx) -> RankedOwners`; `VectorSearch(Protocol)` with `search(view, vector: bytes, limit, ctx) -> RankedOwners` (implemented in Task 9).
  - `adapters/sqlite/lexical.py`: `WINDOW_LIMIT = 4000`, `OWNERS_PER_KIND = 80`, `SqliteLexicalSearch(db, *, writable=False)`; `ReadTransaction.lexical: LexicalSearch`.
  - `ChannelCandidates.truncated: bool = False`; exact and lexical channels are always `complete=True`.
  - `retrieval/survivors.py`: `SURVIVOR_LIMIT = 200`; `@dataclass(frozen=True) Survivors(candidates: tuple[FusedCandidate, ...], lanes: Mapping[CandidateKey, str], dropped: int)`; `survivor_ids(candidates) -> tuple[str, ...]`; `check_survivors(view, candidates, facts: Mapping[str, OwnerFact]) -> Survivors`; `load_survivors(tx, view, candidates) -> Survivors`.
  - `tests/helpers/eligibility.py`: `revision_view(grants: Mapping[str, str] = {"revision": "extraction"}, *, excluded_units=(), excluded_assets=(), unit_filter=None, all_members=True, batches=None, snapshot_id="snapshot") -> EligibilityView`.

Candidates leave a channel restricted by joins (eligible extraction unless every member is eligible, excluded units and assets, the candidate filter, citable originals or proxies); the survivor check then reads the persisted dependency sets of at most 200 fused candidates and their windows, drops a candidate whose owner or every window fails the view (counted in `Survivors.dropped`, the trace's `dropped.dependency` from phase 5), and takes each survivor's lane from `view.revisions`.

- [ ] **Step 1: Write the failing tests**

In `tests/helpers/eligibility.py`, add:

```python
from collections.abc import Mapping

from tychora.contracts.eligibility import EligibilityView as RevisionView
from tychora.contracts.eligibility import RevisionGrant


def revision_view(
    grants: Mapping[str, str] | None = None,
    *,
    excluded_units=(),
    excluded_assets=(),
    unit_filter=None,
    all_members=True,
    batches=None,
    snapshot_id="snapshot",
    lane="standard",
) -> RevisionView:
    """A revision-level view granting each ``revision -> extraction`` pair."""
    pairs = {"revision": "extraction"} if grants is None else grants
    return RevisionView(
        snapshot_id=snapshot_id,
        policy_epoch=0,
        scope_digest=digest("synthetic-scope"),
        eligibility_policy_digest=digest("synthetic-policy"),
        eligibility_request_digest=digest("synthetic-request"),
        revisions={
            revision: RevisionGrant(
                extraction_id=extraction,
                source_id=f"source-{revision}",
                kind_id="manual@1",
                lane=lane,
                collection_id="collection",
            )
            for revision, extraction in pairs.items()
        },
        excluded_unit_ids=frozenset(excluded_units),
        excluded_asset_ids=frozenset(excluded_assets),
        unit_filter=None if unit_filter is None else frozenset(unit_filter),
        selected_batches=batches or {},
        all_members_eligible=all_members,
    )
```

Rewrite the adapter half of `tests/retrieval/test_lexical.py` (keep the three `lexical_expression` tests at the top unchanged):

```python
import sqlite3

import pytest

from tests.helpers.eligibility import revision_view
from tychora.adapters.sqlite import lexical as lexical_module
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch
from tychora.ports.execution import ExecutionContext


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE snapshot_batches(snapshot_id TEXT, slot TEXT, batch_id TEXT);
        INSERT INTO snapshot_batches VALUES ('snapshot', 'lexical', 'old');
        CREATE TABLE evidence_units(unit_id TEXT PRIMARY KEY, extraction_id TEXT, citable INTEGER);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, unit_id TEXT, extraction_id TEXT);
        CREATE TABLE asset_proxies(proxy_unit_id TEXT, asset_id TEXT);
        CREATE TABLE lexical_records(rowid INTEGER PRIMARY KEY, batch_id TEXT,
          chunk_id TEXT, identifiers TEXT, title TEXT, headings TEXT, body TEXT, context TEXT);
        CREATE VIRTUAL TABLE lexical_fts USING fts5(identifiers,title,headings,body,context,
          content='lexical_records',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER lexical_insert AFTER INSERT ON lexical_records BEGIN
          INSERT INTO lexical_fts(rowid,identifiers,title,headings,body,context)
          VALUES(new.rowid,new.identifiers,new.title,new.headings,new.body,new.context);
        END;
    """)
    return db


def _record(db, batch, chunk, unit, body, *, asset=None, extraction="extraction", citable=1):
    db.execute(
        "INSERT OR IGNORE INTO evidence_units VALUES (?,?,?)",
        (unit, extraction, 0 if asset is not None else citable),
    )
    db.execute("INSERT INTO chunks VALUES (?,?,?)", (chunk, unit, extraction))
    if asset is not None:
        db.execute("INSERT INTO asset_proxies VALUES (?,?)", (unit, asset))
    db.execute(
        "INSERT INTO lexical_records(batch_id,chunk_id,identifiers,title,headings,body,context) VALUES (?,?,?,?,?,?,?)",
        (batch, chunk, "", "", "", body, ""),
    )


def test_joins_restrict_to_snapshot_batches_eligible_extractions_and_exclusions():
    db = _db()
    for index in range(100):
        _record(db, "old", f"window-{index}", "repeated", "alpha")
    _record(db, "old", "end-window", "ending", "alpha beta")
    _record(db, "old", "hidden", "hidden-owner", "alpha " * 100)
    _record(db, "old", "other", "other-owner", "alpha " * 50, extraction="other")
    _record(db, "old", "heading", "heading-owner", "alpha " * 50, citable=0)
    _record(db, "new", "unpublished", "new-owner", "alpha " * 100)
    view = revision_view(excluded_units=("hidden-owner",), all_members=False)
    result = SqliteLexicalSearch(db).search(view, "alpha", 80, ExecutionContext())
    assert {owner.key for owner in result.owners} == {
        ("unit", "repeated"),
        ("unit", "ending"),
    }
    repeated = next(o for o in result.owners if o.key[1] == "repeated")
    assert len(repeated.window_ids) == 100
    assert not result.truncated
    db.close()


def test_proxy_windows_rank_in_an_independent_asset_pool():
    db = _db()
    _record(db, "old", "source-window", "source", "café")
    _record(db, "old", "proxy-window", "proxy", "cafe", asset="asset")
    result = SqliteLexicalSearch(db).search(revision_view(), "cafe", 80, ExecutionContext())
    assert [o.key for o in result.owners if o.key[0] == "unit"] == [("unit", "source")]
    assert [o.key for o in result.owners if o.key[0] == "asset"] == [("asset", "asset")]
    db.close()


def test_lexical_ranks_survive_owner_grouping():
    db = _db()
    for index in range(81):
        _record(db, "old", f"source-window-{index}", f"source-{index}", "alpha")
        _record(db, "old", f"proxy-window-{index}", f"proxy-{index}", "alpha", asset=f"asset-{index}")
    result = SqliteLexicalSearch(db).search(revision_view(), "alpha", 80, ExecutionContext())
    units = [o for o in result.owners if o.key[0] == "unit"]
    assets = [o for o in result.owners if o.key[0] == "asset"]
    assert len(units) == len(assets) == 80
    assert [o.rank for o in units] == list(range(1, 81))
    assert [o.rank for o in assets] == list(range(1, 81))
    assert result.detail["owners"] == 162
    db.close()


def test_window_limit_sets_truncated_not_incomplete(monkeypatch):
    monkeypatch.setattr(lexical_module, "WINDOW_LIMIT", 3)
    db = _db()
    for index in range(5):
        _record(db, "old", f"window-{index}", f"unit-{index}", "alpha")
    result = SqliteLexicalSearch(db).search(revision_view(), "alpha", 80, ExecutionContext())
    assert result.truncated
    assert result.detail["windows"] == 3
    db.close()


def test_eligible_extraction_binding_is_skipped_when_every_member_is_eligible():
    db = _db()
    _record(db, "old", "window", "unit", "alpha")
    statements = []
    db.set_trace_callback(statements.append)
    search = SqliteLexicalSearch(db)
    search.search(revision_view(all_members=True), "alpha", 80, ExecutionContext())
    search.search(revision_view(all_members=False), "alpha", 80, ExecutionContext())
    db.set_trace_callback(None)
    fts = [s for s in statements if "lexical_fts MATCH" in s]
    assert "json_each" not in fts[0]
    assert "json_each" in fts[1]
    db.close()


def test_eligible_extraction_binding_is_built_once_per_view():
    lexical_module._json_ids.cache_clear()
    db = _db()
    _record(db, "old", "window", "unit", "alpha")
    view = revision_view(all_members=False)
    search = SqliteLexicalSearch(db)
    for _ in range(3):
        search.search(view, "alpha", 80, ExecutionContext())
    assert lexical_module._json_ids.cache_info().misses == 1
    db.close()
```

Rewrite the adapter half of `tests/retrieval/test_exact.py` (`_view`, `_store`, `ForbiddenLexical` and the tests calling `store.lookup_identifiers` or `retrieve_exact_lexical` with a store):

```python
from tests.helpers.eligibility import revision_view
from tychora.adapters.sqlite.lexical import SqliteLexicalSearch


def _db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript("""
        CREATE TABLE identifiers(namespace TEXT, normalized_value TEXT, unit_id TEXT);
        CREATE TABLE evidence_units(unit_id TEXT, extraction_id TEXT, citable INTEGER);
        CREATE TABLE asset_proxies(proxy_unit_id TEXT, asset_id TEXT);
        CREATE TABLE snapshot_members(snapshot_id TEXT, extraction_id TEXT);
        INSERT INTO snapshot_members VALUES ('snapshot','extraction');
        INSERT INTO evidence_units VALUES
          ('allowed','extraction',1),('hidden','extraction',1),('old','old',1),
          ('heading','extraction',0);
        INSERT INTO identifiers VALUES
          ('rule','STD-1.2','allowed'),('rule','STD-1.2','hidden'),
          ('rule','STD-1.2','old'),('rule','STD-1.2','heading'),
          ('code.qualified_name','Foo.bar','allowed');
    """)
    return db


def test_exact_uses_namespace_equality_and_the_view():
    db = _db()
    view = revision_view(excluded_units=("hidden",))
    result = SqliteLexicalSearch(db).exact(view, ("rule:STD-1.2",), 32, ExecutionContext())
    assert [o.key for o in result.owners] == [("unit", "allowed")]
    assert not result.truncated
    db.close()


def test_33_identifier_hits_are_complete_and_truncated():
    db = _db()
    for index in range(33):
        db.execute("INSERT INTO evidence_units VALUES (?,?,1)", (f"u{index:02}", "extraction"))
        db.execute("INSERT INTO identifiers VALUES ('rule','STD-9',?)", (f"u{index:02}",))
    result = SqliteLexicalSearch(db).exact(revision_view(), ("rule:STD-9",), 32, ExecutionContext())
    assert len(result.owners) == 32
    assert result.truncated
    assert result.detail["hits"] == 33
    db.close()


def test_unknown_namespace_is_refused_with_a_reason():
    db = _db()
    with pytest.raises(TychoraError) as error:
        SqliteLexicalSearch(db).exact(revision_view(), ("made.up:value",), 32, ExecutionContext())
    assert error.value.reason == "identifier.namespace_unknown"
    db.close()
```

The two `retrieve_exact_lexical` tests keep their requests and assertions and pass a fake transaction: `SimpleNamespace(lexical=SqliteLexicalSearch(_db()), workspace=SimpleNamespace(lexical_epoch=lambda: 0))` as `tx`, `revision_view()` as `view`, and drop the `store`/`ForbiddenLexical` arguments (for the exact-only test, a `lexical` object whose `search` raises `AssertionError` proves lexical is never called).

```python
# tests/retrieval/test_survivors.py
"""Fused candidates survive only when their persisted dependency sets are readable."""

from tests.helpers.eligibility import revision_view
from tychora.ports.repositories import OwnerFact
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.survivors import SURVIVOR_LIMIT, check_survivors, survivor_ids


def _unit(unit_id, *, deps=None, citable=True):
    return OwnerFact(
        owner_id=unit_id,
        kind="unit",
        extraction_id="extraction",
        dependency_ids=frozenset(deps or {"revision", unit_id}),
        unit_id=unit_id,
        citable=citable,
    )


def _chunk(chunk_id, unit_id, *, deps=None):
    return OwnerFact(
        owner_id=chunk_id,
        kind="chunk",
        extraction_id="extraction",
        dependency_ids=frozenset(deps or {"revision", unit_id}),
        unit_id=unit_id,
        chunk_id=chunk_id,
        citable=True,
    )


def _candidate(key, windows=()):
    return FusedCandidate(key=key, rank=1, score=1.0, ranks={"lexical": 1}, matching_window_ids=windows)


def test_survivor_check_drops_ineligible_dependency():
    view = revision_view(excluded_units=("u-excluded",))
    facts = {
        f.owner_id: f
        for f in (
            _unit("u-ok"),
            _chunk("c-ok", "u-ok"),
            _chunk("c-overlap", "u-ok", deps={"revision", "u-ok", "u-excluded"}),
            _unit("u-bad", deps={"revision", "u-bad", "u-excluded"}),
        )
    }
    survivors = check_survivors(
        view,
        (
            _candidate(("unit", "u-ok"), ("c-ok", "c-overlap")),
            _candidate(("unit", "u-bad")),
        ),
        facts,
    )
    assert [c.key for c in survivors.candidates] == [("unit", "u-ok")]
    assert survivors.candidates[0].matching_window_ids == ("c-ok",)
    assert survivors.dropped == 1


def test_a_candidate_whose_every_window_fails_is_dropped():
    view = revision_view(excluded_units=("u-x",))
    facts = {
        f.owner_id: f
        for f in (_unit("u"), _chunk("c", "u", deps={"revision", "u", "u-x"}))
    }
    survivors = check_survivors(view, (_candidate(("unit", "u"), ("c",)),), facts)
    assert survivors.candidates == ()
    assert survivors.dropped == 1


def test_lanes_need_no_sql():
    view = revision_view(lane="authority")
    facts = {f.owner_id: f for f in (_unit("u"),)}
    survivors = check_survivors(view, (_candidate(("unit", "u")),), facts)
    assert survivors.lanes == {("unit", "u"): "authority"}


def test_at_most_200_candidates_are_read():
    candidates = tuple(_candidate(("unit", f"u{i}"), (f"c{i}",)) for i in range(300))
    ids = survivor_ids(candidates)
    assert SURVIVOR_LIMIT == 200
    assert len(ids) == 400
    assert "u199" in ids and "u200" not in ids
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_lexical.py tests/retrieval/test_exact.py tests/retrieval/test_survivors.py -v`
Expected: FAIL at collection with `ImportError: cannot import name 'SqliteLexicalSearch'` and `ModuleNotFoundError: No module named 'tychora.retrieval.survivors'`.

- [ ] **Step 3: Declare the search ports**

```python
# src/tychora/ports/search.py
"""Ranked-owner search ports; each store restricts candidates to an eligibility view."""

from collections.abc import Mapping
from typing import Protocol

from tychora.contracts.base import Contract, Identifier, PositiveInt
from tychora.contracts.candidates import CandidateKey
from tychora.contracts.eligibility import EligibilityView
from tychora.ports.execution import ExecutionContext


class RankedOwner(Contract):
    """One evidence owner, its dense rank within its kind and its matching windows."""

    key: CandidateKey
    rank: PositiveInt
    score: float
    window_ids: tuple[Identifier, ...] = ()


class RankedOwners(Contract):
    """A channel's owners; ``truncated`` when a window or hit limit bound the scan."""

    owners: tuple[RankedOwner, ...]
    truncated: bool = False
    detail: Mapping[str, int] = {}


class LexicalSearch(Protocol):
    """Full-text and identifier search restricted to a view."""

    def search(
        self, view: EligibilityView, query: str, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Rank owners by their best matching window; ``limit`` owners per kind."""
        ...

    def exact(
        self,
        view: EligibilityView,
        identifiers: tuple[str, ...],
        limit: int,
        ctx: ExecutionContext,
    ) -> RankedOwners:
        """Units carrying any of the namespaced identifiers, in identifier order."""
        ...


class VectorSearch(Protocol):
    """Nearest-neighbour search restricted to a view."""

    def search(
        self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Rank owners by their nearest record; ``limit`` owners per kind."""
        ...
```

In `src/tychora/ports/repositories.py`, add `lexical: LexicalSearch` to `ReadTransaction` (import from `tychora.ports.search`). Delete `src/tychora/ports/lexical.py`.

- [ ] **Step 4: Rewrite the SQLite lexical adapter**

Replace `src/tychora/adapters/sqlite/lexical.py` with:

```python
"""SQLite FTS5 and identifier search restricted to an eligibility view by joins."""

import json
import sqlite3
from collections import defaultdict
from functools import lru_cache
from typing import Literal

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext
from tychora.ports.search import RankedOwner, RankedOwners
from tychora.retrieval.lexical_query import lexical_expression, normalize_identifier

WINDOW_LIMIT = 4000
OWNERS_PER_KIND = 80
EXACT_LIMIT = 32
MAX_IDENTIFIERS = 16
_IN = "IN (SELECT value FROM json_each(?))"
_SEARCH = (
    "SELECT r.chunk_id,c.unit_id,p.asset_id,"
    "bm25(lexical_fts,5.0,3.0,2.0,1.0,0.5) AS score FROM lexical_fts "
    "JOIN lexical_records r ON r.rowid=lexical_fts.rowid "
    "JOIN chunks c ON c.chunk_id=r.chunk_id "
    "JOIN evidence_units u ON u.unit_id=c.unit_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    "WHERE lexical_fts MATCH ? AND r.batch_id IN (SELECT batch_id FROM snapshot_batches "
    "WHERE snapshot_id=? AND slot='lexical') "
    "AND (u.citable=1 OR p.proxy_unit_id IS NOT NULL)"
)
_EXACT = (
    "SELECT i.unit_id FROM identifiers i JOIN evidence_units u ON u.unit_id=i.unit_id "
    "JOIN snapshot_members m ON m.extraction_id=u.extraction_id "
    "WHERE m.snapshot_id=? AND i.namespace=? AND i.normalized_value=? AND u.citable=1 "
    "AND NOT EXISTS (SELECT 1 FROM asset_proxies p WHERE p.proxy_unit_id=u.unit_id)"
)


@lru_cache(maxsize=256)
def _json_ids(ids: frozenset[str]) -> str:
    """One JSON binding per ID set; a cached view's sets keep their hash."""
    return json.dumps(sorted(ids))


def _restriction(
    view: EligibilityView, extraction: str, unit: str, asset: str | None = None
) -> tuple[str, tuple[str, ...]]:
    """Predicates and bindings for the view's extractions, exclusions and filter."""
    clauses: list[str] = []
    args: list[str] = []
    if not view.all_members_eligible:
        clauses.append(f"{extraction} {_IN}")
        args.append(_json_ids(view.extraction_ids))
    if view.excluded_unit_ids:
        clauses.append(f"{unit} NOT {_IN}")
        args.append(_json_ids(view.excluded_unit_ids))
    if asset is not None and view.excluded_asset_ids:
        clauses.append(f"({asset} IS NULL OR {asset} NOT {_IN})")
        args.append(_json_ids(view.excluded_asset_ids))
    if view.unit_filter is not None:
        clauses.append(f"{unit} {_IN}")
        args.append(_json_ids(view.unit_filter))
    return "".join(" AND " + clause for clause in clauses), tuple(args)


def _ranked(
    groups: dict[str, list[tuple[str, float]]],
    kind: Literal["unit", "asset"],
    limit: int,
) -> tuple[RankedOwner, ...]:
    ordered = sorted(groups.items(), key=lambda item: (item[1][0][1], item[0]))
    return tuple(
        RankedOwner(
            key=(kind, owner),
            rank=rank,
            score=windows[0][1],
            window_ids=tuple(chunk for chunk, _ in windows),
        )
        for rank, (owner, windows) in enumerate(ordered[:limit], 1)
    )


def _limit(value: int, upper: int) -> None:
    if not 1 <= value <= upper:
        raise TychoraError(
            "INVALID_REQUEST",
            f"Candidate limit must be 1..{upper}",
            reason="request.limit_exceeded",
        )


class SqliteLexicalSearch:
    """FTS5 ranking and identifier equality inside the caller's read transaction."""

    def __init__(self, db: sqlite3.Connection, *, writable: bool = False) -> None:
        self._db = db

    def search(
        self, view: EligibilityView, query: str, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Rank owners by their best of at most ``WINDOW_LIMIT`` eligible windows."""
        _limit(limit, OWNERS_PER_KIND)
        expression = lexical_expression(query, frozenset())
        if expression is None:
            return RankedOwners(owners=(), detail={"windows": 0, "owners": 0})
        restriction, args = _restriction(view, "c.extraction_id", "c.unit_id", "p.asset_id")
        ctx.checkpoint()
        rows = self._db.execute(
            _SEARCH + restriction + " ORDER BY score,r.chunk_id LIMIT ?",
            (expression, view.snapshot_id, *args, WINDOW_LIMIT),
        ).fetchall()
        ctx.checkpoint()
        units: dict[str, list[tuple[str, float]]] = defaultdict(list)
        assets: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for chunk, unit, asset, score in rows:
            (assets[asset] if asset is not None else units[unit]).append((chunk, score))
        return RankedOwners(
            owners=(*_ranked(units, "unit", limit), *_ranked(assets, "asset", limit)),
            truncated=len(rows) >= WINDOW_LIMIT,
            detail={"windows": len(rows), "owners": len(units) + len(assets)},
        )

    def exact(
        self,
        view: EligibilityView,
        identifiers: tuple[str, ...],
        limit: int,
        ctx: ExecutionContext,
    ) -> RankedOwners:
        """Citable original units carrying any identifier; every hit is counted."""
        _limit(limit, EXACT_LIMIT)
        if len(identifiers) > MAX_IDENTIFIERS:
            raise TychoraError(
                "INVALID_REQUEST",
                f"At most {MAX_IDENTIFIERS} exact identifiers are accepted",
                reason="request.limit_exceeded",
            )
        try:
            parsed = tuple(normalize_identifier(value) for value in identifiers)
        except ValueError:
            raise TychoraError(
                "INVALID_REQUEST",
                "Identifier requires a registered namespace and value",
                reason="identifier.namespace_unknown",
            ) from None
        restriction, args = _restriction(view, "u.extraction_id", "u.unit_id")
        hits: dict[str, int] = {}
        for index, (namespace, value) in enumerate(parsed):
            ctx.checkpoint()
            for (unit_id,) in self._db.execute(
                _EXACT + restriction + " ORDER BY i.unit_id",
                (view.snapshot_id, namespace, value, *args),
            ):
                hits.setdefault(unit_id, index)
        ordered = sorted(hits, key=lambda unit: (hits[unit], unit))
        return RankedOwners(
            owners=tuple(
                RankedOwner(key=("unit", unit), rank=rank, score=0.0)
                for rank, unit in enumerate(ordered[:limit], 1)
            ),
            truncated=len(hits) > limit,
            detail={"hits": len(hits)},
        )
```

Register it: `SqliteTransaction.lexical = SqliteLexicalSearch(db, writable=writable)` in `repositories/unit_of_work.py` (the adapter module imports `tychora.retrieval.lexical_query` through the existing allowed edge `tychora.adapters.sqlite.lexical -> tychora.retrieval.lexical_query`; the repositories package imports `tychora.adapters.sqlite.lexical`, which is inside `adapters`). Register reason `identifier.namespace_unknown` (`INVALID_REQUEST`, "Prefix the identifier with a registered namespace such as rule:, provision: or code.qualified_name:.") in `error_reasons.py` and in `CLASSIFIED_REASONS`.

- [ ] **Step 5: Channels on the read pool**

In `src/tychora/contracts/candidates.py`, add `truncated: bool = False` to `ChannelCandidates` after `complete`.

In `src/tychora/retrieval/lexical_query.py`, replace the `ChannelCandidates`, `CorpusStore` and `LexicalIndex` imports with `from tychora.contracts.candidates import ChannelCandidate, ChannelCandidates`, `from tychora.contracts.eligibility import EligibilityView`, `from tychora.ports.repositories import ReadTransaction`, `from tychora.ports.search import RankedOwners`, and replace `retrieve_exact_lexical` (`:77-105`) with:

```python
def _channel(
    name: str,
    view: EligibilityView,
    ranked: RankedOwners,
    epoch: int,
    *,
    scored: bool,
) -> ChannelCandidates:
    def candidates(kind: str) -> tuple[ChannelCandidate, ...]:
        return tuple(
            ChannelCandidate(
                key=owner.key,
                rank=owner.rank,
                matching_window_ids=owner.window_ids,
                score=owner.score if scored else None,
            )
            for owner in ranked.owners
            if owner.key[0] == kind
        )

    return ChannelCandidates(
        channel=name,
        snapshot_id=view.snapshot_id,
        policy_epoch=view.policy_epoch,
        source_candidates=candidates("unit"),
        asset_candidates=candidates("asset"),
        matching_window_count=ranked.detail.get("windows", 0),
        distinct_candidate_count=ranked.detail.get(
            "owners", ranked.detail.get("hits", 0)
        ),
        lexical_statistics_epoch=epoch,
        complete=True,
        truncated=ranked.truncated,
    )


def retrieve_exact_lexical(
    tx: ReadTransaction,
    view: EligibilityView,
    request: QueryRequest,
    registry: Registry,
    candidate_limit: int,
    ctx: ExecutionContext,
) -> tuple[ChannelCandidates, ...]:
    """Run the exact channel, then lexical unless the request is exact-only."""
    ctx.checkpoint()
    if len(request.exact_identifiers) > 16:
        raise TychoraError(
            "INVALID_REQUEST",
            "At most 16 exact identifiers are accepted",
            reason="request.limit_exceeded",
        )
    query = (
        request.input.text
        if isinstance(request.input, (QuestionInput, PassageInput))
        else ""
    )
    namespaced = tuple(
        dict.fromkeys((*request.exact_identifiers, *extract_identifiers(query, registry)))
    )[:16]
    epoch = tx.workspace.lexical_epoch()
    exact = (
        tx.lexical.exact(view, namespaced, min(candidate_limit, 32), ctx)
        if namespaced
        else RankedOwners(owners=())
    )
    exact_channel = _channel("exact", view, exact, epoch, scored=False)
    if request.exact_only or not query:
        return (exact_channel,)
    lexical = tx.lexical.search(view, query, candidate_limit, ctx)
    return (exact_channel, _channel("lexical", view, lexical, epoch, scored=True))
```

Delete `CorpusStore.lookup_identifiers` from `ports/corpus.py:34-36` and `adapters/sqlite/corpus.py:175-180`.

- [ ] **Step 6: The survivor check**

```python
# src/tychora/retrieval/survivors.py
"""Fused candidates checked against persisted dependency sets before admission."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from tychora.contracts.candidates import CandidateKey
from tychora.contracts.eligibility import EligibilityView
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.retrieval.attempt import (
    is_admitted_chunk,
    is_candidate_asset,
    is_eligible_asset,
    is_source_unit,
)
from tychora.retrieval.fusion import FusedCandidate

SURVIVOR_LIMIT = 200


@dataclass(frozen=True)
class Survivors:
    """The candidates the view admits, their lanes and how many were dropped."""

    candidates: tuple[FusedCandidate, ...]
    lanes: Mapping[CandidateKey, str]
    dropped: int


def survivor_ids(candidates: Sequence[FusedCandidate]) -> tuple[str, ...]:
    """The owners and windows of the first ``SURVIVOR_LIMIT`` candidates."""
    ids: set[str] = set()
    for candidate in candidates[:SURVIVOR_LIMIT]:
        ids.add(candidate.key[1])
        ids.update(candidate.matching_window_ids)
    return tuple(sorted(ids))


def _owner_admitted(
    view: EligibilityView,
    fact: OwnerFact,
    facts: Mapping[str, OwnerFact],
    has_windows: bool,
) -> bool:
    if fact.kind == "unit":
        return is_source_unit(view, fact)
    if fact.kind == "asset":
        return (
            is_eligible_asset(view, fact)
            if has_windows
            else is_candidate_asset(view, fact, facts)
        )
    return False


def check_survivors(
    view: EligibilityView,
    candidates: Sequence[FusedCandidate],
    facts: Mapping[str, OwnerFact],
) -> Survivors:
    """Keep each candidate whose owner and at least one window the view admits."""
    kept: list[FusedCandidate] = []
    lanes: dict[CandidateKey, str] = {}
    dropped = max(0, len(candidates) - SURVIVOR_LIMIT)
    for candidate in candidates[:SURVIVOR_LIMIT]:
        fact = facts.get(candidate.key[1])
        windows = tuple(
            window
            for window in candidate.matching_window_ids
            if (chunk := facts.get(window)) is not None
            and is_admitted_chunk(view, chunk, facts)
        )
        if (
            fact is None
            or not _owner_admitted(view, fact, facts, bool(candidate.matching_window_ids))
            or (candidate.matching_window_ids and not windows)
        ):
            dropped += 1
            continue
        kept.append(replace(candidate, matching_window_ids=windows))
        revision = view.revision_of_extraction[fact.extraction_id]
        lanes[candidate.key] = view.revisions[revision].lane
    return Survivors(tuple(kept), MappingProxyType(lanes), dropped)


def load_survivors(
    tx: ReadTransaction, view: EligibilityView, candidates: Sequence[FusedCandidate]
) -> Survivors:
    """Read the candidates', their windows' and those windows' units' facts, then check."""
    facts = {f.owner_id: f for f in tx.eligibility.owner_facts(survivor_ids(candidates))}
    units = tuple(
        sorted(
            {f.unit_id for f in facts.values() if f.kind == "chunk" and f.unit_id}
            - set(facts)
        )
    )
    if units:
        facts.update({f.owner_id: f for f in tx.eligibility.owner_facts(units)})
    return check_survivors(view, candidates, facts)
```

- [ ] **Step 7: Use them in the query service**

In `src/tychora/application/query.py`:
- Delete the `LexicalIndex` import and the `lexical` constructor parameter and attribute (`:17`, `:220`, `:238`); tests that injected a counting lexical index (`tests/retrieval/test_query.py:677`) patch `SqliteLexicalSearch.search` instead.
- `_lexical_epoch` (`:671-679`) is deleted; `operation` reads `lexical_epoch = await self.owner.run_read(lambda tx: tx.workspace.lexical_epoch(), ctx)`.
- The channel call (`:699-713`) becomes:

```python
        channels = (
            await self.owner.run_read(
                lambda tx: retrieve_exact_lexical(
                    tx, attempt.view, request, self.registry, 80, ctx
                ),
                ctx,
            )
            if plan is None or plan.lexical
            else ()
        )
```

and the fallback at `:723-724` becomes `lexical_epoch = await self.owner.run_read(lambda tx: tx.workspace.lexical_epoch(), ctx)`.
- Replace `lanes = await self.owner.run_write(lambda: self._candidate_lanes(fused, view), ctx, "interactive")` (`:845-847`) with:

```python
        survivors = await self.owner.run_read(
            lambda tx: load_survivors(tx, attempt.view, fused), ctx
        )
```

then pass `survivors.candidates` instead of `fused` to `admit(...)` and `lanes=survivors.lanes`; the coverage's `candidate_count` stays `len(fused)`. Delete `_candidate_lanes` (`:1036-1062`).

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_lexical.py tests/retrieval/test_exact.py tests/retrieval/test_survivors.py tests/retrieval/test_query.py -v`
Expected: PASS.

- [ ] **Step 9: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add -A src/tychora tests
git commit -m "feat: restrict lexical and exact search with store-side joins"
```

---

### Task 9: Vector search from the view, owners in one statement, partitioned upserts

**Files:**
- Modify: `src/tychora/adapters/chroma/index.py:37` (partition size), `:219-278` (`upsert_batch` adds per partition), `:326-492` (`search` implements `VectorSearch`; `_rank` deleted; `_present` stays for upserts and verification only)
- Modify: `src/tychora/ports/vectors.py:27-34` (`VectorIndex.search` takes the view), `src/tychora/contracts/vectors.py` (`PARTITION_BYTES`, `partition_size`, `VectorBatch.partitions`; `CandidatePage`, `DenseCandidate` and `WorkLimits` deleted once unused)
- Modify: `src/tychora/indexing/coordinator.py:754-822` and `:932-1005` (extract `_text_stored`, `_image_stored`; add `native_batches`), `src/tychora/application/sources.py:864-878` (upsert partitions as separate background writer tasks before `finalize`)
- Create: `src/tychora/retrieval/vectors.py` (`vector_candidates`, `linked_visual_context`)
- Modify: `src/tychora/application/query.py:186-210` (`linked_visual_context` moves), `:785-805`, `:1064-1197` (`_text_candidates`, `_visual_candidates` through the view; `_text_window_candidates` and `_vector_owners` deleted)
- Modify: `src/tychora/adapters/sqlite/repositories/eligibility.py` (`_OWNERS` record branch maps proxy windows to their asset)
- Test: `tests/retrieval/test_vector_selection.py` (new), `tests/indexing/test_publication.py:98-130` (`RecordingVector.search`), `tests/runtime/test_lanes.py` (one test added)

**Interfaces:**
- Consumes: Task 8 `RankedOwner`, `RankedOwners`, `VectorSearch`, `check_survivors`; Task 7 `is_admitted_record`; Task 1 `run_write(..., "interactive")`.
- Produces:
  - `ChromaVectorIndex.search(view: EligibilityView, vector: bytes, limit: int, ctx) -> RankedOwners` (`VectorSearch`): `where={"revision_id": {"$in": [...]}}` when the view has `REVISION_FILTER_LIMIT = 512` or fewer revisions, otherwise unfiltered with hits kept by their metadata `revision_id`; `n_results = OVERFETCH_FACTOR (4) × limit`, doubled at most `OVERFETCH_DOUBLINGS = 2` times while fewer than `limit` owners were found and the store returned a full page, capped at `OVERFETCH_CAP = 1024`; `truncated` when the cap or the doublings ran out first; `detail = {"returned", "queries", "rescored"}` (`rescored` stays 0 for float32 families).
  - `contracts/vectors.py`: `PARTITION_BYTES = 2_000_000`, `partition_size(dimension: int) -> int` (`max(1, PARTITION_BYTES // (4 * dimension))`, 488 at 1,024), `VectorBatch.partitions(dimension: int) -> tuple[VectorBatch, ...]`.
  - `ProjectionCoordinator.native_batches(prepared, text_records, visual_records) -> tuple[tuple[VectorIndex, VectorBatch, int], ...]` (index, batch, dimension) for the slots the add writes.
  - `retrieval/vectors.py`: `vector_candidates(tx, view, ranked: RankedOwners) -> tuple[tuple[ChannelCandidate, ...], bool]` (canonical owners and the channel's `truncated`), `linked_visual_context(tx, view, assets) -> tuple[ChannelCandidate, ...]`.

The adapter returns owners from Chroma's metadata with their record IDs as windows; one owner-facts statement then maps the records canonically (a text record's window, its unit, a proxy window to its asset, an image record to its asset, its batch and its context group's dependency set), keeps those `is_admitted_record` admits, and regroups them by canonical owner. A record of a batch the snapshot does not serve (an unpublished or collected batch still in the family's collection) is dropped, not refused: v1 never saw such records because it queried by eligible ID lists, which this task removes. Per-query presence fetches go away; presence is verified at activation and by `manage health`. Chroma upserts and queries stay on the writer thread (interactive for queries); an add submits each ≤ 2 MB partition as its own background writer task, so a query waits at most one partition.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_vector_selection.py
"""Vector search reads the view: revision filters, over-fetch, canonical owners."""

import struct
import threading
import time

import pytest

from tests.helpers.eligibility import revision_view
from tychora.adapters.chroma import index as chroma_index
from tychora.adapters.chroma.index import ChromaVectorIndex
from tychora.contracts.embeddings import EmbeddingRecord
from tychora.contracts.identity import digest, space_id
from tychora.contracts.indexing import FamilySpec
from tychora.contracts.vectors import VectorBatch, VectorOwner, partition_size
from tychora.ports.execution import ExecutionContext


def _spec():
    return FamilySpec(
        family_id=digest("vector-family"),
        workspace_id="workspace",
        config_sha=digest("config"),
        creation_uuid="00000000-0000-4000-8000-000000000001",
        modality="text",
        provider="synthetic",
        model="synthetic",
        dimensions=2,
        metric="cosine",
        normalization="unit-l2",
        representation_fingerprint=digest("representation"),
        backend_namespace="text",
        backend_space="ip",
    )


def _index(tmp_path, owners):
    """A family whose record i points at angle i and belongs to owners[i]."""
    index = ChromaVectorIndex(tmp_path / "chroma")
    spec = _spec()
    index.create_family(spec)
    space = space_id("text", "synthetic", "synthetic", 2, "cosine", spec.representation_fingerprint)
    records, rows = [], []
    for position, (revision, unit) in enumerate(owners):
        angle = position / 1000
        values = (1.0 - angle * angle / 2, angle)
        norm = (values[0] ** 2 + values[1] ** 2) ** 0.5
        stored = struct.pack("<ff", values[0] / norm, values[1] / norm)
        record_id = digest("record", position)
        records.append(
            EmbeddingRecord.from_normalized_bytes(
                record_id=record_id, input_sha=digest("input", position), space_id=space, stored=stored
            )
        )
        rows.append(
            VectorOwner(
                record_id=record_id,
                revision_id=revision,
                extraction_id=f"{revision}-extraction",
                owner_kind="unit",
                owner_id=unit,
            )
        )
    index.upsert_batch(
        VectorBatch(family_id=spec.family_id, records=tuple(records), owners=tuple(rows)),
        ExecutionContext(),
    )
    return index


def _query():
    return struct.pack("<ff", 1.0, 0.0)


def test_filtered_search_below_512_revisions(tmp_path):
    owners = [(f"r{i % 4}", f"u{i}") for i in range(40)]
    index = _index(tmp_path, owners)
    view = revision_view({"r1": "r1-extraction"})
    result = index.search(view, _query(), 5, ExecutionContext())
    assert [owner.key for owner in result.owners] == [("unit", f"u{i}") for i in (1, 5, 9, 13, 17)]
    assert not result.truncated
    assert result.detail["rescored"] == 0


def test_overfetch_returns_limit_or_truncated_above_512(tmp_path, monkeypatch):
    # Records sit at increasing distance; revision r(i % 25) owns record i.
    monkeypatch.setattr(chroma_index, "REVISION_FILTER_LIMIT", 1)
    owners = [(f"r{i % 25}", f"u{i}") for i in range(500)]
    index = _index(tmp_path, owners)
    narrow = revision_view({"r7": "r7-extraction", "r9": "r9-extraction"})
    result = index.search(narrow, _query(), 5, ExecutionContext())
    # 20 -> 40 -> 80 nearest records hold u7, u9, u32, u34, u57, u59.
    assert [owner.key[1] for owner in result.owners] == ["u7", "u9", "u32", "u34", "u57"]
    assert not result.truncated
    assert result.detail["queries"] == 3
    monkeypatch.setattr(chroma_index, "OVERFETCH_CAP", 40)
    tiny = revision_view({"r23": "r23-extraction", "r24": "r24-extraction"})
    capped = index.search(tiny, _query(), 5, ExecutionContext())
    assert [owner.key[1] for owner in capped.owners] == ["u23", "u24"]
    assert capped.truncated


def test_upsert_partitions_stay_under_2_mb():
    assert partition_size(1024) == 488
    assert partition_size(1024) * 4 * 1024 <= 2_000_000
    assert partition_size(1_000_000) == 1


def test_vector_batch_partitions_keep_records_and_owners_together(tmp_path):
    spec = _spec()
    space = space_id("text", "synthetic", "synthetic", 2, "cosine", spec.representation_fingerprint)
    records = tuple(
        EmbeddingRecord.from_normalized_bytes(
            record_id=digest("r", i), input_sha=digest("i", i), space_id=space, stored=struct.pack("<ff", 1.0, 0.0)
        )
        for i in range(5)
    )
    owners = tuple(
        VectorOwner(record_id=r.record_id, revision_id="rev", extraction_id="ext", owner_kind="unit", owner_id="u")
        for r in records
    )
    batch = VectorBatch(family_id=spec.family_id, records=records, owners=owners)
    parts = batch.partitions(250_000)
    assert [len(part.records) for part in parts] == [2, 2, 1]
    assert all({o.record_id for o in part.owners} == {r.record_id for r in part.records} for part in parts)
```

In `tests/runtime/test_lanes.py`, add:

```python
def test_interactive_query_waits_at_most_one_partition():
    worker = PriorityWorker()
    finished: list[str] = []

    def partition(index):
        time.sleep(0.05)
        finished.append(f"p{index}")

    first = worker.submit(lambda: partition(0), "background")
    time.sleep(0.01)
    rest = [worker.submit(lambda i=i: partition(i), "background") for i in range(1, 6)]
    query = worker.submit(lambda: finished.append("query"), "interactive")
    query.result(5)
    for future in (first, *rest):
        future.result(5)
    worker.close()
    assert finished.index("query") == 1
```

(add `import time` to its imports).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_vector_selection.py tests/runtime/test_lanes.py -v`
Expected: FAIL — `ImportError: cannot import name 'partition_size'`; `test_interactive_query_waits_at_most_one_partition` already passes (it pins Task 1's ordering for partitions).

- [ ] **Step 3: Partitions in the vector contracts**

In `src/tychora/contracts/vectors.py`, add after `VectorBatch`'s validator:

```python
    def partitions(self, dimension: int) -> tuple["VectorBatch", ...]:
        """Split into batches of at most ``partition_size(dimension)`` records with their owners."""
        size = partition_size(dimension)
        owners = {owner.record_id: owner for owner in self.owners}
        return tuple(
            VectorBatch(
                family_id=self.family_id,
                records=self.records[start : start + size],
                owners=tuple(
                    owners[record.record_id]
                    for record in self.records[start : start + size]
                    if record.record_id in owners
                ),
            )
            for start in range(0, len(self.records), size)
        )
```

and at module level:

```python
PARTITION_BYTES = 2_000_000


def partition_size(dimension: int) -> int:
    """Records per native write so that one call carries at most 2 MB of float32."""
    return max(1, PARTITION_BYTES // (4 * dimension))
```

- [ ] **Step 4: Search through the view in the Chroma adapter**

In `src/tychora/adapters/chroma/index.py`:

1. `upsert_batch`: replace `for chunk in self._chunks(additions):` with `for chunk in self._chunks(additions, partition_size(handle.spec.dimensions)):` (import `partition_size`).
2. Replace `search` (`:326-469`) and `_rank` (`:471-492`) with:

```python
REVISION_FILTER_LIMIT = 512
OVERFETCH_FACTOR = 4
OVERFETCH_DOUBLINGS = 2
OVERFETCH_CAP = 1024


def _owners(
    hits: list[tuple[str, float, Mapping[str, str]]], limit: int
) -> tuple[RankedOwner, ...]:
    """Group hits by metadata owner, best distance first; ``limit`` owners per kind."""
    grouped: dict[CandidateKey, list[tuple[float, str]]] = defaultdict(list)
    for record_id, distance, metadata in hits:
        key = (metadata["owner_kind"], metadata["owner_id"])
        grouped[key].append((distance, record_id))
    ranked: list[RankedOwner] = []
    for kind in ("unit", "asset"):
        pool = sorted(
            ((min(rows), key) for key, rows in grouped.items() if key[0] == kind),
            key=lambda item: (item[0][0], item[1]),
        )
        for rank, ((distance, _), key) in enumerate(pool[:limit], 1):
            rows = sorted(grouped[key])
            ranked.append(
                RankedOwner(
                    key=key,
                    rank=rank,
                    score=-distance,
                    window_ids=tuple(record for _, record in rows),
                )
            )
    return tuple(ranked)
```

(module level, after the constants), and in the class:

```python
    def search(
        self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext
    ) -> RankedOwners:
        """Owners nearest to ``vector`` among the view's revisions."""
        collection, handle = self._require()
        self._check_collection(collection, handle.spec)
        query = list(_float32(vector, handle.spec.dimensions))
        revisions = sorted(view.revisions)
        if not revisions or limit <= 0:
            return RankedOwners(owners=(), detail={"returned": 0, "queries": 0, "rescored": 0})
        where = (
            {"revision_id": {"$in": revisions}}
            if len(revisions) <= REVISION_FILTER_LIMIT
            else None
        )
        wanted = min(OVERFETCH_FACTOR * limit, OVERFETCH_CAP)
        queries = returned = 0
        while True:
            ctx.checkpoint()
            raw = collection.query(
                query_embeddings=[query],
                n_results=wanted,
                where=where,
                include=["distances", "metadatas"],
            )
            queries += 1
            ids = raw["ids"][0]
            returned = len(ids)
            hits = [
                (record_id, distance, metadata)
                for record_id, distance, metadata in zip(
                    ids, raw["distances"][0], raw["metadatas"][0], strict=True
                )
                if metadata.get("revision_id") in view.revisions
            ]
            owners = _owners(hits, limit)
            enough = len({(m["owner_kind"], m["owner_id"]) for _, _, m in hits}) >= limit
            exhausted = returned < wanted
            if enough or exhausted or queries > OVERFETCH_DOUBLINGS or wanted >= OVERFETCH_CAP:
                break
            wanted = min(2 * wanted, OVERFETCH_CAP)
        return RankedOwners(
            owners=owners,
            truncated=not enough and not exhausted,
            detail={"returned": returned, "queries": queries, "rescored": 0},
        )
```

(imports: `EligibilityView` from `tychora.contracts.eligibility`, `RankedOwner`, `RankedOwners` from `tychora.ports.search`, `CandidateKey` stays imported; delete the `_PresenceDeadline` path from search, the `CandidatePage`, `DenseCandidate`, `WorkLimits` imports, and `contracts/vectors.py`'s `CandidatePage`, `DenseCandidate`, `WorkLimits` when `rg -n "CandidatePage|DenseCandidate|WorkLimits" src/tychora tests` shows no other user).

3. `src/tychora/ports/vectors.py`: `search` becomes `def search(self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext) -> RankedOwners: ...`. `tests/indexing/test_publication.py:118-130` (`RecordingVector.search`) forwards `(view, vector, limit, ctx)`.

- [ ] **Step 5: Map records canonically on the read pool**

In `src/tychora/adapters/sqlite/repositories/eligibility.py`, change the record branch of `_OWNERS` so a proxy window's record is owned by its asset, and add the explicit proxy column every branch returns:

```python
    "UNION ALL SELECT 'record',v.record_id,b.extraction_id,c.unit_id,v.chunk_id,"
    "COALESCE(v.asset_id,p.asset_id),0,v.batch_id,COALESCE(v.group_id,v.asset_id),"
    "v.chunk_id IS NOT NULL AND p.proxy_unit_id IS NOT NULL FROM vector_records v "
    "JOIN index_batches b ON b.batch_id=v.batch_id "
    "LEFT JOIN chunks c ON c.chunk_id=v.chunk_id "
    "LEFT JOIN asset_proxies p ON p.proxy_unit_id=c.unit_id "
    f"WHERE v.record_id {_IN}"
```

(the unit and chunk branches end with `,p.proxy_unit_id IS NOT NULL`, the asset branch with `,0`), and in `owner_facts` unpack the tenth column as `proxy` (`proxy=bool(proxy)`) instead of deriving it from the kind.

```python
# src/tychora/retrieval/vectors.py
"""Vector hits mapped to canonical owners and admitted against the view."""

from collections import defaultdict

from tychora.contracts.candidates import CandidateKey, ChannelCandidate
from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.errors import TychoraError
from tychora.ports.repositories import OwnerFact, ReadTransaction
from tychora.ports.search import RankedOwners
from tychora.retrieval.attempt import is_admitted_record, is_source_unit

MAX_SOURCE_LINKS = 2048


def _facts(tx: ReadTransaction, ids: tuple[str, ...]) -> dict[str, OwnerFact]:
    """Facts of the records, then of their windows and the windows' units."""
    facts = {f.owner_id: f for f in tx.eligibility.owner_facts(ids)}
    related = {
        item
        for f in facts.values()
        for item in (f.chunk_id, f.unit_id, f.asset_id)
        if item is not None and item not in facts
    }
    if related:
        facts.update({f.owner_id: f for f in tx.eligibility.owner_facts(tuple(sorted(related)))})
    return facts


def vector_candidates(
    tx: ReadTransaction, view: EligibilityView, ranked: RankedOwners
) -> tuple[tuple[ChannelCandidate, ...], bool]:
    """Canonical owners of the admitted records, ranked by best distance per kind."""
    order = {
        record: (index, owner.score)
        for index, owner in enumerate(ranked.owners)
        for record in owner.window_ids
    }
    facts = _facts(tx, tuple(sorted(order)))
    best: dict[CandidateKey, tuple[float, list[str]]] = {}
    for record, (_, score) in sorted(order.items(), key=lambda item: -item[1][1]):
        fact = facts.get(record)
        if fact is None or not is_admitted_record(view, fact, facts):
            continue
        key: CandidateKey = (
            ("asset", fact.asset_id) if fact.asset_id is not None else ("unit", fact.unit_id or "")
        )
        current = best.setdefault(key, (score, []))
        if fact.chunk_id is not None and fact.chunk_id not in current[1]:
            current[1].append(fact.chunk_id)
    candidates: list[ChannelCandidate] = []
    for kind in ("unit", "asset"):
        pool = sorted(
            ((score, key, windows) for key, (score, windows) in best.items() if key[0] == kind),
            key=lambda item: (-item[0], item[1]),
        )
        candidates.extend(
            ChannelCandidate(key=key, rank=rank, score=score, matching_window_ids=tuple(windows))
            for rank, (score, key, windows) in enumerate(pool, 1)
        )
    return tuple(candidates), ranked.truncated


def linked_visual_context(
    tx: ReadTransaction, view: EligibilityView, assets: tuple[ChannelCandidate, ...]
) -> tuple[ChannelCandidate, ...]:
    """The eligible source units linked to matched original assets, as unit votes."""
    asset_facts = {
        f.owner_id: f
        for f in tx.eligibility.owner_facts(tuple(c.key[1] for c in assets if c.key[0] == "asset"))
    }
    linked: list[str] = []
    for candidate in assets:
        fact = asset_facts.get(candidate.key[1])
        if fact is None:
            continue
        if len(fact.source_owner_ids) > MAX_SOURCE_LINKS:
            raise TychoraError(
                "INDEX_INCOMPLETE",
                "Visual source links exceed the bound.",
                reason="channel.unavailable",
            )
        linked.extend(u for u in sorted(fact.source_owner_ids) if u not in linked)
    units = {f.owner_id: f for f in tx.eligibility.owner_facts(tuple(linked))}
    return tuple(
        ChannelCandidate(key=("unit", unit), rank=rank)
        for rank, unit in enumerate(
            (u for u in linked if u in units and is_source_unit(view, units[u])), 1
        )
    )
```

Delete `linked_visual_context` from `application/query.py:186-210` (its test in `tests/retrieval/test_query.py` imports it from `tychora.retrieval.vectors` and builds a transaction with `owner.run_read`).

- [ ] **Step 6: Query service channels**

In `src/tychora/application/query.py`, `_text_candidates` (`:1064-1119`) after `(query_vector,) = await attempt.owner_provider(...)` becomes:

```python
        payload = struct.pack("<" + "f" * len(query_vector.values), *query_vector.values)
        ranked = await self.owner.run_write(
            lambda: index.search(attempt.view, payload, 80, ctx), ctx, "interactive"
        )
        return await self.owner.run_read(
            lambda tx: vector_candidates(tx, attempt.view, ranked), ctx
        )
```

so `_text_candidates` returns `(candidates, truncated)`; `_visual_candidates` (`:1199-1260`) takes the same shape after its `vector = await attempt.owner_provider(...)`. In `_prepare`, the two wrappers (`text_candidates`, `retrieve_visual_channel`) keep returning a result or a `TychoraError`; the loop over `vector_results` (`:776-806`) unpacks `candidates, truncated = result`, appends the channel to a local `truncated: list[str]` when `truncated` is true (Task 15 reports it in coverage), and uses `candidates` where it used `result`. The visual branch calls `await self.owner.run_read(lambda tx, selected=candidates: linked_visual_context(tx, attempt.view, selected), ctx)`. The service holds no per-query state. Delete `_text_window_candidates` and `_vector_owners` (`:1121-1197`) and the `WorkLimits` import.

- [ ] **Step 7: Native batches and partitioned upserts**

In `src/tychora/indexing/coordinator.py`, extract from `_text_batch` (`:764-790`) the record checks into

```python
    def _text_stored(
        self,
        family: IndexFamily,
        spec: FamilySpec,
        groups: tuple[ContextGroup, ...],
        records: tuple[EmbeddingRecord, ...],
    ) -> tuple[list[EmbeddingRecord], dict[str, tuple[str, ContextGroup]]]:
        """Provider records checked against the requested groups, with family record IDs."""
        assert self.text_embedder is not None
        expected = {
            digest(
                "embedding-record-v1",
                window_id,
                group.input_sha,
                self.text_embedder.capabilities.space_id,
            ): (window_id, group)
            for group in groups
            for window_id in group.window_ids
        }
        if len(records) != len(expected) or {r.record_id for r in records} != set(expected):
            _fail("Text provider returned incomplete or foreign window vectors")
        stored = []
        owners = {}
        for item in records:
            window_id, group = expected[item.record_id]
            if (
                item.input_sha != group.input_sha
                or item.space_id != self.text_embedder.capabilities.space_id
                or len(item.values) != spec.dimensions
            ):
                _fail("Text provider vector differs from exact requested input")
            record_id = vector_record_id(family.family_id, window_id, item.input_sha)
            stored.append(item.model_copy(update={"record_id": record_id}))
            owners[record_id] = (window_id, group)
        return stored, owners
```

so `_text_batch` starts with `stored, owners = self._text_stored(family, spec, groups, records)` after its `ctx.checkpoint()`; extract the matching `_image_stored(family, spec, inputs, records) -> tuple[list[EmbeddingRecord], dict[str, str]]` from `_image_batch` (`:955-990`) the same way. Then add:

```python
    def native_batches(
        self,
        prepared: PreparedProjection,
        text_records: tuple[EmbeddingRecord, ...],
        visual_records: tuple[EmbeddingRecord, ...] = (),
    ) -> tuple[tuple[VectorIndex, VectorBatch, int], ...]:
        """The native vector writes ``finalize`` will verify, for slots this add writes."""
        output = []
        bundle = prepared.bundle
        if (
            prepared.text_family is not None
            and prepared.text_spec is not None
            and text_records
            and self.text_index is not None
            and self._ready_batch(prepared.text_family, bundle) is None
        ):
            stored, owners = self._text_stored(
                prepared.text_family, prepared.text_spec, prepared.text_groups, text_records
            )
            batch = VectorBatch(
                family_id=prepared.text_spec.family_id,
                records=tuple(sorted(stored, key=lambda item: item.record_id)),
                owners=_text_owners(
                    bundle, prepared.windows, {k: v[0] for k, v in owners.items()}
                ),
            )
            output.append((self.text_index, batch, prepared.text_spec.dimensions))
        if (
            prepared.image_family is not None
            and prepared.image_spec is not None
            and visual_records
            and self.image_index is not None
            and self._ready_batch(prepared.image_family, bundle) is None
        ):
            stored, assets = self._image_stored(
                prepared.image_family, prepared.image_spec, prepared.visual_inputs, visual_records
            )
            batch = VectorBatch(
                family_id=prepared.image_spec.family_id,
                records=tuple(sorted(stored, key=lambda item: item.record_id)),
                owners=_asset_owners(bundle, assets),
            )
            output.append((self.image_index, batch, prepared.image_spec.dimensions))
        return tuple(output)
```

In `src/tychora/application/sources.py`, between `visual_records = await self._visual_records(...)` and the `project` stage (`:867-871`):

```python
        native = await self.owner.run_write(
            lambda: self.coordinator.native_batches(prepared, records, visual_records),
            ctx,
        )
        for index, batch, dimension in native:
            for part in batch.partitions(dimension):
                await self.owner.run_write(
                    lambda i=index, p=part: i.upsert_batch(p, ctx), ctx
                )
```

`finalize` then calls `upsert_batch` with the whole batch, which finds every record present (`upsert_batch` adds only absent records) and returns the receipt; the HNSW inserts already ran one partition per writer task.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_vector_selection.py tests/runtime/test_lanes.py tests/indexing/test_publication.py tests/retrieval/test_query.py tests/acceptance/test_combined_channels.py -v`
Expected: PASS.

- [ ] **Step 9: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add -A src/tychora tests
git commit -m "feat: filter vector search by eligibility and check survivors once"
```

---

### Task 10: Rerank facts and segments from per-candidate slices (C2, Q3)

Reranking and segment assembly load each admitted candidate's whole extraction and run `prepare_extraction` on it (`application/reranking.py:115-343`, `:757`, `:557`), so one result over a repository source revalidates the repository. This task reads a *slice* instead: the candidate's units, its windows' units, its neighbours within the same `boundary_key` (radius 2; a code unit's whole file, the binding scope phase 1 introduced for evaluation, `adapters/sqlite/retained_bindings.py:21-89`), the targets of the candidate's continuation paths, and the persisted ownership intervals and dependency sets of those units, built into the same `RetainedRepresentationRows` and `PreparedExtraction` contracts the readers already consume. `RepresentationRepository.neighbours` arrives here, its first caller. Readers decide readability with `view.allows` over the persisted sets; the legacy view stays only for continuation paths until Task 12.

**Files:**
- Modify: `src/tychora/contracts/corpus.py:180-196` (add `RepresentationSlice`)
- Modify: `src/tychora/adapters/sqlite/retained_bindings.py:21-89` (`BindingScope.unit_ids`)
- Create: `src/tychora/adapters/sqlite/repositories/slices.py` (moved from `adapters/sqlite/eligibility.py:59-251`: `_rows`, `_retained_heads`, `_retained_units`, `_retained_assets`, `_retained_records`, `_retained_artifacts`, `retained_rows`; new `prepared_slice`)
- Modify: `src/tychora/ports/repositories.py` (`RepresentationRepository.neighbours`, `.slice`), `src/tychora/adapters/sqlite/repositories/representations.py`
- Modify: `src/tychora/application/reranking.py:82-343` (delete `_rows`, `_one`, `load_retained_representation`), `:530-584` (`_prepare_canonical` reads slices), `:716-828` (`prepare_rerank_facts` reads slices on the read pool)
- Modify: `src/tychora/retrieval/rerank.py:58-65` (`RerankFacts.dependencies`), `:210-236`, `:285-300`, `:365-420`, `:500-530`, `:620-625` (checks through `view.allows`)
- Modify: `src/tychora/retrieval/anchors.py:12-50`, `src/tychora/retrieval/segments.py:213-281`, `:295-388`, `:424-470`, `:500-527`, `:649-760`, `:812-870` (view v2; `graph_view` for continuations)
- Modify: `src/tychora/retrieval/response.py:636-721` (`prepare_records(tx, selection, ranked, attempt)` on the read pool)
- Modify: `src/tychora/application/query.py:908-978` (rerank facts after graph paths are known; segments and records with the v2 view)
- Test: `tests/storage/test_representation_slices.py` (new); `tests/retrieval/test_segments.py`, `tests/retrieval/test_degraded.py`, `tests/formatting/test_record_attachment.py`, `tests/formatting/test_code_record_binding.py` (views built with `revision_view`; facts carry their persisted sets)

**Interfaces:**
- Consumes: Task 8 `Survivors` (admitted candidates and windows); Task 7 `EligibilityAttempt.view`, `.legacy`; phase 3 `RepresentationRepository.intervals`, `.dependencies`.
- Produces:
  - `contracts/corpus.py`: `class RepresentationSlice(Contract): rows: RetainedRepresentationRows; prepared: PreparedExtraction`.
  - `RepresentationRepository.neighbours(unit_id: str, radius: int | None) -> tuple[UnitRow, ...]` (same extraction and `boundary_key`, `|ordinal − unit's ordinal| ≤ radius`, all of them when `radius` is `None`, by ordinal; the index's name and result); `RepresentationRepository.slice(extraction_id: str, *, unit_ids: tuple[str, ...] = (), relative_path: str | None = None) -> RepresentationSlice` (the listed units, or one code file's units, with their locators, records, linked assets, proxies, the revision's artifact bindings — only the file's own source binding for a file — and the ownership intervals resolved through their dependency sets; a missing interval set is `canonical.facts_missing`).
  - `RerankFacts(rows, prepared, matches, graph_support=None, dependencies: Mapping[str, frozenset[str]] = {})` — the persisted sets of the slice's units and the candidate's windows.
  - `prepare_rerank_facts(owner, attempt, admission, channels, ctx, *, paths: tuple[GraphPath, ...] = ()) -> Mapping[CandidateKey, RerankFacts]`.
  - `permitted_ranges(prepared, unit_id, start, end, view: EligibilityView) -> tuple[tuple[int, int], ...]` (v2 view).
  - `assemble_segments(ranked, view: EligibilityView, config, counter, *, graph_view: LegacyEligibilityView | None = None, ...)`; `prepare_records(tx, selection, ranked, attempt) -> tuple[StructuredRecord, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/storage/test_representation_slices.py
"""Per-candidate slices: neighbours by boundary key and persisted ownership."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.contracts.errors import TychoraError
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext

_MANUAL = (
    "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
    "Record the result.\n\n# Valves\n\nReplace worn valves.\n"
)


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        job = opened.sources.add(
            {
                "type": "text",
                "source_key": "manual.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": _MANUAL,
                "metadata": {"title": "Manual"},
            }
        )
        assert opened.jobs.wait(job.id).state == "succeeded"
        yield opened


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_neighbours_stay_within_boundary_key(kb):
    owner = kb._workspace.owner
    rows = owner_rows(
        owner, "SELECT unit_id,boundary_key,ordinal FROM evidence_units ORDER BY ordinal"
    )
    unit_id, boundary, ordinal = owner_row(
        owner,
        "SELECT unit_id,boundary_key,ordinal FROM evidence_units WHERE text LIKE 'Inspect%'",
    )
    near = _read(kb, lambda tx: tx.representations.neighbours(unit_id, 1))
    every = _read(kb, lambda tx: tx.representations.neighbours(unit_id, None))
    assert all(row.boundary_key == boundary for row in near + every)
    assert all(abs(row.ordinal - ordinal) <= 1 for row in near)
    assert {row.unit_id for row in near} < {row.unit_id for row in every}
    assert {r[0] for r in rows if r[1] == boundary} == {row.unit_id for row in every}


def test_slice_ownership_equals_prepared_extraction(kb, monkeypatch):
    owner = kb._workspace.owner
    extraction_id, unit_id = owner_row(
        owner, "SELECT extraction_id,unit_id FROM evidence_units WHERE text LIKE 'Inspect%'"
    )
    piece = _read(
        kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
    )
    full = on_owner(owner, lambda: prepare_extraction(_full_rows(owner, extraction_id)))
    assert piece.prepared.representation(unit_id) == full.representation(unit_id)
    assert [u.unit_id for u in piece.rows.units] == [unit_id]


def _full_rows(owner, extraction_id):
    """Runs on the writer thread (called inside `on_owner`)."""
    from tychora.adapters.sqlite.repositories.slices import retained_rows
    from tychora.adapters.sqlite.retained_bindings import BindingScope

    revision_id = owner.db.execute(
        "SELECT revision_id FROM extractions WHERE extraction_id=?", (extraction_id,)
    ).fetchone()[0]
    return retained_rows(owner.db, BindingScope(extraction_id, revision_id))


def test_slice_without_interval_sets_is_refused(kb):
    owner = kb._workspace.owner
    extraction_id, unit_id = owner_row(
        owner, "SELECT extraction_id,unit_id FROM evidence_units WHERE text LIKE 'Inspect%'"
    )
    owner_rows(
        owner,
        "DELETE FROM representation_dependencies WHERE owner_id IN "
        "(SELECT dependency_digest FROM ownership_intervals WHERE unit_id=?)",
        (unit_id,),
    )
    with pytest.raises(TychoraError) as error:
        _read(kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,)))
    assert error.value.reason == "canonical.facts_missing"


def test_no_read_path_calls_prepare_extraction():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "tychora"
    allowed = {"indexing/coordinator.py", "indexing/representations.py", "application/maintenance.py"}
    offenders = []
    for path in [*source.joinpath("application").rglob("*.py"), *source.joinpath("retrieval").rglob("*.py")]:
        relative = str(path.relative_to(source))
        if relative in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id == "prepare_extraction":
                offenders.append(relative)
    assert offenders == []
```

`test_no_read_path_calls_prepare_extraction` still fails after this task for `application/inspection.py`, `application/retained_content.py` and `retrieval/visuals.py`; Task 11 moves them. Mark it `@pytest.mark.xfail(strict=True, reason="inspection, retained content and visuals move in the next commit")` in this task and remove the marker in Task 11.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_representation_slices.py -v`
Expected: FAIL with `AttributeError: 'SqliteRepresentationRepository' object has no attribute 'neighbours'` and `ModuleNotFoundError: No module named 'tychora.adapters.sqlite.repositories.slices'`.

- [ ] **Step 3: The slice contract and scope**

In `src/tychora/contracts/corpus.py`, add `from .indexing import PreparedExtraction` and after `RetainedRepresentationRows`:

```python
class RepresentationSlice(Contract):
    """Some units of one extraction: their retained rows and persisted ownership."""

    rows: RetainedRepresentationRows
    prepared: PreparedExtraction
```

In `src/tychora/adapters/sqlite/retained_bindings.py`, `BindingScope` gains `unit_ids: tuple[str, ...] | None = None` (after `relative_path`), and `units()` becomes:

```python
    def units(self) -> tuple[str, tuple[str, ...]]:
        """Select the scope's evidence units through the alias ``u``."""
        if self.unit_ids is not None:
            return (
                "u.extraction_id=? AND u.unit_id IN (SELECT value FROM json_each(?))",
                (self.extraction_id, json.dumps(sorted(set(self.unit_ids)))),
            )
        if self.relative_path is None:
            return "u.extraction_id=?", (self.extraction_id,)
        return (
            (
                "u.extraction_id=? AND EXISTS(SELECT 1 FROM locators c "
                "WHERE c.unit_id=u.unit_id AND c.locator_type='code' "
                "AND c.source_ref=?)"
            ),
            (self.extraction_id, self.relative_path),
        )
```

(add `import json`; `assets()`, `revision_artifacts()` and `artifacts()` already derive from `units()`).

- [ ] **Step 4: Move the retained-row readers and add the prepared slice**

Create `src/tychora/adapters/sqlite/repositories/slices.py` with the docstring `"""Retained rows of a scope and their ownership from persisted intervals."""` and move into it, verbatim, `_integrity`, `_rows`, `_retained_heads`, `_retained_units`, `_retained_assets`, `_retained_records`, `_retained_artifacts` from `src/tychora/adapters/sqlite/eligibility.py:59-209`, with `retained_rows` changed to take a connection and a scope:

```python
def retained_rows(db: sqlite3.Connection, scope: BindingScope) -> RetainedRepresentationRows:
    """The retained rows of one extraction, one code file or some units of it."""
    (source_id,) = db.execute(
        "SELECT source_id FROM revisions WHERE revision_id=?", (scope.revision_id,)
    ).fetchone()
    member = SnapshotMember(
        source_id=source_id,
        revision_id=scope.revision_id,
        extraction_id=scope.extraction_id,
    )
    source, revision, extraction = _retained_heads(db, member)
    units, locators = _retained_units(db, scope)
    assets, links, proxies = _retained_assets(db, scope)
    records, identifiers = _retained_records(db, scope)
    where, args = scope.revision_artifacts()
    revision_artifacts = [
        RevisionArtifactRow.model_validate(row)
        for row in _rows(db, f"SELECT ra.* FROM revision_artifacts ra WHERE {where}", args)
    ]
    artifact_ids = {revision["original_sha"], extraction["document_artifact_sha"]}
    artifact_ids.update(row.artifact_sha for row in revision_artifacts)
    artifact_ids.update(row.artifact_sha for row in assets)
    artifact_ids.update(row.derivation_sha for row in proxies)
    artifact_ids.update(locator.locator.artifact_ref for locator in locators)
    return RetainedRepresentationRows(
        source=SourceRow.model_validate(source),
        artifacts=tuple(_retained_artifacts(db, artifact_ids)),
        revision=RevisionRow.model_validate(revision),
        revision_artifacts=tuple(revision_artifacts),
        extraction=ExtractionRow.model_validate(extraction),
        units=tuple(units),
        locators=tuple(locators),
        assets=tuple(assets),
        unit_assets=tuple(links),
        proxies=tuple(proxies),
        records=tuple(records),
        identifiers=tuple(identifiers),
    )


def _symbol_path(rows: RetainedRepresentationRows, unit_id: str) -> tuple[str, ...]:
    for record in rows.records:
        if record.unit_id == unit_id and record.record_type == "code_symbol":
            data = record.data_json
            return (str(data["relative_path"]), str(data["qualified_name"]))
    return ()


def prepared_slice(
    rows: RetainedRepresentationRows,
    intervals: tuple[IntervalRow, ...],
    sets: Mapping[str, frozenset[str]],
) -> PreparedExtraction:
    """The rows' units with the ownership their persisted intervals record."""
    owned: dict[str, list[OwnedInterval]] = defaultdict(list)
    for row in intervals:
        dependencies = sets.get(row.dependency_digest)
        if not dependencies:
            raise facts_missing()
        owned[row.unit_id].append(
            OwnedInterval(
                char_start=row.char_start, char_end=row.char_end, dependency_ids=dependencies
            )
        )
    return PreparedExtraction(
        extraction_id=rows.extraction.extraction_id,
        revision_id=rows.revision.revision_id,
        units=tuple(
            RepresentationUnit(
                unit=unit,
                revision_id=rows.revision.revision_id,
                title=rows.revision.title,
                kind_id=rows.revision.kind_id,
                symbol_path=_symbol_path(rows, unit.unit_id),
                ownership=tuple(owned.get(unit.unit_id, ())),
            )
            for unit in sorted(rows.units, key=lambda item: (item.ordinal, item.unit_id))
        ),
    )
```

(imports: `sqlite3`, `json`, `defaultdict`, `Mapping`; the contract rows the moved helpers already import; `IntervalRow`, `OwnedInterval`, `PreparedExtraction`, `RepresentationUnit` from `tychora.contracts.indexing`; `SnapshotMember` from `tychora.contracts.publication`; `BindingScope` from `..retained_bindings`; `facts_missing` from `.eligibility`). Delete the moved code from `adapters/sqlite/eligibility.py`; its remaining importers of `retained_rows` (`evaluation/provenance.py`, `application/inspection.py:409`) call `retained_rows(db, BindingScope.of(member, relative_path))` from the new module until Task 11 moves them to slices.

- [ ] **Step 5: Repository methods**

In `src/tychora/ports/repositories.py`, add to `RepresentationRepository`:

```python
    def neighbours(self, unit_id: str, radius: int | None) -> tuple[UnitRow, ...]:
        """Units of the same extraction and boundary key within ``radius`` ordinals, by ordinal."""
        ...

    def slice(
        self,
        extraction_id: str,
        *,
        unit_ids: tuple[str, ...] = (),
        relative_path: str | None = None,
    ) -> RepresentationSlice:
        """Retained rows and persisted ownership of the listed units or of one code file."""
        ...
```

In `src/tychora/adapters/sqlite/repositories/representations.py`:

```python
_NEIGHBOURS = (
    "SELECT n.unit_id,n.extraction_id,n.boundary_key,n.ordinal,n.text_sha,"
    "length(n.text) AS char_count FROM evidence_units u JOIN evidence_units n "
    "ON n.extraction_id=u.extraction_id AND n.boundary_key=u.boundary_key "
    "WHERE u.unit_id=? AND (? IS NULL OR abs(n.ordinal-u.ordinal)<=?) "
    "ORDER BY n.ordinal"
)

    def neighbours(self, unit_id: str, radius: int | None) -> tuple[UnitRow, ...]:
        """Units of the same extraction and boundary key within ``radius`` ordinals, by ordinal."""
        return rows_by_name(UnitRow, self._db.execute(_NEIGHBOURS, (unit_id, radius, radius)))

    def slice(
        self,
        extraction_id: str,
        *,
        unit_ids: tuple[str, ...] = (),
        relative_path: str | None = None,
    ) -> RepresentationSlice:
        """Retained rows and persisted ownership of the listed units or of one code file."""
        row = self._db.execute(
            "SELECT revision_id FROM extractions WHERE extraction_id=?", (extraction_id,)
        ).fetchone()
        if row is None:
            raise TychoraError(
                "INTEGRITY_ERROR",
                "Slice extraction is not registered.",
                reason="canonical.row_invalid",
            )
        scope = BindingScope(
            extraction_id,
            row[0],
            relative_path,
            None if relative_path is not None else unit_ids,
        )
        rows = retained_rows(self._db, scope)
        intervals = self.intervals(tuple(unit.unit_id for unit in rows.units))
        sets = self.dependencies(tuple({row.dependency_digest for row in intervals}))
        return RepresentationSlice(rows=rows, prepared=prepared_slice(rows, intervals, sets))
```

(imports: `RepresentationSlice`, `TychoraError`, `BindingScope` from `..retained_bindings`, `prepared_slice`, `retained_rows` from `.slices`.)

- [ ] **Step 6: Rerank facts from slices**

In `src/tychora/retrieval/rerank.py`, `RerankFacts` (`:58-65`) gains `dependencies: Mapping[str, frozenset[str]] = field(default_factory=dict)` (import `field`), and every legacy check becomes a view check:

- `rerank_candidates` (`:210-236`): delete the `allowed` union; the loop becomes

```python
        for source_id in source_ids:
            dependencies = facts.dependencies.get(source_id)
            if dependencies is None or not ctx.attempt.view.allows(dependencies):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Ranking window is not eligible.",
                    reason="scope.dependency_ineligible",
                )
```

- `:285-300` (after `ranking_input`): the same replacement for `match_ids`.
- `matches_from_channels(candidate, channels, chunks, prepared, view, dependencies)` (`:365-420`): `chunks` is `Mapping[str, ports.repositories.ChunkRow]`; the window check becomes `if window_id not in chunks or not view.allows(dependencies.get(window_id, ())):` and the integrity check keeps `chunk.extraction_id != prepared.extraction_id`, `chunk.char_end > len(unit.text)` and the candidate-unit equality (the chunk ID derivation and text comparison are write-time invariants: dropped).
- `ranking_input` (`:447-664`): delete `allowed`; in `span_for`, `match.window_id not in view.chunk_ids` becomes `match.window_id not in facts.dependencies`, and `if not dependencies <= allowed:` becomes `if not view.allows(dependencies):`; the code-context check `if source_id not in view.context_unit_ids | view.structural_unit_ids:` becomes `if not view.allows(facts.dependencies.get(source_id, ())):`.

Register reason `scope.dependency_ineligible` (`FORBIDDEN_SCOPE`, "Retry the query; a cited range lost its grant while the query ran.") in `error_reasons.py` and `CLASSIFIED_REASONS`.

In `src/tychora/application/reranking.py`, delete `_rows`, `_one`, `load_retained_representation` (`:82-343`) and replace `prepare_rerank_facts` (`:716-828`) with:

```python
NEIGHBOUR_RADIUS = 2


def _window_ids(candidate, channels) -> tuple[str, ...]:
    windows = tuple(
        dict.fromkeys(
            window_id
            for hits in channels.values()
            for hit in hits
            if hit.key == candidate.key
            for window_id in hit.matching_window_ids
        )
    )
    if len(windows) > 2048:
        raise TychoraError(
            "INVALID_REQUEST", "Rerank window limit exceeded.", reason="request.limit_exceeded"
        )
    return windows


def _candidate_slice(tx, unit_id: str, chunks, paths) -> RepresentationSlice:
    """The candidate's units, windows, neighbours and continuation targets; a code file whole."""
    (row,) = tx.representations.units((unit_id,))
    first = tx.representations.slice(row.extraction_id, unit_ids=(unit_id,))
    code = next(
        (loc for loc in first.rows.units[0].locators if isinstance(loc, CodeLocator)), None
    )
    if code is not None:
        return tx.representations.slice(row.extraction_id, relative_path=code.relative_path)
    needed = {unit_id, *(chunk.unit_id for chunk in chunks.values())}
    needed.update(n.unit_id for n in tx.representations.neighbours(unit_id, NEIGHBOUR_RADIUS))
    for path in paths:
        if unit_id in (path.source_unit_id, path.target_unit_id):
            needed.update((path.source_unit_id, path.target_unit_id))
    return tx.representations.slice(row.extraction_id, unit_ids=tuple(sorted(needed)))


async def prepare_rerank_facts(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    admission: Admission,
    channels: Mapping[str, tuple[ChannelCandidate, ...]],
    ctx: ExecutionContext,
    *,
    paths: tuple[GraphPath, ...] = (),
) -> Mapping[CandidateKey, RerankFacts]:
    """Read each admitted text candidate's slice and persisted sets on the read pool."""
    from tychora.retrieval.rerank import RerankFacts, matches_from_channels

    if attempt.owner is not owner or len(admission.selected) > 64:
        raise TychoraError(
            "INTEGRITY_ERROR", "Rerank attempt owner differs.", reason="invariant.unexpected"
        )
    view = attempt.view

    def load(tx) -> Mapping[CandidateKey, RerankFacts]:
        output: dict[CandidateKey, RerankFacts] = {}
        for candidate in admission.selected:
            ctx.checkpoint()
            kind, candidate_id = candidate.key
            if kind == "asset":
                continue  # Native image ranking is independent of text reranking.
            windows = _window_ids(candidate, channels)
            chunks = {row.chunk_id: row for row in tx.representations.chunks(windows)}
            piece = _candidate_slice(tx, candidate_id, chunks, paths)
            dependencies = tx.representations.dependencies(
                (*(u.unit_id for u in piece.rows.units), *windows)
            )
            output[candidate.key] = RerankFacts(
                piece.rows,
                piece.prepared,
                matches_from_channels(
                    candidate, channels, chunks, piece.prepared, view, dependencies
                ),
                dependencies=dependencies,
            )
        return MappingProxyType(output)

    return await owner.run_read(load, ctx)
```

(the function-body import of `RerankFacts` and `matches_from_channels` is the one the current function already has, because `retrieval.rerank` imports this module.) `OwnerRerankAccess._prepare_canonical` (`:530-584`) runs on the writer through `run_write` (it authorizes an upload, a writer duty); wrap its body in `with self.owner.store.uow.read() as tx:` and, per extraction group, replace `load_retained_representation` + `prepare_extraction` by `piece = tx.representations.slice(extraction_id, unit_ids=<every span's and anchor's unit of the group>)` (a code group reads `slice(extraction_id, relative_path=...)` as `_candidate_slice` does), then `attest_ranking_input(item, piece.rows, piece.prepared)`; the anchor chunk check reads `tx.representations.chunks((anchor.indexed_window_id,))`.

- [ ] **Step 7: Segments, anchors and records on the v2 view**

- `src/tychora/retrieval/anchors.py`: import `EligibilityView` from `tychora.contracts.eligibility`; delete `allowed`; `if interval.dependency_ids <= allowed:` becomes `if view.allows(interval.dependency_ids):`.
- `src/tychora/retrieval/segments.py`: import `EligibilityView` from `tychora.contracts.eligibility` and `LegacyEligibilityView` from `tychora.contracts.graph`; add

```python
def _readable(item: RankedEvidenceItem, unit_id: str, view: EligibilityView) -> bool:
    """Whether a unit of the item's slice is readable: its persisted set is allowed."""
    assert item.facts is not None
    dependencies = item.facts.dependencies.get(unit_id)
    return dependencies is not None and view.allows(dependencies)
```

  and replace: `:253` `if source_id not in view.context_unit_ids | view.structural_unit_ids:` → `if not _readable(item, source_id, view):`; `:449` `neighbor.unit_id not in view.context_unit_ids` → `not _readable(item, neighbor.unit_id, view)`; `:727` `gap.unit_id not in view.context_unit_ids` → `not _readable(owner, gap.unit_id, view)`; `:848` `item.facts.prepared.revision_id not in view.revision_ids` → `item.facts.prepared.revision_id not in view.revisions`; `:853` `anchor.indexed_window_id not in view.chunk_ids` → `anchor.indexed_window_id not in item.facts.dependencies`. `_continuation_context` takes `graph_view: LegacyEligibilityView | None` in place of `view` for `graph_generation_id`, `context_unit_ids | structural_unit_ids` and `blocked_assertion_ids` (it returns `(), ()` when `graph_view is None`), and its `target_id not in eligible` check stays on that set; its `permitted_ranges(...)` call passes the v2 `view`. `assemble_segments` gains `graph_view: LegacyEligibilityView | None = None` and forwards it.
- `src/tychora/retrieval/response.py`: `prepare_records(tx, selection, ranked, attempt)` replaces `eligible` and `view.revision_ids`:

```python
    view = attempt.view
    ...
        if segment.revision_id not in view.revisions:
    ...
                dependencies = _record_dependencies(row.data_json, row.unit_id)
                sets = tx.representations.dependencies(tuple(sorted(dependencies)))
                if len(sets) != len(dependencies) or not all(
                    view.allows(sets[unit]) for unit in dependencies
                ):
                    continue
```

  (the function-body `import json` and `contracts` imports move to the module top.)
- `src/tychora/application/query.py` (`_prepare`, `:908-978`): compute `paths` (from `graph_candidates`, `:944-948`) before the facts and call `facts = await prepare_rerank_facts(self.owner, attempt, admission, votes, ctx, paths=paths)`; `assemble_segments(ranked, attempt.view, self.owner.config, self.counter, graph_view=attempt.legacy, continuation_paths=paths, ...)`; `records = await self.owner.run_read(lambda tx: prepare_records(tx, selection, ranked, attempt), ctx)`.

- [ ] **Step 8: Update the unit tests that build views and facts**

- `tests/retrieval/test_segments.py`: `case(...)` (`:52-66`) and every `eligibility_view(...)` call build `revision_view({bundle.revision.revision_id: bundle.extraction.extraction_id}, excluded_units=<the bundle's units the test keeps out of context_unit_ids and structural_unit_ids>)`; `RerankFacts(bundle, prepare_extraction(bundle), matches)` becomes `RerankFacts(bundle, prepare_extraction(bundle), matches, dependencies=_sets(bundle))` with `_sets(bundle) = representation_facts(bundle, prepare_extraction(bundle), (ChunkSpan(c.chunk_id, c.unit_id, c.char_start, c.char_end) for c in bundle.chunks)).dependency_map()` (a test-local helper; tests build facts from the bundle directly, which is why `RerankFacts.rows` keeps accepting an `ExtractionBundle`); tests that exercise continuation pass the old view as `graph_view=`. The assertions do not change (the Docling neighbour-context regression tests stay as they are: `test_same_boundary_neighbors_require_complete_current_eligibility`, `test_one_eligible_ordinal_gap_can_join_matches_but_denied_gap_cannot`).
- `tests/retrieval/test_degraded.py:57-100`, `tests/formatting/test_record_attachment.py`, `tests/formatting/test_code_record_binding.py`: the same view and facts construction; `prepare_records` is called with a transaction: `owner.run_read(lambda tx: prepare_records(tx, ...))` or a fake `SimpleNamespace(representations=SimpleNamespace(dependencies=lambda ids: {i: sets[i] for i in ids if i in sets}))`.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/storage/test_representation_slices.py tests/retrieval/test_segments.py tests/retrieval/test_degraded.py tests/formatting tests/retrieval/test_query.py -v`
Expected: PASS (with the strict xfail of `test_no_read_path_calls_prepare_extraction`).

- [ ] **Step 10: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add -A src/tychora tests
git commit -m "perf: read rerank and segment facts per candidate"
```

---

### Task 11: Visuals, inspection, retained content and discovery from persisted rows

The remaining readers of unit-level sets and the remaining `prepare_extraction` calls on read paths (`retrieval/visuals.py:292`, `application/inspection.py:409`, `application/retained_content.py:148`) move to the v2 view: a unit is readable when `view.allows` its persisted set, an asset likewise, and the rows come from slices. Inspection and discovery compile a v2 view on the writer thread inside `store.uow.read()` (they already run there; moving them to the read pool is phase 5's endpoint work, spec §4.15). Their graph reads (`_relations_sync`, diagnostics resolution rates) stay on the legacy view until Task 12.

**Files:**
- Modify: `src/tychora/retrieval/compiler.py` (`EligibilityCompiler.ensure_compatible`; `compile(..., include_archived=False)`)
- Modify: `src/tychora/retrieval/attempt.py` (helpers `readable_units`, `require_epoch`)
- Modify: `src/tychora/retrieval/visuals.py:1-130`, `:245-314` (`AssetCandidate` with unit sets; `prepare_visual_candidates` on the read pool from slices)
- Modify: `src/tychora/application/inspection.py:76-114`, `:212-300`, `:368-635`, `:740-760` (v2 attempt; slices; legacy kept for `_relations_sync`)
- Modify: `src/tychora/application/retained_content.py:96-266`, `src/tychora/application/discovery.py:1020-1105`
- Modify: `src/tychora/application/query.py:957-962` (`prepare_visual_candidates` with the v2 attempt)
- Test: `tests/storage/test_representation_slices.py` (remove the xfail), `tests/application/test_inspection.py`, `tests/application/test_content_inspection.py`, `tests/retrieval/test_visual_selection.py` (views and attempts in v2 form)

**Interfaces:**
- Consumes: Task 10 `RepresentationRepository.slice`, `RepresentationSlice`; Task 7 predicates.
- Produces:
  - `EligibilityCompiler.ensure_compatible(snapshot, config, read_retained: Callable[[], ResolvedConfig]) -> None`; `EligibilityCompiler.compile(tx, snapshot, scope, request, config, *, members=None, include_archived=False)` (`include_archived` admits archived collections, for exact retained inspection only; such views are never cached).
  - `retrieval/attempt.py`: `readable_units(tx, view, unit_ids) -> frozenset[str]` (the listed units `is_eligible_unit` admits), `require_epoch(tx, view) -> None` (`SCOPE_CHANGED` `policy.epoch_changed` when the epoch moved).
  - `AssetCandidate(candidate, rows, view: EligibilityView, dependency_ids, unit_dependencies: Mapping[str, frozenset[str]], native_multimodal=False)`.
  - `RepresentationRepository.slice(..., asset_ids: tuple[str, ...] = ())` (the units linked to the listed assets, the assets and their proxies).

- [ ] **Step 1: Write the failing tests**

Remove the `xfail` marker from `tests/storage/test_representation_slices.py::test_no_read_path_calls_prepare_extraction`, and add to the same file:

```python
def test_inspecting_a_unit_reads_one_slice(kb, monkeypatch):
    from tychora.adapters.sqlite.repositories import SqliteRepresentationRepository

    owner = kb._workspace.owner
    (unit_id,) = owner_row(
        owner, "SELECT unit_id FROM evidence_units WHERE text LIKE 'Inspect%'"
    )
    calls = []
    original = SqliteRepresentationRepository.slice

    def counted(self, extraction_id, **kwargs):
        calls.append(kwargs)
        return original(self, extraction_id, **kwargs)

    monkeypatch.setattr(SqliteRepresentationRepository, "slice", counted)
    result = kb.inspect(
        {"subject": "evidence", "id": unit_id, "range": {"char_start": 0, "char_end": 7}}
    )
    assert result.excerpts[0].text == "Inspect"
    assert calls == [{"unit_ids": (unit_id,)}]


def test_retained_content_pages_without_whole_extraction_reads(kb, monkeypatch):
    import tychora.adapters.sqlite.repositories.slices as slices

    owner = kb._workspace.owner
    source_id, revision_id, extraction_id = owner_row(
        owner,
        "SELECT r.source_id,e.revision_id,e.extraction_id FROM extractions e "
        "JOIN revisions r USING(revision_id)",
    )
    scopes = []
    original = slices.retained_rows

    def recording(db, scope):
        scopes.append(scope)
        return original(db, scope)

    monkeypatch.setattr(slices, "retained_rows", recording)
    request = ExtractionContentRequest(
        source_id=source_id, revision_id=revision_id, extraction_id=extraction_id, limit=2
    )
    page = kb._workspace.bridge.call(
        kb._workspace.services.inspection.extraction_windows(
            request, kb.scope, ExecutionContext()
        )
    )
    assert page.windows
    assert scopes and all(scope.unit_ids is not None for scope in scopes)
```

(import `ExtractionContentRequest` from `tychora.contracts.operations`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/storage/test_representation_slices.py -v`
Expected: FAIL — `test_no_read_path_calls_prepare_extraction` lists `application/inspection.py`, `application/retained_content.py`, `retrieval/visuals.py`; the slice count tests see whole-extraction reads.

- [ ] **Step 3: Compiler and attempt helpers**

In `src/tychora/retrieval/compiler.py`, add to `EligibilityCompiler`:

```python
    def ensure_compatible(
        self,
        snapshot: SnapshotView,
        config: ResolvedConfig,
        read_retained: Callable[[], ResolvedConfig],
    ) -> None:
        """Check the snapshot's retained configuration once per configuration pair."""
        if not self.knows_compatible(snapshot, config):
            self.mark_compatible(snapshot, config, read_retained())
```

give `compile` a keyword `include_archived: bool = False` passed through `_compile` to `_admitted_members`, where the archive check becomes `active = include_archived or collection_is_active(...)`.

In `src/tychora/retrieval/attempt.py`:

```python
def readable_units(
    tx: ReadTransaction, view: EligibilityView, unit_ids: Iterable[str]
) -> frozenset[str]:
    """The listed units whose persisted sets the view allows."""
    wanted = tuple(sorted(set(unit_ids)))
    if not wanted:
        return frozenset()
    return frozenset(
        fact.owner_id
        for fact in tx.eligibility.owner_facts(wanted)
        if is_eligible_unit(view, fact)
    )


def require_epoch(tx: ReadTransaction, view: EligibilityView) -> None:
    """Refuse delivery when the policy epoch moved since the view was compiled."""
    if tx.workspace.policy_epoch() != view.policy_epoch:
        raise _changed()
```

- [ ] **Step 4: Visual candidates from slices**

In `src/tychora/adapters/sqlite/retained_bindings.py`, `BindingScope` gains `asset_ids: tuple[str, ...] | None = None`; when set, `units()` selects `u.extraction_id=? AND (u.unit_id IN (SELECT unit_id FROM unit_assets WHERE asset_id IN (SELECT value FROM json_each(?))) OR u.unit_id IN (SELECT proxy_unit_id FROM asset_proxies WHERE asset_id IN (SELECT value FROM json_each(?))))` with the extraction and the asset list twice. `RepresentationRepository.slice` gains `asset_ids: tuple[str, ...] = ()` and builds the scope with it.

In `src/tychora/retrieval/visuals.py`, drop the `application.reranking` and `indexing.representations` imports (and the line `tychora.retrieval.visuals -> tychora.application.reranking` from both `ignore_imports` lists of `pyproject.toml`, contracts `layers` and `storage-through-ports`); `AssetCandidate` gains `unit_dependencies: Mapping[str, frozenset[str]]` after `dependency_ids`, with `view: EligibilityView` from `tychora.contracts.eligibility`. In `_visual`:

```python
    readable = candidate.unit_dependencies

    def unit_readable(unit_id: str) -> bool:
        sets = readable.get(unit_id)
        return sets is not None and candidate.view.allows(sets)

    if (
        not {asset_id, rows.revision.revision_id} <= candidate.dependency_ids
        or not candidate.view.allows(candidate.dependency_ids)
        or asset is None
        or not isinstance(asset.locator, ImageLocator)
        or rows.revision.revision_id not in candidate.view.revisions
    ):
        return None
```

the source check `unit_id not in candidate.view.context_unit_ids | candidate.view.structural_unit_ids` becomes `not unit_readable(unit_id)`; the proxy filter `proxy.proxy_unit_id in candidate.view.proxy_unit_ids` becomes `unit_readable(proxy.proxy_unit_id)` and `proxy.required_unit_ids <= allowed` becomes `all(unit_readable(u) for u in proxy.required_unit_ids)`; the caption check `link.unit_id in candidate.view.context_unit_ids` becomes `unit_readable(link.unit_id) and units[link.unit_id].citable` (the unit is in the slice: captions are linked units). `prepare_visual_candidates` becomes:

```python
async def prepare_visual_candidates(
    owner: WorkspaceOwner,
    attempt: EligibilityAttempt,
    ranked: RankedEvidence,
    ctx: ExecutionContext,
) -> tuple[AssetCandidate, ...]:
    """Read each original asset's slice and persisted sets on the read pool."""
    if attempt.owner is not owner or len(ranked.items) > 64:
        raise TychoraError(
            "INTEGRITY_ERROR", "Visual attempt owner differs.", reason="invariant.unexpected"
        )
    view = attempt.view

    def load(tx) -> tuple[AssetCandidate, ...]:
        output: list[AssetCandidate] = []
        assets = [item for item in ranked.items if item.candidate.key[0] == "asset"]
        facts = {
            f.owner_id: f
            for f in tx.eligibility.owner_facts(tuple(i.candidate.key[1] for i in assets))
        }
        for item in assets:
            ctx.checkpoint()
            asset_id = item.candidate.key[1]
            fact = facts.get(asset_id)
            if fact is None or not view.allows(fact.dependency_ids):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Visual asset is not eligible.",
                    reason="scope.dependency_ineligible",
                )
            piece = tx.representations.slice(fact.extraction_id, asset_ids=(asset_id,))
            output.append(
                AssetCandidate(
                    item.candidate,
                    piece.rows,
                    view,
                    fact.dependency_ids,
                    tx.representations.dependencies(
                        tuple(u.unit_id for u in piece.rows.units)
                    ),
                    native_multimodal="visual" in item.candidate.ranks,
                )
            )
        return tuple(output)

    return await owner.run_read(load, ctx)
```

- [ ] **Step 5: Inspection on the v2 view**

In `src/tychora/application/inspection.py`:

1. `__init__` builds `self.compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)` and keeps `self.legacy = LegacyEligibilityCompiler(owner.store)` for `_relations_sync`.
2. `_attempt(self, snapshot, scope, members=None)` compiles inside the caller's read:

```python
    def _attempt(self, tx, snapshot, scope: RequestScope, members=None) -> EligibilityView:
        mode = self._mode(scope)
        query = self._exact_query(mode)
        self.compiler.ensure_compatible(
            snapshot, self.owner.config, lambda: self.owner.store.retained_config(snapshot)
        )
        return self.compiler.compile(
            tx,
            snapshot,
            scope,
            query,
            self.owner.config,
            members=members,
            include_archived=members is not None,
        ).view
```

   where `_mode` and `_exact_query` are the two halves of the current `_attempt` body (`:81-112`), split out unchanged so each stays short.
3. `_inspect_sync` (`:740-757`) wraps the pinned block in `with self.owner.store.uow.read() as tx:`, computes `view = self._attempt(tx, pin.view, granted, pin.members)` and passes `tx` and `view` to the subject functions; `self.compiler.revalidate_epoch(attempt.view, "delivery")` becomes `require_epoch(tx, view)`.
4. `_inspect_diagnostics(tx, request, pin, view)`: `member.revision_id not in attempt.view.revision_ids` → `not in view.revisions`; `eligible = attempt.view.graph_eligible_unit_ids` becomes `eligible = readable_units(tx, view, _mentioned_units(records))` with

```python
def _mentioned_units(records: tuple[Resolution, ...]) -> set[str]:
    return {
        unit
        for item in records
        for unit in (item.source_unit_id, *item.candidates, item.selected_target)
        if unit is not None
    }
```

   and `attempt.view.revision_ids` → `frozenset(view.revisions)`.
5. `_inspect_evidence(tx, request, pin, view, *, prepared=None)`: `revision not in attempt.view.revision_ids` → `revision not in view.revisions`; replace the `eligible` union and `full_unit_readable` with

```python
        (fact,) = tx.eligibility.owner_facts((request.id,))
        full_unit_readable = is_eligible_unit(view, fact)
```

   `prepared = prepare_extraction(retained_rows(owner.store, member))` becomes `prepared = tx.representations.slice(extraction, unit_ids=(request.id,)).prepared`; the proxy check `request.id not in attempt.view.proxy_unit_ids` becomes `not (fact.proxy and full_unit_readable)`; the range check `if not dependencies <= eligible | {request.id}:` becomes `if not view.allows(dependencies - {request.id}):`. Delete the `prepare_extraction` and `retained_rows` imports.
6. `_inspect_asset(tx, request, view)`: replace the check with

```python
        (fact,) = tx.eligibility.owner_facts((request.id,)) or (None,)
        if fact is None or not is_eligible_asset(view, fact) or revision not in view.revisions:
            _deny()
```

7. `_inspect_revision(tx, request, pin, view)`: `request.id not in attempt.view.revision_ids` → `request.id not in view.revisions`; the original-readability loop becomes

```python
            for extraction in selected_extractions:
                originals = tuple(
                    row.unit_id
                    for row in tx.eligibility.unit_flags((extraction,))
                    if not row.proxy
                )
                if readable_units(tx, view, originals) != frozenset(originals):
                    _deny()
```

8. `_relations_sync` keeps `self.legacy.compile_attempt(...)` and its legacy checks until Task 12.

- [ ] **Step 6: Retained content and discovery**

In `src/tychora/application/retained_content.py` (`:96-266`): open `with owner.store.uow.read() as tx:` around the pinned block; `attempt = service._attempt(...)` becomes `view = service._attempt(tx, pin.view, granted, pin.members)`; `request.revision_id not in attempt.view.revision_ids` → `not in view.revisions`; the cursor binding uses `eligible_cache_key(view)` from `tychora.retrieval.compiler`; for windows, `retained = retained_rows(...)` and `prepared = prepare_extraction(retained)` become one slice of the page's units, read after the page query:

```python
        page_units = tuple(dict.fromkeys(row[columns.index("unit_id")] for row in candidates[:MAX_CANDIDATES]))
        piece = tx.representations.slice(request.extraction_id, unit_ids=page_units) if windows else None
        retained, prepared = (piece.rows, piece.prepared) if piece is not None else (None, None)
```

the units branch's `uid not in attempt.view.context_unit_ids | attempt.view.structural_unit_ids` becomes `uid not in readable` with `readable = readable_units(tx, view, page_units)` computed once before the loop; `service._inspect_evidence(...)` receives `tx`, the pin and `view`; the final `service.compiler.revalidate_epoch(...)` becomes `require_epoch(tx, view)`.

In `src/tychora/application/discovery.py:1020-1105`: `compiler = EligibilityCompiler(owner.store.registry, owner.artifacts)`; inside `with owner.store.uow.read() as tx:`, `compiler.ensure_compatible(snapshot, owner.config, lambda: owner.store.retained_config(snapshot))` and `view = compiler.compile(tx, snapshot, scope, request, owner.config).view`; `row[0] not in view.revision_ids` → `row[0] not in view.revisions`; `_scoped_resolution_rates(records, view.graph_eligible_unit_ids, view.revision_ids, row[2])` becomes `_scoped_resolution_rates(records, readable_units(tx, view, _mentioned_units(records)), frozenset(view.revisions), row[2])` (move `_mentioned_units` next to `_permitted_resolutions` and import it in inspection); `compiler.revalidate_epoch(view, "delivery")` becomes `require_epoch(tx, view)`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/storage/test_representation_slices.py tests/application/test_inspection.py tests/application/test_content_inspection.py tests/retrieval/test_visual_selection.py tests/application/test_discovery.py -v`
Expected: PASS (unit tests that built legacy views for these functions now build `revision_view(...)` and pass a transaction or a `SimpleNamespace` with `eligibility`/`representations` fakes).

- [ ] **Step 8: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add -A src/tychora tests pyproject.toml
git commit -m "refactor: read visuals and inspection through the revision-level view"
```

---

### Task 12: Graph scope and removal of the legacy view

The graph store still filters traversal by explicit unit lists (`adapters/ladybug/traversal.py:452-460`), which the corpus-wide generation needs until phase 6 replaces it with contributions (spec §4.5). This task derives those lists from the v2 view only when a graph channel runs, cached with the view, and deletes the legacy view, compiler, attempt fields and fact reader. A unit of an eligible extraction is eligible exactly when it is not excluded, because exclusions are closed under dependency and a unit's set names the unit itself, so the scope needs no dependency read.

**Files:**
- Modify: `src/tychora/contracts/graph.py:188-238` (delete `LegacyEligibilityView`; add `GraphScope`)
- Modify: `src/tychora/ports/graph.py:8`, `:40-60`, `src/tychora/adapters/ladybug/traversal.py`, `src/tychora/adapters/ladybug/generation.py:18`, `:78-104`, `src/tychora/retrieval/graph.py:7`, `:36-60`, `src/tychora/retrieval/response.py:493-560`, `src/tychora/retrieval/segments.py` (`graph_view: GraphScope | None`) — type and parameter `GraphScope`
- Modify: `src/tychora/ports/repositories.py`, `src/tychora/adapters/sqlite/repositories/eligibility.py` (`EligibilityRepository.blocked_assertions`)
- Create: `src/tychora/retrieval/graph_scope.py` (`graph_scope`)
- Modify: `src/tychora/retrieval/cache.py` (`EligibilityCache.graph_scope`)
- Modify: `src/tychora/retrieval/attempt.py` (delete `legacy`, `legacy_dependencies`, `dependencies_for` and the legacy compile in `_attempt`; `run_pinned_eligibility(compiler, selector, ...)` loses the `legacy` parameter)
- Modify: `src/tychora/application/query.py:815-842` (graph scope), `:917-940` (relation checks), `:1262-1355` (`_registered_image_query` checks through facts), `src/tychora/application/inspection.py:795-900` (`_relations_sync`)
- Delete: `src/tychora/retrieval/eligibility.py`, `src/tychora/adapters/sqlite/eligibility.py`, `CorpusStore.read_eligibility_facts` and `CorpusStore.policy_epoch` (`adapters/sqlite/corpus.py:114-117`, `:170-173`); `pyproject.toml` ignores `tychora.adapters.sqlite.eligibility -> tychora.retrieval.eligibility` and `tychora.retrieval.eligibility -> tychora.application.owner` (both lists)
- Create: `tests/helpers/eligibility_v1_oracle.py` (the deleted compiler and reader, kept as the equivalence oracle)
- Test: `tests/retrieval/test_graph_scope.py` (new); `tests/retrieval/test_eligibility_v2.py` (oracle import); every test importing the deleted names (`grep -rln "LegacyEligibilityView\|LegacyEligibilityCompiler\|LegacyAttempt\|read_eligibility_facts\|retrieval.eligibility" tests`)

**Interfaces:**
- Consumes: Task 7 `CompiledEligibility`, `EligibilityCache`; Task 6 `unit_flags`.
- Produces:
  - `contracts/graph.py`: `class GraphScope(Contract)` with `snapshot_id`, `policy_epoch`, `scope_digest`, `eligibility_policy_digest`, `eligibility_request_digest`, `receipt: GraphBatchReceipt`, `revision_ids`, `selected_batch_ids`, `source_unit_ids`, `context_unit_ids`, `structural_unit_ids`, `blocked_assertion_ids` and the properties `graph_generation_id`, `graph_family_id`, `graph_config_sha`, `graph_decision_watermark`, `graph_manifest_digest`, `graph_eligible_unit_ids` (the names the traversal adapter reads, so its code changes in type only).
  - `EligibilityRepository.blocked_assertions(watermark: int) -> frozenset[str]`.
  - `retrieval/graph_scope.py`: `graph_scope(tx, view) -> GraphScope | None`.
  - `EligibilityCache.graph_scope(key: EligibilityKey, build: Callable[[], GraphScope | None]) -> GraphScope | None`.
  - `EligibilityAttempt(view, snapshot, warnings, historical_kind_roles, historical_kind_envelopes, owner, ctx)`; `run_pinned_eligibility(compiler, selector, scope, request, config, operation, *, owner, ctx, on_invalidated=None)`.
  - `tests/helpers/eligibility_v1_oracle.py`: `OracleCompiler(store)` with `compile_eligibility(snapshot, scope, request, config) -> OracleView`.

- [ ] **Step 1: Keep the oracle before deleting it**

Create `tests/helpers/eligibility_v1_oracle.py` with the docstring `"""The unit-level eligibility compiler v1 used, kept to check the revision-level one."""` and, copied verbatim: `LegacyEligibilityView` from `contracts/graph.py` renamed `OracleView`; `RevisionFact` usage through `tychora.retrieval.selection`; `UnitFact`, `ChunkFact`, `AssetFact`, `VectorFact`, `EligibilityFacts`, `_allowed_dependencies`, `_dependents_index`, `_dependency_fixpoint` and `LegacyEligibilityCompiler` (renamed `OracleCompiler`, keeping only `__init__`, `compile_eligibility` and `_compile`) from `retrieval/eligibility.py`; `read_eligibility_facts` and its helpers (`_persisted`, `_vector_facts`, `_graph_receipt`, `_unit_facets`, the three SQL constants) from `adapters/sqlite/eligibility.py`, with `filter_matches` imported from `tychora.retrieval.compiler` and `resolve_graph_traversal_policy` from `tychora.configuration.graph_policy`. `OracleCompiler(store).compile_eligibility(...)` calls the copied `read_eligibility_facts(store, snapshot)`. In `tests/retrieval/test_eligibility_v2.py`, replace `LegacyEligibilityCompiler(owner.store)` with `OracleCompiler(owner.store)`.

Run: `uv run --locked pytest tests/retrieval/test_eligibility_v2.py -v`
Expected: PASS against the oracle (nothing in `src` changed yet).

- [ ] **Step 2: Write the failing tests**

```python
# tests/retrieval/test_graph_scope.py
"""Graph traversal scopes come from the revision-level view, only when needed."""

from types import SimpleNamespace

from tests.helpers.eligibility import revision_view
from tychora.contracts.eligibility import GraphBinding
from tychora.contracts.graph import GraphBatchReceipt
from tychora.contracts.identity import digest
from tychora.ports.repositories import UnitFlags
from tychora.retrieval.graph_scope import graph_scope


def _receipt():
    return GraphBatchReceipt(
        generation_id="generation",
        family_id=digest("graph-family"),
        config_sha=digest("graph-config"),
        decision_watermark=4,
        manifest_digest=digest("manifest"),
        entity_count=0,
        evidence_count=0,
        assertion_count=0,
    )


def _tx(flags, blocked=frozenset()):
    calls = []

    def unit_flags(extractions):
        calls.append(extractions)
        return tuple(f for f in flags if f.extraction_id in extractions)

    eligibility = SimpleNamespace(
        unit_flags=unit_flags, blocked_assertions=lambda watermark: blocked
    )
    return SimpleNamespace(eligibility=eligibility), calls


def test_no_graph_binding_reads_nothing():
    tx, calls = _tx(())
    assert graph_scope(tx, revision_view()) is None
    assert calls == []


def test_scope_is_eligible_units_minus_exclusions():
    receipt = _receipt()
    view = revision_view(excluded_units=("excluded",), unit_filter=("source", "proxy"))
    view = view.model_copy(
        update={"graph": GraphBinding(family_id=receipt.family_id, all_eligible=False, receipt=receipt)}
    )
    flags = (
        UnitFlags("source", "extraction", True, False),
        UnitFlags("filtered", "extraction", True, False),
        UnitFlags("heading", "extraction", False, False),
        UnitFlags("proxy", "extraction", False, True),
        UnitFlags("excluded", "extraction", True, False),
        UnitFlags("other", "elsewhere", True, False),
    )
    tx, calls = _tx(flags, blocked=frozenset({"assertion"}))
    scope = graph_scope(tx, view)
    assert calls == [("extraction",)]
    assert scope.source_unit_ids == {"source"}
    assert scope.context_unit_ids == {"source", "filtered", "proxy"}
    assert scope.structural_unit_ids == {"heading"}
    assert scope.graph_eligible_unit_ids == {"source", "filtered", "proxy", "heading"}
    assert scope.blocked_assertion_ids == {"assertion"}
    assert scope.graph_generation_id == "generation"
    assert "generation" in scope.selected_batch_ids


def test_legacy_names_are_gone():
    import importlib.util

    assert importlib.util.find_spec("tychora.retrieval.eligibility") is None
    assert importlib.util.find_spec("tychora.adapters.sqlite.eligibility") is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_graph_scope.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'tychora.retrieval.graph_scope'`.

- [ ] **Step 4: The graph scope contract and its derivation**

In `src/tychora/contracts/graph.py`, replace `class LegacyEligibilityView` (`:188-238`) with:

```python
class GraphScope(Contract):
    """The units, batches and blocked assertions one query's traversal may read."""

    snapshot_id: Identifier
    policy_epoch: NonNegativeInt
    scope_digest: Sha256
    eligibility_policy_digest: Sha256
    eligibility_request_digest: Sha256
    receipt: GraphBatchReceipt
    revision_ids: frozenset[Identifier]
    selected_batch_ids: tuple[Identifier, ...]
    source_unit_ids: frozenset[Identifier]
    context_unit_ids: frozenset[Identifier]
    structural_unit_ids: frozenset[Identifier]
    blocked_assertion_ids: frozenset[Sha256]

    @property
    def graph_generation_id(self) -> str:
        return self.receipt.generation_id

    @property
    def graph_family_id(self) -> str:
        return self.receipt.family_id

    @property
    def graph_config_sha(self) -> str:
        return self.receipt.config_sha

    @property
    def graph_decision_watermark(self) -> int:
        return self.receipt.decision_watermark

    @property
    def graph_manifest_digest(self) -> str:
        return self.receipt.manifest_digest

    @property
    def graph_eligible_unit_ids(self) -> frozenset[str]:
        return self.context_unit_ids | self.structural_unit_ids
```

(the `model_validator` of the old class goes with it).

Add to `EligibilityRepository` (port) and `SqliteEligibilityRepository`:

```python
    def blocked_assertions(self, watermark: int) -> frozenset[str]:
        """Assertions with a decision recorded after ``watermark``."""
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT DISTINCT assertion_id FROM assertion_decisions WHERE decision_id>?",
                (watermark,),
            )
        )
```

```python
# src/tychora/retrieval/graph_scope.py
"""The unit lists a graph traversal filters by, derived from a revision-level view."""

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.graph import GraphScope
from tychora.ports.repositories import ReadTransaction


def graph_scope(tx: ReadTransaction, view: EligibilityView) -> GraphScope | None:
    """Eligible units of eligible extractions, split as traversal reads them.

    A unit of an eligible extraction is eligible exactly when it is not
    excluded: its persisted set names itself and its revision, and exclusions
    are closed under dependency.
    """
    if view.graph is None:
        return None
    flags = [
        row
        for row in tx.eligibility.unit_flags(tuple(sorted(view.extraction_ids)))
        if row.unit_id not in view.excluded_unit_ids
    ]
    unit_filter = view.unit_filter
    return GraphScope(
        snapshot_id=view.snapshot_id,
        policy_epoch=view.policy_epoch,
        scope_digest=view.scope_digest,
        eligibility_policy_digest=view.eligibility_policy_digest,
        eligibility_request_digest=view.eligibility_request_digest,
        receipt=view.graph.receipt,
        revision_ids=frozenset(view.revisions),
        selected_batch_ids=(
            *sorted(view.batch_ids - {view.graph.receipt.generation_id}),
            view.graph.receipt.generation_id,
        ),
        source_unit_ids=frozenset(
            row.unit_id
            for row in flags
            if not row.proxy
            and row.citable
            and (unit_filter is None or row.unit_id in unit_filter)
        ),
        context_unit_ids=frozenset(
            row.unit_id for row in flags if row.proxy or row.citable
        ),
        structural_unit_ids=frozenset(
            row.unit_id for row in flags if not row.proxy and not row.citable
        ),
        blocked_assertion_ids=tx.eligibility.blocked_assertions(
            view.graph.receipt.decision_watermark
        ),
    )
```

In `src/tychora/retrieval/cache.py`, `EligibilityCache` keeps a second `OrderedDict[EligibilityKey, GraphScope | None]` (`self._graphs`, same capacity, same lock) with:

```python
    def graph_scope(
        self, key: EligibilityKey, build: Callable[[], GraphScope | None]
    ) -> GraphScope | None:
        """The graph scope of a cached view's key, built once."""
        with self._lock:
            if key in self._graphs:
                self._graphs.move_to_end(key)
                return self._graphs[key]
        scope = build()
        with self._lock:
            self._graphs[key] = scope
            while len(self._graphs) > self._capacity:
                self._graphs.popitem(last=False)
        return scope
```

and `purge` clears both dictionaries.

- [ ] **Step 5: Retype the graph readers**

Run:

```bash
grep -rl "LegacyEligibilityView" src/tychora | xargs sed -i '' -E 's/\bLegacyEligibilityView\b/GraphScope/g'
```

Then `ports/graph.py`, `adapters/ladybug/traversal.py`, `adapters/ladybug/generation.py`, `retrieval/graph.py`, `retrieval/response.py` (`inspect_relations`) and `retrieval/segments.py` (`graph_view`) take a `GraphScope` where they took the view; their attribute reads (`graph_generation_id`, `source_unit_ids`, `graph_eligible_unit_ids`, `blocked_assertion_ids`, the `graph_*` receipt fields, `selected_batch_ids`, `revision_ids`) are the scope's. The traversal's `_bind_budget` digest keeps its field list.

- [ ] **Step 6: The query service and inspection use the scope**

In `src/tychora/application/query.py` (`_prepare`), before graph augmentation:

```python
        graph = None
        if not request.exact_only and self.graph is not None and (plan is None or plan.graph_strategies):
            key = eligible_cache_key(attempt.view)
            graph = await self.owner.run_read(
                lambda tx: self.owner.eligibility_cache.graph_scope(
                    key, lambda: graph_scope(tx, attempt.view)
                ),
                ctx,
            )
            if plan is not None and graph is None:
                raise TychoraError(
                    "CAPABILITY_MISSING",
                    "Pinned graph generation is unavailable",
                    reason="graph.disabled",
                )
```

`augment(...)`, the graph votes filter (`item.unit_id in graph.source_unit_ids`), the relation checks (`eligible = graph.graph_eligible_unit_ids`, `view.blocked_assertion_ids` → `graph.blocked_assertion_ids`), `assemble_segments(..., graph_view=graph)` and `inspect_relations(self.graph, graph, ...)` read `graph`; when `graph is None` the graph branches are skipped as they are today when `self.graph is None`. `_registered_image_query(self, asset_id, attempt)` keeps its SQL (moved by Task 19) and replaces its legacy checks inside `with self.owner.store.uow.read() as tx:`:

```python
            facts = {
                f.owner_id: f
                for f in tx.eligibility.owner_facts((asset_id, *(item[0] for item in source_rows)))
            }
            asset_fact = facts.get(asset_id)
            if asset_fact is None or not is_eligible_asset(view, asset_fact):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Image query asset is not eligible.",
                    reason="scope.dependency_ineligible",
                )
            if row[8] not in view.revisions or not all(
                (unit := facts.get(item[0])) is not None and is_eligible_unit(view, unit)
                for item in source_rows
            ):
                raise TychoraError(
                    "FORBIDDEN_SCOPE",
                    "Image query source is not eligible.",
                    reason="scope.dependency_ineligible",
                )
```

and `dependencies` stays `frozenset({asset_id, *(item[0] for item in source_rows)})`.

In `src/tychora/retrieval/attempt.py`, delete `legacy`, `legacy_dependencies` and `dependencies_for` from `EligibilityAttempt`, the `legacy` parameter of `run_pinned_eligibility`, and the legacy compile and epoch comparison in `_attempt` (which now returns the attempt directly). `QueryService` drops `self.legacy_compiler`.

In `src/tychora/application/inspection.py`, `_relations_sync` compiles with `self._attempt(tx, snapshot, granted)` and `graph = graph_scope(tx, view)` inside `with self.owner.store.uow.read() as tx:`, and reads `graph` where it read `view`; delete `self.legacy`.

- [ ] **Step 7: Delete the legacy modules**

Delete `src/tychora/retrieval/eligibility.py` and `src/tychora/adapters/sqlite/eligibility.py`; delete `CorpusStore.read_eligibility_facts` (`:114-117`) and `CorpusStore.policy_epoch` (`:170-173`) and their port declarations; delete the two `pyproject.toml` ignores named in **Files** (and `tychora.adapters.sqlite.eligibility -> tychora.application.collection_policy` if Task 7 left it). Update the remaining test importers (`tests/retrieval/test_eligibility.py` tests of the deleted compiler move to the oracle or are deleted when they test only v1 internals; `tests/helpers/eligibility.py` loses `eligibility_view`).

Run: `rg -n "LegacyEligibilityView|LegacyEligibilityCompiler|LegacyAttempt|read_eligibility_facts|retrieval\.eligibility|attempt\.legacy|dependencies_for" src/tychora`
Expected: no match.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval tests/formatting tests/application/test_inspection.py tests/evaluation/test_graph_scenarios.py -q`
Expected: PASS, including `test_v2_admits_exactly_what_v1_admitted_on_500_randomized_fixtures` against the oracle.

- [ ] **Step 9: Full suite and imports**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'` and `uv run --locked lint-imports --no-cache`
Expected: PASS; every contract kept with fewer exceptions.

- [ ] **Step 10: Commit**

```bash
git add -A src/tychora tests pyproject.toml
git commit -m "refactor: derive graph scopes from the revision-level view and drop the unit-level view"
```

---

### Task 13: The result cache stores `CachedSelection` (Q5)

**Files:**
- Modify: `src/tychora/retrieval/cache.py:110-115` (`get_result`/`put_result` take and return bytes)
- Create: `src/tychora/retrieval/cached_selection.py` (`CachedSelection`; its own module because `retrieval/cache.py` is imported by `adapters/voyage/runtime.py` and `ports/query.py`, and importing `retrieval.segments` from it would close an import cycle through `retrieval.rerank` and `application.reranking`)
- Modify: `src/tychora/application/query.py:97-108` (`_PreparedResult` becomes `CachedSelection` plus the selection), `:599-618`
- Test: `tests/retrieval/test_result_cache.py` (new); `tests/retrieval/test_query.py` (cache assertions that inspected `_PreparedResult`)

**Interfaces:**
- Consumes: `Selection` (`retrieval/segments.py:48-54`), `StructuredRecord`, `RelationChecks`, `RelationPath`, `VisualEvidence`, `Coverage`, `DecisionRequirement` (`contracts/evidence.py`).
- Produces:
  - `retrieval/cached_selection.py`: `CACHE_OVERHEAD_BYTES = 65536`; `class CachedSelection(Contract)` with `segments`, `reservation_priority`, `omitted_anchors`, `lane_coverage`, `text_budget`, `visual_reserved_tokens`, `records`, `checks`, `relations`, `visuals`, `coverage`, `warnings`, `decisions`, `candidate_count`, and `to_bytes() -> bytes` (canonical JSON), `from_bytes(raw: bytes) -> CachedSelection`, `of(selection, ...) -> CachedSelection`, `selection() -> Selection`; `QueryCaches.get_result(key) -> bytes | None`, `QueryCaches.put_result(key, value: bytes) -> None` (no pickle of values).

`QueryService` caches an entry only when its canonical JSON is at most `max_response_bytes + CACHE_OVERHEAD_BYTES` bytes; a larger result is served uncached. The query trace joins the entry in phase 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_result_cache.py
"""Result cache entries are canonical JSON within the response bound; no pickle."""

import pickle

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.retrieval.cache import QueryCaches
from tychora.retrieval.cached_selection import CACHE_OVERHEAD_BYTES, CachedSelection


def _add(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_cache_entry_under_bound_for_300_page_result(tmp_path, monkeypatch):
    pages = "\n\n".join(
        f"# Page {n}\n\nThe pump seal inspection outcome is recorded on page {n}. "
        + "Operators verify the seal and record the verification outcome. " * 30
        for n in range(300)
    )
    stored: list[bytes] = []
    original = QueryCaches.put_result

    def recording(self, key, value):
        stored.append(value)
        return original(self, key, value)

    monkeypatch.setattr(QueryCaches, "put_result", recording)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "book.md", pages)
        first = kb.search({"question": "verification outcome"})
        again = kb.search({"question": "verification outcome"})
        limit = kb._workspace.owner.config.values.limits.max_response_bytes
    assert len(stored) == 1
    assert len(stored[0]) <= limit + CACHE_OVERHEAD_BYTES
    assert [s.segment_id for s in again.segments] == [s.segment_id for s in first.segments]


def test_cached_selection_round_trips_without_pickle(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pickle used for a cached result")

    monkeypatch.setattr(pickle, "loads", forbidden)
    from tychora.contracts.evidence import Coverage, RelationChecks

    entry = CachedSelection(
        segments=(),
        reservation_priority=("standard",),
        omitted_anchors=(),
        lane_coverage=("standard",),
        text_budget=5400,
        visual_reserved_tokens=600,
        records=(),
        checks=RelationChecks(),
        relations=(),
        visuals=(),
        coverage=Coverage(status="complete", completed_channels=("lexical",)),
        warnings=("edition warning",),
        decisions=(),
        candidate_count=3,
    )
    raw = entry.to_bytes()
    assert raw.startswith(b"{")
    assert CachedSelection.from_bytes(raw) == entry
    caches = QueryCaches()
    caches.put_result("key", raw)
    assert caches.get_result("key") == raw


def test_put_result_refuses_non_bytes():
    with pytest.raises(TypeError):
        QueryCaches().put_result("key", object())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_result_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.retrieval.cached_selection'`.

- [ ] **Step 3: The cached selection contract**

Create `src/tychora/retrieval/cached_selection.py` with the docstring `"""Prepared query results as canonical JSON cache entries."""` and the imports `Contract` from `tychora.contracts.base`; `AnchorRange`, `Coverage`, `DecisionRequirement`, `RelationChecks`, `RelationPath`, `Segment`, `StructuredRecord`, `VisualEvidence` from `tychora.contracts.evidence`; `canonical_bytes` from `tychora.contracts.identity`; `Selection` from `tychora.retrieval.segments`:

```python
CACHE_OVERHEAD_BYTES = 65536


class CachedSelection(Contract):
    """A prepared query result kept as canonical JSON; never a pickle."""

    segments: tuple[Segment, ...]
    reservation_priority: tuple[str, ...]
    omitted_anchors: tuple[AnchorRange, ...]
    lane_coverage: tuple[str, ...]
    text_budget: int
    visual_reserved_tokens: int
    records: tuple[StructuredRecord, ...]
    checks: RelationChecks
    relations: tuple[RelationPath, ...]
    visuals: tuple[VisualEvidence, ...]
    coverage: Coverage
    warnings: tuple[str, ...]
    decisions: tuple[DecisionRequirement, ...]
    candidate_count: int

    @classmethod
    def of(cls, selection: Selection, **result: object) -> "CachedSelection":
        """The entry for a selection and the rest of its prepared result."""
        return cls(
            segments=selection.segments,
            reservation_priority=selection.reservation_priority,
            omitted_anchors=selection.omitted_anchors,
            lane_coverage=selection.lane_coverage,
            text_budget=selection.text_budget,
            visual_reserved_tokens=selection.visual_reserved_tokens,
            **result,
        )

    def selection(self) -> Selection:
        """The selection this entry holds."""
        return Selection(
            segments=self.segments,
            reservation_priority=self.reservation_priority,
            omitted_anchors=self.omitted_anchors,
            lane_coverage=self.lane_coverage,
            text_budget=self.text_budget,
            visual_reserved_tokens=self.visual_reserved_tokens,
        )

    def to_bytes(self) -> bytes:
        """Canonical JSON of the entry."""
        return canonical_bytes(self.model_dump(mode="json"))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "CachedSelection":
        """Read an entry written by ``to_bytes``."""
        return cls.model_validate_json(raw)
```

In `src/tychora/retrieval/cache.py`, replace `get_result`/`put_result` (the `pickle` import stays for hashing keys of the embedding pool, which are tuples of scalars; values are never pickled):

```python
    def get_result(self, key: Hashable) -> bytes | None:
        value = self._get(1, key)
        return value if isinstance(value, bytes) else None

    def put_result(self, key: Hashable, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise TypeError("Cached results are canonical JSON bytes")
        self._put(1, key, value)
```

- [ ] **Step 4: The query service caches entries**

In `src/tychora/application/query.py`, delete `_PreparedResult` (`:97-108`); `_prepare` returns `(CachedSelection.of(selection, records=records, checks=checks, relations=tuple(relations), visuals=visuals, coverage=coverage, warnings=attempt.warnings, decisions=decisions, candidate_count=len(fused)), lexical_epoch)`; in `operation` (`:599-618`):

```python
            raw = self.caches.get_result(key)
            if raw is not None:
                prepared = CachedSelection.from_bytes(raw)
                cache_hit = True
            else:
                prepared, actual_epoch = await self._prepare(
                    request, granted, attempt, mode, ctx
                )
                key = result_cache_key(...)  # unchanged
                entry = prepared.to_bytes()
                if (
                    len(entry)
                    <= self.owner.config.values.limits.max_response_bytes
                    + CACHE_OVERHEAD_BYTES
                ):
                    self.caches.put_result(key, entry)
                lexical_epoch = actual_epoch
                cache_hit = False
```

`_deliver` reads `prepared.selection()`, `prepared.records`, `prepared.checks`, `prepared.relations`, `prepared.visuals`, `prepared.coverage`, `prepared.warnings`, `prepared.decisions`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_result_cache.py tests/retrieval/test_query.py -v`
Expected: PASS.

- [ ] **Step 6: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/retrieval/cache.py src/tychora/retrieval/cached_selection.py src/tychora/application/query.py tests/retrieval
git commit -m "perf: cache query results as canonical JSON"
```

---

### Task 14: Stop words (C4)

**Files:**
- Create: `src/tychora/resources/stopwords/en@1.txt`, `src/tychora/resources/stopwords/fr@1.txt`, `src/tychora/resources/stopwords/LICENSE-snowball.txt`
- Create: `src/tychora/configuration/stopwords.py`
- Modify: `src/tychora/configuration/models.py:31-35` (`LexicalSettings`, `RetrievalSettings.lexical`), `src/tychora/configuration/resolve.py:570` (`query_fingerprint` binds the lists' digest), `src/tychora/configuration/indexed_representation.py:40-56` (the lexical profile is query-side: changing it needs no rebuild)
- Modify: `src/tychora/retrieval/lexical_query.py:108-131` (keep the original terms when removal empties the query), `src/tychora/retrieval/lexical_query.py` (`retrieve_exact_lexical(..., stopwords=)`), `src/tychora/ports/search.py` and `src/tychora/adapters/sqlite/lexical.py` (`search(..., stopwords=frozenset())`), `src/tychora/application/query.py` (load the set once per service)
- Test: `tests/retrieval/test_stopwords.py` (new), `tests/retrieval/test_lexical.py:20-31` (the empty-query assertion), `tests/release/test_resources.py:13-22` (the lists ship in the wheel)

**Interfaces:**
- Consumes: Task 8 `SqliteLexicalSearch`, `retrieve_exact_lexical`.
- Produces:
  - `RetrievalSettings.lexical: LexicalSettings` with `languages: tuple[Literal["en", "fr"], ...] = ("en", "fr")` and `stopwords: Literal["builtin@1", "none"] = "builtin@1"` (the profile key is `[configuration.retrieval.lexical]`).
  - `configuration/stopwords.py`: `class StopwordSet(NamedTuple): words: frozenset[str]; digest: str`; `load_stopwords(languages: tuple[str, ...], selection: str) -> StopwordSet` (cached; the digest binds each list's file bytes).
  - `LexicalSearch.search(view, query, limit, ctx, *, stopwords: frozenset[str] = frozenset())`; `retrieve_exact_lexical(tx, view, request, registry, limit, ctx, *, stopwords: frozenset[str] = frozenset())`.

The lists are derived from the Snowball English and French lists (BSD 3-Clause): forms with apostrophes are left out because the query tokenizer splits on them, and the French "ai" and "eu" are left out because both lists apply to every query and they collide with the English acronyms AI and EU. Removal happens when the FTS expression is built, so the index never changes with the lists; the lexical profile is excluded from the pinned-configuration comparison for that reason.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_stopwords.py
"""Versioned stop-word lists shape lexical expressions, never the index."""

from pathlib import Path

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.configuration.stopwords import load_stopwords
from tychora.retrieval.lexical_query import lexical_expression


def _config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n' + extra)
    return resolve_config(path, "test", {})


def test_builtin_lists_load_and_are_lowercase_and_sorted():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    assert {"the", "of", "and", "le", "la", "été"} <= words
    assert not {"ai", "eu"} & words
    assert load_stopwords(("en", "fr"), "none").words == frozenset()
    root = Path(__file__).resolve().parents[2] / "src/tychora/resources/stopwords"
    for name in ("en@1.txt", "fr@1.txt"):
        lines = [l for l in (root / name).read_text().splitlines() if l and not l.startswith("#")]
        assert lines == sorted(lines) and all(l == l.casefold() for l in lines)


def test_stopwords_removed_from_fts_expression():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    expression = lexical_expression("the inspection of the pump seal", words)
    assert expression is not None
    assert '"the"' not in expression and '"of"' not in expression
    assert '"inspection"' in expression and '"seal"' in expression


def test_only_stopwords_keeps_original_terms():
    words = load_stopwords(("en", "fr"), "builtin@1").words
    expression = lexical_expression("the of and", words)
    assert expression is not None
    assert {'"the"', '"of"', '"and"'} <= set(expression.split(" OR "))


def test_stopword_digest_enters_query_fingerprint(tmp_path):
    builtin = _config(tmp_path)
    none = _config(tmp_path, '[configuration.retrieval.lexical]\nstopwords = "none"\n')
    english = _config(tmp_path, '[configuration.retrieval.lexical]\nlanguages = ["en"]\n')
    assert len({builtin.query_fingerprint, none.query_fingerprint, english.query_fingerprint}) == 3
    assert builtin.query_fingerprint == _config(tmp_path).query_fingerprint
    assert builtin.index_fingerprint == none.index_fingerprint


def test_changing_lists_needs_no_rebuild(tmp_path):
    workspace = tmp_path / "ws"
    with Tychora.open(workspace, token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "pumps.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# Pumps\n\nThe inspection of the pump seal is quarterly.\n",
                "metadata": {"title": "Pumps"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        before = kb.search({"question": "the inspection of the seal"})
    changed = _config(tmp_path, '[configuration.retrieval.lexical]\nstopwords = "none"\n')
    with Tychora.open(workspace, config=changed, token_counter=OutputCounter()) as kb:
        after = kb.search({"question": "the inspection of the seal"})
    assert before.segments and after.segments
    assert before.snapshot_id == after.snapshot_id
```

In `tests/retrieval/test_lexical.py::test_bounded_phrases_stopwords_and_diacritics`, replace `assert lexical_expression("the", frozenset({"the"})) is None` with `assert lexical_expression("the", frozenset({"the"})) == '"the"'`. In `tests/release/test_resources.py`, add to the installed-package code `assert root.joinpath("resources/stopwords/en@1.txt").is_file()` and `assert root.joinpath("resources/stopwords/fr@1.txt").is_file()`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_stopwords.py tests/retrieval/test_lexical.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.configuration.stopwords'`.

- [ ] **Step 3: Ship the lists**

`src/tychora/resources/stopwords/en@1.txt`:

```text
# Tychora English stop words, version 1. Derived from the Snowball English list
# (https://snowballstem.org/, BSD 3-Clause, see LICENSE-snowball.txt); forms with
# apostrophes are left out because the query tokenizer splits on them.
a
about
above
after
again
against
all
am
an
and
any
are
as
at
be
because
been
before
being
below
between
both
but
by
cannot
could
did
do
does
doing
down
during
each
few
for
from
further
had
has
have
having
he
her
here
hers
herself
him
himself
his
how
i
if
in
into
is
it
its
itself
me
more
most
my
myself
no
nor
not
of
off
on
once
only
or
other
ought
our
ours
ourselves
out
over
own
same
she
should
so
some
such
than
that
the
their
theirs
them
themselves
then
there
these
they
this
those
through
to
too
under
until
up
very
was
we
were
what
when
where
which
while
who
whom
why
with
would
you
your
yours
yourself
yourselves
```

`src/tychora/resources/stopwords/fr@1.txt`:

```text
# Tychora French stop words, version 1. Derived from the Snowball French list
# (https://snowballstem.org/, BSD 3-Clause, see LICENSE-snowball.txt); "ai" and
# "eu" are left out because they collide with English acronyms.
aie
aient
aies
ait
as
au
aura
aurai
auraient
aurais
aurait
auras
aurez
auriez
aurions
aurons
auront
aux
avaient
avais
avait
avec
avez
aviez
avions
avons
ayant
ayez
ayons
c
ce
ceci
cela
celà
ces
cet
cette
d
dans
de
des
du
elle
en
es
est
et
eue
eues
eurent
eus
eusse
eussent
eusses
eussiez
eussions
eut
eux
eûmes
eût
eûtes
furent
fus
fusse
fussent
fusses
fussiez
fussions
fut
fûmes
fût
fûtes
ici
il
ils
j
je
l
la
le
les
leur
leurs
lui
m
ma
mais
me
mes
moi
mon
même
n
ne
nos
notre
nous
on
ont
ou
par
pas
pour
qu
que
quel
quelle
quelles
quels
qui
s
sa
sans
se
sera
serai
seraient
serais
serait
seras
serez
seriez
serions
serons
seront
ses
soi
soient
sois
soit
sommes
son
sont
soyez
soyons
suis
sur
t
ta
te
tes
toi
ton
tu
un
une
vos
votre
vous
y
à
étaient
étais
était
étant
étiez
étions
été
étée
étées
étés
êtes
```

`src/tychora/resources/stopwords/LICENSE-snowball.txt`: the Snowball BSD 3-Clause licence as published at https://snowballstem.org/license.html (copyright lines of Dr Martin Porter, Richard Boulton, Yoshiki Shibukawa and Olly Betts, the three conditions and the disclaimer), copied verbatim from that page. The wheel already ships every file under `src/tychora` (`pyproject.toml` `only-include`).

- [ ] **Step 4: Settings, loader and fingerprint**

In `src/tychora/configuration/models.py`, before `RetrievalSettings`:

```python
class LexicalSettings(Contract):
    """Query-side lexical profile: stop-word languages and list version."""

    languages: tuple[Literal["en", "fr"], ...] = ("en", "fr")
    stopwords: Literal["builtin@1", "none"] = "builtin@1"
```

and in `RetrievalSettings`: `lexical: LexicalSettings = Field(default_factory=LexicalSettings)`.

```python
# src/tychora/configuration/stopwords.py
"""Packaged stop-word lists selected by the lexical profile."""

from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from typing import NamedTuple

from tychora.contracts.identity import digest


class StopwordSet(NamedTuple):
    """The words removed from lexical queries and the digest of their lists."""

    words: frozenset[str]
    digest: str


@lru_cache(maxsize=16)
def _list(language: str, version: str) -> tuple[frozenset[str], str]:
    raw = (
        files("tychora")
        .joinpath(f"resources/stopwords/{language}@{version}.txt")
        .read_bytes()
    )
    words = frozenset(
        line.strip().casefold()
        for line in raw.decode("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    return words, sha256(raw).hexdigest()


@lru_cache(maxsize=16)
def load_stopwords(languages: tuple[str, ...], selection: str) -> StopwordSet:
    """The union of the selected lists; ``none`` selects no list."""
    if selection == "none":
        return StopwordSet(frozenset(), digest("stopwords-v1", "none"))
    version = selection.removeprefix("builtin@")
    lists = [(language, *_list(language, version)) for language in sorted(languages)]
    return StopwordSet(
        frozenset().union(*(words for _, words, _ in lists)),
        digest("stopwords-v1", selection, [[language, sha] for language, _, sha in lists]),
    )
```

In `src/tychora/configuration/resolve.py:570`, the query fingerprint becomes:

```python
        "query_fingerprint": digest(
            "query-config-v2",
            registry_sha,
            values,
            load_stopwords(
                tuple(values["retrieval"]["lexical"]["languages"]),
                values["retrieval"]["lexical"]["stopwords"],
            ).digest,
        ),
```

In `src/tychora/configuration/indexed_representation.py`, inside the normalisation loop (after `value["values"]["acquisition"].pop("roots")`), add `value["values"]["retrieval"].pop("lexical")`: the lexical profile only shapes query expressions.

- [ ] **Step 5: Keep the original terms and pass the set through**

Replace `lexical_expression` (`src/tychora/retrieval/lexical_query.py:108-131`) with:

```python
def lexical_expression(text: str, stopwords: frozenset[str]) -> str | None:
    """Return a literal OR query; user text never supplies FTS5 syntax.

    Stop words are dropped from loose terms; a query made only of stop words
    keeps its terms.
    """
    excluded = {word.casefold() for word in stopwords}
    words = [match.group().casefold() for match in _TOKEN.finditer(text)]
    phrases: list[tuple[str, ...]] = []
    used = 0
    for match in _PHRASE.finditer(text):
        terms = tuple(token.casefold() for token in _TOKEN.findall(match.group(1)))
        if terms and len(terms) <= 8 and used + len(terms) <= 32:
            phrases.append(terms)
            used += len(terms)
            if len(phrases) == 4:
                break
    tokens = [token for token in words if token not in excluded]
    if not tokens and not phrases:
        tokens = words
    frequency = Counter(tokens)
    first = {token: tokens.index(token) for token in frequency}
    selected = sorted(
        frequency, key=lambda token: (-frequency[token], -len(token), first[token])
    )[: 32 - used]
    clauses = ['"' + " ".join(terms).replace('"', '""') + '"' for terms in phrases]
    clauses.extend('"' + token.replace('"', '""') + '"' for token in selected)
    return " OR ".join(clauses) or None
```

`SqliteLexicalSearch.search` gains `*, stopwords: frozenset[str] = frozenset()` and calls `lexical_expression(query, stopwords)` (the port declares the same keyword); `retrieve_exact_lexical` gains `*, stopwords: frozenset[str] = frozenset()` and passes it to `tx.lexical.search`. In `QueryService.__init__`, add `settings = owner.config.values.retrieval.lexical` and `self.stopwords = load_stopwords(settings.languages, settings.stopwords).words`; the channel call passes `stopwords=self.stopwords`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_stopwords.py tests/retrieval/test_lexical.py tests/configuration -v`
Expected: PASS. A configuration test that pinned a `query_fingerprint` value is updated to the new value (the formula is now `query-config-v2`).

- [ ] **Step 7: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS. If an evaluation fixture asserts exact lexical scores, stop words changed the expression; the gate in Task 21 judges quality against the baselines, so update the expected values only after checking that the metric did not fall.

- [ ] **Step 8: Commit**

```bash
git add src/tychora/resources/stopwords src/tychora/configuration src/tychora/retrieval src/tychora/ports/search.py src/tychora/adapters/sqlite/lexical.py src/tychora/application/query.py tests
git commit -m "feat: apply versioned stop-word lists to lexical queries"
```

---

### Task 15: Strict mode and coverage semantics (Q6)

Strict stays the default (`retrieval.allow_degraded = false`). A channel is incomplete only when its index is unavailable, a batch the view needs is missing, or the deadline passes (which already raises `DEADLINE_EXCEEDED` through `ctx.checkpoint()`). A bound that cut a scan (the lexical window limit, exact hits beyond 32, a vector over-fetch cap, the graph work budget) is reported in `Coverage.truncated_channels` and leaves `status` at `complete`.

**Files:**
- Modify: `src/tychora/contracts/evidence.py:194-198` (`Coverage.truncated_channels`)
- Create: `src/tychora/retrieval/coverage.py` (`build_coverage`, `missing_batch_slots`)
- Modify: `src/tychora/application/query.py:725-733` (strict refusal with a reason; missing batches), `:1003-1012` (coverage from `build_coverage`; graph budget truncation is `truncated`)
- Test: `tests/retrieval/test_degraded.py` (extend)

**Interfaces:**
- Consumes: Task 8 `ChannelCandidates.truncated`; Task 9 the vector channels' `truncated`; Task 6 `EligibilityView.selected_batches`.
- Produces:
  - `Coverage.truncated_channels: tuple[NonEmptyStr, ...] = ()`.
  - `retrieval/coverage.py`: `build_coverage(*, completed: Iterable[str], unavailable: Iterable[str], truncated: Iterable[str], rerank_failed: bool, graph_exhausted: bool) -> Coverage`; `missing_batch_slots(view: EligibilityView, slots: Iterable[str]) -> tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/retrieval/test_degraded.py`:

```python
from tests.helpers.eligibility import revision_view
from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.adapters.sqlite import lexical as lexical_module
from tychora.retrieval.coverage import build_coverage, missing_batch_slots


def _add_text(kb, key, text):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_truncated_channel_is_not_incomplete_in_strict_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(lexical_module, "WINDOW_LIMIT", 1)
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for index in range(3):
            _add_text(kb, f"{index}.md", f"# Report {index}\n\nThe verification outcome is final.\n")
        result = kb.search({"question": "verification outcome"})
    assert result.coverage.status == "complete"
    assert result.coverage.truncated_channels == ("lexical",)
    assert "lexical" not in result.coverage.unavailable_channels


def test_verification_outcome_succeeds_strict_on_20_documents(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for index in range(20):
            _add_text(
                kb,
                f"{index}.md",
                f"# Audit {index}\n\nRecord the verification outcome for item {index}.\n",
            )
        result = kb.search({"question": "verification outcome"})
    assert result.segments
    assert result.coverage.status == "complete"


def test_missing_required_batch_is_incomplete(tmp_path):
    from tychora.contracts.errors import TychoraError
    from tychora.ports.execution import ExecutionContext

    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_text(kb, "a.md", "# A\n\nThe verification outcome.\n")
        owner = kb._workspace.owner
        owner.run_write_sync(
            lambda: owner.db.execute("DELETE FROM page_batches WHERE slot='lexical'"),
            ExecutionContext(),
        )
        with pytest.raises(TychoraError) as error:
            kb.search({"question": "verification outcome"})
    assert (error.value.code, error.value.reason) == ("INDEX_INCOMPLETE", "batch.missing")


def test_missing_batch_slots_counts_one_batch_per_eligible_revision():
    view = revision_view(
        {"r1": "e1", "r2": "e2"}, batches={"lexical": ("b1", "b2"), "text": ("t1",)}
    )
    assert missing_batch_slots(view, ("lexical", "text")) == ("text",)


def test_graph_budget_truncation_keeps_coverage_complete():
    coverage = build_coverage(
        completed=("lexical",),
        unavailable=(),
        truncated=(),
        rerank_failed=False,
        graph_exhausted=False,
    )
    assert coverage.status == "complete"
    assert coverage.truncated_channels == ("graph",)
    failed = build_coverage(
        completed=("lexical",),
        unavailable=("text",),
        truncated=("lexical",),
        rerank_failed=True,
        graph_exhausted=True,
    )
    assert failed.status == "partial"
    assert failed.unavailable_channels == ("text", "rerank")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_degraded.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.retrieval.coverage'`.

- [ ] **Step 3: Coverage contract and builder**

In `src/tychora/contracts/evidence.py`, `Coverage` gains `truncated_channels: tuple[NonEmptyStr, ...] = ()` after `unavailable_channels`.

```python
# src/tychora/retrieval/coverage.py
"""Coverage of one query: unavailable channels make it partial, bounds do not."""

from collections.abc import Iterable

from tychora.contracts.eligibility import EligibilityView
from tychora.contracts.evidence import Coverage


def build_coverage(
    *,
    completed: Iterable[str],
    unavailable: Iterable[str],
    truncated: Iterable[str],
    rerank_failed: bool,
    graph_exhausted: bool,
) -> Coverage:
    """Partial only when a channel could not run; bounded scans are truncated."""
    missing = (*unavailable, *(("rerank",) if rerank_failed else ()))
    bounded = (*truncated, *(() if graph_exhausted else ("graph",)))
    return Coverage(
        status="partial" if missing else "complete",
        completed_channels=tuple(completed),
        unavailable_channels=tuple(dict.fromkeys(missing)),
        truncated_channels=tuple(dict.fromkeys(bounded)),
    )


def missing_batch_slots(view: EligibilityView, slots: Iterable[str]) -> tuple[str, ...]:
    """Slots whose selected batches do not cover every eligible revision.

    A family serves one batch per extraction, so a slot is complete when it
    selects as many batches as the view grants revisions.
    """
    return tuple(
        slot
        for slot in slots
        if len(view.selected_batches.get(slot, ())) < len(view.revisions)
    )
```

- [ ] **Step 4: Strict refusals carry reasons; bounds become truncation**

In `src/tychora/application/query.py` (`_prepare`):

- Before the channel call, refuse or degrade a missing lexical batch:

```python
        if (plan is None or plan.lexical) and missing_batch_slots(attempt.view, ("lexical",)):
            if not self.owner.config.values.retrieval.allow_degraded:
                raise TychoraError(
                    "INDEX_INCOMPLETE",
                    "A batch of an eligible revision is missing.",
                    reason="batch.missing",
                )
            incomplete_batches = ["lexical"]
        else:
            incomplete_batches = []
```

  and seed `incomplete` with `incomplete_batches`; the text and visual branches append their slot to `incomplete` the same way (`missing_batch_slots(attempt.view, ("text",))` before embedding).
- `:732-733` becomes `raise TychoraError("INDEX_INCOMPLETE", "A retrieval channel is incomplete.", reason="channel.unavailable")`.
- `truncated` (the list Task 9 started) gains `channel.channel for channel in channels if channel.truncated`.
- `:1003-1012`: `coverage = build_coverage(completed=completed, unavailable=incomplete, truncated=truncated, rerank_failed=ranked.failure is not None, graph_exhausted=graph_candidates.exhausted)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_degraded.py tests/retrieval/test_query.py tests/formatting -v`
Expected: PASS. A rendering test that pinned a `coverage` JSON object gains `"truncated_channels": []`.

- [ ] **Step 6: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora/contracts/evidence.py src/tychora/retrieval/coverage.py src/tychora/application/query.py tests
git commit -m "fix: separate truncation from incompleteness in strict mode"
```

---

### Task 16: Editions v2 (C5)

`select_temporal_revisions` orders by `effective_from` even when only `cutoff` is given, warns whenever more than one dated edition passes (`retrieval/selection.py`, moved verbatim by Task 6 from `retrieval/eligibility.py:56-115`), and merges permissive undated editions into the winners. This task rewrites it to spec §4.9 and turns its decisions into `DecisionRequirement`s.

**Files:**
- Modify: `src/tychora/retrieval/selection.py` (rewrite; `RevisionFact.edition_key`; `EditionSelection`, `EditionDecision`)
- Modify: `src/tychora/retrieval/compiler.py` (`_revision_fact` sets `edition_key`; `CompiledEligibility.decisions`; the applicable selector requires `as_of`), `tests/helpers/eligibility_v1_oracle.py` (reads `.selected`)
- Modify: `src/tychora/contracts/requests.py:365` (`revision_selector: Literal["current", "all", "applicable"]`), `src/tychora/contracts/evidence.py:207-212` (`DecisionRequirement.revision_ids`)
- Modify: `src/tychora/application/query.py:1013-1020` (decisions from the compiled selection)
- Modify: `src/tychora/contracts/error_reasons.py` (reason `edition.as_of_required`)
- Test: `tests/retrieval/test_temporal.py` (rewrite against the new result; add five tests)

**Interfaces:**
- Consumes: Task 7 `CompiledEligibility`, `MemberRow.edition_key`.
- Produces:
  - `retrieval/selection.py`: `RevisionFact(..., edition_key: str = "")`; `@dataclass(frozen=True) EditionDecision(reason: Literal["tie", "overlap", "undated", "applicable"], source_id: str, revision_ids: tuple[str, ...])` with `message() -> str`; `@dataclass(frozen=True) EditionSelection(selected: frozenset[str], decisions: tuple[EditionDecision, ...] = (), undated: frozenset[str] = frozenset())` with `warnings -> tuple[str, ...]`; `select_temporal_revisions(revisions, *, explicit_ids=(), selector="current", as_of=None, cutoff=None, unknown_dates="strict") -> EditionSelection`.
  - `CompiledEligibility.decisions: tuple[EditionDecision, ...] = ()`; `EligibilityAttempt.decisions`.
  - `DecisionRequirement.revision_ids: tuple[Identifier, ...] = ()` (additive).
  - Reason `INVALID_REQUEST` `edition.as_of_required`.

Rules, per source: a dated edition is in force at `as_of` when `effective_from <= as_of < effective_until` (an open end never ends) and, with `cutoff`, published on or before it. With `as_of`, the winner has the greatest `(effective_from, published_on, edition_key)`; with only `cutoff`, the greatest `(published_on, edition_key)`; without either, the members marked default win (unchanged). A decision arises only when (a) two or more editions share the winner's whole key (`tie`; all of them are selected) or (b) with `as_of`, another edition in force declares an `effective_until` later than the winner's `effective_from` (`overlap`: an older edition that says it still applies). An edition whose interval is open-ended is superseded by any later start, with no decision. Permissive unknown-date editions are selected beside the winners and reported apart (`undated`). `revision_selector="applicable"` selects every edition in force at `as_of` with one `applicable` decision listing them, and requires `as_of`.

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/retrieval/test_temporal.py` so the helper `_revision(name, *, start=None, end=None, published=None, default=False, edition="1")` passes `edition_key=edition`, every existing call reads `select_temporal_revisions(...).selected` where it indexed `[0]` and `.warnings` where it indexed `[1]`, and add:

```python
def test_unique_winner_yields_no_decision():
    old = _revision("old", start="2020-01-01", edition="1")
    new = _revision("new", start="2022-01-01", edition="2", default=True)
    result = select_temporal_revisions((old, new), as_of=date(2023, 1, 1))
    assert result.selected == {"new"}
    assert result.decisions == ()
    cutoff = select_temporal_revisions(
        (_revision("a", published="2020-01-01"), _revision("b", published="2021-01-01")),
        cutoff=date(2024, 1, 1),
    )
    assert cutoff.selected == {"b"} and cutoff.decisions == ()


def test_effective_versus_publication_ordering():
    early_effect_late_publish = _revision("x", start="2021-01-01", published="2023-06-01", edition="2")
    late_effect_early_publish = _revision("y", start="2022-01-01", published="2022-02-01", edition="1")
    pair = (early_effect_late_publish, late_effect_early_publish)
    assert select_temporal_revisions(pair, as_of=date(2024, 1, 1)).selected == {"y"}
    assert select_temporal_revisions(pair, cutoff=date(2024, 1, 1)).selected == {"x"}


def test_tie_and_overlap_yield_one_decision():
    first = _revision("first", start="2022-01-01", edition="A")
    second = _revision("second", start="2022-01-01", edition="A")
    tie = select_temporal_revisions((first, second), as_of=date(2023, 1, 1))
    assert tie.selected == {"first", "second"}
    assert [(d.reason, d.revision_ids) for d in tie.decisions] == [("tie", ("first", "second"))]
    older = _revision("older", start="2020-01-01", end="2024-01-01", edition="1")
    newer = _revision("newer", start="2022-01-01", edition="2")
    overlap = select_temporal_revisions((older, newer), as_of=date(2023, 1, 1))
    assert overlap.selected == {"newer"}
    assert [(d.reason, d.revision_ids) for d in overlap.decisions] == [("overlap", ("newer", "older"))]
    superseded = _revision("superseded", start="2020-01-01", edition="1")
    quiet = select_temporal_revisions((superseded, newer), as_of=date(2023, 1, 1))
    assert quiet.selected == {"newer"} and quiet.decisions == ()


def test_undated_candidates_are_separate():
    known = _revision("known", start="2022-01-01")
    unknown = _revision("unknown", edition="2")
    result = select_temporal_revisions(
        (known, unknown), as_of=date(2023, 1, 1), unknown_dates="permissive"
    )
    assert result.selected == {"known", "unknown"}
    assert result.undated == {"unknown"}
    assert [(d.reason, d.revision_ids) for d in result.decisions] == [("undated", ("unknown",))]
    strict = select_temporal_revisions((known, unknown), as_of=date(2023, 1, 1))
    assert strict.selected == {"known"} and strict.undated == frozenset()


def test_applicable_selector_lists_editions_in_force():
    older = _revision("older", start="2020-01-01", edition="1")
    newer = _revision("newer", start="2022-01-01", edition="2")
    expired = _revision("expired", start="2019-01-01", end="2021-01-01", edition="0")
    result = select_temporal_revisions(
        (older, newer, expired), selector="applicable", as_of=date(2023, 1, 1)
    )
    assert result.selected == {"older", "newer"}
    assert [(d.reason, d.revision_ids) for d in result.decisions] == [
        ("applicable", ("newer", "older"))
    ]
```

and:

```python
def test_applicable_selector_requires_as_of():
    from tychora.contracts.errors import TychoraError

    with pytest.raises(TychoraError) as error:
        select_temporal_revisions((_revision("a", start="2020-01-01"),), selector="applicable")
    assert error.value.reason == "edition.as_of_required"
```

(add `import pytest` at the top of the module.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_temporal.py -v`
Expected: FAIL with `AttributeError: 'tuple' object has no attribute 'selected'`.

- [ ] **Step 3: Rewrite edition selection**

Replace the body of `src/tychora/retrieval/selection.py` after `RevisionFact` (which gains `edition_key: str = ""` as its last field) with:

```python
@dataclass(frozen=True)
class EditionDecision:
    """An edition choice the caller must make: a tie, a declared overlap, undated or applicable editions."""

    reason: Literal["tie", "overlap", "undated", "applicable"]
    source_id: str
    revision_ids: tuple[str, ...]

    def message(self) -> str:
        """The decision as the warning text a result carries."""
        texts = {
            "tie": "Retained editions tie for source",
            "overlap": "Overlapping retained editions for source",
            "undated": "Undated retained editions may apply for source",
            "applicable": "Several retained editions apply for source",
        }
        return f"{texts[self.reason]} {self.source_id}"


@dataclass(frozen=True)
class EditionSelection:
    """The selected revisions and the decisions their selection leaves open."""

    selected: frozenset[str]
    decisions: tuple[EditionDecision, ...] = ()
    undated: frozenset[str] = frozenset()

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(decision.message() for decision in self.decisions)


def _parsed_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _in_force(
    edition: RevisionFact, as_of: date | None, cutoff: date | None
) -> bool | None:
    """Whether a dated edition passes both constraints; ``None`` when a needed date is unknown."""
    start, end = _parsed_date(edition.effective_from), _parsed_date(edition.effective_until)
    published = _parsed_date(edition.published_on)
    if as_of is not None and end is not None and as_of >= end:
        return False
    if cutoff is not None and published is not None and published > cutoff:
        return False
    if (as_of is not None and start is None) or (cutoff is not None and published is None):
        return None
    return not (as_of is not None and start is not None and as_of < start)


def _key(edition: RevisionFact, as_of: date | None) -> tuple[str, str, str]:
    published = edition.published_on or ""
    if as_of is not None:
        return (edition.effective_from or "", published, edition.edition_key)
    return (published, edition.edition_key, "")


def _winners(
    source: str, known: list[RevisionFact], as_of: date | None
) -> tuple[list[RevisionFact], list[EditionDecision]]:
    best = max(_key(edition, as_of) for edition in known)
    winners = [edition for edition in known if _key(edition, as_of) == best]
    decisions: list[EditionDecision] = []
    if len(winners) > 1:
        ids = tuple(sorted(edition.revision_id for edition in winners))
        decisions.append(EditionDecision("tie", source, ids))
    elif as_of is not None:
        start = winners[0].effective_from or ""
        overlapping = sorted(
            edition.revision_id
            for edition in known
            if edition is not winners[0]
            and edition.effective_until is not None
            and edition.effective_until > start
        )
        if overlapping:
            decisions.append(
                EditionDecision(
                    "overlap", source, tuple(sorted((winners[0].revision_id, *overlapping)))
                )
            )
    return winners, decisions


def select_temporal_revisions(
    revisions: tuple[RevisionFact, ...],
    *,
    explicit_ids: tuple[str, ...] = (),
    selector: Literal["current", "all", "applicable"] = "current",
    as_of: date | None = None,
    cutoff: date | None = None,
    unknown_dates: Literal["strict", "permissive"] = "strict",
) -> EditionSelection:
    """Select editions per source after applying both date constraints."""
    if selector == "applicable" and as_of is None:
        raise TychoraError(
            "INVALID_REQUEST",
            "The applicable selector needs an as_of date",
            reason="edition.as_of_required",
        )
    explicit = set(explicit_ids)
    grouped: dict[str, list[RevisionFact]] = defaultdict(list)
    for revision in revisions:
        if not explicit or revision.revision_id in explicit:
            grouped[revision.source_id].append(revision)
    selected: set[str] = set()
    undated: set[str] = set()
    decisions: list[EditionDecision] = []
    for source, editions in sorted(grouped.items()):
        states = [(edition, _in_force(edition, as_of, cutoff)) for edition in editions]
        known = [edition for edition, state in states if state]
        unknown = [edition for edition, state in states if state is None]
        if unknown_dates == "permissive" and unknown:
            ids = tuple(sorted(edition.revision_id for edition in unknown))
            undated.update(ids)
            decisions.append(EditionDecision("undated", source, ids))
        if explicit or selector == "all":
            selected.update(edition.revision_id for edition in known)
        elif selector == "applicable":
            ids = tuple(sorted(edition.revision_id for edition in known))
            selected.update(ids)
            if len(ids) > 1:
                decisions.append(EditionDecision("applicable", source, ids))
        elif as_of is not None or cutoff is not None:
            if known:
                winners, found = _winners(source, known, as_of)
                selected.update(edition.revision_id for edition in winners)
                decisions.extend(found)
        else:
            selected.update(edition.revision_id for edition in known if edition.is_default)
    return EditionSelection(
        frozenset(selected | undated), tuple(decisions), frozenset(undated)
    )
```

(imports: `from collections import defaultdict`; `from tychora.contracts.errors import TychoraError`; the old `_parsed_date` is replaced by the one above.) Register `edition.as_of_required` (`INVALID_REQUEST`, "Give an as_of date with the applicable revision selector.") in `error_reasons.py` and `CLASSIFIED_REASONS`.

- [ ] **Step 4: Thread decisions to the result**

- `src/tychora/contracts/requests.py:365`: `revision_selector: Literal["current", "all", "applicable"] = "current"`.
- `src/tychora/contracts/evidence.py`: `DecisionRequirement` gains `revision_ids: tuple[Identifier, ...] = ()`.
- `src/tychora/retrieval/compiler.py`: `_revision_fact` passes `edition_key=row.edition_key`; `_compile` reads `edition = select_temporal_revisions(...)`, uses `edition.selected` where it used `selected_ids` and returns `CompiledEligibility(view, edition.warnings, roles, envelopes, edition.decisions)`; `CompiledEligibility` gains `decisions: tuple[EditionDecision, ...] = ()` (last field). `retrieval/attempt.py`: `EligibilityAttempt.decisions` carried from the compiled entry.
- `tests/helpers/eligibility_v1_oracle.py`: `revision_ids = select_temporal_revisions(...).selected` (the oracle checks unit admission; edition policy is pinned by this task's tests).
- `src/tychora/application/query.py:1013-1020`:

```python
        decisions = tuple(
            DecisionRequirement(
                requirement_id=digest(
                    "edition-decision-v1",
                    decision.reason,
                    decision.source_id,
                    list(decision.revision_ids),
                ),
                reason=decision.message(),
                revision_ids=decision.revision_ids,
            )
            for decision in attempt.decisions
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_temporal.py tests/retrieval/test_eligibility_v2.py tests/evaluation/test_temporal_scenarios.py -v`
Expected: PASS. A scenario test that expected the old cutoff-only warning now expects none; that is the "spurious cutoff decision" defect this task fixes.

- [ ] **Step 6: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tychora tests
git commit -m "fix: select editions by the applicable date and report real ambiguities"
```

---

### Task 17: Per-request narrowing and `QueryBindings`

`QueryRequest` gains `channels`, `rerank` and `review_states`, which can only narrow what the profile configures (§4.14 "Per-query channels and rerank", "Review gating per request"); they replace the internal `QueryExecutionPlan` (`application/query.py:110-129`), which only evaluation ablations could set. A channel the caller turns off is reported as skipped, never as incomplete. The six optional query collaborators become one `QueryBindings` value whose provider identities are checked once when services are built.

**Files:**
- Modify: `src/tychora/contracts/requests.py:352-380` (`ChannelSelection`, `channels`, `rerank`, `review_states`), `src/tychora/contracts/evidence.py:194-199` (`Coverage.skipped_channels`)
- Modify: `src/tychora/retrieval/compiler.py` (`request_digest` and `ModePolicy` include `review_states` and `channels`)
- Create: `src/tychora/application/query_plan.py` (`QueryBindings`, `QueryPlan`, `plan_for`)
- Modify: `src/tychora/application/query.py:110-129` (delete `QueryExecutionPlan`), `:212-246` (`QueryService(owner, *, output_counter, bindings, ...)`), `:296-330` (ablation checks become `plan_for`), every `plan is None or plan.X` test in `_prepare`
- Modify: `src/tychora/application/services.py:428-440`, `src/tychora/evaluation/runner.py:63-140`, `:437-442`, `:479` (ablations become request fields)
- Modify: `src/tychora/interfaces/mcp/tools.py` (`search` accepts `channels`, `rerank`, `review_states`), `src/tychora/interfaces/http/routes.py:560-576` (the request model already parses `QueryRequest`; no change beyond the schema)
- Modify: `src/tychora/contracts/error_reasons.py` (reason `channel.not_configured`)
- Test: `tests/retrieval/test_query_plan.py` (new); `tests/evaluation/test_runner.py`, `tests/api/test_scope.py`, `tests/api/test_configured_runtime.py`, `tests/acceptance/test_combined_channels.py` (ablations through the request), every `QueryService(...)` construction in tests (`grep -rn "QueryService(" tests`)

**Interfaces:**
- Consumes: Task 7 `ModePolicy`, `request_digest`; Task 15 `build_coverage`.
- Produces:
  - `contracts/requests.py`: `class ChannelSelection(Contract): lexical: bool = True; text: bool = True; image: bool = True; graph: bool = True; graph_strategies: frozenset[Literal["structural", "semantic"]] | None = None`; `QueryRequest.channels: ChannelSelection | None = None`, `rerank: Literal["auto", "on", "off"] = "auto"`, `review_states: tuple[Literal["unreviewed", "needs_review", "accepted", "rejected"], ...] | None = None`. A field of `channels` the caller sets to `True` must be configured; a field left at its default follows the configuration. (`graph_strategies` is not in the index: evaluation ablates the structural and semantic graph strategies separately, which a single `graph` flag cannot express.)
  - `Coverage.skipped_channels: tuple[NonEmptyStr, ...] = ()`.
  - `application/query_plan.py`: `@dataclass(frozen=True) QueryBindings(text_provider=None, text_index=None, visual_provider=None, visual_index=None, reranker=None, rerank_counter=None, rerank_unavailable=None, graph=None)` with `validate(config, caches) -> None`; `@dataclass(frozen=True) QueryPlan(lexical, text, image, graph_strategies: frozenset[str], rerank, skipped: tuple[str, ...])`; `plan_for(request, config, bindings) -> QueryPlan`.
  - Reason `INVALID_REQUEST` `channel.not_configured`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/retrieval/test_query_plan.py
"""A request narrows the configured channels, reranking and review states."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import owner_row
from tychora import Tychora
from tychora.application.query_plan import QueryBindings, plan_for
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest
from tychora.retrieval.compiler import request_digest


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


def test_channels_can_only_narrow_configuration(config):
    request = QueryRequest.from_mapping({"question": "seal", "channels": {"text": True}})
    with pytest.raises(TychoraError) as error:
        plan_for(request, config, QueryBindings())
    assert (error.value.code, error.value.reason) == ("INVALID_REQUEST", "channel.not_configured")
    plan = plan_for(QueryRequest.from_mapping({"question": "seal"}), config, QueryBindings())
    assert plan.lexical and not plan.text and not plan.image


def test_a_channel_turned_off_is_skipped(config):
    request = QueryRequest.from_mapping({"question": "seal", "channels": {"lexical": False}})
    plan = plan_for(request, config, QueryBindings())
    assert not plan.lexical
    assert plan.skipped == ("lexical",)


def test_rerank_off_skips_rerank_stage(config):
    off = plan_for(QueryRequest.from_mapping({"question": "seal", "rerank": "off"}), config, QueryBindings())
    assert not off.rerank
    with pytest.raises(TychoraError) as error:
        plan_for(QueryRequest.from_mapping({"question": "seal", "rerank": "on"}), config, QueryBindings())
    assert error.value.reason == "channel.not_configured"


def test_review_states_change_request_digest():
    plain = QueryRequest.from_mapping({"question": "seal"})
    accepted = QueryRequest.from_mapping({"question": "seal", "review_states": ["accepted"]})
    other_question = QueryRequest.from_mapping({"question": "valve"})
    assert request_digest(plain, None) != request_digest(accepted, None)
    assert request_digest(plain, None) == request_digest(other_question, None)


def _add(kb, key, text, approval):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key, "approval": approval},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_review_states_narrow_mode_states(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "accepted.md", "# A\n\nSeal inspection accepted.\n", "accepted")
        _add(kb, "draft.md", "# B\n\nSeal inspection draft.\n", "unreviewed")
        everything = kb.search({"question": "seal inspection"})
        narrowed = kb.search({"question": "seal inspection", "review_states": ["accepted"]})
    assert {s.source_id for s in everything.segments} > {s.source_id for s in narrowed.segments}
    assert len({s.source_id for s in narrowed.segments}) == 1


def test_generation_policy_never_generate_excluded_from_compose(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "kept.md", "# A\n\nSeal inspection kept.\n", "accepted")
        _add(kb, "reference.md", "# B\n\nSeal inspection reference.\n", "accepted")
        owner = kb._workspace.owner
        (source_id,) = owner_row(
            owner, "SELECT source_id FROM sources WHERE source_key='reference.md'"
        )
        kb.sources.set_allowed_uses(source_id, "never_generate")
        composed = kb.search({"question": "seal inspection", "mode": "compose"})
        looked_up = kb.search({"question": "seal inspection", "mode": "lookup"})
    assert source_id not in {s.source_id for s in composed.segments}
    assert source_id in {s.source_id for s in looked_up.segments}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/retrieval/test_query_plan.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tychora.application.query_plan'`.

- [ ] **Step 3: Request fields and digests**

In `src/tychora/contracts/requests.py`, before `QueryRequest`:

```python
class ChannelSelection(Contract):
    """Channels a query may use; it narrows the profile and never widens it."""

    lexical: bool = True
    text: bool = True
    image: bool = True
    graph: bool = True
    graph_strategies: frozenset[Literal["structural", "semantic"]] | None = None
```

and in `QueryRequest`, after `revision_selector`:

```python
    channels: ChannelSelection | None = None
    rerank: Literal["auto", "on", "off"] = "auto"
    review_states: (
        tuple[Literal["unreviewed", "needs_review", "accepted", "rejected"], ...] | None
    ) = None
```

In `src/tychora/contracts/evidence.py`, `Coverage` gains `skipped_channels: tuple[NonEmptyStr, ...] = ()`.

In `src/tychora/retrieval/compiler.py`, `request_digest` adds `list(request.review_states) if request.review_states is not None else None` and `request.channels.model_dump(mode="json") if request.channels is not None else None` to its digest inputs (spec §4.7: "filters, temporal selector, review states, channels"); `EligibilityCompiler.mode` keys its cache by `(config.config_sha, name, scope.uses, request.review_states)` and narrows `original_reviews` with `frozenset(definition["original_review_states"]) & frozenset(request.review_states)` when they are given.

- [ ] **Step 4: Plans and bindings**

```python
# src/tychora/application/query_plan.py
"""What one query runs: the configured channels narrowed by its request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tychora.configuration.models import ResolvedConfig
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import ChannelSelection, QueryRequest

if TYPE_CHECKING:
    from tychora.retrieval.cache import QueryCaches


def _not_configured(name: str) -> TychoraError:
    return TychoraError(
        "INVALID_REQUEST",
        f"The {name} channel is not configured; a request can only narrow channels",
        reason="channel.not_configured",
    )


@dataclass(frozen=True)
class QueryBindings:
    """The optional query collaborators a workspace is built with."""

    text_provider: Any = None
    text_index: Any = None
    visual_provider: Any = None
    visual_index: Any = None
    reranker: Any = None
    rerank_counter: Any = None
    rerank_unavailable: TychoraError | None = None
    graph: Any = None

    def validate(self, config: ResolvedConfig, caches: QueryCaches) -> None:
        """Refuse providers whose identity differs from the configured one."""
        for provider, settings, modality in (
            (self.text_provider, config.values.providers.text, "text"),
            (self.visual_provider, config.values.providers.image, "visual"),
        ):
            if provider is None:
                continue
            caps = provider.capabilities
            if (
                provider.caches is not caches
                or caps.provider != settings.provider
                or caps.model != settings.model
                or caps.dimensions != settings.dimensions
                or caps.modality != modality
            ):
                raise TychoraError(
                    "INTEGRITY_ERROR",
                    "Query provider binding differs from configuration.",
                    reason="provider.binding_mismatch",
                )


@dataclass(frozen=True)
class QueryPlan:
    """The channels, graph strategies and rerank stage one query runs."""

    lexical: bool
    text: bool
    image: bool
    graph_strategies: frozenset[str]
    rerank: bool
    skipped: tuple[str, ...]


def _configured(config: ResolvedConfig, bindings: QueryBindings) -> dict[str, Any]:
    values = config.values
    graph = values.retrieval.graph
    strategies = frozenset(
        name
        for name, enabled in (
            ("structural", graph.structural == "code_context@1"),
            ("semantic", graph.semantic),
        )
        if enabled and bindings.graph is not None
    )
    return {
        "lexical": True,
        "text": values.index.text.backend != "disabled" and bindings.text_index is not None,
        "image": values.index.image.backend != "disabled" and bindings.visual_index is not None,
        "graph": strategies,
        "rerank": values.providers.rerank.provider != "disabled" and bindings.reranker is not None,
    }


def plan_for(
    request: QueryRequest, config: ResolvedConfig, bindings: QueryBindings
) -> QueryPlan:
    """Narrow the configured plan by the request; enabling anything unconfigured is refused."""
    configured = _configured(config, bindings)
    selection = request.channels or ChannelSelection()
    chosen = {}
    for name in ("lexical", "text", "image"):
        asked = getattr(selection, name)
        if name in selection.model_fields_set and asked and not configured[name]:
            raise _not_configured(name)
        chosen[name] = asked and configured[name]
    strategies = configured["graph"] if selection.graph else frozenset()
    if selection.graph_strategies is not None:
        if not selection.graph_strategies <= configured["graph"]:
            raise _not_configured("graph")
        strategies = selection.graph_strategies if selection.graph else frozenset()
    if request.rerank == "on" and not configured["rerank"]:
        raise _not_configured("rerank")
    skipped = tuple(
        name
        for name in ("lexical", "text", "image", "graph")
        if name in selection.model_fields_set and not getattr(selection, name)
    )
    return QueryPlan(
        lexical=chosen["lexical"],
        text=chosen["text"],
        image=chosen["image"],
        graph_strategies=strategies,
        rerank=configured["rerank"] and request.rerank != "off",
        skipped=skipped + (("rerank",) if request.rerank == "off" else ()),
    )
```

Register `channel.not_configured` (`INVALID_REQUEST`, "Remove the channel from the request or configure it in the profile.") in `error_reasons.py` and `CLASSIFIED_REASONS`.

- [ ] **Step 5: The query service runs the plan**

In `src/tychora/application/query.py`:
- Delete `QueryExecutionPlan`. `QueryService.__init__(self, owner, *, output_counter, bindings: QueryBindings = QueryBindings(), auto_rerank=False, caches=None, history=None, profiles=None)` stores `self.bindings` and keeps `self.text_provider = bindings.text_provider` and the other attribute names its methods read; it calls `bindings.validate(owner.config, self.caches)`.
- `_validate_provider_families` loses the ablation checks (`:304-332`) and the per-provider capability comparison that `QueryBindings.validate` now does once; it keeps the per-snapshot family comparison.
- In `search`, compute `plan = plan_for(request, self.owner.config, self.bindings)` once and pass it to `_prepare`; every `plan is None or plan.lexical` / `plan.text` / `plan.image` / `plan.graph_strategies` / `plan.rerank` test reads `plan.lexical` and so on; `plan.rerank` false keeps the exact-only branch (`RankedEvidenceItem(..., "disabled")`); the coverage call passes `skipped=plan.skipped` (add `skipped: Iterable[str] = ()` to `build_coverage`, stored as `Coverage.skipped_channels`).
- `_provider_fingerprint` replaces `self.execution_plan.__dict__ if self.execution_plan else None` with nothing (the request, which carries the plan's inputs, is already in the result cache key).

In `src/tychora/application/services.py:428-440`, build `QueryService(owner, output_counter=counter, bindings=QueryBindings(text_provider=providers.text_query, text_index=providers.text_index, visual_provider=providers.visual_query, visual_index=providers.visual_index, reranker=providers.reranker, rerank_counter=providers.rerank_counter, graph=providers.graph), caches=caches, auto_rerank=bool(owned_runtimes))`.

- [ ] **Step 6: Evaluation ablations through the request**

In `src/tychora/evaluation/runner.py`, `execution_plan(variant)` becomes `variant_channels(variant) -> tuple[ChannelSelection | None, Literal["auto", "on", "off"]]`:

```python
def variant_channels(variant: ConfigurationVariant):
    """Explicit ablation controls as request fields."""
    controls = dict(variant.overrides)
    if "channels" not in controls:
        if "rerank" in controls:
            raise ValueError("Rerank override requires explicit channels")
        return None, "auto"
    channels = controls["channels"]
    if (
        not isinstance(channels, tuple)
        or not channels
        or any(not isinstance(channel, str) for channel in channels)
        or len(set(channels)) != len(channels)
        or set(channels) - _CHANNELS
    ):
        raise ValueError("Unsupported or duplicate evaluation channels")
    rerank = controls.get("rerank", False)
    if type(rerank) is not bool:
        raise ValueError("Rerank override must be boolean")
    strategies = frozenset(
        name
        for name, channel in (("structural", "structural_graph"), ("semantic", "semantic_graph"))
        if channel in channels
    )
    return (
        ChannelSelection(
            lexical="lexical" in channels,
            text="text" in channels,
            image="image" in channels,
            graph=bool(strategies),
            graph_strategies=strategies or None,
        ),
        "on" if rerank else "off",
    )
```

`variant_query` uses it (`plan.text` → `selection.text`, `plan.graph_strategies` → `selection.graph_strategies`, `plan.rerank` → `rerank == "on"`) and adds `"channels"` and `"rerank"` to the returned request mapping; the runner (`:437-442`) no longer copies the query service, and the result entry records `"channels": query.channels.model_dump(mode="json") if query.channels else None` in place of `"execution_plan"`. Because each field the variant sets is explicit, a variant that names an unconfigured channel now fails with `channel.not_configured` instead of `CAPABILITY_MISSING`; update `tests/evaluation/test_runner.py` accordingly.

- [ ] **Step 7: Transports accept the fields**

`src/tychora/interfaces/mcp/tools.py`: the `search` tool's parameters gain `channels: dict | None = None`, `rerank: str = "auto"`, `review_states: list[str] | None = None`, forwarded into the `QueryRequest.from_mapping` call (omit keys left at their defaults). HTTP `POST /v1/queries` already parses a `QueryRequest`, so its OpenAPI schema gains the fields with no code change; run `uv run --locked pytest tests/transports -q` to refresh any recorded schema fixture the suite compares.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/retrieval/test_query_plan.py tests/retrieval/test_query.py tests/evaluation/test_runner.py tests/api tests/transports -v`
Expected: PASS.

- [ ] **Step 9: Full default suite**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add -A src/tychora tests
git commit -m "feat: let a query narrow channels, reranking and review states"
```

---

### Task 18: Trimmed code records keep their definition key

When the response budget trims an optional record, the stub keeps nothing but `{"omitted_budget": true}` (`retrieval/response.py:375-385`). For code results this drops the `definition_key` that tells a caller which symbol a segment cites; the code gold set measured 143 of 147 `code_symbol` records trimmed and `definition_recall` near zero (`tests/fixtures/evaluation/code-gold/README.md`, "definition_recall"). The index gives phase 4 this fix. The stub of a `code_symbol` record now keeps its three short identity fields; any other record's stub is unchanged.

**Files:**
- Modify: `src/tychora/retrieval/response.py:375-385` (`budget_stub`)
- Modify: `tests/fixtures/evaluation/code-gold/README.md` ("definition_recall": the stub keeps the key; the measured values after the fix go into Task 21)
- Test: `tests/formatting/test_budget_stub.py` (new; default suite), `tests/formatting/test_response_budget.py:140-154` (asset-gated; unchanged assertion for a non-code record)

**Interfaces:**
- Consumes: `StructuredRecord` (`contracts/evidence.py:134`).
- Produces: `retrieval/response.py`: `STUB_IDENTITY_FIELDS = ("definition_key", "qualified_name", "relative_path")`, `budget_stub(record: StructuredRecord) -> StructuredRecord`.

- [ ] **Step 1: Write the failing test**

```python
# tests/formatting/test_budget_stub.py
"""A trimmed code record still names the symbol it stood for."""

from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.evidence import StructuredRecord
from tychora.retrieval.response import budget_stub


def _record(record_type, value):
    return StructuredRecord(
        record_id="record-1",
        unit_id="unit-1",
        record_type=record_type,
        schema_id="code-symbol@1" if record_type == "code_symbol" else "feedback@1",
        value=FrozenJsonMap(value),
        dependencies=frozenset({"unit-1"}),
    )


def test_code_symbol_stub_keeps_its_identity():
    record = _record(
        "code_symbol",
        {
            "definition_key": "python:src/a.py:Box.run",
            "qualified_name": "Box.run",
            "relative_path": "src/a.py",
            "context_spans": [{"reason": "parent_declaration"}] * 50,
        },
    )
    stub = budget_stub(record)
    assert stub.value == FrozenJsonMap(
        {
            "omitted_budget": True,
            "definition_key": "python:src/a.py:Box.run",
            "qualified_name": "Box.run",
            "relative_path": "src/a.py",
        }
    )
    assert stub.record_id == record.record_id


def test_other_record_stub_is_unchanged():
    stub = budget_stub(_record("feedback", {"literal_quote": "x" * 5000}))
    assert stub.value == FrozenJsonMap({"omitted_budget": True})


def test_oversized_identity_values_are_not_kept():
    stub = budget_stub(_record("code_symbol", {"definition_key": "k" * 600}))
    assert stub.value == FrozenJsonMap({"omitted_budget": True})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --locked pytest tests/formatting/test_budget_stub.py -v`
Expected: FAIL with `ImportError: cannot import name 'budget_stub'`.

- [ ] **Step 3: Build stubs in one place**

In `src/tychora/retrieval/response.py`, at module level:

```python
STUB_IDENTITY_FIELDS = ("definition_key", "qualified_name", "relative_path")
_STUB_FIELD_CHARS = 512


def budget_stub(record: StructuredRecord) -> StructuredRecord:
    """The record trimmed by the output budget; a code symbol keeps what names it."""
    value: dict[str, object] = {"omitted_budget": True}
    if record.record_type == "code_symbol":
        for name in STUB_IDENTITY_FIELDS:
            field = record.value.get(name)
            if isinstance(field, str) and len(field) <= _STUB_FIELD_CHARS:
                value[name] = field
    return record.model_copy(update={"value": FrozenJsonMap(value)})
```

and in the trimming loop (`:375-377`) replace `stub = record.model_copy(update={"value": FrozenJsonMap({"omitted_budget": True})})` with `stub = budget_stub(record)`. The two readers that skip stubs (`:239`, `:269`) test `"omitted_budget" not in record.value` and keep working.

In `tests/fixtures/evaluation/code-gold/README.md`, replace the paragraph "A fix changes either the stub shape…" with: "Trimmed `code_symbol` records keep `definition_key`, `qualified_name` and `relative_path` in their stub (`retrieval/response.py`, `budget_stub`), so the metric sees every definition a result names; the values measured after this change are in the table above."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/formatting -v`
Expected: PASS (the asset-gated `test_response_budget.py` cases are skipped without `TYCHORA_TEST_MODEL_CACHE`; with it, `test_long_optional_record_becomes_stub_without_changing_source_quote` still expects `{"omitted_budget": True}` because its record is not a code symbol).

- [ ] **Step 5: Commit**

```bash
git add src/tychora/retrieval/response.py tests/formatting/test_budget_stub.py tests/fixtures/evaluation/code-gold/README.md
git commit -m "fix: keep the definition key in trimmed code records"
```

---

### Task 19: Query stages, configuration dumps and SQL-free query modules (D7, ratchet)

`QueryService._prepare` is one function from `:681` to `:1034` (350 lines) and the query path still serializes configuration on every query: `_validate_provider_families` rebuilds the retained registry document (`:334-342`), `_provider_fingerprint` dumps the provider and index settings (`:269-294`), `_projection_fingerprint` dumps the output profiles (`:250-264`), and `assemble_segments` dumps the whole configuration to read a mode's required lanes (`retrieval/segments.py:899`). This task splits `_prepare` into typed stages under 80 lines, computes those digests once per configuration, and moves the SQL left in the query modules into repositories, so `application/query.py`, `application/reranking.py`, `application/visual.py`, `application/inspection.py` and `retrieval/visuals.py` join `SQL_FREE_MODULES` (Task 20 adds `owner`, `removal` and `restore`).

**Files:**
- Modify: `src/tychora/application/query.py` (stages; cached fingerprints; `_registered_image_query` through `AssetRepository`)
- Modify: `src/tychora/retrieval/segments.py:893-903` (required lanes from the registry, not a dump)
- Modify: `src/tychora/application/reranking.py:530-584` (extraction lookups through `tx.representations.units` and `tx.eligibility.owner_facts`)
- Modify: `src/tychora/application/visual.py:86-190` (collections and asset rows through repositories)
- Modify: `src/tychora/application/inspection.py` (every statement through `InspectionRepository`, `ArtifactRepository.require`)
- Create: `src/tychora/adapters/sqlite/repositories/assets.py`, `src/tychora/adapters/sqlite/repositories/inspection.py`
- Modify: `src/tychora/ports/repositories.py` (`AssetRepository`, `InspectionRepository`, `ArtifactRepository.require`, `WorkspaceRepository.collection`), `repositories/unit_of_work.py`, `repositories/artifacts.py`, `repositories/workspace.py`
- Test: `tests/architecture/test_sql_free_modules.py:7-16` (five modules appended), `tests/retrieval/test_query_stages.py` (new)

**Interfaces:**
- Consumes: every earlier task's query path.
- Produces:
  - `ArtifactRepository.require(sha256: str) -> ArtifactRef` (the index's name, deferred by phase 1 to its first caller: `INTEGRITY_ERROR` `payload.missing` when absent or not present).
  - `WorkspaceRepository.collection(collection_id: str) -> tuple[str, int] | None` (workspace ID and access floor).
  - `AssetRepository` (`ReadTransaction.assets`): `registered(asset_id) -> RegisteredAsset | None` with `RegisteredAsset(asset: AssetRow, revision_id: str, ref: ArtifactRef, payload_state: str)`; `links(asset_id, roles: tuple[str, ...], limit: int) -> tuple[AssetLink, ...]` with `AssetLink(unit_id, role, extraction_id, text_origin, citable: bool)`.
  - `InspectionRepository` (`ReadTransaction.inspection`), one method per statement listed in Step 5.
  - `application/query.py` stage methods: `_channels(request, scope, attempt, plan, ctx) -> _Channels`, `_fuse(...)`, `_admit(...)`, `_rerank(...)`, `_assemble(...)`, `_coverage(...)`; `_Channels`, `_Fused`, `_Ranked` dataclasses carry what the next stage reads.

- [ ] **Step 1: Write the failing tests**

In `tests/architecture/test_sql_free_modules.py`, append to `SQL_FREE_MODULES`:

```python
    "application/query.py",
    "application/reranking.py",
    "application/visual.py",
    "application/inspection.py",
    "retrieval/visuals.py",
```

```python
# tests/retrieval/test_query_stages.py
"""Queries run as typed stages and never serialize the configuration."""

import ast
from pathlib import Path

from tests.helpers.legacy import OutputCounter
from tychora import Tychora
from tychora.configuration.models import ResolvedConfig

SOURCE = Path(__file__).resolve().parents[2] / "src" / "tychora"


def test_query_never_dumps_configuration(tmp_path, monkeypatch):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": "a.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": "# A\n\nThe verification outcome.\n",
                "metadata": {"title": "A"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"
        kb.search({"question": "warm the caches"})
        dumps = []
        original = ResolvedConfig.model_dump

        def counted(self, *args, **kwargs):
            dumps.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(ResolvedConfig, "model_dump", counted)
        kb.search({"question": "verification outcome"})
    assert dumps == []


def test_query_functions_stay_under_80_lines():
    tree = ast.parse((SOURCE / "application/query.py").read_text())
    long = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.end_lineno - node.lineno + 1 > 80
    ]
    assert long == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/architecture/test_sql_free_modules.py tests/retrieval/test_query_stages.py -v`
Expected: FAIL — the SQL-free test lists the remaining `.execute(` calls of the five modules; `test_query_never_dumps_configuration` counts the dumps of `_validate_provider_families`, `_provider_fingerprint` and `assemble_segments`; `test_query_functions_stay_under_80_lines` names `_prepare` and `_validate_provider_families`.

- [ ] **Step 3: Digests once per configuration**

In `src/tychora/application/query.py`:
- `_validate_provider_families`: `current_artifact_sha = sha256(canonical_bytes({...}))` (`:334-342`) becomes `current_artifact_sha = self.owner.config.retained_sha`; the retained-identity comparison (`:432-443`) compares `settings` and `retained_settings` as contracts (`settings.model_copy(update={"credential_ref": None}) == retained_settings.model_copy(update={"credential_ref": None})`) instead of their JSON dumps, and the family check's `retained_settings.model_dump(mode="json")` digest is computed once per `(origin_sha, channel)` in a `dict` on the service. Split the function into `_check_family(channel, provider, index, settings, modality, snapshot, retained_configs)` (the loop body) and the loop, so both stay under 80 lines. Because the snapshot is immutable and the providers are fixed for the service's life, record the snapshot IDs that passed in a `set` on the service and skip the check for them.
- `_provider_fingerprint(config=None)` and `_projection_fingerprint(config=None)` (`:257-296`) memoize their digest in a `dict` on the service keyed by the inputs that can vary: `(config.config_sha,)` for the provider digest (providers are fixed for a service's life; `settings.py` builds a new service on reconfiguration) and `(config.config_sha, self.profiles, self.delivery_serialization)` for the projection digest, because the MCP tool copies the service and replaces its profiles and serialization (`interfaces/mcp/tools.py:156-164`; a shallow copy shares the memo, which is keyed by those inputs). `replay` (`:524-540`) passes a retained configuration and is served by the same memo under that configuration's `config_sha`.
- `src/tychora/retrieval/segments.py:893-903`: `config.model_dump(mode="json")["registry"]["modes"][mode].get("required_lanes", ())` becomes `cast(Mapping[str, Any], config.registry["modes"][mode]).get("required_lanes", ())` (the registry is already a mapping on the resolved configuration, `segments.py:58`).

- [ ] **Step 4: `_prepare` as stages**

Replace `_prepare` with an orchestrator and six stage methods; each stage takes what the previous one produced, in small frozen dataclasses defined above `QueryService`:

```python
@dataclass(frozen=True)
class _Channels:
    exact_lexical: tuple[ChannelCandidates, ...]
    votes: dict[str, tuple[ChannelCandidate, ...]]
    completed: list[str]
    incomplete: list[str]
    truncated: list[str]
    lexical_epoch: int


@dataclass(frozen=True)
class _Fused:
    fused: tuple[FusedCandidate, ...]
    graph: GraphScope | None
    graph_candidates: GraphCandidates
    paths: tuple[GraphPath, ...]
```

and, in `QueryService`:

```python
    async def _prepare(self, request, scope, attempt, mode, plan, ctx) -> tuple[CachedSelection, int]:
        channels = await self._channels(request, scope, attempt, plan, ctx)
        fused = await self._fuse(request, attempt, plan, channels, ctx)
        admission = await self._admit(request, attempt, mode, channels, fused, ctx)
        ranked = await self._rerank(request, scope, attempt, plan, admission, channels, fused, ctx)
        selection, records, visuals, checks, relations = await self._assemble(
            request, attempt, plan, ranked, fused, ctx
        )
        coverage = self._coverage(plan, channels, fused, ranked)
        decisions = self._decisions(attempt)
        return (
            CachedSelection.of(
                selection,
                records=records,
                checks=checks,
                relations=relations,
                visuals=visuals,
                coverage=coverage,
                warnings=attempt.warnings,
                decisions=decisions,
                candidate_count=len(fused.fused),
            ),
            channels.lexical_epoch,
        )
```

with the current body moved, unchanged in behaviour, as follows: `_channels` takes the missing-batch check, the exact/lexical call, the strict refusal and the two vector channels with linked visual context (current `:699-813`, after Tasks 8, 9 and 15); `_fuse` takes the first fusion, the graph scope and augmentation and the second fusion, and computes `paths` (`:814-841`, `:944-948`); `_admit` takes the required lanes, `load_survivors` and `admit(...)` (`:845-874`); `_rerank` takes the rerank-facts load and the rerank branch (`:875-913`); `_assemble` takes the relation-path checks, `assemble_segments`, `prepare_visual_candidates`, `select_visuals`, `prepare_records` and the relation checks (`:914-1000`); `_coverage` calls `build_coverage(...)` and `_decisions` builds the edition `DecisionRequirement`s (Task 16). Each new function is under 80 lines; split a stage further (for example `_relation_paths` out of `_assemble`) when it is not.

- [ ] **Step 5: Repositories for the remaining statements**

Add to `src/tychora/ports/repositories.py`:

```python
class RegisteredAsset(Contract):
    """A registered asset, its revision and its stored payload."""

    asset: AssetRow
    revision_id: Identifier
    ref: ArtifactRef
    payload_state: Literal["present", "delete_pending", "deleted"]


class AssetLink(Contract):
    """A unit linked to an asset in a role."""

    unit_id: Identifier
    role: NonEmptyStr
    extraction_id: Identifier
    text_origin: NonEmptyStr
    citable: bool


class AssetRepository(Protocol):
    """Registered assets and their unit links."""

    def registered(self, asset_id: str) -> RegisteredAsset | None:
        """The asset with its revision and payload, or ``None``."""
        ...

    def links(self, asset_id: str, roles: tuple[str, ...], limit: int) -> tuple[AssetLink, ...]:
        """Up to ``limit`` units linked to the asset in the given roles, by unit ID."""
        ...
```

and implement them in `src/tychora/adapters/sqlite/repositories/assets.py` with the statements moved verbatim from `application/visual.py:130-137` (identical to `application/query.py:1268-1275`) and `application/visual.py:173-177` (with `ORDER BY u.unit_id LIMIT ?` and the roles bound through `json_each`), building `AssetRow` the way `visual.py:140-160` does. `application/visual.py` and `application/query.py:_registered_image_query` call `tx.assets.registered(...)` and `tx.assets.links(...)` inside `with self.owner.store.uow.read() as tx:`; `visual.py:_resolve`'s collection loop calls `tx.workspace.collection(cid)` (add `collection(self, collection_id: str) -> tuple[str, int] | None` to the workspace port and repository: `SELECT workspace_id,access_floor FROM collections WHERE collection_id=?`), and its own `BEGIN`/`rollback` becomes `with owner.store.uow.read() as tx:`.

Add `ArtifactRepository.require(sha256)` (port and `repositories/artifacts.py`): `get(sha256)` plus the payload state, raising `INTEGRITY_ERROR` reason `payload.missing` when absent or not `present`; `InspectionService._artifact` (`:124-141`) calls it.

Add `InspectionRepository` (port and `src/tychora/adapters/sqlite/repositories/inspection.py`), one method per remaining statement of `application/inspection.py`, each statement moved verbatim:

| Method | Returns | Statement moved from `inspection.py` |
| --- | --- | --- |
| `job_summary(job_id)` | `tuple[str, str, str, str] \| None` | `:152-155` |
| `run_summary(run_id)` | `tuple \| None` (principal, scope digest, state, created, snapshot, retained payload) | `:178-182` |
| `extraction_diagnostics(extraction_id)` | `tuple[str \| None, str] \| None` (diagnostics sha, source) | `:221-225` |
| `code_file_units(extraction_id, relative_path)` | `frozenset[str]` | `:284-290` |
| `unit_owner(unit_id)` | `tuple[str, str] \| None` (extraction, revision) | `:370-374` |
| `unit_records(unit_id)` | `tuple[tuple[str, str, str], ...]` | `:466-469` |
| `proxy_derivation(unit_id)` | `str \| None` | `:482-485` |
| `asset_summary(asset_id)` | `tuple[str, str, str] \| None` | `:541-545` |
| `tombstoned(revision_id, artifact_sha)` | `bool` | `:554-557`, `:606-609`, `:733-737` |
| `revision_summary(revision_id)` | `tuple[str, str] \| None` | `:579-582` |
| `original_bound(revision_id, artifact_sha)` | `bool` | `:602-605` |
| `retained_members(subject, item_id, workspace_id)` | `tuple[SnapshotMember, ...]` | `:643-662` (the four selectors become a closed `match` on `subject` inside the adapter) |
| `member_payloads(member)` | `frozenset[str]` | `:670-677` |
| `revision_policy(revision_id, workspace_id)` | `tuple \| None` | `:721-727` |

`_inspect_revision`'s unit loop (`:594-598`) already reads `tx.eligibility.unit_flags` after Task 11. Every method receives and returns scalars, tuples or contracts, so the port-neutrality test passes. In `application/reranking.py:_prepare_canonical`, the per-candidate `SELECT extraction_id FROM {table} WHERE {column}=?` (`:544-548`) becomes `tx.eligibility.owner_facts(tuple(item.key[1] for item in representations))` and the anchor chunk check (`:568-572`) reads `tx.representations.chunks(...)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/architecture tests/retrieval/test_query_stages.py tests/application tests/retrieval -q`
Expected: PASS.

- [ ] **Step 7: Full suite and health**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`, then `uv run --locked python -m tools.health check --test-reports without`
Expected: PASS; the health check reports no function over 80 lines among the functions this phase added and no new function-body import.

- [ ] **Step 8: Commit**

```bash
git add -A src/tychora tests
git commit -m "refactor: run queries as typed stages without configuration dumps"
```

---

### Task 20: SQL-free owner, removal and restore

The index's phase-4 ratchet adds `application/owner`, `application/removal` and `application/restore` to `SQL_FREE_MODULES` (their retention and visual SQL stayed after phase 2). This task moves their 67 statements (21 in `owner.py`, 22 in `removal.py`, 24 in `restore.py`) into repositories and adapter modules without changing what they read or write; transaction control (`BEGIN`, `BEGIN IMMEDIATE`, `commit`, `rollback` through `execute`) becomes `store.uow.read()` / `store.uow.write()`. It is a mechanical move: each method below holds one statement moved verbatim from the cited lines, and the existing tests of these services are the safety net.

**Files:**
- Create: `src/tychora/adapters/sqlite/bootstrap.py` (workspace identity bootstrap and retained kinds), `src/tychora/adapters/sqlite/repositories/upload_policy.py`, `src/tychora/adapters/sqlite/repositories/removal.py`, `src/tychora/adapters/sqlite/restore_rows.py`
- Modify: `src/tychora/adapters/sqlite/repositories/artifacts.py` (payload states), `src/tychora/ports/repositories.py` (`UploadPolicyRepository`, `RemovalRepository`, artifact state methods; `ReadTransaction.upload_policy`, `.removal`), `repositories/unit_of_work.py`
- Modify: `src/tychora/application/owner.py:184-235`, `:938-1450`; `src/tychora/application/removal.py`; `src/tychora/application/restore.py:121-480`, `:711-740`
- Test: `tests/architecture/test_sql_free_modules.py` (three modules appended)

**Interfaces:**
- Consumes: Task 4 `WorkspaceRepository.bump_policy_epoch`; Task 19 `ArtifactRepository.require`, `WorkspaceRepository.collection`.
- Produces (every method returns scalars, tuples or contracts):
  - `ArtifactRepository`: `state(sha256) -> tuple[ArtifactRef, str] | None`, `pending_deletions() -> tuple[str, ...]`, `mark_present(sha256) -> None`, `mark_delete_pending(sha256) -> int`, `mark_deleted(sha256, deleted_at: datetime) -> int`.
  - `UploadPolicyRepository`: `collection_policy(collection_id) -> tuple[str, int, str] | None`, `kind_row(kind_id) -> tuple | None`, `insert_kind(row: tuple) -> None`, `source_rows(source_id, collection_id, source_key) -> tuple[tuple, ...]`, `insert_source(row: tuple) -> None`, `captured_binding(kind_id, source_id) -> tuple | None`, `revision_grant(revision_id) -> tuple | None`, `identity_owner(identity) -> tuple[tuple[str, str], ...]`, `proxy_requirements(unit_id, limit) -> tuple[str, tuple[str, ...]] | None`.
  - `RemovalRepository`: `source_grant(source_id, workspace_id) -> tuple | None`, `revision_original(revision_id, source_id) -> tuple[str, int] | None`, `original_artifacts(revision_id) -> tuple[str, ...]`, `revision_payloads(revision_id) -> dict[str, tuple[str, ...]]`, `tombstoned(revision_id, sha) -> bool` (shared with `InspectionRepository.tombstoned`: one implementation), `insert_tombstone(revision_id, sha, job_id, removed_at) -> None`, `manifest_roots() -> frozenset[tuple[str, str]]`, `capture_roots() -> tuple[tuple[str, bool], ...]`.
  - `adapters/sqlite/bootstrap.py`: `bootstrap_workspace(db) -> str` (`owner.py:190-222`), `retained_kinds(db, workspace_id, collection_ids) -> dict[str, JsonValue]` (`owner.py:229-236`).
  - `adapters/sqlite/restore_rows.py`: `authorize_restore(db, scope)` (`restore.py:121-143`), `bootstrap_scope(db) -> RequestScope` (`:145-164`), `active_snapshot_config_ref(db) -> ArtifactRef | None` (`:166-205`, its SQL), `retained_document_rows(db) -> ...` (the reads of `_validate_retained_documents`, `:236-470`), `restored_workspace_ids(db) -> tuple[str, ...]` (`:479`), `set_operational_state(path)` (`:711-740`).

- [ ] **Step 1: Write the failing test**

Append to `SQL_FREE_MODULES` in `tests/architecture/test_sql_free_modules.py`:

```python
    "application/owner.py",
    "application/removal.py",
    "application/restore.py",
```

Run: `uv run --locked pytest tests/architecture/test_sql_free_modules.py -v`
Expected: FAIL listing the 67 statements and the `sqlite3` imports of the three modules.

- [ ] **Step 2: Owner bootstrap**

Move `owner.py:184-222` (database path checks stay; the workspace-row reads, the empty-schema check over `sqlite_master` and the insert) into `bootstrap_workspace(db) -> str` in `src/tychora/adapters/sqlite/bootstrap.py`, raising the same errors, and `owner.py:229-236` into `retained_kinds(db, workspace_id, collection_ids)`. `_initialize` calls them; `owner.py` keeps `open_corpus` and catches `sqlite3.DatabaseError` through `typed_storage_errors()` instead of naming `sqlite3`. Remove `import sqlite3` from `owner.py`.

- [ ] **Step 3: Upload policy reads and writes**

Create `src/tychora/adapters/sqlite/repositories/upload_policy.py` with `SqliteUploadPolicyRepository(db, *, writable=False)` whose methods hold, verbatim, the statements at `owner.py:965-968` (`collection_policy`), `:993-997` (`kind_row`), `:999-1004` (`insert_kind`, `require_write` first), `:1017-1021` (`source_rows`), `:1024-1029` (`insert_source`), `:1260-1266` (`captured_binding`), `:1445-1449` (`revision_grant`), `:1418-1424` (`identity_owner`), `:1429-1437` (`proxy_requirements`: the proxy row and its required units, `LIMIT ?`). Declare the protocol in `ports/repositories.py` and add `upload_policy` to `SqliteTransaction` and `ReadTransaction`. In `CanonicalUploadPolicy`, `register_captured_source` runs its body in `with owner.store.uow.write() as tx:` instead of `BEGIN IMMEDIATE`/`commit`/`rollback` (`:962-1035`), and every read-only method (`authorize_captured`, `authorize`, `authorize_local`, `authorize_query`, `_resolve`, `_check_revision`) uses `with owner.store.uow.read() as tx:` instead of `BEGIN`/`rollback` (`:1055`, `:1334`, `:1361`, `:1391`). The `(ValueError, TypeError, KeyError)` handlers stay where they are.

- [ ] **Step 4: Removal**

Create `src/tychora/adapters/sqlite/repositories/removal.py` with `SqliteRemovalRepository` holding the statements at `removal.py:55-59` (`source_grant`), `:118-121` (`revision_original`), `:129-133` (`original_artifacts`), `:154-186` (`revision_payloads`: the original, its bindings, extraction artifacts, asset and proxy artifacts and vector batch artifacts of one revision, returned by role), `:199-202` and `:314-317` (`tombstoned`), `:318-321` (`insert_tombstone`), `:404-410` (`manifest_roots` over `TYPED_MANIFEST_ROOTS`, which moves into this module, and `capture_roots`). Artifact state statements go to `SqliteArtifactRepository`: `:212-214`, `:275-278`, `:350-352`, `:466-469` (`state`), `:451-453` (`pending_deletions`), `:481-484` (`mark_present`), `:328-331` (`mark_delete_pending`), `:500-503` (`mark_deleted`). `:322-325` becomes `tx.workspace.bump_policy_epoch()`. `RemovalService` opens `self.owner.store.uow.read()` / `.write()` where it used the raw connection, and `store.immediate_transaction()` blocks become `store.uow.write()`. `BoundSet.load(self.owner.db, ...)` and `workspace_closure_budget(db)` are adapter functions already (`adapters/files/references.py`); they keep taking the connection, which `removal.py` reads as `self.owner.store.db` without executing on it.

- [ ] **Step 5: Restore**

Create `src/tychora/adapters/sqlite/restore_rows.py` and move into it, verbatim with their callers' signatures, `_authorize` (`restore.py:121-143`, as `authorize_restore`), `_local_owner_bootstrap_scope` (`:145-164`, as `bootstrap_scope`), the SQL of `_active_snapshot_config` (`:169-180`, as `active_snapshot_config_ref`), the row reads of `_validate_retained_documents` (`:244-247`, `:266-270`, `:288-296`, `:323-336`, `:388-406`) as one function per statement, the workspace read of `load_restored_runtime_config` (`:479`) and `_set_operational_state` (`:711-740`, as `set_operational_state`). `application/restore.py` calls them with the connection `open_verified_import` returns; it no longer imports `sqlite3` (the connection is passed through untyped, as `Any`, or typed with the adapter's alias `RestoreDatabase = sqlite3.Connection` defined in `restore_rows.py`).

- [ ] **Step 6: Run the tests**

Run: `uv run --locked pytest tests/architecture tests/runtime tests/operations tests/security tests/transports/test_dashboard_restore.py tests/ingestion/test_vision.py -q`
Expected: PASS.

- [ ] **Step 7: Full suite and imports**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'` and `uv run --locked lint-imports --no-cache`
Expected: PASS; contracts kept.

- [ ] **Step 8: Commit**

```bash
git add -A src/tychora tests
git commit -m "refactor: move owner, removal and restore SQL into repositories"
```

---

### Task 21: Gate, ratchet and index

**Files:**
- Modify: `src/tychora/evaluation/bench/harness.py:342-413` (E4 assertion beside E3)
- Create: `tests/acceptance/test_concurrency.py`, `tests/acceptance/test_strict_scale.py`
- Modify: `tests/evaluation/bench/test_measurement.py` (E4 verdicts)
- Modify: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md` (phase table row 4, "Carried over from phase 1", "Produced by phase 4", spec gaps)
- Modify: `tests/fixtures/evaluation/code-gold/README.md` ("Search time", "definition_recall" table values, measured where the corpus is available)
- Modify: `quality/baseline.json` (through the checker, only where values improved)
- Not committed from macOS: `bench-results/` (the reference runner publishes results)

**Interfaces:** `e4_assertions(points: list[ScalePoint], reached: int) -> list[Assertion]` in `evaluation/bench/harness.py`; `E4_LATENCY_RATIO = 1.5`.

The phase-4 gate (index, "Benchmark gates per phase"): local query share p95 < 300 ms on `bench-small` (wall clock; per-stage budgets are asserted from phase 5, when traces exist); search latency at 10,000 documents ≤ 1.5× at 1,000 (E4); strict "verification outcome" succeeds at 20, 1,000 and 10,000 documents (E5); evaluation metrics at or above the phase-1 baselines (E6 lexical at `b91d808`, `bench-results/baseline/evaluation-b91d808.json`; code gold G22 at phase-1 HEAD, `tests/fixtures/evaluation/code-gold/baseline.json`); one 300-page add with 20 concurrent queries keeps query p95 ≤ 1.5× idle. For reference: phase 2's scale run measured search p50 24 s and p95 36 s at 10,000 documents (offline, `bench-small`), and a full-repository code search took about 12 s (code gold README); both are what this phase removes, and the E4 ratio plus the 300 ms local ceiling are the targets that replace them.

- [ ] **Step 1: E4 in the scale suite**

In `src/tychora/evaluation/bench/harness.py`, after `e3_assertions`:

```python
E4_LATENCY_RATIO = 1.5


def e4_assertions(points: list[ScalePoint], reached: int) -> list[Assertion]:
    """E4: search p95 at 10,000 documents within 1.5x of p95 at 1,000."""
    by_size = {point.documents: point for point in points}
    if 1000 not in by_size or 10_000 not in by_size:
        return [_not_measured("E4.latency_ratio", reached)]
    small, large = by_size[1000].search_p95_ms, by_size[10_000].search_p95_ms
    return [
        Assertion(
            name="E4.latency_ratio",
            passed=large <= E4_LATENCY_RATIO * max(small, 1.0),
            detail=f"search p95: {small:.1f} ms at 1000, {large:.1f} ms at 10000",
        )
    ]
```

and in `run_scale`, both `samples.assertions.extend(e3_assertions(...))` calls become `samples.assertions.extend((*e3_assertions(samples.scale, growth.added), *e4_assertions(samples.scale, growth.added)))`. Add to `tests/evaluation/bench/test_measurement.py`:

```python
def test_e4_compares_search_p95_at_ten_thousand_with_one_thousand():
    from tychora.evaluation.bench.harness import e4_assertions
    from tychora.evaluation.bench.results import ScalePoint

    def point(documents, p95):
        return ScalePoint(
            documents=documents,
            add_p50_ms=1.0,
            bytes_per_add=1.0,
            search_p50_ms=p95,
            search_p95_ms=p95,
            largest_artifact_bytes=1,
            footprint_bytes=1,
        )

    (passed,) = e4_assertions([point(1000, 40.0), point(10_000, 59.0)], 10_000)
    (failed,) = e4_assertions([point(1000, 40.0), point(10_000, 61.0)], 10_000)
    (missing,) = e4_assertions([point(1000, 40.0)], 1000)
    assert passed.passed and not failed.passed
    assert missing.measured is False
```

Run: `uv run --locked pytest tests/evaluation/bench/test_measurement.py -v`
Expected: PASS after the change (FAIL with `ImportError` before it).

- [ ] **Step 2: Concurrency acceptance**

```python
# tests/acceptance/test_concurrency.py
"""A large add does not stall queries: no busy refusals, p95 within 1.5x of idle."""

import asyncio
import time

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import AsyncTychora

pytestmark = pytest.mark.slow

_PAGE = "Operators inspect the pump seal and record the verification outcome. " * 30


def _source(key, pages):
    return {
        "type": "text",
        "source_key": key,
        "collection_id": "default",
        "kind": "document@1",
        "format": "markdown",
        "text": "\n\n".join(f"# Page {n}\n\n{_PAGE}" for n in range(pages)),
        "metadata": {"title": key},
    }


async def _timed(kb, question):
    started = time.perf_counter()
    await kb.search({"question": question})
    return time.perf_counter() - started


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


@pytest.mark.asyncio
async def test_add_of_300_pages_with_20_queries(tmp_path):
    async with AsyncTychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        for index in range(5):
            job = await kb.sources.add(_source(f"seed-{index}.md", 3))
            assert (await kb.jobs.wait(job.id)).state == "succeeded"
        await _timed(kb, "pump seal")
        idle = [await _timed(kb, "verification outcome") for _ in range(20)]
        job = await kb.sources.add(_source("book.md", 300))
        busy = await asyncio.gather(*(_timed(kb, "verification outcome") for _ in range(20)))
        assert (await kb.jobs.wait(job.id)).state == "succeeded"
        callers = await asyncio.gather(
            *(kb.search({"question": f"seal {n}"}) for n in range(33)),
            return_exceptions=True,
        )
    assert not [c for c in callers if isinstance(c, BaseException)]
    assert _p95(busy) <= 1.5 * max(_p95(idle), 0.05)
```

(`AsyncTychora.open(...)` is an async context manager, as `tests/api/test_public_async.py:57` uses it.) The test is `slow` because a 300-page add takes tens of seconds; it runs in the nightly job and in Step 6 below.

- [ ] **Step 3: Strict regression at scale**

```python
# tests/acceptance/test_strict_scale.py
"""Strict search for an ordinary phrase succeeds at 20, 1,000 and 10,000 documents."""

import pytest

from tests.helpers.legacy import OutputCounter
from tychora import Tychora


def _add_many(kb, count):
    for index in range(count):
        job = kb.sources.add(
            {
                "type": "text",
                "source_key": f"doc-{index:05}.md",
                "collection_id": "default",
                "kind": "document@1",
                "format": "markdown",
                "text": f"# Record {index}\n\nThe verification outcome of item {index} is stored.\n",
                "metadata": {"title": f"Record {index}"},
            }
        )
        assert kb.jobs.wait(job.id).state == "succeeded"


@pytest.mark.parametrize(
    "count",
    [
        20,
        pytest.param(1000, marks=pytest.mark.slow),
        pytest.param(10_000, marks=pytest.mark.slow),
    ],
)
def test_verification_outcome_succeeds_strict(tmp_path, count):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add_many(kb, count)
        result = kb.search({"question": "verification outcome"})
    assert result.segments
    assert result.coverage.status == "complete"
```

Run: `uv run --locked pytest tests/acceptance/test_strict_scale.py -v -m "not slow"`
Expected: PASS for 20 documents.

- [ ] **Step 4: Deletions owned by this phase**

Run: `rg -n "run_blocking|_vector_owners|_candidate_lanes|load_retained_representation|snapshot_slots|RetainedSlot|RetainedBatchCache|read_eligibility_facts|LegacyEligibilityView|QueryExecutionPlan|retrieval\.eligibility|_PreparedResult|pickle\.dumps\(value" src/tychora`
Expected: no match. Then `rg -n "prepare_extraction\(" src/tychora/application src/tychora/retrieval`
Expected: only `application/maintenance.py` (manage health) — read paths no longer call it (spec §4.8).

- [ ] **Step 5: Full suite, imports and code health**

Run: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`
Expected: PASS, including `tests/evaluation/test_retrieval_baseline.py` (E6 lexical at or above `b91d808`) and the kept fixes (`tests/retrieval/test_segments.py`, `tests/application/test_upload_batches.py`, `tests/ingestion/test_docling_mapping.py`).

Run: `uv run --locked lint-imports --no-cache`
Expected: every contract kept; the exception lists are shorter than at `505e8ff` (Tasks 5, 7, 11 and 12 removed entries, none was added).

Run: `uv run --locked python -m tools.health check --test-reports without`
Expected: PASS, or "stale baseline" naming only improved metrics (fewer layering exceptions, fewer reasonless `TychoraError` sites, the deleted long functions `_prepare`, `read_eligibility_facts`, `_compile`, smaller duplication). G12 reports no dead code: every new public name has a caller in `src`. If stale:

Run: `uv run --locked python -m tools.health update --test-reports without`
Expected: `quality/baseline.json` rewritten with only improved values. G16–G18 (coverage and test durations) are never recorded from macOS; the pull request's CI run records them with `tools.health update --gates G16 --test-reports only` from its merged reports, as phases 2 and 3 did.

- [ ] **Step 6: Benchmarks and slow acceptance, informative on macOS**

Run: `uv run --locked pytest tests/acceptance/test_concurrency.py tests/acceptance/test_strict_scale.py -v -m slow`
Expected: PASS (informative on a laptop; the nightly job is the record).

Run: `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite query --profile offline --out "$TMPDIR/bench-phase4"` and `uv run --locked python -m tychora.interfaces.bench run --corpus bench-small --suite scale --profile offline --checkpoints 100,1000,10000 --out "$TMPDIR/bench-phase4"`
Expected: exit 0. Read the results:

Run: `uv run --locked python -c "import json, sys; q = json.load(open(sys.argv[1])); s = json.load(open(sys.argv[2])); print('total p95', q['stages']['total']['p95']); print([(p['documents'], p['search_p95_ms']) for p in s['scale']]); print([a for a in s['assertions'] if a['name'].startswith(('E3', 'E4'))])" "$TMPDIR"/bench-phase4/*/query-offline.json "$TMPDIR"/bench-phase4/*/scale-offline.json`
Expected: `total p95` well under 300 ms, `E4.latency_ratio` passed, search p95 at 10,000 far below phase 2's 36 s. Do not copy these results into the repository.

With the pinned code corpus available (`TYCHORA_TEST_CODE_CORPUS`, see `tests/fixtures/evaluation/code-gold/README.md`), run `TYCHORA_CODE_GOLD_REPORT="$TMPDIR/code-gold.json" uv run --locked pytest tests/evaluation/test_code_gold.py -p no:xdist` and update the README's "Search time" section (median and maximum search time of the run) and its table with the measured `definition_recall`; every metric must be at or above `baseline.json` (G22 is the nightly gate; a fall blocks the merge).

- [ ] **Step 7: The gate on the reference runner**

1. Ask the owner to dispatch the `Benchmarks` workflow (`.github/workflows/bench.yml`) on the phase-4 branch head; a development session never dispatches it. It runs the offline ingest, query and scale suites on `github-ubuntu-22.04` and publishes to the `bench-results` branch.
2. With the new query result `NEW_Q`, the new scale result `NEW_S` and the latest phase-3 results of the same runner class `OLD_Q`, `OLD_S` fetched from `bench-results`:

Run: `uv run --locked python -c "import sys; from pathlib import Path; from tychora.evaluation.bench.results import load_result, regressions; q, s, oq, os_ = (load_result(Path(p)) for p in sys.argv[1:]); print(q.stages['total'].p95, [a for a in s.assertions if a.name.startswith(('E3', 'E4'))], regressions(q, oq), regressions(s, os_))" NEW_Q NEW_S OLD_Q OLD_S`
Expected: a total p95 below 300, every E3 and E4 assertion passed, and `()` twice (no stage regressed by more than 20%; the query suite should improve). A failed assertion or a regression blocks the merge; report it rather than relaxing the gate. Gate verdicts come only from this run.

- [ ] **Step 8: Record the phase in the index**

In `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`:

1. Phase table row 4, column "Detail": `Fully detailed`.
2. "Carried over from phase 1": add after the paragraph about phase 4's ownership: "Phase 4 added `WorkspaceRepository.bump_policy_epoch` and `ArtifactRepository.require` with their first callers; `WorkspaceRepository.format_version` and `WorkspaceOwner.uow` still wait for theirs."
3. Replace the `### Produced by phase 4` code block with:

```python
# src/tychora/application/owner.py, runtime.py (changed public surface)
Priority = Literal["interactive", "background"]
class WorkspaceOwner:
    async def run_write[T](self, call: Callable[[], T], ctx: ExecutionContext, priority: Priority = "background") -> T: ...
    def run_write_sync[T](self, call: Callable[[], T], ctx: ExecutionContext, priority: Priority = "background") -> T: ...
    async def run_read[T](self, call: Callable[[ReadTransaction], T], ctx: ExecutionContext) -> T: ...  # read pool, one WAL snapshot
    async def run_cpu[T](self, call: Callable[[], T], ctx: ExecutionContext) -> T: ...
    catalog: SnapshotCatalog; eligibility_cache: EligibilityCache
# run_blocking and run_blocking_sync are removed. Queue bounds: interactive 256, background 64, read 128 (WORKSPACE_BUSY queue.full).

# src/tychora/contracts/eligibility.py
class RevisionGrant(Contract): extraction_id; source_id; kind_id; lane: str; collection_id
class GraphBinding(Contract): family_id: Sha256; all_eligible: bool; receipt: GraphBatchReceipt   # receipt until phase 6
class EligibilityView(Contract):
    snapshot_id; policy_epoch: int; scope_digest; eligibility_policy_digest; eligibility_request_digest
    revisions: Mapping[str, RevisionGrant]; excluded_unit_ids: frozenset[str]; excluded_asset_ids: frozenset[str]
    unit_filter: frozenset[str] | None; selected_batches: Mapping[str, tuple[str, ...]]; graph: GraphBinding | None
    all_members_eligible: bool
    def allows(self, dependency_ids: Iterable[str]) -> bool: ...   # one revision per persisted set; exclusions closed under dependency
    revision_of_extraction; extraction_ids; batch_ids   # cached properties
# src/tychora/contracts/graph.py: GraphScope (unit lists a traversal filters by, derived per view when a graph channel runs; phase 6 replaces it)

# src/tychora/retrieval/
# selection.py: RevisionFact(edition_key), EditionDecision, EditionSelection, select_temporal_revisions
# compiler.py: EligibilityKey, eligible_cache_key, CompiledEligibility, ModePolicy, EligibilityCompiler, filter_matches
# attempt.py: EligibilityAttempt, run_pinned_eligibility, admission predicates (is_source_unit, is_admitted_chunk, ...), readable_units, require_epoch
# catalog.py: class SnapshotCatalog: def get(self, tx: ReadTransaction, snapshot_id: str) -> SnapshotView   # LRU 32, SQL only
# cache.py: class EligibilityCache: def get_or_compile(self, key: EligibilityKey, compile: Callable[[], CompiledEligibility]) -> CompiledEligibility; graph_scope(key, build)
# cached_selection.py: class CachedSelection(Contract): canonical JSON <= max_response_bytes + 65536, no pickle
# survivors.py (at most 200 candidates), vectors.py, coverage.py, graph_scope.py

# src/tychora/ports/search.py
class RankedOwner(Contract): key: CandidateKey; rank: PositiveInt; score: float; window_ids: tuple[str, ...] = ()
class RankedOwners(Contract): owners: tuple[RankedOwner, ...]; truncated: bool; detail: Mapping[str, int]
class LexicalSearch(Protocol): search(view, query, limit, ctx, *, stopwords=frozenset()) -> RankedOwners; exact(view, identifiers, limit, ctx) -> RankedOwners
class VectorSearch(Protocol): def search(self, view: EligibilityView, vector: bytes, limit: int, ctx: ExecutionContext) -> RankedOwners: ...

# src/tychora/ports/repositories.py (added)
MemberRow, ReviewRow, UnitFlags, FacetValue (NamedTuples); OwnerFact(Contract); EligibilityRepository; AssetRepository;
InspectionRepository; UploadPolicyRepository; RemovalRepository
RepresentationRepository.neighbours(unit_id, radius: int | None) -> tuple[UnitRow, ...]; .slice(extraction_id, *, unit_ids=(), asset_ids=(), relative_path=None) -> RepresentationSlice
SnapshotRepository.member(snapshot_id, extraction_id); .members(snapshot_id, *, after=None, limit=1000) -> (page, cursor)
WorkspaceRepository.bump_policy_epoch(); .collection(collection_id); ArtifactRepository.require(sha256); payload-state methods
# ReadTransaction gains eligibility, lexical, assets, inspection, upload_policy, removal

# src/tychora/contracts/requests.py (changed, additive)
class ChannelSelection(Contract): lexical: bool = True; text: bool = True; image: bool = True; graph: bool = True
                                  graph_strategies: frozenset[Literal["structural","semantic"]] | None = None
class QueryRequest: channels: ChannelSelection | None = None; rerank: Literal["auto","on","off"] = "auto"
                    review_states: tuple[str, ...] | None = None; revision_selector adds "applicable"
# contracts/evidence.py: Coverage.truncated_channels, Coverage.skipped_channels, DecisionRequirement.revision_ids
# contracts/publication.py: SnapshotView without members and lexical_statistics_epoch; RetainedSlot removed; RetainedBatch kept for native recovery
# configuration: RetrievalSettings.lexical (languages, stopwords); configuration/stopwords.py; collection_policy.py and graph_policy.py moved here
```

   and the sentence after it with: "Schema added: `review_current(target_kind, target_id, state, review_event_id)`, `sources.allowed_uses_override`, `vector_records.group_id`, indexes `representation_dependencies_dependency` and `units_declared_review`; `SCHEMA_CHECKSUM` changed and `be4274cd…` (phase 3) joined `EARLIER_FORMAT_TWO_CHECKSUMS`. Resources added: `resources/stopwords/en@1.txt`, `fr@1.txt`, `LICENSE-snowball.txt`. Reasons added: `canonical.facts_missing`, `policy.use_unknown`, `source.absent`, `scope.capability_missing`, `request.limit_exceeded`, `mode.unavailable`, `profile.snapshot_mismatch`, `kind.historical_unavailable`, `facet.binding_unverified`, `policy.epoch_changed`, `identifier.namespace_unknown`, `scope.dependency_ineligible`, `edition.as_of_required`, `channel.not_configured`."
4. Append this plan's "Spec gaps" 1–14 to the index's "Spec gaps found while planning", numbered from 29.

- [ ] **Step 9: Commit**

```bash
git add src/tychora/evaluation/bench/harness.py tests/acceptance tests/evaluation/bench/test_measurement.py docs/superpowers/plans/2026-10-16-engine-core-v2-index.md tests/fixtures/evaluation/code-gold/README.md quality/baseline.json
git commit -m "chore: record phase 4 benchmark and health baseline"
```

(`quality/baseline.json` only if Step 5 rewrote it; the README only if Step 6 measured the code gold set.)

---


---

## Spec gaps

Each gap names what the spec leaves undecided and the answer this plan applies. Task 21 appends them to the index, numbered from 29.

1. **`query_only` and temp tables.** §4.6 requires `query_only` read connections and §4.7 a per-connection temp table "filled once per cache key". SQLite refuses temp writes under `query_only`, and a read task's `ROLLBACK` drops a temp table it created. Answer: bind ID lists through `json_each(?)` inside `IN (...)` (materialised once per statement), skip the restriction when every member is eligible, and cache each view's JSON text (Task 8).
2. **What `allows` needs.** The spec's `allows(dependency_ids, kinds)` leaves `kinds` undefined. Answer: none is needed. Every persisted dependency set names exactly one revision, its owner's, and units or assets of that revision's extraction; with exclusions closed under dependency, a set is readable when its revision is eligible and it names no excluded unit or asset (Tasks 6, 7).
3. **What `excluded_unit_ids` holds.** §4.7 says "rejected/blocked/non-citable for this mode". Answer: units and assets of eligible extractions that fail the mode's review states, the proxy policy or a unit-level (`all_units`) filter, plus every unit or asset depending on one of them (a recursive closure over `representation_dependencies`, which reproduces v1's fixpoint). Non-citable units are not excluded: they are structural context that dependency sets name; citability is a candidate rule applied in the search joins and the survivor check (Task 7).
4. **What `unit_filter` holds.** Answer: the units that pass a `matched_candidates` filter (candidate-only, as v1's candidate filter); the `all_units` filter's failures are exclusions (gap 3). Both are computed only when a request carries a filter, over the units of eligible extractions (Task 7).
5. **Graph eligibility before contributions.** §4.5 binds graph contributions (phase 6); the corpus-wide generation still filters traversal by unit lists. Answer: `GraphScope` lists the eligible units of eligible extractions (all not excluded, by gap 3's closure, so no dependency read), built only when a graph channel runs and cached with the view; phase 6 replaces it (Task 12).
6. **Blocked assertions.** v1 intersected assertions decided after the generation's watermark with the generation manifest, which it loaded per query. Answer: every assertion decided after the watermark is blocked; one outside the generation is never traversed, so the manifest is not read (Tasks 5, 12).
7. **Edition overlap and undated editions.** §4.9 says a decision arises "when winners' `[effective_from, effective_until)` intervals overlap" and that undated editions are "returned as `undated_candidates`". Answer: with `as_of`, an overlap decision arises when another edition in force declares an `effective_until` later than the winner's `effective_from` (an open-ended older edition is superseded by a later start, so it raises nothing — otherwise every amended document would demand a decision); undated editions are selected beside the winners and reported in their own decision; `revision_selector="applicable"` requires `as_of` (`edition.as_of_required`) (Task 16).
8. **Missing persisted facts.** Phase 3's gap 13 left the refusal to this phase, suggesting `canonical.row_invalid`. Answer: a dedicated `INTEGRITY_ERROR` reason `canonical.facts_missing` whose operator action names the workspace rebuild, because the remedy differs from a malformed row (Task 5).
9. **Retained inspection of archived collections.** v1 compiled exact retained inspection with archived collections treated as active and never cached the result. Answer: `compile(..., include_archived=True)` for explicit retained members only, never through the cache (Task 11).
10. **"Survivor check … in one query".** Answer: one statement reads the candidates', windows' and records' rows, one their dependency sets and, when a window's unit was not among the candidates, one more statement reads those units; at most three statements for at most 200 candidates (Tasks 8, 9).
11. **Global facets on every query.** v1 verified each member's global-facet binding artifact on every query. Answer: they are read and verified only when a request carries a filter, and only for the revisions it admits (Task 7).
12. **Stop-word lists.** §4.9 names `builtin@1` but not its content. Answer: the Snowball English and French lists (BSD 3-Clause, licence shipped beside them) without apostrophe forms, which the tokenizer splits, and without French "ai" and "eu", which collide with English acronyms in a combined list; the lexical profile is excluded from the pinned-configuration comparison because it only shapes query expressions (Task 14).
13. **Partitioned upserts.** §4.6 runs each partition as a separate writer task, but the coordinator's `finalize` upserts inside one writer task. Answer: `SourceService` submits each ≤ 2 MB partition as its own background task before `finalize`, which then finds every record present and only reads them back to build the receipt (Task 9).
14. **Vector hits outside the snapshot.** A family's collection holds records of unpublished or collected batches that v1 never saw (it queried by eligible ID lists). Answer: such hits are dropped silently by the canonical owner mapping (`is_admitted_record` checks the batch against the view), not refused (Task 9).

## Open questions for the owner

1. **Task 20 (owner, removal and restore SQL-free) is off the query path.** It moves 67 statements mechanically and is what the index's phase-4 ratchet lists. Keep it in this phase (the plan's default), or move it to phase 5 beside `application/recovery`, which joins `SQL_FREE_MODULES` there?

## Self-Review

**Spec coverage.** §4.6: lanes and priorities (Task 1), read pool, deferred `BEGIN`/`ROLLBACK`, `mode=ro` + `query_only` (2), lease-only pins (already done; Task 3 notes it), partitioned upserts as separate tasks (9), queue bounds and `queue.full` (1, 2). §4.7: SQL-only catalog LRU 32 (3); `EligibilityView` v2 (6); compilation in one read transaction with the member join, Python predicates, exclusions and indexed facet reads (7); `review_current` maintained by `record_review` (4); kind envelopes verified and cached by `(kind_id, definition_hash)` (7); epoch invariant test enumerating writes (4); cache LRU 64 with the eligibility-relevant request digest (7, 17); lexical and exact joins, `LIMIT 4000`, 80 owners, truncation (8); vector filter or over-fetch, owners in one statement, no `_present`, quantized hook `detail["rescored"]` (9); survivor check ≤ 200 and `dropped` count (8); lanes from the view (8); typed errors and the read-pool guard (2). §4.8 read side: per-candidate rows, intervals and neighbours within the boundary key (10, 11); cheap revalidation (`text_sha` through `EvidenceUnit`, policy and epoch: 7, 10); no read-path `prepare_extraction` (5, 10, 11, test in 10); `RerankFacts` per candidate (10); `CachedSelection` (13). §4.9: strict and truncation (15), stop words (14), editions (16). §4.12: E1 local, E4, E5, concurrency in the gate (21). §4.13: repositories own the SQL of the rewritten query modules and owner, removal, restore (19, 20); reasons (all tasks). §4.14: per-query channels and rerank (17), per-source generation policy (4, 17), review gating per request (17), edition defects (16). §6 rows: eligibility equivalence on 500 randomized fixtures (7, oracle kept in 12), epoch invariant (4), concurrency (21), strict regression (8, 15, 21), editions (16), cache (13). §7: deletions checked in Task 21 Step 4; `read_eligibility_facts`, `_prepare`, `EligibilityCompiler._compile` replaced by stage functions (7, 12, 19); `retrieval/eligibility.py` split into `selection.py`, `compiler.py`, `attempt.py` (6, 7, 12); `adapters/sqlite/eligibility.py` no longer imports `retrieval` or `indexing` (5, then deleted in 12); `SQL_FREE_MODULES` (19, 20); interim slot loader deleted (5). Carried items: persisted facts with `canonical.facts_missing` (5); `RepresentationRepository.neighbours` with its first caller (10); search latency targets and code search time (21); single snapshot resolution (3); `context_level` invalidation is not touched (no task changes context levels). Not covered, by the spec's own deferral: `linked_visual_context` rank restart (sub-project 2), incremental `ResponseAssembler` measuring (revisited only if the gate's assembly share misses).

**Placeholder scan.** No TBD or "similar to". Values produced at run time come with the command that produces them (the new `SCHEMA_CHECKSUM`, benchmark results). Two tasks move existing statements verbatim and cite them by line rather than repeating them (Task 19 Step 5's inspection table, Task 20); each names the method, its signature and the exact lines, and the SQL-free architecture test checks the result. The Snowball licence is copied from its published page (Task 14 Step 3) rather than retyped.

**Thread discipline in tests.** The owner's connection belongs to its writer thread. Every test snippet that reads or writes the workspace database goes through `run_read`, `run_write_sync` or the `tests/helpers/owner.py` helpers that Task 3 adds (`on_owner`, `owner_row`, `owner_rows`). Tests that open their own `CorpusStore` on the test thread are the only ones that touch `store.db` directly.

**Type consistency.** `EligibilityView` (v2) and `LegacyEligibilityView` (6–12), `RevisionGrant`, `GraphBinding(receipt)` (6, 7, 12), `OwnerFact` and its fields (`unit_id`, `chunk_id`, `asset_id`, `proxy`, `citable`, `batch_id`, `source_owner_ids`; 6, 7, 8, 9, 11), `EligibilityKey`, `CompiledEligibility(view, warnings, roles, envelopes, decisions)` (7, 12, 16), `EligibilityAttempt(view, snapshot, …, legacy, legacy_dependencies)` then without `legacy` (7, 12), `run_pinned_eligibility(compiler, legacy, …)` then `(compiler, …)` (7, 12), `RankedOwner(window_ids)`, `RankedOwners(truncated, detail)` (8, 9), `Survivors(candidates, lanes, dropped)` (8), `RerankFacts(dependencies)` (10, 11), `RepresentationSlice(rows, prepared)` and `slice(extraction_id, *, unit_ids, asset_ids, relative_path)` (10, 11), `GraphScope` (12), `CachedSelection` (13, 19), `StopwordSet` (14), `Coverage(truncated_channels, skipped_channels)` (15, 17), `EditionSelection`, `EditionDecision` (16), `QueryBindings`, `QueryPlan`, `plan_for` (17, 19), `budget_stub` (18) are used with the same names and fields wherever they appear.

**Review Focus.** Each item has its pinned test in the owning task: 1 in Tasks 4 and 7, 2 in Task 9, 3 in Task 14, 4 in Task 16, 5 in Task 2.

**Index changes needed** (Task 21 Step 8 applies them): the "Produced by phase 4" block as built (including the signature corrections listed in "Changes from the outline"), the schema and resources sentence, fourteen reasons, the phase-1 carry-over note, and spec gaps 1–14.
