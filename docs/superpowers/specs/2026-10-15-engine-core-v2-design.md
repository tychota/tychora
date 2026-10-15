# Engine core v2

Date: 2026-10-15. Status: design, awaiting review. Sub-project 1 of the Tychora v2 programme (`2026-10-15-tychora-v2-program-design.md`, "the programme"). Findings are cited by the identifiers of the flow review (`docs/superpowers/reviews/2026-10-15-ingestion-query-flow-review.md`). Line references are to `main` at `b91d808`.

## 1. Intent and success criteria

Make every add and every query cost proportional to the work it adds or reads, not to the corpus. Keep the semantics the flow review lists under "What to keep" and fix the storage, eligibility and threading layers behind them. Engine core also owns the contracts the other sub-projects consume: boundary keys and section levels, vector family options, query trace, usage ledger, aggregates, background phases, the engine endpoints the dashboard reads, the repository layer, the error reason taxonomy, and the benchmark corpus and harness. Sub-project 0 (code health) only enforces gates over the repository layer and the error taxonomy.

| # | Criterion (measured on benchmark corpus `bench-v1`, reference machine, §6) | Programme |
| --- | --- | --- |
| E1 | Warm query p95 < 1 s, including Voyage query embedding and rerank. Local share p95 < 300 ms. The per-stage budgets in §4.12 hold at p95. | §5.1 |
| E2 | Native PDFs of 100 pages or fewer: ingestion p90 < 30 s. Scanned documents and books are reported per page against §4.12. | §5.1 |
| E3 | 10,000 documents publish without a size error. The bytes written by publication per add are O(1) in corpus size: at 10,000 members they stay within 2× of the bytes written at 100 members. No snapshot artifact exceeds 1 MiB. | §5.1 |
| E4 | Search latency at 10,000 documents stays within 1.5× of latency at 1,000 for the same query set. | §5.1 |
| E5 | The strict default profile answers "verification outcome" over 20, 1,000 and 10,000 documents, and passes the 2026-10-15 real-PDF probes. | §5.2 |
| E6 | Retrieval metrics on the existing evaluation set are equal to or better than the `b91d808` baseline, per stage (lexical, hybrid, reranked). | §5.3 |
| E7 | Every query run stores a trace and every provider call writes a usage row. A first-time user can open both from the dashboard (consumed by sub-project 6). | §5.4 |
| E8 | Each engine-core parity item in §4.14 has a passing test. | §5.5 |
| E9 | Code-health ratchet in §7 is met. | §5.5 |
| E10 | Each dashboard-facing endpoint in §4.15 has a contract test, and its response is recorded as a dashboard fixture. | §5.4 |
| E11 | Graph freshness: after a publication that adds relations, the next query sees them (no stale window), and the graph bytes written per add are O(1) in corpus size, measured like E3. | §5.1 |

## 2. Scope and non-goals

In scope:
- The snapshot and publication format, eligibility, read connections and the threading model.
- Vector batches and vector family options (dimension, quantization, local query encoder).
- Incremental graph writes at publication and background graph compaction.
- The store-agnostic repository layer, starting with the modules rewritten here, and the error reason taxonomy.
- Boundary keys and level-tagged section paths for all normalizers.
- The engine endpoints and MCP tools the dashboard and the replaced projects need (§4.14, §4.15).
- Linear windowing (the counting algorithm only; chunk policy belongs to chunking v2).
- The read-path validation removal, lexical stop words, and the C5 defects.
- The shared contracts listed in §1, the rebuild and maintenance paths, and the engine-core parity items.

Non-goals:
- Chunk sizes and semantic cuts (sub-project 2).
- Model choices (sub-project 3).
- Docling enrichers (sub-project 4).
- Kinds and presets (sub-project 5).
- UI (sub-project 6).
- PostgreSQL, Qdrant and Neo4j adapters. They are sub-project 7 ("Networked stores"). This design makes them possible (§4.13, §4.16) but implements none.
- Generated answers.
- Concrete domain connectors beyond one reference implementation.
- Migrating old workspaces in place.

## 3. Current state

**Snapshot manifest (D1).**
- `PublicationBatch` carries every window's lexical text and every vector binding (`contracts/publication.py:41-89`).
- `_manifest_document` dumps the whole plan into the manifest (`adapters/sqlite/publication.py:60-86`).
- `finalize` reuses earlier batches whole (`indexing/coordinator.py:1066-1072`), so each manifest repeats the corpus.
- `_parse_checked` rejects anything above `MAX_MANIFEST_BYTES = 16 MiB` (`adapters/files/references.py:20,70`). This was observed on real PDFs: 2.7 → 9.7 → 15.9 → 23.6 MiB over four documents, then refusal (`docs/superpowers/2026-10-15-large-document-limits.md`).
- `MAX_REFERENCES = 10000` (`references.py:22`) is a second ceiling: today the manifest lists one embedding artifact per vector (`publication.py:66-78`).

**SQL bindings.** `snapshot_members` and `snapshot_batches` get one row per member and per member-slot for every snapshot (`publication.py:425-458`). Snapshots are never deleted: there is no `DELETE FROM snapshot*` anywhere. Row count is therefore quadratic in the number of adds.

**Publication (D2).**
- `_verify_batches` re-hashes every embedding file and calls `verify_batch` for every vector batch of every member (`publication.py:152-286`).
- Publication then runs one SELECT per lexical and vector record (`publication.py:647-673`).
- `pin_snapshot` pins every batch and every embedding artifact on each read (`adapters/sqlite/corpus.py:193-226`).

**Graph (D3).** `_graph_slot` reads every unit of every member and seals a full generation per add (`indexing/coordinator.py:769-883`). Its `GraphManifest` inlines all evidence and assertion IDs (`contracts/graph.py:138-143`). Code entity IDs are unit IDs (`adapters/code/normalize.py:726-777`), resolved within one capture (`adapters/code/resolve.py`).

**Ladybug (pinned `ladybug==0.20.3`).** The store already writes incrementally: `_transaction` runs bounded `BEGIN TRANSACTION … COMMIT` blocks of `CREATE` statements (`adapters/ladybug/generation.py:191-205`), and `drop_generation` deletes with `MATCH … DETACH DELETE` in bounded transactions (`generation.py:725-758`). The package exposes a write-ahead log with `auto_checkpoint` and `checkpoint_threshold`, and allows one read-write `Database` object per path (`ladybug/database.py`). What a full generation per add costs is the rebuild, not a missing write capability.

**Threading (D4/Q4).** `WorkspaceOwner` has one `BoundedWorker()` with 1 worker and a queue of 32 (`application/runtime.py:30`, `application/owner.py:128`). All of the following run on it: `run_blocking` calls, windowing (`coordinator.py:233-239`), image decoding, eligibility, rerank-fact loading and history.

**Boundary keys (D5).**
- Native, Docling and records normalizers set `boundary_key=unit.native_ref` (`ingestion/normalize.py:100`, `ingestion/documents.py:99`, `ingestion/document_records.py:367`).
- Code uses the file path (`adapters/code/normalize.py:283`).
- Context groups key on it (`indexing/windows.py:99`).

**Vectors (D6).** There is one CAS file per vector (`coordinator.py:494-499`). Record IDs are `digest("embedding-record-v1", window, input, space)` with no family (`coordinator.py:470-477`). Family `creation_uuid` is `uuid5(workspace, config)` (`coordinator.py:422`), and the lexical family ID derives from configuration (`coordinator.py:345`).

**Configuration on hot paths (D7).**
- The registry is dumped and hashed per query (`retrieval/eligibility.py:387-395`, `application/query.py:325-334`).
- It is re-read from the CAS per eligibility read (`corpus.py:109-137`).
- It is dumped per segment for the lane lookup (`retrieval/segments.py:56-79`).

**Rebase (D8).** `finalize` raises `INDEX_INCOMPLETE` when earlier members lack a batch in the new family (`coordinator.py:1061-1069`). No maintenance operation re-batches them.

**Eligibility (Q1/Q2/Q3).**
- `read_eligibility_facts` loads every unit, locator, asset and chunk of every member, and runs `prepare_extraction` per member (`adapters/sqlite/eligibility.py:216-603`, `:377`, unwrapped: C3).
- It issues per-unit review and facet queries.
- The compiler materialises corpus-wide ID sets (`contracts/graph.py:188-204`).
- `eligible_cache_key` has no callers (`retrieval/eligibility.py:8-16`).
- `resolve_snapshot` runs three times per query (`publication.py:686-779`).
- Chroma search queries every eligible ID partition after fetching presence for all of them (`adapters/chroma/index.py:306-449`).
- `_vector_owners` and `_candidate_lanes` issue one SELECT per record or candidate (`application/query.py:1036-1062,1169-1197`).

**Reranking (C2/Q5).**
- `prepare_rerank_facts` re-validates each candidate's extraction and runs `PRAGMA table_info` per window (`application/reranking.py:719-831`).
- The result cache pickles `RerankFacts` holding whole extractions (`retrieval/cache.py:114-115`).

**Windowing (C1).** `_split_interval` re-counts tokens from the cursor for every boundary (`indexing/windows.py:52-79`).

**Lexical (C4/C5/Q6).**
- `LexicalIndex(owner.db)` is built without stop words (`application/query.py:238`).
- The cap fix is on main: `complete=True` with the comment "All eligible rows were scored" (`adapters/sqlite/lexical.py:115-116`, commit `a285b7d`, test `tests/retrieval/test_lexical.py:189-213`).
- The exact channel still reports `complete=len(hits) <= limit` (`lexical.py:185`), so strict mode still fails on over 32 identifier hits.

**Other C5 defects.**
- Editions: `select_temporal_revisions` warns whenever more than one edition is known even with a unique winner, and keys the order on `start or published` (`retrieval/eligibility.py:95-106`). The query turns that warning into decisions (`application/query.py:1012-1020`).
- Purge: `purge-history` and `purge-cache` have no maintenance branch (`application/maintenance.py:90-315`).
- Busy owner: client delegation turns the missing `authority` into `INTEGRITY_ERROR` (`docs/review-assessment.md` item 5).
- Trace: `_deliver` never sets `trace` (`application/query.py:1352-1388`).

