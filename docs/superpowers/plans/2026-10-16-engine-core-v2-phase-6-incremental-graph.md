# Engine Core v2, Phase 6: Incremental Graph Implementation Plan

> **Status: task granularity, to be expanded before execution.** Files, interfaces and tests are fixed at task level. Before executing, expand each task with the superpowers:writing-plans skill into bite-sized TDD steps with real code and exact commands, against the code as it stands after phase 5.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Never rebuild the graph on the add path: each publication inserts only the new member's nodes and edges, the next query sees them, and superseded content is removed by background compaction. Expose symbol neighbourhood, impact analysis and "most connected" over the same live graph.

**Architecture:** One live Ladybug graph per graph family. A member's graph data is a contribution: a batch of slot `graph` in `index_batches`, bound by the member's membership page like any other batch, with an artifact listing its evidence, entity and assertion IDs. Traversal filters every node and edge by the eligibility view's `selected_batches["graph"]` and skips the filter when every member is eligible. Compaction deletes retired contributions in bounded transactions. A relational `graph_degrees` table answers hub queries without scanning the graph.

**Tech Stack:** Ladybug 0.20.3 (explicit transactions, `MATCH … DETACH DELETE`, `CHECKPOINT`, write-ahead log), SQLite, MCP 2.2.0, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.5, §4.14 rows "Symbol neighbourhood", "Impact analysis", "Graph edge weights", §4.15 "Most connected", §6 rows "Graph contributions", "Ladybug behaviour"; risks 6 and 8. Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`.

## Global Constraints

All constraints of the index apply. Phase-specific:

- No fallback to a periodic full rebuild exists. Fallback order if Ladybug fails Task 1: (1) graph reads on the writer lane at interactive priority; (2) relational `graph_nodes`/`graph_edges` keyed by `contribution_id` behind the same `GraphStore` port, traversed with a bounded recursive CTE.
- `contribution_id = digest("graph-contribution-v1", family_id, extraction_id, layer_id, relation_fingerprint)`.
- Writes are idempotent: nodes keyed by `(contribution_id, external_id)`; bounded transactions use the existing `_MAX_NATIVE_OPERATIONS` (`adapters/ladybug/generation.py`).
- `Entity` primary key `(family_id, entity_id)`; an entity is visible when at least one visible `GroundedIn` edge points from it.
- Compaction runs when retired contributions exceed 10% of ready ones, or daily; it holds the writer for at most one transaction at a time.
- Tool limits: `neighbours` depth ≤ 3, per-predicate limit ≤ 20, limit ≤ 100; `impact` depth ≤ 5, fan-out ≤ 50, limit ≤ 500; hubs limit 1..50 default 20.
- Code edge weight defaults: calls out 1.0 in 0.8 limit 5; imports out 0.5 in 0.4 limit 3; inherits out 0.7 in 0.6 limit 3.

## Review Focus

1. **A new member's call edge points to a function defined in a member not yet published (or later removed).** Expect the edge kept, invisible to traversal until the target is visible, and left alone by compaction; pinned in Task 4.
2. **A crash after half a contribution's transactions committed.** Expect the contribution `building`, invisible, and completed by a retried write without duplicates; pinned in Task 3.
3. **A qualified name matching two entities in scope** (`utils.parse` in two packages). Expect the candidates returned instead of a graph; pinned in Task 7.
4. **An impact query on a hub with 5,000 callers.** Expect `truncated_by: fan_out` with the cut nodes listed, bounded latency, never an unbounded expansion; pinned in Task 8.
5. **A narrowed scope reading hubs or neighbours.** Expect degrees summed over eligible contributions only and no node from outside the scope; pinned in Tasks 7 and 9.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/tychora/contracts/graph.py` (modify; delete `GraphManifest` `:138-175`, `GraphGenerationSpec` `:29-56`) | `contribution_id`, `GraphContribution`, `NeighboursRequest`/`Response`, `ImpactRequest`/`Response`, `HubsRequest`/`Response` |
| `src/tychora/ports/graph.py` (rewrite) | `GraphStore`: `write_contribution`, `contribution_receipt`, `compact`, `expand`, `structural_context`, `get_assertions`, `neighbours`, `impact` |
| `src/tychora/adapters/ladybug/generation.py` → `src/tychora/adapters/ladybug/live.py` (rename and rewrite) | Live graph per family, contribution writes, compaction |
| `src/tychora/adapters/ladybug/traversal.py` (modify `:206-891`) | Visibility filter by contribution set; neighbours and impact |
| `src/tychora/indexing/coordinator.py` (delete `_graph_slot` `:769-883`; add `_graph_contribution`) | Member-local contribution build |
| `src/tychora/adapters/sqlite/publication.py` | Verify only the new contribution; drop `snapshot_root_batches` handling |
| `src/tychora/resources/migrations/0001.sql` | `graph_degrees`; drop `snapshot_root_batches` from the schema and the `snapshot_batches` view |
| `src/tychora/application/maintenance.py` | `graph-compact` job, health drift report |
| `src/tychora/retrieval/graph.py` (`augment` `:35`) | Edge weights multiply graph RRF votes |
| `src/tychora/profiles/documents/relations/{calls,imports,inherits}.toml` | `weights` and `limits` per direction |
| `src/tychora/interfaces/mcp/tools.py`, `src/tychora/interfaces/http/routes.py`, `src/tychora/clients/sync.py`, `src/tychora/clients/async_.py` | `neighbours`, `impact`, hubs |