**Real-data fixes already on main, to keep:**
- Excerpt ranges on picture and table separators: `retrieval/segments.py` (`f070323`, `d4eb75f`), tested in `tests/retrieval/test_segments.py:1346,1383`.
- Upload authorization batching: `application/sources.py:99-137` (`409d0bb`), tested in `tests/application/test_upload_batches.py`.
- Docling formula `orig` spans: `contracts/documents.py` (`e2d2091`), tested in `tests/ingestion/test_docling_mapping.py:653`.
- Neighbour context and adjacent-result merging for Docling documents. `_neighbor_context` and `_coalesce_boundary_matches` skip every unit with a `DocumentLocator` (`retrieval/segments.py:392,640,659`). The programme fixes this test-first on `main`, immediately and separately (programme §2: before sub-project 2 at the latest). Engine core keeps the fix and its regression tests in `tests/retrieval/test_segments.py`; the boundary-key test in §6 runs on a Docling fixture too.

**Dashboard reads.** `GET /v1/documents` has no title filter or sort, `GET /v1/usage` does not exist, there is no query-latency or graph-degree endpoint, `GET /v1/assets/{id}/content` loads the whole payload in memory up to 16 MiB (`interfaces/http/routes.py:534-547`, `application/inspection.py:48`), the code capture records `commit` and `dirty` but not the branch or language counts (`contracts/acquisition.py:48-57`), and no endpoint tests a provider connection.

**Layering.** `application/` issues 210 `.execute(` calls (`grep -c`), and `indexing/coordinator.py` issues 17. `INTEGRITY_ERROR` appears 382 times (380 raising sites, code health §3.8) with no sub-code.

**Measured symptom.** A workspace of about 22,000 units runs strict online searches in 15–31 s (`large-document-limits.md` item 6).

## 4. Design

### 4.1 Principles

1. **Validate at write, trust at read.** `commit_extraction` and publication prove structure once and persist what read paths need. Reads re-check only policy (withdrawal, access, epoch) and, cheaply, the identity of rows they fetch.
2. **SQL is the binding.** The snapshot is a set of SQL rows. Artifacts are retention and backup documents, never parsed on the query path.
3. **Structural sharing.** Unchanged parts of a snapshot are referenced, not copied.
4. **One writer, many readers.** The writer thread owns mutations and native handles. Queries read through a WAL read pool.
5. **Store-agnostic ports.** Ports speak in value objects, IDs and cursors. SQLite, Chroma and Ladybug specifics stay inside their adapters, so a PostgreSQL, Qdrant or Neo4j adapter can implement the same ports (§4.13, §4.16).

### 4.2 Snapshot format v2: membership pages (D1)

Members are partitioned into 256 **buckets**: `bucket = int(sha256(source_id)[:2], 16)`. A **page** is the immutable binding of one bucket's members and their batches, including each member's graph contribution (slot `graph`, §4.5). A **snapshot** is the set of up to 256 page IDs plus slot families. An add rewrites one page and the root.

At 10,000 documents a page holds about 40 members, roughly 40 KB, and the root about 30 KB. At 100,000 documents a page holds about 400 members and stays under `MAX_REFERENCES`.

SQL (new migration `0001.sql` of format 2):

```sql
CREATE TABLE membership_pages (
  page_id TEXT PRIMARY KEY,               -- digest("membership-page-v2", bucket, members, batches)
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  member_count INTEGER NOT NULL
) STRICT;
CREATE TABLE page_members (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  source_id TEXT NOT NULL, revision_id TEXT NOT NULL, extraction_id TEXT NOT NULL,
  is_default INTEGER NOT NULL, PRIMARY KEY(page_id, revision_id)) STRICT;
CREATE TABLE page_batches (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  slot TEXT NOT NULL, batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  PRIMARY KEY(page_id, slot, batch_id)) STRICT;
CREATE TABLE snapshot_pages (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  bucket INTEGER NOT NULL, page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  PRIMARY KEY(snapshot_id, bucket)) STRICT;
CREATE VIEW snapshot_members AS SELECT sp.snapshot_id, m.* FROM snapshot_pages sp JOIN page_members m USING(page_id);
CREATE VIEW snapshot_batches AS SELECT sp.snapshot_id, b.slot, b.batch_id FROM snapshot_pages sp JOIN page_batches b USING(page_id);
```

`snapshots` gains `job_id` and `member_count`; `snapshot_indexes` (slot → family) is kept and now includes the graph family. Each page artifact (`type: "membership_page"`, `schema_version: "2"`) contains:
- members;
- per member and slot: `{batch_id, family_id, count, receipt_digest, input_manifest}`;
- `required_manifests` = the page's batch input manifests;
- `required_artifacts` = the page's vector batch artifacts (§4.4).

The root (`type: "snapshot"`, `schema_version: "2"`) contains workspace, base, job, `config_sha`, families, `membership_sha`, member count, and the 256 page refs as `required_manifests`. Neither document holds lexical text, per-vector digests or graph ID lists. Per-vector receipt rows move into the vector batch artifact, and graph ID lists into the graph contribution artifact (§4.5).

Identity:

```
snapshot_id = digest("snapshot-v2", workspace_id, base_snapshot_id, job_id, config_sha, families, sorted(page_ids))
```

Page IDs bind members and batch IDs (graph contributions included) transitively, so binding strength equals `snapshot-v1`. Source, revision, extraction, unit and chunk identities are unchanged ("What to keep").

**Relational shape.** The four tables are plain relational tables with text keys, an integer bucket and composite primary keys; nothing depends on SQLite beyond the `STRICT` keyword. Pages are content-addressed, so inserting a page is idempotent (`INSERT … ON CONFLICT DO NOTHING` in both SQLite and PostgreSQL). The two views are ordinary joins. §4.16 gives the PostgreSQL mapping.

`MAX_MANIFEST_BYTES` stays at 16 MiB as a guard. Publication fails with `INTEGRITY_ERROR` reason `invariant.page_oversize` if a page exceeds 4 MiB. That would need about 4,000 members in one bucket, roughly 1M documents.

**Snapshot retention.** A snapshot is retained when any of these holds:
- it is active;
- it is leased;
- it is referenced by an unexpired `query_runs` row or an evaluation run;
- it is among the last 16 published.

`manage gc` and a bounded post-publication step (at most 64 snapshots per publication) delete unretained `snapshot_pages`, then orphan pages, then orphan batches. Deleting an orphan batch deletes its `lexical_records` rows (FTS delete trigger) and its Chroma records, marks an orphan graph contribution `retired` for graph compaction (§4.5), and releases its artifacts to CAS GC. This keeps `lexical_fts` proportional to retained content.

### 4.3 Publication (D2, D7)

The `PublicationPlan` v2 contains: job, base, `config_sha`, families, `new_pages`, `reused_page_ids`, `new_batches` (full receipts), and the graph binding. `publish_snapshot` runs:

1. **Outside the transaction.** Verify only `new_batches`:
   - lexical: entry digest against rows staged by the batch writer;
   - vector: batch artifact digest, row count, `value_sha` per row, and `verify_batch` for that batch only;
   - graph: the new member's contribution receipt (node and edge counts and digest, §4.5).

   Reused batches are checked in one statement: `BatchRepository.count_ready(batch_ids)` must equal their number. The SQLite adapter binds the list through `json_each(?)`; a PostgreSQL adapter would use `= ANY($1)`.
2. **Publication transaction** (`BEGIN IMMEDIATE` in SQLite; kept, together with the base-snapshot comparison and the job, workspace, target and withdrawal checks from `publication.py:577-646`). Ownership is checked set-based: the new chunk and asset IDs are bound as one list, then joined once against `chunks` and `assets` grouped by `extraction_id`. Per-row SELECTs are removed.
3. Insert pages, `snapshot_pages`, `snapshots` and the aggregates delta (§4.10), then switch the active snapshot.

Lexical rows are inserted when the lexical batch is written. `index_batches.state` goes `building` → `ready` at that point, which fixes the "inserted ready" item in `docs/review-assessment.md`. The FTS epoch is bumped once per batch by the repository, not once per row: the per-row `UPDATE workspaces` is removed from the `lexical_insert` and `lexical_delete` triggers.

Configuration (D7):
- `ResolvedConfig` gains `retained_sha`: the sha256 of its canonical retained registry document. It is computed once in `resolve_config`, and is the value stored as `snapshot.config_sha` and `job.effective_config_sha`.
- Hot paths compare strings only.
- The registry artifact is written once per distinct `retained_sha`. The repository checks `artifacts` before serialising.
- `segments._lane` and `_role` use `registry.lane_bindings` and `registry.kinds`.

Whole-corpus verification moves to `manage health --verify-payloads --verify-native`, which runs as a cancellable job that reports per batch (existing `HealthMaintenance`, `application/maintenance.py:124-157`).

### 4.4 Vector batches and family identity (D6, C5)

A vector batch artifact, media type `application/vnd.tychora.vector-batch.v1`, is laid out as:
- the magic bytes `TYVB0001`;
- a `u32` header length;
- a canonical JSON header `{family_id, dimension, records: [{record_id, input_sha, value_sha}] sorted by record_id}`;
- `count × dimension` little-endian float32 rows in header order.

The artifact always holds float32 rows at the family's dimension, whatever the family's index quantization (below). That keeps native rebuild provider-free and gives the full-precision rows used for rescoring. `value_sha` stays the sha256 of one row's float32 bytes, so receipt digests keep their meaning. Schema changes:
- `vector_records.embedding_cache_sha` becomes `vector_records.row_index`;
- `index_batches` gains `vectors_sha`.

Cache semantics are preserved:
- Native rebuild (`manage rebuild native`, `application/recovery.py:249-320`) reads rows from the batch artifact without provider calls.
- Rows are verified against `value_sha`.
- A missing artifact yields `CAPABILITY_MISSING` with reason `vectors.cache_absent`.

The cost becomes one write and two fsyncs per batch, and one verified open per batch on rebuild.

Identity:
- Families get a persisted creation nonce. The new table `index_families.creation_nonce` is set from `new_family_nonce()` (`contracts/identity.py:122`, no longer dead) when a family is created by `manage rebuild index` or first use.
- `family_id = digest("family-v1", workspace_id, config_sha, nonce)` for lexical, text, image and graph alike.
- Vector record IDs are `vector_record_id(family_id, chunk_or_asset_id, input_sha)` (`identity.py:111-114`), so two families in one space cannot collide.