---

### Task 1: Prove Ladybug behaviour and choose the path

**Files:** Create `tests/qualification/test_ladybug_live_graph.py` (marked `qualification`, runs in the default suite only when `ladybug` is importable — it is part of the `graph` extra installed in CI).

Tests: `test_read_connection_runs_while_write_transaction_open` (two connections on one `Database`; the read completes before the write commits and sees pre-write state); `test_traversal_p95_after_compacting_half_of_contributions` (10,000 synthetic contributions, compact 50%, 200 traversals; p95 within the graph budget share of §4.12's 70 ms parallel row); `test_wal_replay_after_kill` (subprocess killed mid-transaction; reopen succeeds).

Decision step: record the outcome in `docs/performance.md` ("Graph store behaviour") and in this plan's expanded version: all green → Ladybug live graph; read overlap fails → fallback 1; replay or latency fails → fallback 2 (Tasks 2–5 then target the relational adapter with the same port). Commit `test: qualify Ladybug for incremental graph writes`.

### Task 2: Contribution contract, identity and port

**Files:** `src/tychora/contracts/graph.py`, `src/tychora/ports/graph.py`; tests `tests/contracts/test_graph_contribution.py`.

**Interfaces:** Produces `contribution_id(family_id, extraction_id, layer_id, relation_fingerprint) -> str`, `GraphContribution(contribution_id, family_id, extraction_id, layer_id, evidence_ids, entity_ids, assertion_ids, node_count, edge_count, digest)` (artifact media type `application/vnd.tychora.graph-contribution.v1`, canonical JSON), `GraphStore.write_contribution(contribution, units, assertions, ctx) -> GraphContributionReceipt`, `GraphStore.contribution_receipt(contribution_id) -> GraphContributionReceipt | None`, `GraphStore.compact(retired: tuple[str, ...], ctx) -> CompactionReport`.

Tests: `test_contribution_id_binds_family_extraction_layer_and_relations`; `test_contribution_artifact_is_canonical_and_member_local`; port-neutrality test stays green.

Commit `feat: define graph contributions`.

### Task 3: Live graph writes per contribution

**Files:** `src/tychora/adapters/ladybug/live.py` (from `generation.py`: keep `_transaction` `:191-205` bounded loop; nodes and edges carry `extraction_id` and `contribution_id`; `Entity` keyed by `(family_id, entity_id)`; state `building` → `ready` when the last transaction commits and read-back counts match), `src/tychora/indexing/coordinator.py` (`_graph_contribution(bundle, family, ctx)` reads only the member's units and assertions; written on the writer lane after lexical and vector batches, before publication; registered as a `graph` slot batch through `BatchRepository`).

Tests (`tests/storage/test_graph_contributions.py`): `test_add_writes_only_new_member_nodes_and_edges` (counts read back equal the artifact); `test_retry_after_crash_is_idempotent` (Review Focus 2; crash probe in `tests/acceptance/graph_crash_probe.py` updated); `test_contribution_cost_independent_of_corpus` (write time at 10 and 1,000 members within 1.5×).

Commit `feat: write graph contributions incrementally`.

### Task 4: Visibility by eligible contributions

**Files:** `src/tychora/adapters/ladybug/traversal.py` (`_ready` `:255`, `_fetch` `:403`, `_iter_edges` `:479`: filter `Evidence`, `RelationAssertion` and edges by `view.selected_batches["graph"]`; skip when `view.graph.all_eligible`), `src/tychora/retrieval/compiler.py` (fills `selected_batches["graph"]` and `GraphBinding`).

Tests: `test_next_query_sees_new_relations` (E11 freshness); `test_building_contribution_invisible`; `test_unpublished_member_contribution_invisible`; `test_dangling_cross_member_edge_expands_once_target_visible` (Review Focus 1); `test_all_eligible_skips_filter` (query plan spy).

Commit `feat: filter graph traversal by eligible contributions`.

### Task 5: Publication binds contributions; drop the transitional root batch

**Files:** `src/tychora/adapters/sqlite/publication.py` (verify only the new contribution receipt; remove `GraphBatchReceipt` generation checks and the decision-watermark comparison moves into the contribution's `relation_fingerprint`), `src/tychora/contracts/publication.py` (remove `root_batches` and `graph_watermark`; `snapshot_id_v2` drops `root_batch_ids`), `0001.sql` (drop `snapshot_root_batches` and its `UNION ALL` branch in `snapshot_batches`), checksum; `src/tychora/indexing/coordinator.py` (delete `_graph_slot` and `reuse_existing`'s generation rebuild `:1244-1266`), `src/tychora/application/recovery.py` (graph native rebuild replays contributions from artifacts), delete `read_graph_build` callers.

Tests: `test_publication_never_reads_other_members_graph` (spy on unit reads); `test_reused_contribution_is_never_reopened`; `test_snapshot_identity_binds_contribution_through_page`.

Commit `refactor: bind graph contributions through membership pages`.

### Task 6: Compaction and degree table

**Files:** `0001.sql` `graph_degrees(family_id TEXT NOT NULL, entity_id TEXT NOT NULL, contribution_id TEXT NOT NULL, predicate TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('in','out')), edges INTEGER NOT NULL CHECK(edges > 0), PRIMARY KEY(family_id, entity_id, contribution_id, predicate, direction)) STRICT` plus index `(family_id, contribution_id)`; degree rows written in the contribution's writer task (`AggregateRepository.add_degrees`); `src/tychora/application/maintenance.py` `graph-compact` job (retired contributions from phase 2 GC → `DETACH DELETE` in bounded transactions keyed by `contribution_id`; orphan entities removed; `CHECKPOINT`; 100-entity random drift check reported by `manage health`); scheduler trigger at retired > 10% of ready, or daily; CLI `tychora manage graph-compact`.

Tests (`tests/operations/test_graph_compaction.py`): `test_compaction_removes_retired_contributions_and_orphan_entities`; `test_compaction_leaves_query_results_unchanged`; `test_compaction_keeps_dangling_edges_of_unpublished_targets`; `test_degree_drift_check_reports_mismatch`; `test_queries_interleave_with_compaction` (query latency during compaction ≤ one transaction).

Commit `feat: compact retired graph contributions in the background`.

### Task 7: Neighbours tool and endpoint

**Files:** `src/tychora/contracts/graph.py` (`NeighboursRequest`, `NeighboursResponse` with `nodes`, `edges` (`reason` strings such as `CALLS f`), `truncated` per predicate, `depth_reached`, `snapshot_id`; `AmbiguousTarget` candidates response), `traversal.py` (`neighbours`), `src/tychora/application/inspection.py` (new subject `neighbours`, target resolution: unit ID, entity ID or qualified name within scope), MCP tool `neighbours`, HTTP `POST /v1/relations/neighbours`, clients.

Tests (`tests/transports/test_neighbours.py`): `test_neighbours_both_directions_depth_two`; `test_per_predicate_truncation_flags`; `test_ambiguous_qualified_name_returns_candidates` (Review Focus 3); `test_scope_hides_out_of_scope_nodes` (Review Focus 5); `test_mcp_and_http_return_same_object`; fixture recording.

Commit `feat: add the neighbours relation tool`.

### Task 8: Impact analysis tool and endpoint

**Files:** `traversal.py` (`impact`: breadth-first incoming closure over `calls`, `imports`, `inherits` by default; `fan_out` caps edges per node; `limit` caps nodes; each node carries depth and a `reason` path such as `f ← CALLED_BY g ← CALLED_BY h`; response `depth_reached`, `truncated_by: depth|fan_out|limit|null`, `cut_nodes`), MCP tool `impact`, HTTP `POST /v1/relations/impact`, clients.

Tests (`tests/transports/test_impact.py`): `test_transitive_incoming_closure_with_reason_paths`; `test_fan_out_cut_lists_nodes` (Review Focus 4, 5,000 synthetic callers; latency bound); `test_limit_and_depth_truncation_reported`; fixture recording.

Commit `feat: add the impact analysis tool`.

### Task 9: Hubs endpoint

**Files:** `routes.py` `GET /v1/relations/hubs` (spec §4.15: sums `graph_degrees` over the view's graph contributions, ordered by degree desc then `entity_id`, cursor paging; graph disabled → `CAPABILITY_MISSING` reason `graph.disabled`), `AggregateRepository.hubs(view, query)`.

Tests: `test_hubs_sum_degrees_over_eligible_contributions` (Review Focus 5); `test_hubs_paging_stable`; `test_hubs_graph_disabled_reason`; `test_hubs_touch_no_graph_store` (spy); fixture recording.

Commit `feat: serve the most connected entities`.

### Task 10: Edge weights

**Files:** relation TOML schema in `src/tychora/configuration/registry.py` (optional `[weights] out=…, in=…` and `[limits] out=…, in=…`), `src/tychora/profiles/documents/relations/{calls,imports,inherits}.toml` (code defaults above), `src/tychora/retrieval/graph.py:35` (`augment` multiplies RRF votes by the predicate-direction weight and caps per-predicate expansions by the limit).

Tests (`tests/retrieval/test_graph_policy.py`, extend): `test_weights_multiply_graph_votes`; `test_limits_cap_expansion_per_predicate`; `test_missing_weights_default_to_one`.

Commit `feat: weight graph votes per predicate and direction`.

### Task 11: Gate and ratchet

Steps: E11 gate (`python -m tychora.interfaces.bench run --corpus bench-small --suite scale --profile offline` with the graph-enabled synthetic code corpus: graph bytes per add at 10,000 ≤ 2× at 100); Task 1 latency test green on the chosen path; `uv run --locked python -m tools.health check` and `update` (per-add generations, `_graph_slot`, `GraphManifest` deleted). Commit `chore: record phase 6 benchmark and health baseline`.

## Self-review (outline level)

- Spec coverage: §4.5 live graph, contributions, visibility, compaction, degree table, Ladybug proof and fallbacks → Tasks 1–6; §4.14 neighbours, impact, edge weights → Tasks 7, 8, 10; §4.15 hubs → Task 9; E11 → Tasks 4, 11; risk 8 (dangling edges) → Tasks 4, 6.
- Names match the index's "Produced by phase 6" block; `snapshot_root_batches` from phase 2 is removed in Task 5.