Chroma record metadata gains `revision_id`, `extraction_id`, `owner_kind` (`unit` or `asset`) and `owner_id`.

**Family options.** Sub-project 3 chooses the models; engine core owns the family contract that carries them. A vector family definition is:

```python
class VectorFamilySpec(Contract):
    role: Literal["text", "code", "image"]
    space_id: str                     # embedding space, from the model registry
    document_model: str               # e.g. "voyage-context-4", "voyage-code-4", "voyage-4-large"
    query_encoder: QueryEncoder       # hosted model id, or a pinned local asset
    dimension: Literal[256, 512, 1024, 2048]
    index_dtype: Literal["float32", "int8", "binary"]
    rescore_top: int | None           # full-precision rescoring depth; required for binary, default 100
    contextual: bool                  # document chunks embedded per context group

class QueryEncoder(Contract):
    kind: Literal["hosted", "local"]
    model: str                        # hosted model id, or local asset id (e.g. "voyage-4-nano")
    asset_sha256: Sha256 | None       # required when kind == "local"
```

- Every field except `role` and `rescore_top` is an input to `config_sha`, and therefore to `family_id`. Changing any of them creates a new family and needs `manage rebuild index` (§4.11). `rescore_top` only sets query-time rescoring depth: it enters the query fingerprint, not the family identity, so changing it needs no rebuild.
- **Space rule.** The model registry assigns each model one `space_id`. A family is valid only when its document model and its query encoder have the same `space_id`. `voyage-4-large`, `voyage-4`, `voyage-4-lite` and `voyage-4-nano` share the `voyage-4` space, as Voyage documents. `voyage-context-4` and `voyage-code-4` each have their own space, because no Voyage source documents that they share the `voyage-4` space. Configuration that mixes spaces is refused with `PROFILE_INCOMPATIBLE` reason `family.space_mismatch`. Vector record IDs already include `family_id`, so two families never mix rows in one index.
- **Default text family:** `voyage-context-4`, contextual, hosted query embedding through `voyage-context-4`. **Optional "fast/offline query" text family:** documents embedded with `voyage-4-large` (not contextual), queries embedded locally with the open-weight `voyage-4-nano` (Apache-2.0), pinned by `asset_sha256` and verified at load (`INTEGRITY_ERROR` reason `model_asset.mismatch`). **Code family:** `voyage-code-4`, hosted query embedding.
- **Local query encoder.** It runs on the CPU lane (§4.6) through a `LocalQueryEncoder` port implemented by an ONNX adapter. It is loaded once per owner and kept warm. Its calls write usage rows with `provider='local'` and `operation='embed_query'` through `run_local_text` (§4.10). A missing or unverified asset makes the text channel `unavailable` with `CAPABILITY_MISSING` reason `model_asset.absent`, naming `manage setup`; it never falls back silently to a hosted model, because a hosted fallback would send the query off the machine.
- **Dimension and quantization.** Matryoshka truncation is requested from the provider (`output_dimension`), never done locally, so stored rows are exactly what the provider returned. The vector index adapter declares which `index_dtype` values it supports natively. For `int8` and `binary` the adapter stores quantized vectors in the index and rescores the top `rescore_top` candidates against the float32 rows of the batch artifact, read through the read pool. An adapter without native support for a requested `index_dtype` refuses the family at configuration time with `PROFILE_INCOMPATIBLE` reason `family.dtype_unsupported`; nothing is emulated silently. The Chroma adapter declares `float32` only, so int8 and binary become available with a store that supports them (sub-project 7, §4.16).
- **Choosing defaults.** The default `dimension` and `index_dtype` per family are chosen by evaluation, not fixed here. The harness (§4.10) gains a `--suite families` run that, for each candidate `(dimension, index_dtype, rescore_top)`, records storage bytes per vector, text ANN p50 and p95, and nDCG@10 and recall@50 on the evaluation set, and reports them side by side. Until a report is accepted, families use `dimension = 1024` and `index_dtype = "float32"`.

### 4.5 Graph (D3): incremental writes at publication, background compaction

Graph stays disabled in the base profile and is enabled by presets that define relations (the software preset among them). Decision: the graph is never rebuilt on the add path. Each publication inserts only the new member's nodes and edges, and the next query sees them. Removing superseded content is a background compaction.

**One live graph per graph family.** The per-add generation is replaced by one live Ladybug graph per graph family. Every `Evidence` node, `RelationAssertion` node and edge carries the `extraction_id` and `contribution_id` that wrote it. `Entity` nodes are shared: their primary key is `(family_id, entity_id)`, and an entity is visible when at least one visible `GroundedIn` edge points from it.

**Contributions.** A member's graph data is one **contribution**, recorded as a batch of slot `graph` in `index_batches` (so membership pages bind it like any other batch, §4.2):

- `contribution_id = digest("graph-contribution-v1", family_id, extraction_id, layer_id, relation_fingerprint)`.
- Its artifact (`application/vnd.tychora.graph-contribution.v1`, canonical JSON) lists the evidence, entity and assertion IDs it wrote and their digest. This replaces the corpus-wide `GraphManifest`; no artifact lists the whole graph.
- It is written on the writer lane after the member's lexical and vector batches and before publication, as bounded Ladybug transactions (the existing `_MAX_NATIVE_OPERATIONS` bound). `index_batches.state` goes `building` → `ready` when the last transaction commits and the node and edge counts read back match the artifact.
- Writes are idempotent: nodes are keyed by `(contribution_id, external_id)`, so a retried write after a crash re-creates only what is missing.
- Cost is O(the member's assertions), independent of corpus size (E11).

**Visibility without a generation.** A query sees exactly the contributions of its pinned snapshot. Traversal filters every `Evidence`, `RelationAssertion` and edge by the eligibility view's extraction set (`selected_batches["graph"]`, §4.7), and skips the filter when every member is eligible. A `building` contribution, or a contribution of a member that is not in the pinned snapshot, is therefore invisible, so no MVCC is needed from the graph store. An edge from a new member to an entity grounded only in a member that is not visible is kept and expands once that member is visible; cross-member edges need no rebuild.

**Compaction.** A low-priority `graph-compact` maintenance job on the writer lane:
1. deletes the nodes and edges of `retired` contributions (§4.2 GC) with `MATCH … DETACH DELETE` in bounded transactions (the existing `drop_generation` loop, keyed by `contribution_id`);
2. deletes `Entity` nodes left with no `GroundedIn` edge;
3. runs a Ladybug `CHECKPOINT`;
4. checks the degree table (below) against the graph for one random sample of 100 entities and reports drift through `manage health`.

It runs when retired contributions exceed 10% of ready ones, or daily, and holds the writer for at most one transaction at a time, so queries interleave.

**Degree table for hubs.** Each contribution also writes rows `graph_degrees(family_id, entity_id, contribution_id, predicate, direction, edges)` in the relational store, in the same writer task. "Most connected" (§4.15) sums them over eligible contributions; nothing scans the graph store.

**Ladybug support and fallback.** The pinned Ladybug supports what this needs: explicit transactions, incremental `CREATE`, `MATCH … DETACH DELETE` and checkpoints (§3). Two behaviours are not yet proven and are covered by tests before the switch: that a read connection can run while a write transaction is open on another connection of the same `Database`, and that delete-heavy compaction keeps traversal latency within the graph budget. Fallbacks, in order:
1. If reads cannot overlap an open write transaction, graph reads run on the writer lane at interactive priority (where Chroma queries already run, §4.6). A query then waits at most one bounded write transaction.
2. If incremental writes or compaction prove unreliable in Ladybug (write-ahead log replay failures, or traversal p95 over budget after compaction), the graph moves to relational tables: `graph_nodes` and `graph_edges` keyed by `contribution_id`, traversed with a recursive CTE bounded by the depth and fan-out limits of §4.14. This uses the same `GraphStore` port, works in SQLite and PostgreSQL, and keeps the contribution model unchanged. Ladybug then stays an optional adapter.

There is no fallback to a periodic full rebuild.

### 4.6 Threads, connections and CPU placement (D4/Q4)

| Lane | Threads | Owns | Runs |
| --- | --- | --- | --- |
| Writer (existing owner worker) | 1 | read-write connection, Chroma handle, the Ladybug `Database` and its write connection, CAS mutations, maintenance barrier | commits, batch writes, graph contributions and compaction, publication, job states, history, trace, usage and aggregates writes; Chroma `query` and `upsert` |
| Read pool | `min(4, cpu_count)` | one `mode=ro`, `query_only` connection each, WAL; one Ladybug read connection each on the writer's `Database` | snapshot resolution, eligibility, exact and FTS, graph traversal, rerank facts, segments, inspection, dashboard reads (§4.15) |
| CPU lane | 2 | the warm local query encoder, when configured | tokenization and windowing, write-time validation (`prepare_extraction`), image decode and resize, response measuring, local query embedding (§4.4) |
| Parser processes (kept) | 2 | private copies | Docling, Tree-sitter |
| Provider lanes (kept) | 2 per provider | none | provider calls, after the writer-lane authorization (kept, `owner.py:513-561`) |

The writer queue has two priorities. Interactive work (Chroma queries, history writes) runs before background work (ingestion writes). Chroma upserts are partitioned into 512 records, and each partition is a separate background task. A query therefore waits at most one partition, about 50 ms, never a whole document. The native-handle owner stays single.

Queue bounds:

| Queue | Bound |
| --- | --- |
| Interactive | 256 |
| Background | 64 |
| Read pool | 128 |

`WORKSPACE_BUSY` (reason `queue.full`) only when a bound is exceeded.

Each read task opens a deferred `BEGIN`, so it sees one WAL snapshot, and ends with `ROLLBACK`. Snapshot pins are `LeaseSet` entries on `snapshot_id` only. GC retention (§4.2) honours them, and per-artifact payload pins are removed from the read path.

`WorkspaceOwner` keeps its public API. `run_blocking` becomes `run_write`, and `run_read` and `run_cpu` are added. Call sites are migrated by the repository split (§4.13).

### 4.7 Eligibility (Q1, Q2, Q3, C2, C3)

**Snapshot cache (Q2).** `SnapshotCatalog.get(snapshot_id)` returns a `SnapshotView` v2: IDs, families, `member_count` and page IDs, with no members. It is built from SQL and cached by `snapshot_id` (immutable, LRU of 32). Manifests are never parsed on reads.

**`EligibilityView` v2** (replaces `contracts/graph.py:188-238`):

```python
class EligibilityView(Contract):
    snapshot_id, policy_epoch, scope_digest, eligibility_policy_digest, eligibility_request_digest
    revisions: Mapping[str, RevisionGrant]       # revision_id -> extraction_id, kind_id, lane, collection_id
    excluded_unit_ids: frozenset[str]            # rejected/blocked/non-citable for this mode
    excluded_asset_ids: frozenset[str]
    unit_filter: frozenset[str] | None           # only when a unit-level facet filter is present
    selected_batches: Mapping[str, tuple[str, ...]]  # slot -> batch ids of eligible revisions; "graph" holds contributions
    graph: GraphBinding | None                   # graph family and whether every contribution is eligible
    def allows(self, dependency_ids: Iterable[str], kinds: Mapping[str, str]) -> bool
```

Its size is O(revisions + exclusions). `allows` replaces the six recomputed "allowed" unions (flow review §4.4).

**Compilation** runs on the read pool in one read transaction:

1. One query joins `snapshot_members` ⨝ `revisions` ⨝ `sources` ⨝ `collections` ⨝ `kinds` ⨝ `review_current`, giving one row per member.
2. Python predicates per revision: scope collections, access ranks and floors, allowed uses (§4.14 usage override), withdrawn and archived, temporal selection (§4.9), revision-level facets. Facets are loaded by one query on the requested fields only.
3. One query computes exclusions from `review_current` and `evidence_units.citable` for eligible extractions, with the mode's review states.
4. Unit-level facet filters use one indexed `facet_values` query per filter leaf.

`review_current(target_kind, target_id, state)` is maintained in the `record_review` transaction, which already bumps `policy_epoch` (`adapters/sqlite/policy.py:84-100`). Kind envelopes are digest-verified at registration and cached by `(kind_id, definition_hash)`.

Invariant, enforced by a test that enumerates repository write methods: every write that changes an eligibility input increments `policy_epoch` in the same transaction. That covers review, withdrawal, collection policy, floors, facet bindings, consent and usage overrides.

**Cache.** `eligible_cache_key` (`retrieval/eligibility.py:8-16`) is wired as `EligibilityCache`: LRU of 64 views keyed by `(snapshot_id, policy_epoch, scope_digest, policy_digest, request_digest)`. The request digest is computed over the eligibility-relevant request fields only (filters, temporal selector, review states, channels), so two different questions share a view.

**Candidate selection** uses store-side joins, not shipped ID sets. The ports (`LexicalSearch`, `VectorSearch`, `GraphStore`) take the `EligibilityView` and a limit; the temp tables, FTS5 syntax and Chroma `where` clauses below are details of the SQLite and Chroma adapters (§4.16 gives the PostgreSQL and Qdrant equivalents):

- **Lexical.** `lexical_fts MATCH ?` joins `lexical_records` and `chunks`. It is restricted to `snapshot_batches` for the pinned snapshot and lexical slot, and to a per-connection temp table `elig_ext(extraction_id)`. The temp table is filled once per cache key per connection and skipped when every member is eligible. Exclusions use `NOT EXISTS` against a temp table.
  - The query orders by `bm25` with `LIMIT 4000` matching windows, grouped to 80 owners per kind.
  - Hitting the window limit sets `truncated = true`. It never sets `complete = false`.
- **Exact.** Already a SQL join (`lexical.py:164-171`). It is restricted the same way, and `complete` becomes `True` with truncation reported (C5).
- **Vector.** Chroma is queried with `where={"owner_kind": k}` plus `revision_id $in` when there are 512 or fewer eligible revisions. Otherwise it over-fetches `k = 4 × limit`, with up to two doublings and a cap of 1,024. Survivors are mapped to owners in one SQL statement over `vector_records` (removes `_vector_owners`'s per-record loop) and filtered by the view. Per-query presence fetches (`_present`) are removed: presence is verified at activation and by health. For a quantized family the adapter first rescores its top `rescore_top` hits with the float32 rows (§4.4), and the trace's `text_ann` stage reports the count in `detail.rescored`.
- **Survivor check.** Dependencies persisted at write time (§4.8) are read for at most 200 candidates in one query and checked with `view.allows`. A candidate failing the check is dropped and counted in the trace as `dropped.dependency`.
- **Lanes.** Lanes come from `view.revisions[...].lane`, so `_candidate_lanes`'s SELECTs are removed.

**Typed errors (C3).** Read paths raise only `TychoraError`. Unexpected row shapes raise `INTEGRITY_ERROR` reason `canonical.row_invalid`. A guard at the read-pool boundary converts any other exception into `INTEGRITY_ERROR` reason `invariant.unexpected`, with a diagnostic ID.

### 4.8 Write-time representation facts (C2, Q3, Q5)

`commit_extraction` (`adapters/sqlite/corpus.py:262`) already validates the bundle. Validation is made linear and its results are persisted:

- `prepare_extraction`'s overlap computation (`indexing/representations.py:661-731`) becomes a sort-and-sweep over `(artifact, source_ref, start)` intervals, O(n log n). `_owners_by_locator` is deleted.
- New table `representation_dependencies(owner_id, dependency_id, PRIMARY KEY(owner_id, dependency_id))` for units, chunks, assets and vector groups. It holds what `read_eligibility_facts` derives today (`eligibility.py:377-478`).
- New table `ownership_intervals(unit_id, char_start, char_end, dependency_digest)`.

Read paths load per candidate: unit rows, chunk rows (explicit column list, no `PRAGMA`), intervals, and neighbours within the same `boundary_key` (index `units_extraction_boundary` exists, `0001.sql:473`). Cheap revalidation stays: the `text_sha` of each fetched unit, policy, and epoch.

`prepare_extraction` is called only at ingestion and by `manage health`. Its calls in eligibility, reranking, visuals, inspection and code-citation projection are removed (C2).

`RerankFacts` holds per-candidate rows only. The result cache stores `CachedSelection`: the selected segments, citations, coverage, warnings, decisions and trace, as canonical JSON bytes no larger than `max_response_bytes` plus 64 KiB, with no pickle (Q5).

### 4.9 Lexical profile, strict mode and editions (C4, C5, Q6)

**Strict mode.** Strict stays the default. A channel is `incomplete` only when its index is unavailable, a required batch is missing, or the deadline is exceeded. Truncation is reported as `truncated` in coverage and in the trace (Q6).

**Stop words.** The lexical profile gains `languages = ["en", "fr"]` and `stopwords = "builtin@1"`. Lists ship as `resources/stopwords/{lang}@1.txt`, with digests pinned into `query_fingerprint`. Removal is query-side only, so changing lists needs no rebuild. If removal empties the query, the original terms are kept (C4).

**Editions (C5).** `select_temporal_revisions` is rewritten:
- Order by `effective_from` when `as_of` is given, and by `published_on` when only `cutoff` is given. Ties break on `(published_on, edition_key)`.
- A warning and a `DecisionRequirement` arise only when two or more winners tie on the key, or when winners' `[effective_from, effective_until)` intervals overlap.
- Permissive unknown-date editions are not merged into winners. They are returned as `undated_candidates` with their own decision requirement.
- `revision_selector="applicable"` (new) returns every edition in force at `as_of`, with one decision requirement listing them.

### 4.10 Shared contracts

**Boundary keys (D5).** The format is `"<scheme>:<native_ref>"`. Schemes:

| Scheme | Assigned to | How the section is identified |
| --- | --- | --- |
| `file:` | code units (key = relative path), and text with no headings | path |
| `section:` | native Markdown and text units | the nearest preceding ATX or Setext heading unit whose level is at or above the unit's (the existing `heading_path` computation); `section:#root` before the first heading |
| `section:` | Docling units | the nearest enclosing `SectionHeaderItem` in reading order, using the normalizer's `heading_level` |
| `section:` | picture and caption units | the enclosing section (same rule as Docling) |
| `table:` | tables, rows and cells | the table item's `native_ref` |
| `article:` | records units (`numbered_provisions`, `rule_records`) | the record itself is the article |
| `figure:` | standalone image assets with no section (image documents) | the asset |

- Units also carry `attributes.section_path`, the ordered ancestors from the root to the innermost section. Each entry is `{key, level, depth}`: `key` is the ancestor's boundary key, `depth` its heading depth (1 for a top-level chapter), and `level` a named section level or `null`.

**Section levels and context level per kind.** Named levels come from a closed vocabulary in `contracts/sections.py`: `part`, `chapter`, `section`, `article`, `annex`, `clause`, `file`. Normalizers assign them from structure where it is explicit: records normalizers mark articles and annexes, the code normalizer marks files. For headings, a kind may declare heading patterns that name a level (for example a clause-number pattern for standards, or "Article N" and "Annex N" for legislation); the patterns are kind data owned by sub-project 5 and applied by the normalizer, which bumps its fingerprint when they change. Each kind declares a `context_level`: a named level, `depth:N`, or `document` (the whole document, i.e. the root key). The default is `depth:1`, the top-level chapter. This is the only definition of the field; the kind ontology (sub-project 5) declares values per kind and chunking v2 consumes them. Examples from the decision: legislation `article` or `annex`, book `depth:1`, standard `clause`, code `file`.

Engine core provides one resolver used by chunking, context groups and segment adjacency:

```python
def section_key(section_path: Sequence[SectionEntry], level: SectionLevel | Depth | Document) -> str
```

It returns the key of the innermost ancestor at the requested named level, or of the ancestor at the requested depth, or the document's root key for `document`. When no ancestor matches it falls back to the depth-1 ancestor, and to the document's root key when the unit has no headings. The stored `boundary_key` (table above) stays the innermost enclosing key. Two derived keys follow from the resolver:
- `context_key = section_key(section_path, kind.context_level)`: the `voyage-context-4` context group (chunking v2 §4.10 builds and packs groups from it).
- `chunk_section_key = section_key(section_path, depth:section_depth)` with the preset's `section_depth`; the file for code and the record for record kinds. This is the "chunking section" of chunking v2 §4.1 (its open question 1 is answered by this definition). It is never coarser than `context_key`: when `section_depth` would cross a context unit, the resolver uses the context level. Segment adjacency (chunking v2 §4.11) uses it too.

Both derived keys are computed at chunking time from the stored `section_path` and the kind, so a change of `context_level` or `section_depth` takes effect at the next `manage rebuild index` without re-normalizing. A change of heading patterns changes the normalizer fingerprint and re-normalizes.
- Normalizer fingerprints bump (`docling-normalizer-v3`, `native-normalizer-v2`), so extraction IDs change in the new format. Unit identity formulas are unchanged.
- `boundary_key` must not be a paragraph's own `native_ref` unless the unit is a section, article, table or file root. A normalizer contract test asserts this over every fixture.

**Linear windowing (C1).** Each ownership interval is tokenised once with `offsets` (`TokenCounter.count_with_offsets`, a new port method implemented by the HF tokenizers adapter). Boundary token positions come from a binary search over the offset array. `_validate_windows` and `_make_group` reuse the counts. This keeps the lossless contract and changes only the algorithm, so output is identical: a property test compares it with the old function on 1,000 random texts.

**Query trace v1.** Stored in `query_traces(run_id PRIMARY KEY REFERENCES query_runs, schema_version INTEGER, trace_json TEXT, size_bytes INTEGER)`. It is retained and purged with `query_runs.expires_at`, and is at most 64 KiB; over that, per-result detail beyond rank 20 is dropped and `truncated: true` is set.

```json
{"schema": "tychora.query-trace", "version": 1, "run_id": "…", "snapshot_id": "…", "policy_epoch": 7,
 "cache": {"result": false, "eligibility": true, "query_embedding": false},
 "total_ms": 412,
 "stages": [{"name": "eligibility|exact|lexical|text_embed|text_ann|image_embed|image_ann|graph|fusion|admission|rerank_facts|rerank|assembly|record",
             "start_ms": 0, "duration_ms": 9, "status": "ok|skipped|truncated|unavailable|failed",
             "in": 0, "out": 0, "detail": {"eligible_revisions": 0, "excluded_units": 0, "matching_windows": 0,
             "distinct_candidates": 0, "dropped_dependency": 0, "rescored": 0, "provider": "voyage", "model": "rerank-3", "usage_id": "…"}}],
 "results": [{"key": "unit:…", "final_rank": 1,
              "channels": {"exact": null, "lexical": {"rank": 3, "score": -7.1}, "text": {"rank": 1, "score": 0.82}, "image": null, "graph": null},
              "fused": {"rank": 1, "score": 0.031}, "rerank": {"before": 2, "after": 1, "score": 0.91},
              "admitted_by": "lane:authority|reservation:exact|reservation:graph|score", "omitted": null}],
 "omissions": {"budget": 0, "lane_cap": 0, "source_cap": 0}}
```

- `detail` keys come from a closed list.
- Counts are post-scope only. Nothing about candidates outside the caller's scope is recorded.
- A trace is returned when `request.trace` is true, or through `GET /v1/query-runs/{id}/trace`. The reader's narrowed scope digest must equal the run's, or the reader must hold whole-workspace authority. Otherwise the result is `FORBIDDEN_SCOPE`.
- MCP and CLI return the same object.

**Usage ledger.**

```sql
CREATE TABLE usage_events (
  usage_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('embed_document','embed_query','embed_image','rerank','describe_image','ocr')),
  input_tokens INTEGER, output_tokens INTEGER, images INTEGER, pixels INTEGER, pages INTEGER,
  latency_ms INTEGER NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN ('ok','failed','cancelled','denied')),
  error_code TEXT, collection_id TEXT, job_id TEXT, run_id TEXT,
  batch INTEGER NOT NULL DEFAULT 0 CHECK(batch IN (0,1)),
  units_source TEXT NOT NULL DEFAULT 'provider' CHECK(units_source IN ('provider','local')),
  CHECK(job_id IS NULL OR run_id IS NULL)) STRICT;
CREATE INDEX usage_time ON usage_events(occurred_at);
CREATE INDEX usage_run ON usage_events(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX usage_job ON usage_events(job_id) WHERE job_id IS NOT NULL;
CREATE TABLE price_tables (
  version_id TEXT PRIMARY KEY, effective_from TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('builtin','user')),
  document_json TEXT NOT NULL CHECK(json_valid(document_json)), created_at TEXT NOT NULL) STRICT;
```

- Rows are written at the three owner seams (`run_provider`, `run_query_provider`, `run_captured_provider`, plus `run_local_text` for local models with `provider='local'`) from a `ProviderUsage` value that each adapter returns with its result. Voyage reports `usage.total_tokens` (stored as `input_tokens`), Anthropic reports input and output tokens, and pixels are computed locally. `batch` marks calls made through a provider batch API; `units_source = 'local'` marks rows whose units were counted locally because the provider omitted them (model defaults §4.6).
- Writes are queued to the writer at interactive priority and flushed every 250 ms or at the end of the job or run.
- A price document maps `(provider, model, operation, unit, batch)` to a decimal string in USD per million units. The `builtin` versions are generated from the packaged price table of model defaults §4.6. Cost is computed at read time from the latest table whose `effective_from` is at or before `occurred_at`, and is never stored.
- Edits create a new `user` version (`PUT /v1/prices`, `tychora manage prices set`).
- `GET /v1/usage` returns units and computed cost, plus the price version used. Its full shape, including grouping by run and operation, is in §4.15.
- A provider connection test (§4.15) writes one row with its real operation and neither `job_id` nor `run_id`.

**Aggregates.**

```sql
CREATE TABLE document_status (
  source_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, revision_id TEXT, extraction_id TEXT,
  kind_id TEXT, title TEXT NOT NULL, title_fold TEXT NOT NULL,   -- title_fold: casefolded, accents stripped (NFKD)
  state TEXT NOT NULL CHECK(state IN ('queued','processing','ready','needs_attention','failed','removed')),
  phase TEXT NOT NULL CHECK(phase IN ('core','enriching','complete')), reason_code TEXT,
  units INTEGER, chunks INTEGER, assets INTEGER, tokens INTEGER,
  pages INTEGER, pages_native INTEGER, pages_ocr INTEGER, pages_blank INTEGER,
  chunk_hist_json TEXT, last_job_id TEXT, updated_at TEXT NOT NULL) STRICT;
CREATE INDEX document_status_title ON document_status(collection_id, title_fold, source_id);
CREATE INDEX document_status_updated ON document_status(collection_id, updated_at, source_id);
CREATE TABLE collection_aggregates (
  snapshot_id TEXT NOT NULL, collection_id TEXT NOT NULL,
  documents_ready INTEGER, documents_attention INTEGER, documents_failed INTEGER,
  units INTEGER, chunks INTEGER, assets INTEGER, tokens INTEGER, pages INTEGER, chunk_hist_json TEXT,
  PRIMARY KEY(snapshot_id, collection_id)) STRICT;
```

- Chunk histogram bins (token counts): `[0,64,128,256,512,1024,1536,2048,∞)`.
- Publication computes the new rows as base rows minus the replaced member's stats plus the new member's stats, computed when the batch was written, inside the activation transaction. The cost is O(collections).
- Job transitions update `document_status.state`.
- `needs_attention` is set when coverage is partial, OCR is review-required, or an enrichment phase failed. `reason_code` comes from the §4.13 reason registry.
- `title` is the document's display title (metadata title, else the first heading, else the file name), set at registration and updated at publication.
- Endpoints `GET /v1/collections/{id}/summary` and `GET /v1/documents` (§4.15) read these tables only.

**Background phases.**

```sql
CREATE TABLE extraction_layers (
  layer_id TEXT PRIMARY KEY,   -- digest("layer-v1", extraction_id, phase, enricher_fingerprint)
  extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id), phase TEXT NOT NULL,
  enricher_fingerprint TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','running','ready','failed','skipped')),
  job_id TEXT, reason_code TEXT, created_at TEXT NOT NULL, completed_at TEXT) STRICT;
```

- `evidence_units` and `assets` gain a nullable `layer_id`; NULL means the core phase.
- Derived unit ID: `digest("derived-unit-v1", layer_id, native_ref, ordinal, text_sha)`. This is a new identity function; existing ones are unchanged.
- Derived units have `text_origin='derived'`, a `parent_id` pointing to the original unit or asset, and locators of their parent. Citability is set by phase policy.
- `index_batches` gains `layer_id`, with `UNIQUE(family_id, extraction_id, layer_id)`.
- A document is searchable when its core batches are published; its graph contribution is one of them (§4.5). Phases are registered by name (`pictures`, `formula_code`), run as low-priority jobs (one per extraction and phase), and pass upload authorization like any provider call.
- A phase publication rewrites only the member's page, adding its layer batches.
- If the member's extraction is no longer selected at publish time, the layer stays retained for reuse and nothing publishes.

**Benchmark corpus and harness.**
- `benchmarks/bench-v1.toml` lists about 1,000 documents by URL plus SHA-256 and licence. Sources are public-domain or openly licensed: national legislation and regulations, open-access papers, public-domain books, permissively licensed source repositories, and CC0 images. A seeded generator (`tychora.evaluation.bench.synth`, seed recorded) fills the rest.
- The mix: 600 native PDFs of 100 pages or fewer, 50 scanned PDFs, 10 books over 300 pages, 200 Markdown or text documents, 100 code repositories or files, and 40 images. That gives about 50,000 chunks with chunking v2 defaults.
- 200 queries, including "verification outcome" and the 2026-10-15 probe queries, with gold labels in the stable format of §4.14.
- The corpus is downloaded to a cache outside the repository and is never committed.
- Command: `python -m tychora.evaluation.bench run --corpus bench-v1|bench-small --suite ingest|query|scale|families --profile offline|online`. The `families` suite compares vector family options (§4.4).
- `bench-small` is 100 documents. `scale` replays 1,000 to 10,000 by duplicating synthetic sources under new keys.
- Output: `bench-results/<commit>/<suite>-<profile>.json` with the commit, machine fingerprint (CPU, cores, RAM, OS, Python, SQLite, Chroma), corpus digest, `retained_sha`, per-stage `n`, p50, p90, p95 and p99 (from trace and job stage timings), budget verdicts, and usage totals.
- Results are pushed to the orphan branch `bench-results` by a scheduled workflow on the reference runner.
- The harness extends `evaluation/benchmarks.py` (four cache states kept).

### 4.11 Rebuild paths (D8)

`manage rebuild` gains a required scope argument:

- **`native <channel>`.** The existing recovery path: re-project the same family from retained rows.
- **`index [--channel …]`.** Creates new families (fresh nonces) for the current configuration. It re-windows from canonical units and writes lexical batches and, for a new graph family, graph contributions into a new live graph (the old one is dropped by compaction once no retained snapshot uses its family). It re-embeds, with each batch authorised under current consent. Vectors are reused from retained batch artifacts when `(space_id, input_sha)` matches. It publishes one snapshot, as a resumable job with per-member checkpoints. While it runs, adds continue against the old families. At the end, members added meanwhile are re-batched before the switch (bounded rebase loop, 3 attempts).
  - `finalize` (D8) no longer raises on a family change. It returns `PROFILE_INCOMPATIBLE` reason `index.rebuild_required`, naming this command.
  - `application/settings.py` activation offers it.
- **`workspace --from <old-path>`.** The format migration. It opens the old workspace read-only, enumerates retained originals and their capture metadata, collections, consent and review events, and re-adds them into a new workspace directory as jobs. It reports skipped items: removed originals and unsupported kinds.

### 4.12 Performance budgets

Query, warm, `bench-v1`, p95. Stages in one row run in parallel; the wall budget is the slowest.

| Stage | Lane | Budget (ms) |
| --- | --- | ---: |
| Validate, scope, snapshot catalog | read | 5 |
| Eligibility (cache hit 1; miss at 10k revisions) | read | 40 |
| Exact + lexical FTS ‖ text ANN ‖ image ANN | read / writer-interactive | 70 |
| Fusion, admission, lanes | read | 5 |
| Survivor dependency check + rerank facts | read | 30 |
| Segments, assembly, measure | read + CPU | 50 |
| Record run, trace and usage (enqueue only) | writer | 5 |
| **Local total** | | **205** (< 300) |
| Query embedding (Voyage) | provider | 300 |
| Rerank (Voyage, ≤ 64 candidates) | provider | 450 |
| **Total** | | **955** (< 1,000) |

With the optional local query encoder (§4.4), query embedding moves from the provider row to the CPU lane with a budget of 60 ms at p95 for queries of up to 64 tokens, measured on the reference machine by the `families` suite. The local total is then 265 ms (< 300) and the total about 715 ms. If the measured p95 exceeds 60 ms, the family is still allowed and the trace reports the stage over budget; the benchmark records it.

Ingestion, native PDF of 100 pages, p90:

| Stage | Lane | Budget (s) |
| --- | --- | ---: |
| Acquire, hash, register | writer | 0.5 |
| Docling parse with always-on local enrichment | parser process | 15.0 |
| Normalize, validate, commit | CPU + writer | 1.5 |
| Windowing and chunking | CPU | 1.0 |
| Embedding | provider | 6.0 |
| Batch writes (lexical, vector, graph contribution) | writer | 1.0 |
| Publication + aggregates | writer | 0.5 |
| **Total** | | **25.5** (< 30) |

Separately reported budgets:
- scanned pages: 2.5 s per page with OCR;
- books: 0.25 s per native page;
- background phases: excluded from p90.

**Assertions.** The harness takes stage durations from traces and job events. On the reference runner, `run` fails when any stage p95 (or p90 for ingestion) exceeds its budget by more than 10%. On shared CI runners, `bench-small` fails only on a regression of more than 20% against the last result from the same runner class. The offline profile uses a zero-latency deterministic provider, so it asserts the local share. The online profile runs weekly with credentials and asserts totals.

### 4.13 Repository layer and error reasons

Engine core v2 owns the repository layer and the error reason taxonomy. Sub-project 0 (code health) owns only the gates that enforce them (import-linter contracts, the reasonless-error count, duplication and size ratchets).

**Repositories.** Store-agnostic `Protocol`s in `ports/repositories.py`, implemented for SQLite in the new package `adapters/sqlite/repositories/`. The repository layer owns all SQL: no module outside `adapters/` builds or executes a statement.

| Repository | Responsibility |
| --- | --- |
| `WorkspaceRepository` | policy and lexical epochs, decision watermark (replaces 4 copies and 6 copies respectively) |
| `ArtifactRepository` | `ref(row)` helper replacing about a dozen `ArtifactRef(sha256=row[0], …)` constructions; registration |
| `SnapshotRepository` | catalog, pages, retention, GC |
| `BatchRepository` | lexical, vector and graph-contribution batch rows, state transitions |
| `RepresentationRepository` | units, chunks, intervals, dependencies, neighbours |
| `EligibilityRepository` | the queries of §4.7 |
| `SourceRegistrationRepository` | the SQL of `_registration` and `execute_accepted` |
| `HistoryRepository` (kept) | query runs, stage timings (§4.15) |
| `UsageRepository` | usage rows, grouped reads (§4.15) |
| `TraceRepository` | |
| `AggregateRepository` | document status, collection aggregates, graph degrees |

**Interface rules**, checked by a contract test over every `Protocol` in `ports/` and by review:
- Parameters and results are contracts, IDs, plain scalars, tuples and opaque `Cursor` values. No connection, cursor, row or `sqlite3` type crosses a port; no method takes SQL text, an FTS expression or a store filter syntax.
- Paging is keyset-based through `Cursor` (encoded sort key plus last ID), never `OFFSET` or `rowid`.
- Transactions are a `UnitOfWork` port: `with uow.write() as tx:` and `with uow.read() as tx:` yield repository instances bound to one transaction. The SQLite adapter maps `write()` to `BEGIN IMMEDIATE` on the writer connection and `read()` to a deferred `BEGIN` on a read-pool connection; a PostgreSQL adapter maps them to `READ COMMITTED` with a row lock on the workspace and to `REPEATABLE READ, READ ONLY`.
- Lists of IDs are passed as tuples; each adapter binds them its own way (`json_each`, temp tables, `= ANY`).
- Time values are timezone-aware `datetime`s and stored in UTC; JSON columns hold canonical JSON produced by contracts, never queried with store-specific JSON operators on hot paths.
- Search ports (`LexicalSearch`, `VectorSearch`, `GraphStore`) return ranked owners with a rank and a channel-local score. Fusion uses ranks only (RRF), so BM25 from FTS5 and `ts_rank` from PostgreSQL are interchangeable.

Rule: `application/`, `retrieval/` and `indexing/` modules rewritten here contain no `.execute(` and no `sqlite3` import. These are `sources`, `owner`, `query`, `reranking`, `recovery`, `removal`, `restore`, `inspection` and `visual`, plus `indexing/coordinator` and `retrieval/visuals`, which together hold 176 of today's 228 statements. An import-linter contract enforces the rule on them. The remaining 52 (`discovery`, `collections`, `consent` and others) move under the code-health ratchet using these repositories.

**Error reasons.**
- `ErrorEnvelope` `schema_version` becomes `"2"`, with `reason: str | None`.
- `contracts/error_reasons.py` holds a closed registry of `Reason(code, reason, retryable, operator_action, http_status=None)`. `http_status` overrides the code's status only where HTTP requires it (`range.unsatisfiable` and `range.multiple` are served as 416). Reasons are dotted, for example:
  - `INTEGRITY_ERROR`: `payload.hash_mismatch`, `payload.missing`, `canonical.row_invalid`, `canonical.binding_mismatch`, `config.drift`, `provider.binding_mismatch`, `model_asset.mismatch`, `invariant.unexpected`, `invariant.page_oversize`;
  - `WORKSPACE_BUSY`: `owner.live`, `queue.full`, `publication.rebase`;
  - `INDEX_INCOMPLETE`: `channel.unavailable`, `batch.missing`;
  - `PROFILE_INCOMPATIBLE`: `index.rebuild_required`, `workspace.format_old`, `family.space_mismatch`, `family.dtype_unsupported`;
  - `CAPABILITY_MISSING`: `vectors.cache_absent`, `model_asset.absent`, `graph.disabled`;
  - `INVALID_REQUEST`: `range.unsatisfiable`, `range.multiple`, `cursor.invalid`, `filter.unknown_field`;
  - `PROVIDER_FAILURE`: `provider.auth`, `provider.unreachable`, `provider.rate_limited`, `provider.timeout`.
- Other sub-projects add reasons to this registry under their own prefix (for example the `parser.*` reasons of Docling enrichment §4.8); the registry stays one closed module owned here.
- `TychoraError(code, message, reason=…)` validates the reason against the registry.
- Every site in the modules above gets a reason. `reason=None` remains legal elsewhere and is counted by the ratchet.
- Busy owner (C5): when the runtime record has no `authority`, client delegation returns `WORKSPACE_BUSY` reason `owner.live`, `retryable=True`.

**Dead code (C5).**
- `purge-history` dispatches to `HistoryRepository.purge` (which also deletes `query_traces`).
- `purge-cache` dispatches to `QueryCaches.purge`, the eligibility and snapshot caches, and `JobRepository.expire_diagnostics`.
- A daily retention task on the writer calls both.
- `eligible_cache_key`, `vector_record_id` and `new_family_nonce` gain callers (§4.4, §4.7).

### 4.14 Parity requirements owned by engine core

The two replaced projects are described generically, per the programme §1.

| Requirement | Design | Priority |
| --- | --- | --- |
| Symbol neighbourhood | Confirmed must-have. Engine core owns the query, the MCP tool and the HTTP endpoint; the dashboard only displays them. New `neighbours` inspection subject, MCP tool `neighbours(target, direction=in\|out\|both, predicates, depth≤3, per_predicate_limit≤20, limit≤100)` and `POST /v1/relations/neighbours` with the same request. `target` is a unit ID, an entity ID or a qualified name (resolved within scope; an ambiguous name returns the candidates instead of a graph). Runs over the pinned snapshot's graph contributions, eligibility-filtered (§4.5); no other view of the graph exists, so the result is always fresh. Response: `{target, nodes: [{id, kind, label, unit_id, source_id, locator}], edges: [{from, to, predicate, depth, reason}], truncated: {<predicate>: bool}, depth_reached, snapshot_id}`, where `reason` is a short string such as `CALLS f`, `CALLED_BY g`, `IMPORTS m`, `INHERITS c`. | must |
| Impact analysis | Must-have, with the same ownership as the row above. MCP tool `impact(target, predicates=[calls, imports, inherits], depth≤5, fan_out≤50, limit≤500)` and `POST /v1/relations/impact`: the transitive incoming closure, breadth-first. `fan_out` caps the edges followed from any one node; `limit` caps the nodes returned. Each returned node carries its depth and a `reason` path from the target (for example `f ← CALLED_BY g ← CALLED_BY h`). The response reports `depth_reached`, `truncated_by: depth\|fan_out\|limit\|null`, and the nodes whose fan-out was cut, so the caller can expand them. | must |
| Per-query channels and rerank | `QueryRequest.channels: {lexical, text, image, graph}` and `rerank: auto\|on\|off` can only narrow the configured plan. This replaces the internal `QueryExecutionPlan` (`application/query.py:110-129`). A channel the caller turned off is `skipped`, never incomplete. Score breakdown in the trace (§4.10). | should |
| Graph edge weights | Decided: per-predicate, per-direction `weight` and `limit` in relation definitions. Code defaults: calls out 1.0 and in 0.8, limit 5; imports out 0.5 and in 0.4, limit 3; inherits out 0.7 and in 0.6, limit 3. They multiply graph RRF votes in `retrieval/graph.py`, and are tuned on the evaluation set by the software preset. | should |
| Source connectors with version history | Port `SourceConnector`: `list(cursor) -> ConnectorItem{external_id, edition_key, published_on, effective_from, effective_until, url, metadata}` and `fetch(item) -> bytes`. `external_id` becomes `source_key` and each version a revision with its `edition_key`, so history is kept. `manage connector sync <name>` runs as a resumable job with a cursor checkpoint. Reference implementation: a **Git-history connector** (each commit touching a path is an edition dated by the commit, as for a legislation mirror), tested offline on a fixture repository. Doctrine, court-decision and guide connectors are left to presets as recipes. | must |
| Bulk catalog import | `manage import-catalog <file.jsonl>`. Each line is `{source: {path\|url}, source_key, collection, kind, edition_key?, metadata}`. One idempotent add per line (key = `digest(catalog_sha, line_no)`), at most 100,000 lines, with a catalog job reporting per-line outcomes. | must |
| Per-source generation policy | `sources.allowed_uses_override` (JSON, nullable) can only narrow the kind's `allowed_uses`. Values include `never_generate` (excluded from every compose and draft use) and `reference_only`. Setting it bumps `policy_epoch`, and eligibility applies it at revision level (§4.7). | should |
| Review gating per request | `QueryRequest.review_states` narrows the mode's allowed states and is part of the request digest. | should |
| Stable gold labels | `GoldLabel{collection, source_key, edition_key?, anchor: {native_ref} \| {char_start, char_end} \| {page}}` is resolved to canonical IDs per workspace at evaluation time. Ambiguous or missing labels fail the case with a reason. It replaces canonical-ID-only labels in `evaluation/dataset.py`. | must |
| Edition defects | Spurious cutoff decision; effective versus publication ordering; undated merge; "multiple applicable versions" as a decision (§4.9). | must |

### 4.15 Engine endpoints for the dashboard

Dashboard v2 (sub-project 6, its §5 table) needs the endpoints below. Engine core adds them in the HTTP layer over the repositories; every one runs on the read pool except the connection test, reads bounded tables only, applies the caller's scope, and pages with an opaque `cursor` (`INVALID_REQUEST` reason `cursor.invalid` when it does not decode). Timestamps are RFC 3339 UTC; costs are decimal strings in USD, `null` when no price applies.

**Documents: title filter and sort.**

```
GET /v1/documents?collection=<id>&status=<state>&q=<text>&sort=title|updated_at|pages|status&order=asc|desc&cursor=<c>&limit=<1..100, default 50>
→ {"items": [{"source_id", "title", "collection_id", "kind_id", "state", "phase", "reason_code",
              "pages", "pages_native", "pages_ocr", "pages_blank", "units", "chunks", "chunk_hist", "updated_at"}],
   "next_cursor": "…" | null}
```

`q` matches a substring of `title_fold` after the same folding (case and accents ignored). Sorts use the `document_status` indexes (§4.10) with `source_id` as the tie-break, so paging is stable.

**Usage grouped by run and by operation.**

```
GET /v1/usage?from=<t>&to=<t>&group_by=day|provider|model|collection|operation|run|job[,<second key>]
             &provider=&model=&operation=&collection=&run_id=&job_id=&cursor=&limit=<1..500, default 100>
→ {"price_version": "…", "from": "…", "to": "…",
   "groups": [{"key": {"run": "…", "operation": "rerank"}, "calls": 3, "failed": 0,
               "input_tokens": 0, "output_tokens": 0, "images": 0, "pixels": 0, "pages": 0,
               "latency_ms": {"sum": 0, "p50": 0, "p95": 0}, "cost_usd": "0.0004"}],
   "totals": {"calls": 3, "failed": 0, "input_tokens": 0, "output_tokens": 0, "images": 0, "pixels": 0, "pages": 0,
              "latency_ms": {"sum": 0, "p50": 0, "p95": 0}, "cost_usd": "0.0004"},
   "next_cursor": "…" | null}
```

At most two grouping keys. `group_by=run` returns query runs only, ordered by the run's first `occurred_at`, newest first; `group_by=job` does the same for jobs. The range is at most 400 days.

**Query latency percentiles.** Each recorded run writes `query_runs.total_ms` and one `query_stage_timings(run_id, stage, duration_ms, status, occurred_at)` row per trace stage, in the same writer task as the trace; rows expire with the run.

```
GET /v1/metrics/queries?from=<t>&to=<t>&bucket=hour|day&mode=<mode>&cache=hit|miss|any
→ {"buckets": [{"start": "…", "runs": 0, "p50_ms": 0, "p95_ms": 0,
                "stages": {"<stage>": {"p50_ms": 0, "p95_ms": 0, "runs": 0}}}],
   "overall": {"runs": 0, "p50_ms": 0, "p95_ms": 0}, "budgets": {"<stage>": 0}, "truncated": false}
```

Percentiles are nearest-rank over the runs in each bucket. At most 100,000 runs are read per request; beyond that the newest 100,000 are used and `truncated` is true. `budgets` echoes §4.12 so the view can mark stages over budget.

**Most connected.**

```
GET /v1/relations/hubs?collection=<id>&kind=<kind_id>&predicate=<p>&direction=in|out|both&cursor=&limit=<1..50, default 20>
→ {"items": [{"entity_id", "label", "kind_id", "unit_id", "source_id", "degree_in", "degree_out", "degree"}],
   "snapshot_id": "…", "next_cursor": "…" | null}
```

It sums `graph_degrees` (§4.5) over the eligibility view's graph contributions, ordered by `degree` descending then `entity_id`. With graph disabled it returns `CAPABILITY_MISSING` reason `graph.disabled`.

**Byte ranges for large payloads.** `GET /v1/assets/{id}/content` and the new `GET /v1/sources/{id}/revisions/{revision_id}/original` stream from the content store instead of loading the payload in memory:
- They send `Accept-Ranges: bytes`, `ETag: "<sha256>"`, `Content-Length` and the stored media type.
- A single `Range: bytes=a-b` (or `a-`, `-n`) returns `206` with `Content-Range`. `If-Range` with the ETag is honoured. An unsatisfiable range returns `416` (`INVALID_REQUEST` reason `range.unsatisfiable`); more than one range returns `416` with reason `range.multiple`.
- A response without `Range` streams the whole body, at any size.
- The content is verified against its sha256 when it is written to the store and by `manage health`, not on each ranged read (§4.1, principle 1). Scope and withdrawal are checked on every request.

**Repository index facts.** Code capture also records the branch (`null` when detached) and per-language counts from the language packs. `GET /v1/sources/{id}` gains:

```
"index_facts": {"vcs": "git" | null, "branch": "main" | null, "commit": "…" | null, "dirty": false,
                "captured_at": "…", "languages": [{"language": "python", "files": 0, "units": 0}],
                "skipped": {"binary": 0, "too_large": 0, "ignored": 0, "unsupported": 0}}
```

`index_facts` is `null` for sources that are not code captures.

**Provider connection test.**

```
POST /v1/settings/providers/{role}/test   body: {"draft": true | false}
→ {"ok": true, "role": "text", "provider": "voyage", "model": "voyage-context-4", "latency_ms": 180,
   "checked": ["credential", "endpoint", "model", "dimension"], "usage_id": "…" | null,
   "error": null | {"code": "PROVIDER_FAILURE", "reason": "provider.auth", "message": "…"}}
```

- `draft: true` tests the settings draft, `false` the active settings.
- The test sends one fixed synthetic input (`"tychora connection test"`, or a 1×1 image for image roles), never corpus content, so it needs no collection consent. It still requires the credential reference to resolve.
- It checks that the returned dimension equals the family's `dimension`. For a local query encoder it loads and verifies the asset and encodes the probe, with `provider: "local"`.
- It runs on the provider lane with a 10 s timeout and writes one usage row (§4.10). Failures are reported in `error` with HTTP 200, so the form can show them; only an unknown role is an error response (`NOT_FOUND`).

### 4.16 Future networked stores (sub-project 7)

Sub-project 7 implements PostgreSQL, Qdrant and Neo4j adapters behind the ports of this design. Engine core implements none of them, but each decision above is checked against this mapping:

- **PostgreSQL (relational store).** The format-2 schema maps table for table. `STRICT` tables become typed tables (`TEXT`, `INTEGER`, `SMALLINT` for `bucket`, `TIMESTAMPTZ` for times, `JSONB` for canonical JSON). `membership_pages`, `page_members`, `page_batches` and `snapshot_pages` keep their composite primary keys; `snapshot_members` and `snapshot_batches` stay views over the same joins. Content-addressed page and batch inserts use `ON CONFLICT DO NOTHING`. The publication transaction locks the workspace row (`SELECT … FOR UPDATE`) and compares the active snapshot, which replaces SQLite's `BEGIN IMMEDIATE` single writer. Per-connection temp tables become array parameters (`= ANY($1)`) or `UNNEST` joins. Lexical search uses a `tsvector` column with a GIN index and the same stop-word lists; RRF uses ranks, so score scales do not matter. GC deletes are the same statements. Read-pool transactions become `REPEATABLE READ, READ ONLY`.
- **Qdrant (vectors).** One collection per vector family. The Chroma metadata (`revision_id`, `extraction_id`, `owner_kind`, `owner_id`) becomes the payload, with payload indexes on `revision_id` and `owner_kind`, so eligibility filters are native. Qdrant's scalar and binary quantization with rescoring implement `index_dtype` `int8` and `binary`, rescoring from the original vectors. Batch artifacts stay the retained copy and the native-rebuild source.
- **Neo4j (graph).** Contributions map one to one: nodes and relationships carry `contribution_id` and `extraction_id`, writes are per contribution in bounded transactions, and compaction deletes by `contribution_id`. Traversal filters by eligible extractions passed as a parameter list. `graph_degrees` stays in the relational store.
- **Owner model.** With networked stores, several readers can run in separate processes. The writer lane stays single per workspace, enforced by the workspace row lock instead of the local owner record.

What sub-project 7 must not need: changes to contracts in `contracts/`, to port signatures in `ports/`, to identity formulas, or to the snapshot model. A port change it finds necessary is a defect in this design and is fixed here first.

## 5. Migration and compatibility

- **New format.** It is a new format: `schema_version` 2 in `workspaces`, with a fresh `0001.sql`. Opening a format-1 workspace fails with `PROFILE_INCOMPATIBLE` reason `workspace.format_old`, naming `manage rebuild workspace --from`. There is no dual-read code.
- **Archives.** Format-1 export archives are refused by import with the same reason. Export and import write format-2 pages, batch artifacts and the root (`adapters/files/archive.py`, `application/restore.py`, `references.from_backup`).
- **Public API.** Request and result contracts change additively: `trace` is populated, and there are new optional request fields and new error `reason`s. `ErrorEnvelope` version 2 keeps every v1 field.
- **Identities.** Source, revision, extraction, unit and chunk formulas are unchanged. Extraction IDs differ after rebuild only because normalizer fingerprints changed (boundary keys). Snapshot, family and vector record IDs change, and nothing outside a workspace cites them.
- **Test workspaces.** Old test workspaces are discarded, and fixtures are regenerated by a script committed with the plan.

## 6. Testing and verification

| What | Proves |
| --- | --- |
| Contract tests: page and snapshot identity, page determinism, view equivalence (members and batches via views equal the plan) | D1, identity binding |
| Publication tests: a reused batch is never re-opened (spy on `open_verified`, `verify_batch`); a crash between batch write and activation leaves `building` batches collected by GC | D2 |
| Scale test (`bench --suite scale`): 10,000 synthetic documents; asserts E3 and E4 | D1, Q1, E3, E4 |
| Eligibility equivalence: on 500 randomized fixtures (scopes, floors, reviews, filters, editions), v2 view plus survivor check admits exactly the candidates the v1 compiler admitted | Q1 safety |
| Epoch invariant test enumerating repository writes | cache correctness |
| Concurrency: one 300-page add while 20 queries run; query p95 within 1.5× of idle; 33 concurrent callers succeed | D4/Q4 |
| Normalizer boundary-key contract over every fixture; neighbour attach and adjacent merge now happen on a sectioned native fixture and a sectioned Docling fixture | D5, Docling neighbour fix |
| Section levels: `section_key` on fixtures for each named level and for `depth:N`, including fallback to depth 1 and to the document root; `context_key` follows the kind's `context_level` (legislation article, standard clause, book chapter, code file); `chunk_section_key` is never coarser than `context_key` | context level contract |
| Graph contributions: an add writes only the new member's nodes and edges (count read back equals the contribution artifact); the next query sees them; a `building` or unpublished contribution is invisible; a retried write after a simulated crash is idempotent; compaction removes retired contributions and orphan entities and leaves query results unchanged | D3, E11 |
| Ladybug behaviour: a traversal on a read connection while a write transaction is open on another connection of the same `Database` completes; traversal p95 after compacting 50% of contributions stays within the graph budget. A failure selects fallback 1 or 2 of §4.5 | §4.5 fallback choice |
| Vector families: mixed spaces refused with `family.space_mismatch`; unsupported `index_dtype` refused with `family.dtype_unsupported`; the local query encoder refuses an asset with a wrong digest and never falls back to a hosted model; the `families` suite writes storage, latency and quality per option | §4.4 |
| Port neutrality: a contract test walks every `Protocol` in `ports/` and fails on a parameter or return annotation naming a `sqlite3`, Chroma or Ladybug type, or a `str` parameter named `sql`, `where` or `match` | §4.13, §4.16 |
| Dashboard endpoints: one contract test per endpoint of §4.15 (shapes, paging stability, scope filtering, `416` cases, an asset above 16 MiB served by ranges, connection test with a fake provider for success and each failure reason); recorded responses become dashboard fixtures | E10 |
| Windowing property test (old vs new output identical) plus a 200-boundary unit counted in fewer than 2× its length in tokens | C1 |
| Strict regression: "verification outcome" over 20, 1,000 and 10,000 documents; 33 identifier hits are not incomplete | Q6, C5, E5 |
| Editions: unique winner yields no decision; ordering by effective versus publication; tie and overlap yield one decision; undated candidates are separate | C5 |
| Cache: an entry stays under `max_response_bytes` + 64 KiB for a 300-page document result | Q5 |
| Trace: a hidden-source fixture shows no leakage in counts or results; the same object over HTTP, MCP and CLI | trace contract |
| Usage: every provider seam writes exactly one row (fake providers), including failures and denials; cost recomputes when a new price version is added | ledger |
| Rebuild `index`: a profile edit followed by an add is refused with `index.rebuild_required`, then rebuild, then the add succeeds; vectors are reused when the space is unchanged | D8 |
| Kept fixes: the test files named in §3 stay green, including the Docling neighbour-context regression tests | regressions |
| Parity tests, one per row of §4.14 | E8 |
| Evaluation run versus the `b91d808` baseline, per stage | E6 |

The default suite stays green without model assets. Asset-dependent tests keep their markers.

## 7. Code-health ratchet

- Deleted:
  - `_owners_by_locator`;
  - read-path `prepare_extraction` calls (6 sites);
  - per-row ownership SELECTs in publication;
  - `_vector_owners` and `_candidate_lanes` loops;
  - `pin_snapshot` payload pinning;
  - three `resolve_snapshot` calls per query;
  - the configuration dumps in `query`, `eligibility` and `segments`;
  - the per-vector CAS writes;
  - per-add graph generations, `_graph_slot`'s corpus-wide read and the corpus-wide `GraphManifest`.
- Function size: `execute_accepted` splits into `capture`, `register`, `reuse_or_extract`, `project` and `publish` stages of at most 60 lines each. `read_eligibility_facts`, `_prepare` and `EligibilityCompiler._compile` are replaced by stage functions under 80 lines. No function over 120 lines remains in the touched modules.
- Layering: the import-linter contract of §4.13 passes. `adapters/sqlite/eligibility.py` no longer imports `retrieval` or `indexing`. `retrieval/eligibility.py` splits into `selection.py` (editions), `compiler.py` and `attempt.py`, ending the three import blocks.
- Errors: 0 reasonless `INTEGRITY_ERROR` sites in touched modules.
- Duplication: `ArtifactRef` row construction, watermark and epoch reads exist once.
- Comments: no plan-task references ("Task 7") in touched modules; docstrings say what the function does.
- Tests: mutation score on `adapters/sqlite/repositories/` and `retrieval/compiler.py` at or above the code-health baseline.

## 8. Risks and open questions

1. **Chroma metadata filters with `$in` on 512 values may be slow.** Default: the threshold is a constant tuned by the scale benchmark. Over-fetch is the fallback for everything above it.
2. **Bucketing by source hash puts collections across all pages**, so a single-collection rebuild still touches up to 256 pages. Default: accept, because rebuild is a maintenance path.
3. **The Chroma upsert partition of 512 may still exceed 50 ms on large dimensions.** Default: the partition size is derived from the dimension so it stays at or below 2 MB per call.
4. **Code-health coordination.** Decided: engine core v2 owns the repository layer and the error reason taxonomy (§4.13); code health only enforces gates. The 52 statements outside the rewritten modules move onto these repositories under the code-health ratchet, without new repository designs there.
5. **The Docling throughput assumption in the ingestion budget is not yet measured** (15 s per 100 pages). Default: the first `bench` run fixes it, and sub-project 4 must stay within the remaining slack.
6. **Ladybug read concurrency and compaction cost are not yet proven** (§4.5). Default: the tests of §6 decide before the switch; fallback 1 keeps Ladybug with reads on the writer lane, fallback 2 moves the graph to relational tables.
7. **Local query encoder latency on CPU is an estimate** (60 ms p95 budget, §4.12). Default: the `families` suite measures it; the option stays available whatever the result, and the dashboard shows the measured latency.
8. **Contributions keep dangling cross-member edges** until the other member is visible (§4.5). Default: accept; traversal skips them, and compaction leaves them, because they become valid when the member is published.

## 9. Flow-review findings

Closed: D1 (§4.2), D2 (§4.3), D3 (§4.5, incremental contributions with background compaction), D4 (§4.6), D5 (§4.10), D6 (§4.4), D7 (§4.3), D8 (§4.11), C1 (§4.10), C2 (§4.8), C3 (§4.7), C4 (§4.9), C5 (§4.9, §4.4, §4.13; the lexical cap is confirmed fixed and the exact-channel variant is fixed here), Q1 (§4.7), Q2 (§4.7), Q3 (§4.8), Q4 (§4.6), Q5 (§4.8), Q6 (§4.9).

Also closed: the `_vector_owners` and `_candidate_lanes` loops (flow review §4.3), and graph budget truncation marking coverage partial (`truncated` is separate from `partial`, §4.9).

Deferred:
- `linked_visual_context` rank restart (`application/query.py:188-210`) goes to sub-project 2, because it depends on chunk and asset grouping.
- Incremental `ResponseAssembler` measuring (flow review §4.3) is kept bounded and revisited only if the assembly budget (50 ms) is missed.
