# Tychora Main RAG Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete versioned evidence engine, including document and code ingestion, dedicated graph/vector indexes, deterministic evidence retrieval, Python API, CLI/MCP/HTTP, operational lifecycle, evaluation, and qualified distribution artifacts.

**Architecture:** SQLite owns canonical evidence, policy, manifests, jobs and FTS; content-addressed files preserve originals and derived artifacts. Chroma and a qualified Ladybug adapter provide independent vector and graph projections, published through immutable snapshot receipts under one workspace owner. Thin sync/async Python, CLI, MCP and HTTP adapters call the same application services; calling agents write answers and reviews.

**Tech Stack:** Python 3.12, uv/Hatchling, Ruff/ty/pytest, Pydantic 2, sqlite3/FTS5, Chroma, Ladybug, Docling, Tree-sitter, Voyage, optional Anthropic vision, Typer/Rich, FastAPI/HTTPX/AnyIO, official MCP SDK, PyInstaller.

**Spec:** [Tychora engine specification](../specs/2026-09-10-tychora-engine-design.md). Read it with this plan. Baseline Git commit: `0accfb3`; spec SHA-256: `a536b3439f4e70cae6cafb2107978326ba34cf9e698a728342ac6e80f3160395`.

## Global Constraints

The following requirements are copied from the specification; every task inherits them.

- “Public code, documentation, tests, screenshots, examples, package metadata, and Git history use generic terminology, synthetic fixtures, and redistributable public references.”
- “Local corpora, credentials, conversation exports, and machine-specific paths are excluded from distributions through explicit allowlists.”
- “Proposed project license: MIT.”
- “Target Python 3.12 initially.”
- “Support Python 3.12 first, SQLite >=3.38 with FTS5/JSON functions and local filesystems.”
- “Domain contracts do not import database clients, HTTP frameworks, CLI libraries, or model SDKs.”
- “SQLite is not the graph traversal engine or a brute-force vector backend in the production profile.”
- “There is no distributed transaction across SQLite, Chroma and Ladybug.”
- “No external API or parser runs inside a SQLite write transaction.”
- “Do not configure multiple Uvicorn workers over one embedded workspace.”
- “Search returns evidence, never an LLM-written answer or review.”
- “Offline execution disables external provider construction/calls rather than merely omitting the API key.”
- “Command help and configuration inspection must not load model weights, initialize databases, or construct external clients.”
- “Remote network exposure is outside the initial deployment profile.”
- “An Agno adapter remains a separate optional integration and is not required by `full`.”
- “Until then, discovery reports `dashboard=false`; backend release does not advertise an implemented UI.”
- “Initial candidate targets are Python 3.12 wheels plus standalone macOS arm64 (macOS 15+) and Linux x86_64 (glibc 2.35+, qualified on Ubuntu 22.04).”

---

## Execution rules and scope

This repository currently contains specifications only. All implementation paths below are new unless an earlier task creates them. The dashboard has its own specification and is excluded from implementation here; its HTTP, event, inspection and diagnostic dependencies are included. An actual Agno package, generative answering, automatic conversation memory, advanced graph propagation, alternative database adapters and remote hosting are outside this release. Their extension boundaries are implemented and tested.

Use an isolated worktree at execution time with `superpowers:using-git-worktrees`. Read the current repository instructions first. Do not copy a reference repository wholesale: use only reviewed generic algorithms, retain third-party notices when importing code, and keep private fixtures outside this repository. No application implementation, package installation, model download or paid provider call has been performed by writing this plan. External provider/model qualification uses synthetic inputs only; absent credentials leave that live gate explicitly unexecuted.

Each numbered task is a review/commit boundary; its checkbox steps are individual actions. Each concrete test below starts one red/green cycle. Parameterized acceptance rows are additional red/green cycles, one row at a time; do not implement an entire adapter before running its first failing test. Where a step supplies a small algorithm kernel, complete its surrounding contract with the explicitly listed cases in that task, rather than treating the kernel as the full subsystem. The spec supplies normative schemas, algorithms and invariants; this plan supplies file ownership, interfaces, fixtures, commands and order.

Run commands from the repository root. `uv run --locked` is used after Task 1 creates the lock. Tests never use private corpora or production credentials. Default tests deny network; `qualification` tests exercise real local libraries, and `live` tests require an explicit synthetic-input provider run. A skipped qualification/live test is not a passing release gate. Record failing external/platform gates and continue independent tasks; do not substitute a fake implementation or quietly drop the capability. Releases require every advertised gate, including target-specific signing where applicable.

Candidate pins remain `mcp==2.2.0`, `chromadb==1.5.9`, `ladybug==0.20.3`, and `voyageai==0.5.0`. Their distribution pages were checked while preparing this plan; existence does not establish integration compatibility. Task 3 tests the selected stack before graph-dependent work. Other packages are resolved together in `uv.lock`, not assigned invented patch versions. See [MCP](https://pypi.org/project/mcp/2.2.0/), [Chroma](https://pypi.org/project/chromadb/1.5.9/), [Ladybug](https://pypi.org/project/ladybug/0.20.3/), and [Voyage SDK](https://pypi.org/project/voyageai/0.5.0/).

## File structure and ownership

Create package `__init__.py` files as needed; they must not construct runtime resources. `contracts/__init__.py` reexports public DTOs. Every path listed here is expanded into exact files in its owning task.

| Area | Exact primary files | Responsibility / owning tasks |
| --- | --- | --- |
| Build | `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `LICENSE`, `src/tychora/__init__.py`, `src/tychora/__main__.py`, `src/tychora/py.typed` | Base import/CLI/build; 1, 3, 45 |
| Contracts | `contracts/base.py`, `contracts/identity.py`, `contracts/errors.py`, `contracts/requests.py`, `contracts/evidence.py`, `contracts/operations.py`, `contracts/pipeline.py` under `src/tychora/` | Immutable public DTOs and serialization; 2 |
| Configuration | `configuration/models.py`, `configuration/resolve.py`, `configuration/registry.py`, `configuration/schemas.py`, `configuration/compatibility.py` | Strict data-only packs and historical semantics; 4–5 |
| Canonical storage | `adapters/sqlite/connection.py`, `migrations.py`, `corpus.py`, `jobs.py`, `publication.py`, `eligibility.py`, `lexical.py`, `history.py`, `evaluations.py` | Explicit prepared SQL and transactions; 6, 9, 26–28, 33, 42 |
| Files/runtime | `adapters/files/cas.py`, `references.py`; `application/owner.py`, `runtime.py`, `leases.py`, `jobs.py`, `recovery.py`, `maintenance.py` | Durable artifacts, ownership, bounded jobs and recovery; 7–10, 39–41, 43 |
| Document ingestion | `ingestion/acquire.py`, `native.py`, `normalize.py`, `tables.py`, `assets.py`; `adapters/docling/parser.py`, `mapping.py`; `adapters/vision/anthropic.py`; `application/vision_selection.py` | Original ranges, typed/visual extraction and public selected vision; 10–14, 39a |
| Code ingestion | `adapters/code/capture.py`, `syntax.py`, `python.py`, `typescript.py`, `resolve.py`, `normalize.py` | Captured trees, language forms, context and dependencies; 15–18 |
| Projections/providers | `indexing/windows.py`, `groups.py`, `representations.py`, `receipts.py`, `coordinator.py`; `adapters/voyage/text.py`, `visual.py`, `rerank.py`, `validation.py`; `adapters/chroma/index.py`; `adapters/ladybug/generation.py`, `traversal.py` | Exact representation/cache identities and dedicated engines; 19–26 |
| Retrieval | `retrieval/eligibility.py`, `lexical_query.py`, `fusion.py`, `admission.py`, `graph.py`, `rerank.py`, `anchors.py`, `segments.py`, `visuals.py`, `response.py`, `query.py`, `cache.py` | One policy-aware retrieval pipeline; 27–33 |
| Formatting | `formatting/render.py`, `hierarchical.py`, `budget.py` | Pure stable renderings and measured output; 32 |
| Services/API | `application/services.py`, `sources.py`, `discovery.py`, `inspection.py`, `query.py`; `api.py`, `clients/sync.py`, `clients/async_.py` | Local and delegated typed interfaces; 26, 33–35 |
| Transports | `interfaces/cli.py`, `interfaces/commands/init.py`, `add.py`, `search.py`, `inspect.py`, `serve.py`, `manage.py`; `interfaces/http/app.py`, `auth.py`, `routes.py`, `events.py`, `client.py`; `interfaces/mcp/server.py`, `tools.py`, `resources.py` | Thin adapters, same operations/errors; 36–38 |
| Lifecycle | `application/removal.py`, `backup.py`, `restore.py`, `setup.py`; `adapters/files/archive.py` | Withdrawal, tombstones, canonical backup/restore, model setup; 14, 39–41 |
| Evaluation | `evaluation/dataset.py`, `metrics.py`, `runner.py`, `benchmarks.py` | Integrity, semantic and performance evidence; 42–43 |
| Resources/docs | `resources/migrations/0001.sql`, `resources/schemas/`, `profiles/`, `resources/skills/`; `docs/api.md`, `configuration.md`, `operations.md`, `security.md`, `evaluation.md`, `distribution.md` | Packaged contracts and user instructions; respective feature tasks and 44–45 |
| Qualification/build | `tests/qualification/`, `tests/security/`, `tests/transports/`, `tests/acceptance/`, `packaging/tychora.spec`, `packaging/hooks/`, `.github/workflows/` | Real dependencies, whole-system checks and installed artifacts; 3, 43, 45–46 |

Directories in the table are relative to `src/tychora/` except explicitly rooted repository paths. Tests mirror the owner, not a single growing integration file. A test helper never replaces a production service. Storage/provider ports are introduced with their first implementation and consumed by application code; concrete clients stay behind them.

## Dependency order and runnable checkpoints

| Checkpoint | Tasks | Independently observable outcome |
| --- | --- | --- |
| Foundation | 1–9 | Installed lightweight CLI, registry validation, canonical storage, one owner and resumable job records |
| Sources | 10–18 | Inspectable normalized documents, visuals and revision-qualified code with real parser checks |
| Indexes | 19–26 | Verified lexical/vector batches and a sealed graph published atomically |
| Evidence retrieval | 27–33 | Filtered exact/hybrid search with bounded context, records, output and cache isolation |
| Product interfaces | 34–38 | Equivalent Python, CLI, HTTP and real MCP operations |
| Operations | 39–41, including 39a | Withdrawal, source-bound selected vision composition, original-removal plans and verified portable canonical backups |
| Release | 42–46 | Evaluation, failure/security/performance evidence, docs/skills, installed wheel and qualified binaries |

Dependencies are explicit in each task. Tasks 12–14 and 15–18 may run independently after their prerequisites; graph integration waits for Task 3. Interface implementations can run independently once Task 35 contracts are fixed. Keep one integrator for shared contracts, package metadata, lock and composition root. No dashboard task is hidden in this plan.

## Shared contract ledger

Task 2 creates the following immutable record types in `contracts/pipeline.py`; the named owning task implements their behavior and cross-record validation. Use explicit types, not an untyped bag passed between modules. IDs/digests are strings validated at construction; sequence fields are tuples, membership fields frozensets, JSON metadata uses the immutable JSON mapping from Task 2. Public models stay independent of SDK/database types. `CandidateKey = tuple[Literal["unit", "asset"], str]` is the shared typed ranking key.

| Type | Fields / behavior; owning task |
| --- | --- |
| `ResolvedConfig`, `Registry`, `ScopedFilter` | Immutable resolved values/origins/fingerprints; full versioned definitions/mappings/schema digests; separate `all_units` and `matched_candidates` FilterExpr; 4–5 |
| `ArtifactRef`, `ArtifactStat` | SHA-256, bytes, media type, storage key; stat additionally payload state/deletion time; 7 |
| `ExtractionBundle`, `ReviewTarget` | Immutable validated row sets for revision/extraction/units/locators/chunks/assets/proxies/records/facets/identifiers; exactly one review subject; 6 |
| `CaptureManifest` | Original ref/hash, sidecar refs, captured files `(relative_path,mode,blob_sha,artifact_ref)`, commit/null, dirty, capture time, acquisition/config hashes; 10, 15 |
| `ParsedUnit`, `ExtractionArtifact` | Kind/native ref/original text/ranges/heading hierarchy; artifact adds revision/parser/normalizer hashes, units/assets/assertions/diagnostics and `raw_document_ref`; 11–12 |
| `TableFragment`, `ContinuationDecision`, `ProxyRecord` | Original table grid/header/caption/page/bbox/heading/support IDs; score/status/reasons; proxy unit/asset/origin/dependencies/derivation/review; 13 |
| `RegionInput`, `DerivedObservation`, `ModelAssetPlan` | Registered image crop and full context dependencies/allowed targets; literal OCR and separate interpretation/uncertainties/target/model/schema/review; selected model asset inventory/cost-free download size/license/path plan; 14 |
| `LanguageCapabilities`, `SyntaxResult` | Language/dialects/grammar/version/forms/limits; captured bytes/encoding/line+codepoint map/serialized syntax facts/diagnostics. Native Tree-sitter objects stay adapter-local, never serialized in this DTO; 16–17 |
| `Definition`, `Reference`, `ContextSpan`, `OwnedRange` | Names/qualified scope/kind/parent_name/native ref/byte/decorator/header spans/discriminator; reference source scope/span/import spelling/form; context source unit/char+byte+line ranges/reason/full dependencies; owned ranges include owner unit ID and artifact coordinates; 16–18 |
| `SymbolCatalog`, `Resolution` | One revision's definition/import bindings and explicit revision-qualified dependencies; reference ID/state/selected target or null/candidates/rule/reasons/truncated/total owner-only count; 18 |
| `SearchWindow`, `ContextGroup`, `IndexRecordDigest` | Chunk/unit IDs/char start+end/text/token count/boundary/group/dependencies; ordered window IDs+input texts/capsule/input hash/full dependencies; `(record_id,input_sha,value_sha)`; 19 |
| `EmbeddingRecord`, `VisualInput`, `TextOrImage` | Record/input/value/space IDs and immutable normalized floats; validated image ref/pixels/context/dependencies/resize metadata; discriminated exact text or registered image query; 20–21 |
| `RankingInput`, `RankedIndex` | CandidateKey/text/anchor ranges/dependencies/token count/omitted-match count; input index/finite score; 22, 30 |
| `FamilySpec`, `FamilyHandle`, `VectorBatch`, `BatchReceipt`, `Verification` | Family identity/model/dimension/metric/representation/backend namespace/nonce; handle identity/capabilities; immutable entry set+input manifest; count/sorted entry-input-value digest/config/watermark/backend durability record; current readiness/missing IDs/hash checks; 19, 23–24 |
| `WorkLimits`, `CandidatePage`, `ChannelCandidates` | Max partitions/returned records/deadline; typed ranked candidate pools/counters/truncation/coverage; adds channel identity and lexical epoch where applicable; 23, 28 |
| `RelationshipAssertion`, `GraphGenerationSpec`, `GraphManifest` | Versioned predicate/typed endpoints/origin/all supports/support spans/status/method/confidence/provenance; full membership/family/config/decision watermark; expected node/edge/assertion content digest; 24 |
| `EligibilityView`, `GraphBudget`, `GraphCandidates`, `AssertionPage` | Exact fields in spec §5, plus graph generation ID and ready slot bindings; request-local lowerable seed/new-unit/assertion counters; source targets/paths/supports/reasons/strategy order/truncation; assertion list/status/pagination; 25, 27 |
| `PublicationPlan`, `SnapshotView` | Base/new snapshot IDs/membership/slot/batch bindings/required receipts/manifest; immutable pinned snapshot bindings/config/epochs; 26 |
| `RankedCandidate`, `FusedCandidate`, `Admission`, `RankedEvidence` | Key/dense rank/matching windows; per-channel ranks/RRF/hint/signals/paths; admitted keys/reservations/discard reasons; ordered selected candidates plus rerank mapping/anchors; 29–30 |
| `AssetCandidate`, `Selection` | Asset key/proxy/window/native/link signals/ranks; segments/assets/priorities/omissions/lane counts; 31 |
| `StructuredRecord`, `RelationChecks`, `OutputProfile`, `DeliveredResult` | Unit/type/schema/value/dependencies; status/predicates/assertions/decisions/truncation; fixed renderer/labels/aliases/fingerprints; canonical result plus exact target byte tuple/operational counts; 32 |
| `Services`, `ProviderBindings` | Composition-root service set for source/query/discovery/inspection/jobs/maintenance/evaluation; public injected provider ports only, no database clients; 35 |
| `BackupReceipt`, `RestoreReceipt`, `RecoveryReport` | Manifest/hash/inventory/epochs/omitted channels; restored stable IDs/availability/interrupted jobs/new runtime identity; reconciliation actions/current capabilities/errors; 40–41 |
| `EvaluationDataset`, `GoldCase`, `MetricSet`, `BenchmarkReport`, `ArtifactAudit` | Versioned cases and splits; exact gold labels/ranges/paths; values with sample counts and N/A; environment/cache states/stage/resource/usage metrics; distribution members/features/licenses/checksums/findings; 42–45 |

`SnapshotView` is read-only metadata, never an open SQLite transaction. `GraphBudget` and owner-local caches are explicitly mutable operational state, never shared across requests. DTOs with image data carry validated bytes/references; Pillow/native syntax objects exist only inside the adapter call. Protocol methods take `ExecutionContext` where blocking work is possible, and implementations preserve the signatures above.

### Test construction contract

The tests below use local fixtures to keep intent visible. Each fixture is created in the named task's test directory `conftest.py` and has the fields explicitly exercised in that task; it is not a hidden integration dependency. Use real SQLite/CAS and real local native adapters for their qualification tests. Recording provider ports return fixed finite vectors and record calls; they never satisfy semantic benchmarks. `counter` implements TokenCounter from a preprovisioned tokenizer in integration/release suites; simple unit tests may inject a deterministic count function explicitly.

- Task 6 `seed_bundle` constructs fully populated immutable rows with deterministic IDs; Task 7 replaces temporary artifact construction with verified LocalArtifacts. No fixture fabricates valid missing artifact hashes.
- Tasks 8–14 `execution` supplies config/ctx/scope, `jobs` supplies repository/scope/config, and rich fixtures supply checked original refs/model paths. Build temporary source and asset bytes in test code; fixture generators and expected locator manifests are checked in.
- Tasks 15–18 `repo` uses subprocess Git only on synthetic files; `capture` calls the actual capture adapter; symbol/structure fixtures construct explicit captured definitions and assertions.
- Tasks 19–25 `long_unit`, `long_candidate`, `units`, `view`, `vector_family`, `graph_fixture` use these contracts with explicit owner IDs/ranges/dependencies. Graph fixture default is assertion a→b supported by a,b,hidden. Vector fixture adapters are real and dimension two only for the test family.
- Task 26 `published_corpus` exposes `store`, a recording provider's `fail_next_batch` switch, and `add_revision(text)` calling the actual coordinator. Failures are injected at the port boundary, never by disabling publication checks.
- Tasks 27–33 `views`, `segment_case`, `response_case`, `query_case` compose already implemented stores/services. `response_case.assemble(targets,tokens)` calls ResponseAssembler; `query_case.search(question,project)` creates QueryRequest and calls QueryService; counters are spies around real calls.
- Tasks 34–38 `discovery`, `reader`, `cli_workspace`, `http_reader`, `mcp_reader` use scoped temporary workspaces. HTTP/MCP fixtures start actual service lifespan and client transports; no direct invocation pretends to validate framing/lifespan.
- Tasks 39–43 `shared_originals`, `backup_case`, `restore_case`, `benchmark_case` are thin wrappers around actual services, with explicit IDs and recorded synthetic provider calls. `withdraw`, `purge`, `original`, `export`, `restore_without_original_workspace`, `run_states` call the matching public service operation and expose the specific receipts/bytes/counters asserted in the test. Add wrapper code in the same test module; do not create a general fixture DSL or alternate engine.

Every test with such a fixture includes its construction in the owning task commit. Run the test once before the implementation change to establish the intended failure, then after the minimal change. Expand each listed acceptance case into its own parameterized row/cycle; tests that only mirror a helper's implementation cannot replace observable storage, policy, transport or source-range assertions.

## Tasks

### Task 1: Installable lightweight package and CLI entry point

**Files:** Create `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `LICENSE`, `src/tychora/__init__.py`, `src/tychora/__main__.py`, `src/tychora/py.typed`, `src/tychora/interfaces/cli.py`, `tests/test_import.py`, `tests/conftest.py`.

**Interfaces:** Consumes Python 3.12; produces `tychora.__version__: str`, `tychora.interfaces.cli.main() -> None`, console entry point `tychora`. No workspace dependency yet. Dependencies: none.

**Red/green command:** `uv run pytest tests/test_import.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write this subprocess acceptance test before creating the package:

```python
import os
import subprocess
import sys

def test_import_has_no_runtime_side_effects(tmp_path):
    code = "import sys, tychora; assert not ({'chromadb','ladybug','torch','docling','voyageai'} & sys.modules.keys())"
    run = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                         env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    assert list(tmp_path.iterdir()) == []
```

- [ ] Create the build metadata sufficient to install the missing package and run `uv run pytest tests/test_import.py -q`; expect the first run to fail on missing `tychora`, then on any heavy import/state creation. Set `.python-version` to `3.12`; use this metadata kernel:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tychora"
version = "0.1.0a1"
requires-python = ">=3.12,<3.13"
dependencies = ["pydantic>=2,<3", "typer", "rich", "markdown-it-py", "jsonschema", "platformdirs", "filelock", "tiktoken", "anyio"]

[project.scripts]
tychora = "tychora.interfaces.cli:main"

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "httpx", "ruff", "ty"]

[tool.hatch.build.targets.wheel]
packages = ["src/tychora"]

[tool.ruff]
target-version = "py312"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = ["qualification: real local adapters", "live: explicitly authorized remote synthetic inputs", "release: installed artifact"]
```

- [ ] Implement the lightweight entry point; `__init__.py` contains only version/public lazy exports. `__main__.py` imports `main` inside its `if __name__ == '__main__'` guard.

```python
import typer

app = typer.Typer(no_args_is_help=True, help="Discover and retrieve versioned evidence.")

@app.callback()
def root() -> None:
    """Configure a workspace, add sources, search and inspect evidence."""

def main() -> None:
    app()
```

- [ ] Add subprocess tests for `python -m tychora --help` and `tychora --help`; deny network in ordinary pytest tests with an autouse fixture that replaces `socket.socket.connect` and `socket.create_connection` with a function raising `AssertionError`. Exempt only tests explicitly marked `qualification` that declare local loopback use, and `live`; no broad disabling in unit tests. Add `.gitignore` entries for `.venv/`, `.tychora/`, `.env`, `*.sqlite3*`, `dist/`, `build/`, test caches, local reports and model caches. Use the standard MIT text with `Copyright (c) 2026 Tycho`.
- [ ] Run `uv lock`, `uv sync --locked`, `uv run --locked pytest tests/test_import.py -q`, `uv run --locked ruff check .`, `uv run --locked ty check src`. Expect passing import/help checks and no network/state creation. The lock records resolved versions; no application schema is created by importing.
- [ ] Commit: `git add pyproject.toml uv.lock .python-version .gitignore LICENSE src tests && git commit -m "build: scaffold lightweight Python package and CLI"`.

### Task 2: Immutable public contracts, identities and errors

**Files:** Create `src/tychora/contracts/{__init__,base,identity,errors,requests,evidence,operations,pipeline}.py`, `tests/contracts/test_identity.py`, `tests/contracts/test_models.py`.

**Interfaces:** Produces all public DTOs below, `canonical_bytes(value: JsonValue) -> bytes`, `digest(tag: str, *parts: JsonValue) -> str`, `source_id(collection_id: UUID, source_key: str) -> str`, and `TychoraError(code: str, message: str, *, retryable: bool = False)` with `to_json() -> str`. Introduce the shared pipeline records from the ledger below in `contracts/pipeline.py`; later owning tasks add their cross-record validators. Dependencies: 1. Frozen means nested lists/maps cannot be mutated through returned objects; use tuples and an immutable mapping wrapper with explicit Pydantic validation/serialization.

| DTO | Required fields and semantics |
| --- | --- |
| `RequestScope` | `principal_id: str`, `collection_ids: frozenset[str]`, `access_ceiling: int`, `uses: frozenset[str]`, `capabilities: frozenset[str]`, `upload_providers: frozenset[str]`; `narrow(other) -> RequestScope` intersects grants and rejects a different principal identity |
| `FilterExpr` | Discriminated `eq`, `in`, `range`, `and`, `or`; registered field name, typed value(s), or tuple of children; no raw SQL |
| `SourceInput` | Public RootModel wrapper over discriminated `file`, `folder`, `repository`, `text`, `bytes`, `url`; `source_key`, `collection_id`, `kind`, `metadata`; format/encoding/media type and exactly the selected payload; local path strings remain input bindings |
| `QueryRequest` | Discriminated `input` question/passage/image; `profile`, `mode`, `filters`, `hints`, `revision_ids`, `revision_selector`, `as_of`, `cutoff`, `unknown_dates`, `include_global`, `modalities`, `previous_unit_ids`, `exact_identifiers`, `exact_only`, `limit`, `budgets`, `render_targets`, `trace`; a `question=` convenience normalizes to the question variant |
| `BudgetContract` | `max_tokens=6000`, `tokenizer_id='cl100k_base'`, `render_targets: tuple[RenderTarget,...]`, `max_response_bytes=1048576` |
| `RenderTarget` | `renderer`, `profile_digest`, `schema_digest`, `serialization` |
| `Locator` | Discriminated text/document/code/image; exact artifact/ref/ranges, optional physical/printed pages and geometry coordinate domain; unknown remains null |
| `EvidenceUnit` | `unit_id`, `extraction_id`, `parent_id`, `ordinal`, `unit_type`, `native_ref`, `boundary_key`, `heading_path`, `text`, `text_sha`, `text_origin`, `citable`, `review_state`, `locators`, `attributes` |
| `AnchorRange` | `unit_id`, `char_start`, `char_end`, `origin`, nullable `indexed_window_id`; half-open codepoint ranges |
| `Segment` | Stable ID and source/revision/extraction/kind/role/lane; exact excerpts, text, `matched_unit_ids`, `context_only_unit_ids`, `context_excerpts`, anchors, signals, citations, complete/truncated/omission fields, structured records |
| `VisualEvidence` | Asset/source/revision IDs, registered asset reference, locator, caption, labelled proxies, selection reasons, citations |
| `QueryResult` | `schema_version='1'`, run/snapshot IDs, applied scope, segments, visuals, relations, citations, coverage, checks, coverage gaps, decision requirements, warnings, budget contract, optional trace |
| `DiscoverRequest`, `DiscoveryResult` | Subject, bounded cursor/limit; typed definitions/counts/capabilities/next cursor; subjects include kinds, facets, relations, profiles, languages, sources, settings, runs, models |
| `InspectRequest`, `InspectionResult` | Subject/registered ID plus original/range/view selector; evidence/assets/revisions/jobs/runs/diagnostics; no arbitrary path reads |
| `RelationRequest`, `RelationResult` | Unit seeds, predicates, direction, semantic/structural view, lowerable hops/edges; eligible paths with provenance and truncation |
| `JobHandle`, `JobEvent` | ID/operation/state; durable per-job sequence, timestamp, stage/state/counters/diagnostic refs |
| `MaintenanceRequest`, `MaintenanceResult` | Discriminated setup/sync/remove/rebuild/export/import/health/evaluate/history-purge/cache-purge/purge-originals/migrate; explicit targets/options and typed plan/job/receipt |
| `EvaluationRequest`, `EvaluationReport` | Dataset/split/configuration variants, pinned run controls; per-case/aggregate metrics and N/A denominators |

**Red/green command:** `uv run --locked pytest tests/contracts -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write the identity test and run `uv run --locked pytest tests/contracts/test_identity.py -q`; expect missing exports initially.

```python
import math
import pytest
from tychora.contracts.identity import canonical_bytes, digest

def test_identity_is_order_stable_and_preserves_source_bytes():
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert digest("revision-v1", "é") != digest("revision-v1", "e\u0301")
    with pytest.raises(ValueError):
        canonical_bytes({"score": math.nan})
```

- [ ] Implement the hashing kernel and all six spec identity formulas, repository manifest ordering and family creation nonce. Source key normalization is lexical relative-path normalization; reject absolute keys and `..`, preserve case, never resolve machine paths into identity.

```python
import hashlib
import json

def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")

def digest(tag: str, *parts) -> str:
    return hashlib.sha256(canonical_bytes([tag, *parts])).hexdigest()
```

- [ ] Make `SourceInput` a `RootModel` over the discriminated source variants with a keyword convenience constructor that passes the supplied mapping as `root`; serialized wire data remains the variant itself. Define `QueryRequest.from_mapping` as the single normalization boundary for primitive mappings. Define DTOs from the table using `ConfigDict(frozen=True, extra='forbid', strict=True)`; normalize primitive public mappings at the explicit API boundary, then validate strictly. Validate dates with ISO roundtrip, finite numbers, exclusive source-input variants, ranges, lowerable limits and immutable nested payloads. Export JSON Schema for each public request/result using stable schema IDs. Cover error codes and CLI/HTTP mappings from spec §2; error serialization excludes provider bodies and stays below 8 KiB.
- [ ] Add concrete parameterized model cases: attempted nested metadata mutation fails; a scope ceiling cannot increase; invalid `2025-02-30` fails; null interval end succeeds; reversed interval fails; path + inline payload fails; unknown field fails; primitive mapping roundtrips losslessly. Run `uv run --locked pytest tests/contracts -q` and `uv run --locked ty check src/tychora/contracts`.
- [ ] Commit: `git add src/tychora/contracts tests/contracts && git commit -m "feat: define immutable evidence and operation contracts"`.

### Task 3: Qualify the selected dependency stack before dependent work

**Files:** Modify `pyproject.toml`, `uv.lock`; create `tests/qualification/test_native_stack.py`, `tests/qualification/test_sdk_surface.py`, `tests/qualification/graph_probe.py`, `docs/qualification.md`, `.github/workflows/qualification.yml`.

**Interfaces:** Produces a checked capability report with package versions, wheel tags, SQLite features, platform, persist/reopen and SDK surface results. Dependencies: 1–2. No graph-dependent task may pass its release gate without this task's real native tests.

**Red/green command:** `uv run --locked pytest tests/qualification/test_native_stack.py tests/qualification/test_sdk_surface.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write this real restricted-vector probe; run `uv run pytest tests/qualification/test_native_stack.py -q` before adding extras, expecting a missing dependency failure.

```python
def test_chroma_search_honors_explicit_ids(tmp_path):
    import chromadb
    client = chromadb.PersistentClient(path=str(tmp_path / "vectors"))
    col = client.create_collection("eligibility_probe", embedding_function=None)
    col.add(ids=["hidden", "allowed"], embeddings=[[1.0, 0.0], [0.8, 0.6]])
    result = col.query(query_embeddings=[[1.0, 0.0]], ids=["allowed"],
                       n_results=1, include=["distances"])
    assert result["ids"] == [["allowed"]]
```

- [ ] Add extras `documents`, `code`, `vector`, `graph`, `voyage`, `vision`, `server`, `full` exactly as spec §8; `full` is the union, without framework dependencies. Resolve with `uv lock`; install with `uv sync --locked --all-extras`. Add build group `pyinstaller`, test-fixture generation group `reportlab`/`python-docx`, and audit group `pip-audit`/license inventory tooling. Keep these outside runtime requirements.
- [ ] Write `graph_probe.py` using the installed Ladybug module discovered from the wheel, with `Database`/`Connection` behind the later adapter. In a subprocess: create a temporary node table, insert a row in a transaction, roll back a second row, close/reopen, and assert exactly the committed row. Run two connections from one Database; kill a writing subprocess after an explicit transaction boundary, reopen and verify consistency. Use this SQL-shaped Cypher fixture verbatim:

```cypher
CREATE NODE TABLE Probe(id STRING PRIMARY KEY, value STRING);
BEGIN TRANSACTION;
CREATE (:Probe {id: 'kept', value: 'source'});
COMMIT;
BEGIN TRANSACTION;
CREATE (:Probe {id: 'rolled_back', value: 'discard'});
ROLLBACK;
MATCH (p:Probe) RETURN p.id ORDER BY p.id;
```

- [ ] Add SDK surface assertions for `from mcp.server import MCPServer`, `MCPServer.streamable_http_app`, `session_manager.run`, the installed Voyage contextual keyword `enable_auto_chunking`, explicit tokenizer access, Docling JSON/image extraction, and each grammar wheel. Check SDK source/signatures when a probe fails; do not shim an unsupported semantic requirement. Record actual import names and APIs in `docs/qualification.md` for subsequent tasks.
- [ ] Add CI jobs on Python 3.12 native macOS arm64 and Linux x86_64, recording exact minimum OS/glibc tags. Run `uv run --locked pytest -m qualification tests/qualification -q`. Record each gate as passed/failed/not-run with evidence; unavailable runner or SDK model access is not success. If Ladybug fails an essential gate, report the failure and revise that dependency decision before implementing its dependent adapter, while continuing independent document tasks.
- [ ] Commit: `git add pyproject.toml uv.lock tests/qualification docs/qualification.md .github && git commit -m "test: qualify native storage and provider SDK contracts"`.

### Task 4: Strict pack resolution with origin reporting

**Files:** Create `src/tychora/configuration/models.py`, `resolve.py`, `tests/configuration/test_resolve.py`, `tests/fixtures/packs/base/profile.toml`, `tests/fixtures/packs/child/profile.toml`.

**Interfaces:** `resolve_config(workspace_file: Path, profile: str, overrides: Mapping[str, JsonValue]) -> ResolvedConfig`; `ResolvedConfig` contains immutable values, field origins, full registry digest and separate extraction/index/eligibility/query/output fingerprints. `merge_values(base: dict, overlay: dict) -> dict` is pure. Dependencies: 2.

**Red/green command:** `uv run --locked pytest tests/configuration/test_resolve.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write and run the first test with `uv run --locked pytest tests/configuration/test_resolve.py -q`:

```python
from tychora.configuration.resolve import merge_values

def test_explicit_false_zero_and_empty_list_survive():
    result = merge_values({"graph": True, "bonus": 1, "lanes": ["a"]},
                          {"graph": False, "bonus": 0, "lanes": []})
    assert result == {"graph": False, "bonus": 0, "lanes": []}
```

- [ ] Implement map-by-key/list-replace resolution; never use truthiness to choose values.

```python
def merge_values(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = value
    return result
```

- [ ] Add the exact resolution order from spec §3, explicit dependency DAG topological order, digest checking and immutable versioned-definition collisions. Permit only registered local roots, contained includes and bounded local schemas. Use `tomllib`; no import/eval/Jinja/SQL, remote includes or parent-directory scan. Origins identify the source layer/key; unauthorized discovery redacts private paths and secret references.
- [ ] Add one red/green case each for dependency cycles, same-ID unequal content, escaping symlink/include, explicit false override, unknown options, bad provider/index combinations, missing root binding, and request attempting to raise a ceiling. Request override allowlist is the one in spec §3. Re-run the file and type checks.
- [ ] Commit: `git add src/tychora/configuration tests/configuration tests/fixtures/packs && git commit -m "feat: resolve strict versioned local profile packs"`.

### Task 5: Generic kinds, facets, relations, modes and historical compatibility

**Files:** Create `src/tychora/configuration/registry.py`, `schemas.py`, `compatibility.py`; create `src/tychora/profiles/documents/{profile.toml,kinds.toml,relations.toml,facets.toml}`, `profiles/code/profile.toml`, `profiles/verified/profile.toml`, `src/tychora/resources/schemas/{metadata,rule,feedback,template-slot,code-symbol}.json`, `tests/configuration/test_registry.py`, `test_historical_kinds.py`.

**Interfaces:** `Registry.from_config(config: ResolvedConfig) -> Registry`; `Registry.validate_record(kind_id: str, value: JsonValue) -> JsonValue`; `require_kind_coverage(readable_kinds: frozenset[str], lane_bindings: Mapping[str,str], historical_definitions: Mapping[str,JsonValue]) -> None`; `compile_filter(expr: FilterExpr, registry: Registry) -> ScopedFilter` separating candidate and all-unit expressions. Dependencies: 4.

**Red/green command:** `uv run --locked pytest tests/configuration/test_registry.py tests/configuration/test_historical_kinds.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run this historical binding test:

```python
import pytest
from tychora.contracts.errors import TychoraError
from tychora.configuration.compatibility import require_kind_coverage

def test_missing_historical_kind_is_not_silently_dropped():
    with pytest.raises(TychoraError) as err:
        require_kind_coverage(frozenset({"standard@1"}), {}, {})
    assert err.value.code == "PROFILE_INCOMPATIBLE"
```

- [ ] Implement strict coverage before lane filtering, verifying stored original schema hashes. Compatibility changes lane assignment only; activation checks all declared retained collections, scoped queries check their selected readable membership. Use a deterministic missing list and sanitized error:

```python
def require_kind_coverage(readable_kinds, lane_bindings, historical_definitions):
    missing = sorted(k for k in readable_kinds
                     if k not in lane_bindings or k not in historical_definitions)
    if missing:
        raise TychoraError("PROFILE_INCOMPATIBLE", "Missing explicit kind/schema bindings: " + ", ".join(missing))
```

- [ ] Create complete descriptions/examples/required metadata for law, regulation, decision, standard, guidance, manual, template, procedure, feedback, evidence, example, rationale, research_note, code_file, architecture_decision. Supply the six generic lanes, compose/audit/lookup modes and original/proxy-state differences from §3/§5. Registered normalizers are `structured_sections@1`, `numbered_provisions@1`, `rule_records@1`, `feedback_records@1`, `template_slots@1`, `code_symbols@1`. Split aggregate TOML registries into explicit local definition files if required by the resolver, retaining identical versioned IDs.
- [ ] Register all spec predicates plus `cites@1` and `continues_table@1`, with required origin/supports, endpoint constraints, status/method/directions, and reserved contains/precedes meanings. Implement local Draft202012 schemas with size/depth/keyword/ref checks and explicit date formats. Implement facets with immutable namespaced keys, exact/global/unknown distinctions; symbol-kind/name are candidate-only, path/language all-unit. Reject mixed-scope OR and policy fields as relevance hints; cap optional hint bonus at 0.01.
- [ ] Test custom kind/relation loading without code changes, zero-count discovery, schema keyword/ref escapes, missing requirement fields, global vs absent scope, inclusive date import conversion, conflicting historical maps and unchanged old schemas. Run `uv run --locked pytest tests/configuration -q`; add golden complete generic pack fixtures, not incomplete illustrative snippets.
- [ ] Commit: `git add src/tychora/configuration src/tychora/profiles src/tychora/resources/schemas tests/configuration && git commit -m "feat: register generic evidence semantics and profile compatibility"`.

### Task 6: Canonical SQLite schema and cross-record repositories

**Files:** Create `src/tychora/resources/migrations/0001.sql`, `src/tychora/adapters/sqlite/{connection,migrations,corpus}.py`, `src/tychora/ports/corpus.py`, `tests/storage/test_schema.py`, `tests/storage/test_corpus.py`, `tests/helpers/corpus.py`.

**Interfaces:** `open_corpus(path: Path, *, initialize: bool=False) -> sqlite3.Connection`; `CorpusStore.commit_extraction(bundle: ExtractionBundle) -> str`, `hydrate(unit_ids: tuple[str,...]) -> tuple[EvidenceUnit,...]`, `record_review(target: ReviewTarget, state: str, actor: str) -> int`, `record_assertion_decision(assertion_id: str, artifact_sha: str, record_id: str, state: str, actor: str) -> int`. Define `ExtractionBundle` as immutable revision/extraction/units/locators/chunks/assets/proxies/records/facets/identifier rows; `ReviewTarget` is exactly one revision/unit/asset ID. Dependencies: 2, 5.

**Red/green command:** `uv run --locked pytest tests/storage/test_schema.py tests/storage/test_corpus.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Extract the single SQL fenced block from the spec byte-for-byte into `0001.sql`; record checksum in migration code. Write the test before `open_corpus` exists, then run `uv run --locked pytest tests/storage/test_schema.py -q`.

```python
from tychora.adapters.sqlite.connection import open_corpus

def test_canonical_schema_integrity(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshot_batches", "asset_proxy_dependencies", "original_payload_tombstones"} <= names
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
```

- [ ] Implement explicit initialization and compatibility-checked opening using these pragmas. Opening never applies an incompatible migration; explicit migration uses preflight/backup in Task 41.

```python
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=FULL")
db.execute("PRAGMA busy_timeout=2000")
```

- [ ] Implement prepared insertion in `BEGIN IMMEDIATE`, immutable-ID/content collision checks, cross-extraction parent/assets restrictions, locator and exact substring bounds, facets/schema validation, required nonempty source dependencies and no proxy cycles. Use `with db:` only where transaction boundaries are explicit. Review/access/withdrawal/assertion changes bump policy epoch transactionally; revision rejection dominates child acceptance. Access floor can rise on registration, never silently fall.
- [ ] Create `tests/helpers/corpus.py::seed_bundle(text: str='Record the result.', *, source_key: str='manual.md', kind: str='manual@1') -> ExtractionBundle` with complete synthetic rows, stable IDs and a real temporary artifact setup; no hidden use of future services. Add failure tests for the 30 schema cases recorded in the spec plus cross-record constraints omitted by SQL. Assert exactly 39 application tables plus FTS and its internal tables counted separately. Run `uv run --locked pytest tests/storage -q`.
- [ ] Commit: `git add src/tychora/resources/migrations src/tychora/adapters/sqlite src/tychora/ports tests/storage tests/helpers && git commit -m "feat: store immutable corpus records and schema invariants"`.

### Task 7: Atomic content-addressed artifacts and typed references

**Files:** Create `src/tychora/adapters/files/cas.py`, `references.py`, `src/tychora/ports/artifacts.py`, `tests/storage/test_artifacts.py`, `test_references.py`.

**Interfaces:** `LocalArtifacts(root: Path)` implements `put_stream(stream: BinaryIO, media_type: str) -> ArtifactRef`, `open_verified(ref: ArtifactRef) -> ContextManager[BinaryIO]`, `stat(ref) -> ArtifactStat`, `pin_payloads(shas: frozenset[str]) -> ContextManager[None]`. `ArtifactRef` is `{sha256, size_bytes, media_type, storage_key}`; `ArtifactStat` adds availability. `required_refs(manifest: JsonValue, *, capture_published: bool) -> frozenset[str]` walks only registered typed references. Dependencies: 2, 6.

**Red/green command:** `uv run --locked pytest tests/storage/test_artifacts.py tests/storage/test_references.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run `uv run --locked pytest tests/storage/test_artifacts.py -q`:

```python
import io
from tychora.adapters.files.cas import LocalArtifacts

def test_artifact_roundtrip_is_verified_and_idempotent(tmp_path):
    store = LocalArtifacts(tmp_path)
    a = store.put_stream(io.BytesIO(b"source bytes"), "text/plain")
    b = store.put_stream(io.BytesIO(b"source bytes"), "text/plain")
    assert a.sha256 == b.sha256
    with store.open_verified(a) as stream:
        assert stream.read() == b"source bytes"
```

- [ ] Implement streaming size limits, SHA-256, per-SHA guard, private temporary file, fsync, same-filesystem atomic rename and parent-directory fsync before returning a reference. Reverify existing payloads, not just metadata; reject escape/symlink paths. The write ordering is:

```python
with staging.open("xb") as out:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        out.write(block)
    out.flush()
    os.fsync(out.fileno())
os.replace(staging, destination)
fd = os.open(destination.parent, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
```

- [ ] Register required-reference schemas for repository capture, extraction, assertion, registry, index input, vector cache, job capture, retained run/evaluation and backup manifests. Return a hard error for unknown required schema versions. Provenance-only hashes are not traversed; terminal published captures retain originals through revision bindings. Add synthetic nested manifests testing closure, cycle termination, bogus SHA-looking text, corrupt existing bytes and cancellation before rename.
- [ ] Run `uv run --locked pytest tests/storage/test_artifacts.py tests/storage/test_references.py -q`; force an interruption before/after each durability boundary and assert no committed reference to absent bytes. Commit: `git add src/tychora/adapters/files src/tychora/ports/artifacts.py tests/storage && git commit -m "feat: persist verified artifacts with typed retention roots"`.

### Task 8: Workspace ownership, queues and resource leases

**Files:** Create `src/tychora/application/{owner,runtime,leases}.py`, `src/tychora/ports/execution.py`, `tests/runtime/test_owner.py`, `test_leases.py`.

**Interfaces:** `WorkspaceOwner.open(path: Path, config: ResolvedConfig, scope: RequestScope) -> ContextManager[WorkspaceOwner]`; `LeaseSet.pin(keys: frozenset[str]) -> ContextManager[None]`, `is_pinned(key: str) -> bool`; `ExecutionContext` carries monotonic deadline, cancellation event and `checkpoint() -> None`. `UploadPolicy.authorize(scope: RequestScope, provider: str, dependencies: frozenset[str], policy_epoch: int) -> None` rechecks current canonical access/upload grants without a provider; ingestion uses it before query compilation exists. `owner.run_blocking(call: Callable[[],T], ctx: ExecutionContext) -> Awaitable[T]` uses bounded workers. Dependencies: 6–7.

**Red/green command:** `uv run --locked pytest tests/runtime -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the independent lease test with `uv run --locked pytest tests/runtime/test_leases.py -q`:

```python
from tychora.application.leases import LeaseSet

def test_nested_leases_keep_resources_pinned():
    leases = LeaseSet()
    with leases.pin(frozenset({"snapshot-a"})):
        with leases.pin(frozenset({"snapshot-a"})):
            assert leases.is_pinned("snapshot-a")
        assert leases.is_pinned("snapshot-a")
    assert not leases.is_pinned("snapshot-a")
```

- [ ] Implement reference-counted owner-local pins; blocking-work futures release pins on actual worker exit, not cancelled client delivery. Use this completion pattern with a worker-owned lease:

```python
async def run_blocking(self, call, ctx):
    ctx.checkpoint()
    return await anyio.to_thread.run_sync(call, limiter=self.blocking_limiter,
                                         abandon_on_cancel=False)
```

- [ ] Implement local filesystem lock, protected runtime directory/files (0700/0600), workspace UUID and protocol handshake, random token outside corpus/config. A busy standalone process yields `WORKSPACE_BUSY`; delegation only occurs after authenticated live identity verification in Task 36. Maximum two parser processes and two concurrent requests/provider; bounded queues initially 32 operations with actionable busy errors. Shutdown stops admission, drains workers, closes databases, removes runtime endpoint and releases lock last.
- [ ] Add subprocess lock contention/stale endpoint tests and an async event-loop heartbeat test. Cancel a waiting request while its blocking operation is held by a threading event; GC must still see the pin until release. Parser children never receive canonical DB handles/paths: copy verified independent inputs under a payload pin and retain staging until child exits or is fenced. Run `uv run --locked pytest tests/runtime -q`; commit with message `feat: own workspace resources and retain leases through cancellation`.

### Task 9: Durable jobs, idempotency and replayable checkpoints

**Files:** Create `src/tychora/adapters/sqlite/jobs.py`, `src/tychora/application/jobs.py`, `src/tychora/ports/jobs.py`, `tests/jobs/test_idempotency.py`, `test_events.py`.

**Interfaces:** `JobRepository.accept(operation: str, scope: RequestScope, key: str|None, intent: JsonValue, effective_config: ArtifactRef) -> JobHandle`; `capture(job_id: str, manifest: ArtifactRef) -> None`; `checkpoint(job_id, step_key, input_sha, output: ArtifactRef) -> None`; `append_event(job_id, event: JobEvent) -> int`; `events_after(job_id, sequence: int, limit: int) -> tuple[JobEvent,...]`. `JobService.get/wait/cancel/retry` retains one keyed job. Dependencies: 6–8.

**Red/green command:** `uv run --locked pytest tests/jobs -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a real SQLite fixture test. `jobs` fixture constructs the repository over Task 6 schema, a complete synthetic workspace/scope, and Task 7 config artifact; define it in `tests/jobs/conftest.py`.

```python
import pytest
from tychora.contracts.errors import TychoraError

def test_key_conflict_does_not_schedule_new_work(jobs):
    a = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    b = jobs.accept("add", jobs.scope, "k", {"path": "a.md"}, jobs.config)
    assert a.id == b.id
    with pytest.raises(TychoraError) as err:
        jobs.accept("add", jobs.scope, "k", {"path": "b.md"}, jobs.config)
    assert err.value.code == "IDEMPOTENCY_CONFLICT"
```

- [ ] Implement digest comparison before default resolution/live-path reads. Preserve omitted fields in caller intent; include supplied inline bytes via digest, explicit configuration references and operation/principal/workspace. Under `BEGIN IMMEDIATE`, compare or insert scoped key; return existing job unchanged for equal digest. Persist capture once before extraction. Kernel:

```python
request_digest = digest("request-v1", workspace_id, scope.principal_id, operation, intent)
if existing is not None and existing.request_digest != request_digest:
    raise TychoraError("IDEMPOTENCY_CONFLICT", "This key is bound to different input.")
```

- [ ] Implement event sequence allocation in the same transaction as state transitions; complete/cancel/fail terminal states, bounded retries only for typed transient failures, cooperative cancellation between stages. Retain minimum digest/config/capture/result binding for workspace lifetime, independently of 30-day diagnostic retention. Missing committed capture fails integrity rather than rereading source.
- [ ] Add concurrent equal/unequal acceptance, restart, changed defaults, changed inline bytes, changed path after capture, fresh key and expired diagnostics cases. Test publication receipt reconciliation through a stub receipt query port defined here and implemented in Task 26; no automatic provider restart. Run `uv run --locked pytest tests/jobs -q`; commit with message `feat: persist idempotent jobs and resumable event checkpoints`.

### Task 10: Bounded file, folder, bytes and opt-in URL acquisition

**Files:** Create `src/tychora/ingestion/acquire.py`, `src/tychora/ports/acquisition.py`, `tests/ingestion/test_acquisition.py`, `tests/security/test_acquisition.py`.

**Interfaces:** `Acquirer(artifacts: LocalArtifacts, config: ResolvedConfig).capture(source: SourceInput, ctx: ExecutionContext) -> CaptureManifest`; manifest binds input/sidecar artifacts, source keys, actual hashes/time and acquire fingerprint. URL adapter is explicitly enabled with a fixed host/scheme allowlist. Repository variant delegates to Task 15. Dependencies: 7–9.

**Red/green command:** `uv run --locked pytest tests/ingestion/test_acquisition.py tests/security/test_acquisition.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a bytes input test:

```python
from tychora.contracts import SourceInput
from tychora.ingestion.acquire import Acquirer

def test_inline_capture_needs_no_caller_temporary_file(artifacts, execution):
    source = SourceInput(type="text", source_key="notes", collection_id="default",
                         kind="manual", text="Résumé")
    captured = Acquirer(artifacts, execution.config).capture(source, execution.ctx)
    with artifacts.open_verified(captured.original_ref) as data:
        assert data.read().decode() == "Résumé"
    assert captured.source_key == "notes"
```

- [ ] Add a `CaptureManifest` test using `SourceInput(type='text', source_key='notes', collection_id='public', kind='manual', text='Résumé')`; require original bytes and sidecars captured once before any parser job. Implement path containment, stable-stat-before/after-read, bounded retries for moving files and manifest sorting. Check containment with this kernel after resolving an explicitly authorized root:

```python
resolved = candidate.resolve(strict=True)
if not resolved.is_relative_to(root.resolve(strict=True)):
    raise TychoraError("FORBIDDEN_SCOPE", "Source escapes its registered root.")
```

- [ ] Set explicit initial ceilings in validated config: 100 MiB/file, 10,000 files/capture, 1 GiB total capture, 10 MiB inline text, 3 redirects, 30-second download timeout. These are implementation resource defaults, lowerable per request; report them in discovery. URL fetching requires HTTPS host allowlist, resolved-address checks against loopback/private/link-local/multicast, DNS rebinding-safe connection validation and rechecking every redirect. Disable proxy/environment inheritance unless explicitly configured. Never fetch a source URL during ordinary query/inspection.
- [ ] Test file/folder containment, symlink escape, oversized content, duplicate source keys, missing sidecars, unstable source, redirect to private IP, DNS-answer change and explicit offline URL failure with a controlled local fake transport. Run `uv run --locked pytest tests/ingestion/test_acquisition.py tests/security/test_acquisition.py -q`; commit with message `feat: capture bounded immutable source inputs`.

### Task 11: Native Markdown/text and typed record normalization

**Files:** Create `src/tychora/ingestion/{native,normalize}.py`, `src/tychora/ports/parsers.py`, `tests/ingestion/test_native.py`, `test_records.py`, `tests/fixtures/documents/standards.md`, `feedback.json`, `template.md`.

**Interfaces:** `NativeParser.extract(original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext) -> ExtractionArtifact`; `normalize(parsed: ExtractionArtifact, revision_id: str, registry: Registry) -> ExtractionBundle`. `ExtractionArtifact` binds parser fingerprint, original reference, units/assets/assertions/diagnostics; parser performs no DB writes. Dependencies: 5, 10.

**Red/green command:** `uv run --locked pytest tests/ingestion/test_native.py tests/ingestion/test_records.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a direct parser test:

```python
from tychora.ingestion.native import parse_markdown

def test_fenced_heading_is_not_a_document_section():
    units = parse_markdown("# Manual\n\n```md\n# fake\n```\n\nRecord results.\n")
    assert [u.text for u in units if u.unit_type == "heading"] == ["Manual"]
    assert any(u.unit_type == "code_block" and "# fake" in u.text for u in units)
```

- [ ] Implement `parse_markdown(text: str) -> tuple[ParsedUnit,...]`, with `ParsedUnit` containing token kind/native reference/line span/source substring/heading path. Use `MarkdownIt('commonmark').enable('table')`; map token lines back to original text, not renderer output. Preserve fenced code, nested lists, table cell structure and zero invented offsets. The parse boundary is:

```python
from markdown_it import MarkdownIt

def block_tokens(text: str):
    return tuple(MarkdownIt("commonmark").enable("table").parse(text))
```

- [ ] Implement the six registered normalization strategies from Task 5 using parser structure and exact field label aliases. Preserve a rule as one semantic unit with typed requirement/proof/control/foundations; feedback literal annotation differs from derived observation, unknown author/target fields stay null; template slots retain original instruction. Numbered provisions preserve IDs and boundaries. Ambiguous blocks produce diagnostics, not guessed records.
- [ ] Add source-range substring assertions and generic multilingual labels, HTML-like prompt text, tables, duplicate rule IDs (ambiguous lookup), and structured JSON unknown-field failures. Run `uv run --locked pytest tests/ingestion/test_native.py tests/ingestion/test_records.py -q`; commit with message `feat: normalize native documents and structured evidence records`.

### Task 12: Real Docling extraction with retained provenance

**Files:** Create `src/tychora/adapters/docling/{parser,mapping}.py`, `tests/ingestion/test_docling_mapping.py`, `tests/qualification/test_docling.py`, `tests/fixtures/documents/create_rich_fixtures.py`, `tests/fixtures/documents/rich-manifest.json`.

**Interfaces:** `DoclingParser.extract(original: ArtifactRef, config: ResolvedConfig, ctx: ExecutionContext) -> ExtractionArtifact`; `map_document(export: JsonValue, original: ArtifactRef) -> MappedDocument`. The pure mapper returns source facts; explicit DocumentRegistration plus owner-retained artifacts produce ExtractionArtifact. Preserve the synchronous Parser port with bounded owner sync bridges and provide an async path; conversion children receive only private staged files/model paths. Qualification uses an explicitly preprovisioned local asset fixture; the user-facing model inventory/setup service is completed in Task 14, avoiding a dependency cycle. Dependencies: 3, 7–8, 10–11.

**Red/green command:** `uv run --locked pytest tests/ingestion/test_docling_mapping.py tests/qualification/test_docling.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Generate a redistributable two-page PDF and DOCX from source in the test fixture builder, containing headings, a table split across PDF pages, a labelled figure and a caption. Record manually checked expected pages/regions in `rich-manifest.json`; generation is a development tool, not a runtime dependency. Write/run the real test before implementing the adapter:

```python
import pytest

@pytest.mark.qualification
def test_real_docling_retains_document_and_locators(rich_capture, docling_parser, execution):
    artifact = docling_parser.extract(rich_capture, execution.config, execution.ctx)
    assert artifact.parser_fingerprint
    assert artifact.raw_document_ref is not None
    assert any(u.locators and u.locators[0].physical_page == 2 for u in artifact.units)
    assert artifact.assets
```

- [ ] Define the three fixtures locally: `rich_capture` uses Task 10 on generated PDF; `docling_parser` uses explicitly preprovisioned model paths; `execution` supplies validated config/context. Missing models fail the qualification command with setup guidance, not an implicit download. Map the qualified SDK using this boundary:

```python
converted = converter.convert(str(private_staging_path))
document_json = converted.document.export_to_dict()
raw_ref = artifacts.put_stream(io.BytesIO(canonical_bytes(document_json)), "application/json")
```

- [ ] Preserve complete JSON, `self_ref`, hierarchy/order, page dimensions/origin, bbox, charspan coordinate domain and picture/table/page assets. Preserve native annotation objects if available; unsupported extraction becomes a capability diagnostic. DOCX without renderer gets no physical page. Normalize geometry only with recorded transforms; keep raw coordinates too. Run parser inside owner-managed private staging, with explicit model paths/download disabled and no canonical handles in child processes.
- [ ] Test fake export mapping separately from real PDF/DOCX extraction; verify five exact source passages and table/figure geometry against manually reviewed expectations. Run `uv run --locked pytest tests/ingestion/test_docling_mapping.py tests/qualification/test_docling.py -q`; commit with message `feat: retain Docling extraction artifacts and source geometry`.

Implementation boundary: this task qualifies `structured_sections@1` for rich documents and rejects other requested strategies explicitly before conversion. Task 12b below completes numbered/rule/feedback/template normalization before table/proxy work. Preserve complete SDK JSON as a separate required artifact; the extraction replay manifest binds typed source facts without duplicating embedded images. Replay validates source facts against that JSON and does not reopen original payloads. Worker output uses bounded private staged files collected by the owner after child exit and before staging cleanup. Initial bounds are 256MiB serialized conversion output, 16MiB extraction manifest and 40 million decoded pixels per image; oversized input fails without truncation. Explicit standalone-image/OCR qualification belongs to Task 14.

### Task 12b: Rich-document semantic records and numbered provisions

**Placement:** Required immediately after Task 12 and before Task 13. This completes a missing format/normalizer integration in the original plan; it is not a later-release feature.

**Files:** Create `src/tychora/ingestion/document_records.py`, `tests/ingestion/test_document_records.py`, `tests/qualification/test_docling_records.py`, and `tests/fixtures/documents/create_record_fixtures.py`. Modify `src/tychora/ingestion/documents.py`, the shared `normalize.py` dispatch, `contracts/documents.py` only for needed grouped source-part coordinates, and the Docling capability preflight in `adapters/docling/parser.py`. Keep pure document-record grammar in its own focused module. Any extraction of shared native semantics must be proposed before restructuring the existing native module.

**Interfaces:** Keep `normalize(parsed: ExtractionArtifact, revision_id: str, registry: Registry) -> ExtractionBundle`. The new focused document-record module exposes `normalize_document_records(parsed: ExtractionArtifact, revision_id: str, registry: Registry) -> ExtractionBundle` for numbered/rule/feedback/template strategies; the existing document normalizer dispatch selects it by the registered strategy. Consume Task 12 raw `DocumentUnit`/`MappedDocument` and verified SDK JSON without rewriting those raw parser facts into synthetic source objects. Group canonical EvidenceUnit/StructuredRecordRow/LocatorRow results deterministically at normalization. The existing raw-item coverage validator remains applicable to raw extraction units. Canonical grouped source parts live in explicit immutable typed records/attributes, never pretend to be a real SDK self_ref. Reuse actual bundle assembly helpers where possible without adding an independent storage API. Dependencies:11,12.

**Fixtures:** Implement `document_record_case(kind, format)` in these tests as a thin real pipeline fixture, not a fake parser: build the selected self-authored source using the locked reportlab/python-docx development dependencies; capture bytes with Acquirer; construct explicit workspace/collection/source/revision and DocumentRegistration; run DoclingParser through WorkspaceOwner with the explicit checksum-verified model bundle; normalize and commit using CorpusStore on the owner lane; return the actual parsed artifact, bundle, store and registry. Reuse qualified Task 12 provisioning and registration helpers when they fit; no hidden fixture or parser output is assumed. All sources and source metadata are synthetic. Pure mapping tests may supply explicit SDK export dictionaries for negative cases.

**Synthetic content:** Create separate documents for these cases, each in PDF and DOCX, with source-native headings/paragraphs and labels rather than Markdown pasted into a PDF:

- standard@1: heading `STD-01 - Sample trace`, paragraph `Requirement: Record the sample before changing the instrument.`, continuation paragraph `Keep the original label.`, `Expected evidence: The sample identifier is quotable.`, `Control: Inspect the source.`, `Severity: warning`, `Foundation: REF-MISSING`.
- template@1: heading `Summary`, paragraph `Instruction: State the observed result.`, second paragraph `Keep the uncertainty explicit.`. A second heading `Limits` has `Instruction: State the supported range.`; neither slot swallows its sibling.
- feedback@1: heading `Observation`, `Comment: The baseline is missing.`, `Target: paragraph-3`, with no author/decision/observation/status field. Expected unknowns remain null; no guessed reviewer or generated observation. A second fixture supplies each explicit field and preserves its exact source value.
- law@1: heading `Article 4`, numbered paragraphs `1. Record the sample.` and `2. Preserve the label.`, a subordinate heading `4.1 Observations` and a numbered child, followed by `Article 5`. Retain every independently identified descendant with exact markers and nearest enclosing canonical parent.

**RED/GREEN command:** `uv run --locked --no-sync pytest tests/ingestion/test_document_records.py tests/qualification/test_docling_records.py -q`. Required local model environment is the same as Task 12; missing assets fail with setup guidance. Add one named failing case, run it, implement the minimum, rerun. Do not rerun expensive conversion for every pure negative case; use a scoped qualified fixture lifetime with real captured facts.

- [ ] Write the standard PDF/DOCX pipeline case before changing capability preflight. The initial expected failure is CAPABILITY_MISSING for rule_records@1. After implementation:

```python
result = document_record_case("standard@1", format)
record = next(row for row in result.bundle.records if row.record_type == "rule@1")
assert "Record the sample before changing the instrument." in record.data_json["requirement"]
assert "Keep the original label." in record.data_json["requirement"]
assert record.data_json["proof"] == "The sample identifier is quotable."
assert record.data_json["control"] == "Inspect the source."
assert record.data_json["severity"] == "warning"
assert record.data_json["foundation_ids"] == ()
assert record.data_json["foundation_refs"] == ("REF-MISSING",)
```

The canonical record_type is the registered schema ID (rule@1, template-slot@1 or feedback@1), matching Task 11 and Registry.definition_schema_bindings; do not introduce competing labels.

- [ ] Implement exact configured heading-prefix/label aliases against Docling source structure. Keep literal fields, unknown nulls and same-extraction original foundation rules from11. No arbitrary pack regex, generated interpretation or silent generic-section fallback. A requested unsupported source shape has a named diagnostic/capability failure. Existing structured_sections@1 remains supported; code_symbols@1 remains explicitly rejected by this document adapter.
- [ ] Add positive PDF/DOCX cases for template, explicit feedback and numbered provisions. Compare exact source parts/field strings rather than only record counts. Preserve source table cell/marker text and heading hierarchy; source formatting structure may supply canonical separators but cannot supply new words.
- [ ] Define grouped source-part mapping explicitly. Each part binds retained SDK item/ref/domain/span to its canonical unit range; canonical join separators are marked structural and never given a fictitious original range. A unit crossing pages retains each physical locator and raw provenance. DOCX physical pages remain unknown. Reject missing/duplicate/conflicting part refs, reversed/out-of-range spans, source text mismatch and overlapping parts with contradictory ownership before canonical commit.
- [ ] Retain independently canonical numbered descendants and every overlapping ancestor owner. Persist the complete ownership map needed by Tasks19/27/31/34, including exact-lookup inspection paths; a restricted child cannot become visible through an ancestor representation. Do not implement retrieval policy here, but provide an actual stored roundtrip fixture and exact handoff fields.
- [ ] Add pure export negative cases for ambiguous labels, duplicate identifiers, unrelated heading siblings, field split across items, literal value markup, unsupported custom grammar, missing required field and forged mapped facts. Preserve diagnostic boundaries instead of assigning an unknown field to the previous value. Ordinary Docling text labels do not require Markdown wrappers; never treat exported Markdown as the canonical original.
- [ ] Validate stable normalize/commit retry and replay from verified extraction+SDK JSON after the temporary original payload is removed. Keep raw JSON binding, semantic normalizer fingerprint and immutable revision identity. The test qualifies pure replay only, not authorized original-deletion lifecycle. Confirm parsed text/provenance substitution is rejected and no canonical row is committed.
- [ ] Run native document/record tests plus changed rich-document/runtime/storage tests covering the actual shared diff. Run scoped Ruff, Ruff format, ty and git diff --check. Self-review and commit `feat: normalize rich document records with exact source parts`. Full report must include exact source-part and overlapping-owner contract, strategy support matrix, RED/GREEN evidence, real PDF/DOCX qualification and any explicit limitations. Independent scoped review is required before Task 13.

The engine specification remains binding, including exact configured aliases, source-part provenance, noncitable derived material and immutable version identities.

### Task 13: Conservative table continuation and asset proxy ownership

**Files:** Create `src/tychora/ingestion/{tables,assets}.py`, `tests/ingestion/test_tables.py`, `test_assets.py`.

**Interfaces:** `continuation(a: TableFragment, b: TableFragment) -> ContinuationDecision`; fragments carry page/columns/headers/caption/table number/heading/bbox/page dimensions and source IDs. Decision has score/status/reasons; no human acceptance. `make_proxy(asset_id: str, kind: str, text: str, dependencies: tuple[str,...], derivation: ArtifactRef) -> ProxyRecord`. Dependencies: 11–12.

**Red/green command:** `uv run --locked pytest tests/ingestion/test_tables.py tests/ingestion/test_assets.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the score kernel test:

```python
from tychora.ingestion.tables import continuation_score

def test_missing_features_do_not_inflate_table_confidence():
    assert continuation_score(headers=True, caption=False, heading=True,
                              overlap=False, page_edges=False, marker=False) == 0.5
```

- [ ] Implement hard rejects before this score, then thresholds `>=0.85` high-confidence derived, `>=0.60` proposed, otherwise separate. Exactly matching normalized header rows may be omitted only from reconstructed view, never original fragments.

```python
def continuation_score(*, headers, caption, heading, overlap, page_edges, marker):
    return round(0.35 * headers + 0.20 * caption + 0.15 * heading
                 + 0.10 * overlap + 0.10 * page_edges + 0.10 * marker, 2)
```

- [ ] Store fragment grids/assets and `continues_table@1` assertions with complete support/locators. Row text remains exact source; repeated column labels belong to index representation. Add figure/table original captions as source units and separate noncitable caption/OCR/description/value proxies. Every proxy has one asset, same-extraction nonempty original dependency set, derivation artifact and review state. Pillow validates decoded pixel limits (initial 40 million pixels), MIME/dimensions and explicit recorded resizing; decompression bombs fail before allocation exceeds limits.
- [ ] Test unrelated adjacent tables with same headers, missing geometry, explicit different table IDs, intervening heading, repeated-header preservation and strict unconfirmed continuation. Test proxy cycles, cross-extraction links, original caption citation, image-only source dependencies and rejected proxy exclusion. Run `uv run --locked pytest tests/ingestion/test_tables.py tests/ingestion/test_assets.py -q`; commit with message `feat: preserve table fragments and derived visual proxies`.

### Task 14: Explicit model setup and optional ingestion-only vision

**Files:** Create `src/tychora/application/setup.py`, `src/tychora/adapters/vision/anthropic.py`, `src/tychora/ports/vision.py`, `src/tychora/resources/model-assets.json`, `tests/ingestion/test_vision.py`, `tests/operations/test_setup.py`.

**Interfaces:** `SetupService.plan(selection: tuple[str,...]) -> ModelAssetPlan`, `apply(plan: ModelAssetPlan, local_bundle: Path|None, ctx: ExecutionContext) -> JobHandle`; `VisionProvider.interpret(region: RegionInput, task_schema: JsonValue, ctx: ExecutionContext) -> DerivedObservation`. Types include original pixel/context hashes, allowed candidate target IDs, literal transcription, separate interpretation, uncertainties/model/prompt schema, fixed `needs_review` state. Dependencies: 7–9, 12–13.

**Red/green command:** `uv run --locked pytest tests/ingestion/test_vision.py tests/operations/test_setup.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the cache identity and target validation test:

```python
import pytest
from tychora.contracts.errors import TychoraError
from tychora.adapters.vision.anthropic import validate_target

def test_vision_cannot_invent_annotation_target():
    with pytest.raises(TychoraError) as err:
        validate_target("invented", frozenset({"paragraph-1"}))
    assert err.value.code == "PROVIDER_FAILURE"
```

- [ ] Implement native/local OCR first, then only explicitly selected minimal region remote tasks with current upload permissions. No answer/review-generation route is added. Target validation kernel:

```python
def validate_target(target, allowed):
    if target is not None and target not in allowed:
        raise TychoraError("PROVIDER_FAILURE", "Vision returned an unknown target.")
```

- [ ] Pin cache identity `H(image_sha, task, model, prompt_schema_version, context_sha)`; enforce strict response schema, preserve original literal OCR separately from generated interpretation and leave uncertain target unresolved. Remote providers are constructed only after offline/capability/upload checks. Model must be explicitly configured; no floating fallback. Test malformed JSON, fake confidence, remote timeout and local OCR success causing zero remote requests.
- [ ] Populate model asset manifest from qualified Docling/tokenizer requirements with exact versions/hashes/sizes/licenses/URLs and package ownership. `plan` is read-only; `apply` fetches selected allowlisted assets or imports a verified local bundle into platform cache, no package installation. Test missing/wrong hash/path traversal/offline bundle and no downloads on import/help/search. Run `uv run --locked pytest tests/ingestion/test_vision.py tests/operations/test_setup.py -q`; commit with message `feat: provision explicit model assets and selective vision`.

### Task 15: Immutable repository capture and incremental parse inputs

**Files:** Create `src/tychora/adapters/code/capture.py`, `tests/code/test_capture.py`, `tests/security/test_git.py`.

**Interfaces:** `capture_repository(root: Path, excludes: tuple[str,...], ctx: ExecutionContext) -> CaptureManifest`; original artifact is sorted relative-path/mode/blob/artifact manifest with commit/dirty status. `parse_cache_key(blob_sha: str, grammar: str, options: JsonValue) -> str`. Dependencies: 3, 10.

**Red/green command:** `uv run --locked pytest tests/code/test_capture.py tests/security/test_git.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run `uv run --locked pytest tests/code/test_capture.py -q` with `repo` fixture creating a temporary local repository and committing only synthetic files:

```python
def test_dirty_bytes_are_not_attributed_only_to_head(repo, capture):
    first = capture(repo)
    (repo / "module.py").write_text("def revised(): return 2\n")
    second = capture(repo)
    assert first.original_sha != second.original_sha
    assert second.dirty is True
    assert first.commit == second.commit
```

- [ ] Implement read-only Git inventory and direct filesystem/blob capture. Use argument arrays, no shell or repository checkout/code execution. System Git capability failure affects only repositories. Command baseline:

```python
argv = [git_executable, "--no-optional-locks", "-C", str(root),
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
```

- [ ] Disable user/global Git configuration where it could execute helpers, optional locks and inherited Git environment overrides; never invoke filters, hooks, submodules or `git show` textconv. Apply pathspec excludes, generated/vendor/secret defaults, containment and moving-file checks. Read NUL delimiters without newline splitting. Files with unsupported encoding remain registered with explicit coverage diagnostics. Historical bytes live in artifacts, not HEAD or live files.
- [ ] Test ignored/untracked/deleted/renamed files, paths with spaces/newlines, symlinks, dirty capture races, malicious hooks/filters/fsmonitor never called, no-op capture and one-file changes. Count capture/parse inputs separately; full graph generation remains O(V+E). Run both test files and commit with message `feat: capture safe revision-qualified repository inputs`.

### Task 16: Python language adapter and exact source spans

**Files:** Create `src/tychora/ports/language.py`, `src/tychora/adapters/code/{syntax,python}.py`, `tests/code/test_python.py`.

**Interfaces:** `LanguageAdapter.capabilities() -> LanguageCapabilities`, `parse(file_bytes: bytes, options: JsonValue) -> SyntaxResult`, `definitions(syntax) -> tuple[Definition,...]`, `references(syntax) -> tuple[Reference,...]`, `context_spans(definition) -> tuple[ContextSpan,...]`; `resolve` is completed in 18. Definitions include kind/name/qualified scope/byte span/decorator span/declaration span/parent/overload discriminator. SyntaxResult owns immutable captured bytes, encoding, byte/line/codepoint maps and diagnostics. Dependencies: 3, 15.

**Red/green command:** `uv run --locked pytest tests/code/test_python.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the actual grammar test:

```python
from tychora.adapters.code.python import PythonAdapter

def test_python_decorated_async_method_ranges():
    data = b"class Store:\n    @logged\n    async def load(self):\n        return 1\n"
    adapter = PythonAdapter()
    defs = adapter.definitions(adapter.parse(data, {}))
    method = next(d for d in defs if d.qualified_name == "Store.load")
    assert data[method.byte_start:method.byte_end].startswith(b"@logged")
    assert method.parent_name == "Store"
```

- [ ] Implement the qualified Tree-sitter binding without runtime grammar download. Traverse syntax nodes and preserve decorators/async/header and original byte ranges:

```python
from tree_sitter import Language, Parser
import tree_sitter_python

parser = Parser(Language(tree_sitter_python.language()))
tree = parser.parse(file_bytes)
```

- [ ] Cover functions/classes/methods/nested scopes/imports/calls/inheritance/docstrings and uncovered meaningful statements. Normalize codepoints only in a faithful decoded text view with explicit byte mapping; Python encoding cookies use `tokenize.detect_encoding`. Error nodes produce diagnostics and faithful text fallback; no invented calls. Define context spans for parent header and referenced import units, bounded later at query time.
- [ ] Add real grammar cases for multibyte identifiers, nested classes, repeated names, async/decorators, syntax recovery, unsupported encoding and empty file. Assert substring/line/byte roundtrips. Run `uv run --locked pytest tests/code/test_python.py -q`; commit with message `feat: parse Python definitions and exact structural context`.

### Task 17: TypeScript, JavaScript and declared dialect support

**Files:** Create `src/tychora/adapters/code/typescript.py`, `tests/code/test_typescript.py`, `tests/code/test_javascript.py`.

**Interfaces:** `TypeScriptAdapter(dialect: Literal['typescript','tsx','javascript','jsx'])` implements Task 16 LanguageAdapter, advertising only dialects whose fixture suite passes. Dependencies: 16; does not modify Python extraction semantics.

**Red/green command:** `uv run --locked pytest tests/code/test_typescript.py tests/code/test_javascript.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a real grammar test:

```python
from tychora.adapters.code.typescript import TypeScriptAdapter

def test_exported_method_has_class_and_export_context():
    source = b"export class Store { async load(): Promise<number> { return 1; } }"
    adapter = TypeScriptAdapter("typescript")
    defs = adapter.definitions(adapter.parse(source, {}))
    method = next(d for d in defs if d.qualified_name == "Store.load")
    assert source[method.byte_start:method.byte_end].startswith(b"async load")
    assert method.parent_name == "Store"
```

- [ ] Map explicit installed grammars, not extension guessing after parsing:

```python
import tree_sitter_javascript
import tree_sitter_typescript

grammars = {
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "javascript": tree_sitter_javascript.language,
    "jsx": tree_sitter_javascript.language,
}
```

- [ ] Extract named/exported/arrow functions, methods/classes/interfaces, overload signature discriminators, imports/aliases/exports/inheritance, nested scopes, decorators where supported and JSX/TSX nodes. Advertise limitations for dynamic calls, computed names and unsupported syntax. Preserve uncovered statements as source units and partial parsing diagnostics. Keep alias spellings separate from definition identity.
- [ ] Test each declared dialect, Unicode byte maps, overloads, anonymous callbacks, import aliases, source comments containing names, malformed syntax and exact export spans. Run `uv run --locked pytest tests/code/test_typescript.py tests/code/test_javascript.py -q`; commit with message `feat: parse TypeScript and JavaScript dialects`.

### Task 18: Code identity, resolution diagnostics and sparse structure

**Files:** Create `src/tychora/adapters/code/{resolve,normalize}.py`, `tests/code/test_resolution.py`, `test_structure.py`, `test_dependencies.py`.

**Interfaces:** `definition_key(revision_id: str, path: str, language: str, qualified_name: str, symbol_kind: str, discriminator: str|int) -> str` implements the spec hash formula. `resolve(references: tuple[Reference,...], catalog: SymbolCatalog) -> tuple[Resolution,...]`; `SymbolCatalog` is revision-qualified definitions/import bindings, `Resolution` is exactly resolved/ambiguous/unresolved with rule/targets and cap flags. `normalize_code(capture: CaptureManifest, parsed: tuple[SyntaxResult,...], registry: Registry) -> ExtractionBundle`; `range_dependencies(spans: tuple[OwnedRange,...]) -> Mapping[str,frozenset[str]]`. Dependencies: 5–7, 15–17.

**Red/green command:** `uv run --locked pytest tests/code/test_resolution.py tests/code/test_structure.py tests/code/test_dependencies.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run identity collision test:

```python
from tychora.adapters.code.normalize import definition_key

def test_definition_identity_includes_file_and_revision():
    def key(rev, path):
        return definition_key(rev, path, "python", "validate", "function", 0)
    assert key("r1", "a.py") != key("r1", "b.py")
    assert key("r1", "a.py") != key("r2", "a.py")
```

- [ ] Implement exact qualified target → explicit import binding → unique same-module/scope target; keep alternatives capped at 20 and never select by arbitrary ordering. Emit the resolution state using this final decision kernel:

```python
unique = tuple(sorted(set(candidate_ids)))
state = "resolved" if len(unique) == 1 else "ambiguous" if unique else "unresolved"
selected = unique[0] if state == "resolved" else None
```

- [ ] Create nonsearchable empty file containers, definition/uncovered units, code_symbol records and four typed facets. Persist context spans with original dependencies and complete overlapping-range map. Construct O(n) `contains` immediate-parent and `precedes` immediate-sibling assertions, requiring both endpoints as supports. Cross-repository references require explicit revision mapping. Artifact records bind exact predicate/version, origin/supports and derivation.
- [ ] Test identical short names across files/repos/revisions, overloads, external unresolved targets, ambiguous alternatives, a uniquely resolved wrong target against gold, accepted parent/rejected child dependency boundaries, top-level imports, codepoint spans and zero quadratic sibling edges. Diagnostic rates exclude unsupported syntax and are N/A for empty denominators; scoped inspection later omits records whose disclosures are unauthorized without hidden counts. Run `uv run --locked pytest tests/code -q`; commit with message `feat: retain code provenance and bounded static relationships`.

### Task 19: Exact windows, contextual groups and representation dependencies

**Files:** Create `src/tychora/indexing/{windows,groups,representations,receipts}.py`, `src/tychora/ports/tokens.py`, `tests/indexing/test_windows.py`, `test_groups.py`, `test_receipts.py`.

**Interfaces:** `TokenCounter.count(text: str) -> int`; `window_unit(unit: EvidenceUnit, counter: TokenCounter) -> tuple[SearchWindow,...]`; `build_groups(windows: tuple[SearchWindow,...], config: ResolvedConfig) -> tuple[ContextGroup,...]`; group contains capsule/ordered inputs/window IDs/full dependency IDs/exact input hash. `receipt_digest(records: tuple[IndexRecordDigest,...]) -> str` hashes sorted record/input/value triples. Dependencies: 11, 13, 18.

**Red/green command:** `uv run --locked pytest tests/indexing/test_windows.py tests/indexing/test_groups.py tests/indexing/test_receipts.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run `uv run --locked pytest tests/indexing/test_windows.py -q`; `long_unit` fixture contains 10,000 tokenizer tokens and a unique end marker, built as a real EvidenceUnit.

```python
def test_windows_cover_end_match_and_preserve_exact_ranges(long_unit, counter):
    from tychora.indexing.windows import window_unit
    windows = window_unit(long_unit, counter)
    assert any("END_MARKER" in long_unit.text[w.char_start:w.char_end] for w in windows)
    assert windows[0].char_start == 0
    assert windows[-1].char_end == len(long_unit.text)
    assert all(a.char_end == b.char_start for a, b in zip(windows, windows[1:]))
```

- [ ] Implement target 512/soft ceiling 800 tokens, zero overlap, paragraph/sentence/AST boundaries, exact Unicode offsets and no crossing revision/boundary. Include whitespace through exact adjacent ranges; no normalized summary replaces original text. Atomic rules may have multiple windows. Hash chunks as:

```python
chunk_id = digest("chunk-v1", unit.unit_id, start, end, chunker_fingerprint)
```

- [ ] Build coherent same-extraction contextual groups, deterministic capsule plus ordered windows; omit mutable commit labels from code input while preserving citations separately. Joint groups bind all capsule/window range dependencies; independent code inputs bind their own capsule/window. Group/request counting includes capsule and uses qualified provider tokenizer, not character estimates. Bound groups 24K and requests 28K prechunked tokens; overflow splits only at windows, oversized atomic input fails explicitly.
- [ ] Add tests for context changes invalidating all joint vectors, one-file unchanged parse reuse with new revision IDs, different model/options/dimension cache misses and parent-child restriction dependencies. Hash vector values as actual normalized little-endian float32 bytes, graph values as canonical records. Test creation UUID distinction for equal-config families and same-job retry identity. Run `uv run --locked pytest tests/indexing -q`; commit with message `feat: define exact search windows and immutable index inputs`.

### Task 20: Contextual and independent text embedding providers

**Files:** Create `src/tychora/ports/embeddings.py`, `src/tychora/adapters/voyage/{text,validation}.py`, `tests/providers/test_text.py`, `tests/providers/test_validation.py`, `tests/qualification/test_voyage_live.py`.

**Interfaces:** `TextEmbedder.embed_groups(groups: tuple[ContextGroup,...], ctx: ExecutionContext) -> tuple[EmbeddingRecord,...]`; `embed_queries(queries: tuple[str,...], ctx) -> tuple[EmbeddingRecord,...]`; capabilities declare joint/independent contextualization/tokenizer/model/dimension. `EmbeddingRecord` has record/input/value IDs, space identity and normalized immutable float values. `normalize_vector(values: Sequence[float], dimension: int) -> bytes`. Dependencies: 3, 8, 19.

**Red/green command:** `uv run --locked pytest tests/providers/test_text.py tests/providers/test_validation.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the vector validation test:

```python
import math
import pytest
from tychora.adapters.voyage.validation import normalize_vector
from tychora.contracts.errors import TychoraError

@pytest.mark.parametrize("values", [[0.0, 0.0], [math.nan, 1.0], [1.0]])
def test_invalid_embeddings_cannot_be_sealed(values):
    with pytest.raises(TychoraError):
        normalize_vector(values, 2)
```

- [ ] Implement finite/dimension/nonzero checks, normalization, float32 roundtrip and hash of stored bytes. Validate response counts and unambiguous group/chunk mapping before caching or sealing. The normalization core is:

```python
norm = math.sqrt(sum(value * value for value in values))
if len(values) != dimension or not all(math.isfinite(v) for v in values) or not math.isfinite(norm) or norm == 0:
    raise TychoraError("PROVIDER_FAILURE", "Invalid embedding shape or values.")
packed = struct.pack("<" + "f" * dimension, *(v / norm for v in values))
```

- [ ] Implement qualified SDK calls with explicit `voyage-context-4`, dimension 1024, float32, input_type document/query, `enable_auto_chunking=False`; discard capsule vector only after full response mapping. Independent code mode selects explicit `voyage-code-4` or compatibility `voyage-code-3` and declares independent contextualization. Do not compare their cosine values across families. Verify current model access/tokenizer in explicit capability checks. Retry transient connection/429/5xx at most three attempts with capped jitter and deadline; malformed/model/access errors do not retry.
- [ ] Test shuffled/missing/duplicate response mappings, unavailable tokenizer, exact limits including capsule, unauthorized group member, offline construction guard, cache reuse and credential-binding isolation. Mark real synthetic contextual/code comparison calls `live`; save provider capability receipts without secrets. Run `uv run --locked pytest tests/providers/test_text.py tests/providers/test_validation.py -q`; commit with message `feat: validate contextual and code embedding providers`.

### Task 21: Actual-image embedding provider

**Files:** Create `src/tychora/adapters/voyage/visual.py`, `tests/providers/test_visual.py`, `tests/qualification/test_visual_live.py`.

**Interfaces:** `VisualEmbedder.embed_assets(items: tuple[VisualInput,...], ctx: ExecutionContext) -> tuple[EmbeddingRecord,...]`; `embed_query(query: TextOrImage, ctx) -> EmbeddingRecord`. `VisualInput` binds actual validated pixels, locator/caption, exact dependencies, resize derivation and input hash. Dependencies: 13, 20.

**Red/green command:** `uv run --locked pytest tests/providers/test_visual.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run an injected SDK test with `RecordingVisualSDK` defined in this test file; it records arguments and returns finite synthetic vectors:

```python
def test_visual_input_contains_pixels_not_only_description(visual_provider, pixel_asset, execution):
    visual_provider.embed_assets((pixel_asset,), execution.ctx)
    sent = visual_provider.sdk.calls[0]
    assert sent["model"] == "voyage-multimodal-3.5"
    assert sent["truncation"] is False
    assert any(not isinstance(part, str) for part in sent["inputs"][0])
```

- [ ] Implement SDK interleaved inputs with Pillow images loaded from registered verified bytes. Bind image dimensions/normalized bytes/representation to input identity; text and image remain separate families even at 1024 dimensions. Submit in bounded batches:

```python
inputs = [[item.context, item.image] for item in batch]
response = sdk.multimodal_embed(inputs=inputs, model=model,
                                input_type="document", truncation=False)
```

- [ ] Enforce qualified per-input/per-image limits plus 32 assets/100K estimated multimodal tokens per request, exact count/mapping validation, current upload scope and recorded explicit resize. Text question and image query share only the matching multimodal model space. No truncation or textual proxy substitution for missing pixels. Capability mismatch returns typed unavailable/failure.
- [ ] Test oversized image, hidden context dependency, mismatched dimension, actual image hashing, image-only query, unavailable model and offline miss. Live fixture uses a synthetic labelled diagram and different phrased text query, not only self-image matching. Run `uv run --locked pytest tests/providers/test_visual.py -q`; commit with message `feat: embed original visual evidence in a separate space`.

### Task 22: Reranker adapter with strict index mapping and upload controls

**Files:** Create `src/tychora/ports/reranking.py`, `src/tychora/adapters/voyage/rerank.py`, `tests/providers/test_reranker.py`.

**Interfaces:** `Reranker.rank(query: str, representations: tuple[RankingInput,...], ctx: ExecutionContext) -> tuple[RankedIndex,...]`; RankingInput binds candidate ID/text/dependencies/token count; RankedIndex is original zero-based index plus finite score. `validate_indices(indices: tuple[int,...], count: int) -> None`. Dependencies: 20.

**Red/green command:** `uv run --locked pytest tests/providers/test_reranker.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run duplicate/out-of-range mapping test:

```python
import pytest
from tychora.adapters.voyage.rerank import validate_indices
from tychora.contracts.errors import TychoraError

@pytest.mark.parametrize("indices", [(0, 0), (0, 2), (0,)])
def test_reranker_cannot_drop_duplicate_or_invent_candidates(indices):
    with pytest.raises(TychoraError):
        validate_indices(indices, 2)
```

- [ ] Implement count/index validation and stable score ordering; the kernel is:

```python
def validate_indices(indices, count):
    if len(indices) != count or set(indices) != set(range(count)):
        raise TychoraError("PROVIDER_FAILURE", "Reranker returned invalid candidate mapping.")
```

- [ ] Select explicit `rerank-3` candidate only after preview access/capability verification; unavailable configuration fails rather than replacing model. Set truncation false, query <=2,000 tokens, representations <=1,200 provider tokens and validate aggregate repeated-query limits using installed tokenizer/API capability. Reduce requested pool only with explicit coverage diagnostics or fail strict mode. Do not split arbitrary rerank calls and assume scores are comparable unless the provider contract establishes that behavior. Apply upload permission to every exact span/context dependency immediately before dispatch.
- [ ] Test forbidden context, missing tokenizer, unavailable preview, limit overflow, nonfinite scores, transient retry and strict/degraded treatment. Run `uv run --locked pytest tests/providers/test_reranker.py -q`; commit with message `feat: enforce bounded and attributable reranking`.

### Task 23: Chroma vector family adapter and bounded owner search

**Files:** Create `src/tychora/ports/vectors.py`, `src/tychora/adapters/chroma/index.py`, `tests/storage/test_vector_contract.py`, `tests/qualification/test_chroma.py`.

**Interfaces:** `VectorIndex.create_family(spec: FamilySpec) -> FamilyHandle`, `upsert_batch(batch: VectorBatch, ctx) -> BatchReceipt`, `verify_batch(receipt) -> Verification`, `search(query_vector: bytes, eligible_records_and_owners: Mapping[str,CandidateKey], candidate_limit: int, work_limits: WorkLimits, ctx) -> CandidatePage`, `drop_records(record_ids, ctx) -> None`. FamilySpec is immutable workspace/channel/provider/model/dimensions/metric/representation/creation UUID. CandidateKey is `(kind: 'unit'|'asset', id: str)`. CandidatePage has dense ranked owners/window IDs/truncation/coverage and counters. Dependencies: 3, 19–21.

**Red/green command:** `uv run --locked pytest tests/storage/test_vector_contract.py tests/qualification/test_chroma.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run real adapter test against two synthetic vectors and one forbidden nearer point:

```python
def test_only_eligible_owners_enter_vector_candidates(vector_index, vector_family, execution):
    vector_index.upsert_batch(vector_family.batch([("hidden", "u0", [1., 0.]),
                                                 ("visible", "u1", [.8, .6])]), execution.ctx)
    page = vector_index.search(vector_family.query([1., 0.]), {"visible": ("unit", "u1")},
                               1, vector_family.limits, execution.ctx)
    assert [c.key for c in page.candidates] == [("unit", "u1")]
```

- [ ] Define `vector_family` fixture as explicit real FamilySpec/batch/query constructors in `tests/storage/conftest.py`; no semantic fake backend. Use qualified Chroma `embedding_function=None`, cosine HNSW `ef_search=200`, persisted full metadata and namespace hash. Compare existing immutable IDs/input/value before retry upsert; different content fails integrity. Durability verification reopens the real store and checks IDs/value digests.
- [ ] Implement eligible ID intersection with verified present IDs; missing expected records mark incomplete coverage. Partition all IDs at <=5,000, max 64 partitions; reject larger scope instead of dropping partitions. Search typed source/proxy owner pools with one family work budget. Begin at `min(2*k, size)` per partition, collapse owners by best distance and adaptively double eligible partitions whose approximate tail can improve kth owner. Core query:

```python
raw = collection.query(query_embeddings=[query], ids=partition_ids,
                       n_results=requested, include=["distances"])
work.returned_records += sum(len(row) for row in raw["ids"])
```

- [ ] Bound cumulative returned records at 20,000 and deadline; report truncation distinctly from exhaustion. Add adversarial tests with one owner occupying hundreds of windows, k unique owners across partitions, missing points, immutable batch conflicts, old snapshot membership, cancellation/reopen and exact exhaustive scoring on a tiny dataset. ANN recall is separately measured, not asserted exact on arbitrary data. Run contract and qualification tests; commit with message `feat: search Chroma within immutable eligible batches`.

### Task 24: Immutable graph generation and grounding validation

**Files:** Create `src/tychora/ports/graph.py`, `src/tychora/adapters/ladybug/generation.py`, `tests/storage/test_graph_generation.py`, `tests/qualification/test_ladybug.py`.

**Interfaces:** `GraphStore.begin_generation(spec: GraphGenerationSpec) -> str`, `upsert_batch(generation_id: str, assertions: tuple[RelationshipAssertion,...], units: tuple[EvidenceUnit,...], ctx) -> None`, `seal_generation(generation_id: str, expected: GraphManifest, ctx) -> BatchReceipt`, `drop_generation(generation_id: str, ctx) -> None`. Assertion carries versioned predicate/subject/object/origin/nonempty supports/optional exact support spans/status/method/confidence/immutable provenance; graph manifest carries full membership and decision watermark. Dependencies: 3, 5–8, 18–19.

**Red/green command:** `uv run --locked pytest tests/storage/test_graph_generation.py tests/qualification/test_ladybug.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a real generation test that rejects missing required supports:

```python
import pytest
from tychora.contracts.errors import TychoraError

def test_graph_cannot_seal_ungrounded_assertion(graph, generation, assertion, execution):
    bad = assertion.model_copy(update={"required_support_ids": ()})
    with pytest.raises(TychoraError) as err:
        graph.upsert_batch(generation, (bad,), (), execution.ctx)
    assert err.value.code == "INTEGRITY_ERROR"
```

- [ ] Implement the exact five node/relationship-table definitions from spec §4 in qualified Ladybug syntax; use generation-qualified primary keys, first-class RelationAssertion nodes and structural GroundedIn/Subject/Object/SupportedBy edges. Persist assertions as canonical JSONL artifacts before projection. Node identity kernel:

```python
pk = digest("graph-node-v1", generation_id, external_id)
```

- [ ] Upsert nodes/edges separately in bounded transactions through one Database's connections. Compare hashes on retries, reject writes after seal. Validate exactly one grounding/subject/object, all support edge/list equality, canonical unit/revision/extraction/locator binding, predicate endpoint/cardinality/revision rules and current decision watermark. Full graph build copies stored assertions for selected membership without rerunning parsers/embedders.
- [ ] Test repeated upsert, contradictory content, rollback, unresolved endpoints, cross-revision binding, proposed model assertions, reopened sealed generation, concurrent read requests and forced termination. Run `uv run --locked pytest tests/storage/test_graph_generation.py tests/qualification/test_ladybug.py -q`; commit with message `feat: seal immutable evidence-grounded graph generations`.

### Task 25: Policy-filtered graph traversal and structural context

**Files:** Create `src/tychora/adapters/ladybug/traversal.py`, extend `src/tychora/ports/graph.py`, create `tests/retrieval/test_graph_policy.py`, `test_graph_pagination.py`, `test_structural_context.py`.

**Interfaces:** `expand(view: EligibilityView, seed_unit_ids: tuple[str,...], limits: GraphBudget, ctx) -> GraphCandidates`; `structural_context(view, seed_unit_ids, limits, ctx) -> GraphCandidates`; `get_assertions(view, selected_unit_ids, predicates, limits, ctx) -> AssertionPage`. Define EligibilityView fields exactly from spec §5 now; Task 27 compiles it. GraphBudget is a shared mutable request-local counter for five seeds/32 new units/2,000 assertion occurrences; inspection adds max two hops/100 returned edges. Dependencies: 24.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_graph_policy.py tests/retrieval/test_graph_pagination.py tests/retrieval/test_structural_context.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run hidden-support test using sealed real graph fixtures:

```python
def test_hidden_support_blocks_visible_endpoints(graph_fixture):
    f = graph_fixture
    view = f.view(context={"a", "b"}, candidates={"a", "b"}, structural=set())
    page = f.graph.expand(view, ("a",), f.budget(), f.ctx)
    assert "b" not in {c.unit_id for c in page.candidates}
```

- [ ] Define fixture assertion a→b supported by a,b,hidden; omit hidden from view. Implement parameterized queries from spec §4 with eligible endpoints/intermediates/origin/all supports/status/predicate/generation and current blocked assertion overlay inside selection before LIMIT. Latest decision beyond receipt watermark blocks even a new confirmation until rebuild.
- [ ] Page deterministically at 128 plus one lookahead; merge directions by status/confidence/target/PK, deduplicate target IDs, distinguish exhaustion/candidate cap/work cap. Increment shared work per inspected eligible assertion occurrence, including both links in sibling pattern:

```python
if budget.inspected_rows + path.assertion_occurrences > budget.max_rows:
    return page.with_truncation("edge_work_limit")
budget.inspected_rows += path.assertion_occurrences
```

- [ ] Implement immediate adjacency and child-parent-child sibling patterns, no skipping denied immediate neighbor. Per seed: previous/next then up to two nearest additional siblings; 16/16 strategy candidate allocations and unused slot return. Structural containers may ground/support but cannot be returned text. Test 1,000 duplicate observations, high-degree nodes, direction merge, hidden parent, method-only candidate facets, rejected child range dependencies, deadline and GC lease after cancellation. Run three test files; commit with message `feat: traverse eligible graph paths with shared work bounds`.

### Task 26: Resumable projections and atomic snapshot publication

**Files:** Create `src/tychora/indexing/coordinator.py`, `src/tychora/adapters/sqlite/publication.py`, `src/tychora/application/sources.py`, `tests/indexing/test_publication.py`, `test_recovery.py`.

**Interfaces:** `ProjectionCoordinator.build(bundle: ExtractionBundle, base_snapshot_id: str|None, job: JobHandle, ctx) -> PublicationPlan`; plan binds membership/required slots/batches/graph generation/receipts. `CorpusStore.publish_snapshot(plan: PublicationPlan) -> str`, `resolve_snapshot(selector: str|None) -> SnapshotView`. `SourceService.add(source, scope, idempotency_key=None) -> JobHandle`, `sync(source_id, scope, idempotency_key=None) -> JobHandle`. Dependencies: 6–25; initial lexical batch writer in this task uses schema triggers, search follows in 28.

**Red/green command:** `uv run --locked pytest tests/indexing/test_publication.py tests/indexing/test_recovery.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run failure-preserves-active test; `published_corpus` fixture builds real SQLite plus recording failure-injection ports, never claims native qualification:

```python
import pytest
from tychora.contracts.errors import TychoraError

def test_partial_new_projection_does_not_replace_active(published_corpus):
    f = published_corpus
    old = f.store.resolve_snapshot(None).snapshot_id
    f.vector.fail_next_batch = True
    with pytest.raises(TychoraError) as err:
        f.add_revision("new source text")
    assert err.value.code == "PROVIDER_FAILURE"
    assert f.store.resolve_snapshot(None).snapshot_id == old
```

- [ ] Implement source-local immutable batches and complete manifests, including required zero-count image batches. All artifacts must be durable before SQL references. No parser/provider inside SQL transaction. Resume checkpoints by exact inputs; verify all IDs/counts/dimensions/input/value digests and backend readiness. Retry same batch cannot change content. Graph binds full selected membership and current decision watermark.
- [ ] Publish under one compare-and-switch transaction:

```python
with corpus.immediate_transaction():
    if corpus.active_snapshot_id() != plan.base_snapshot_id:
        raise TychoraError("WORKSPACE_BUSY", "Publication base changed; rebase required.", retryable=True)
    corpus.insert_snapshot_bindings(plan)
    corpus.set_active_snapshot(plan.snapshot_id)
```

- [ ] Define the three internal corpus helpers shown above here and test exact membership/slot/batch foreign-key semantics. Rebase serialized jobs against current membership, never lose another source. Pin snapshots during queries; retain active/previous graph generations and explicit pins. Reconcile missing receipt, sealed-but-unpublished graph and publication-before-completion-event. Test old query/new publication, concurrent additions, unchanged parse/vector reuse and cancelled job cannot publish. Run `uv run --locked pytest tests/indexing -q`; commit with message `feat: publish complete immutable retrieval snapshots`.

### Task 27: Eligibility compiler, temporal selection and upload boundary

**Files:** Create `src/tychora/adapters/sqlite/eligibility.py`, `src/tychora/retrieval/eligibility.py`, `tests/retrieval/test_eligibility.py`, `test_temporal.py`, `test_policy_cache.py`.

**Interfaces:** `compile_eligibility(snapshot: SnapshotView, scope: RequestScope, request: QueryRequest, config: ResolvedConfig) -> EligibilityView`; `eligible_cache_key(view) -> tuple[str,int,str,str,str]`; `revalidate_epoch(view, stage: Literal['provider','delivery']) -> None`. Dependencies: 5–6, 18–19, 25–26.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_eligibility.py tests/retrieval/test_temporal.py tests/retrieval/test_policy_cache.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run the cache separation regression test:

```python
from tychora.retrieval.eligibility import eligible_cache_key

def test_audit_and_compose_do_not_share_eligibility(views):
    assert views.audit.snapshot_id == views.compose.snapshot_id
    assert views.audit.scope_digest == views.compose.scope_digest
    assert eligible_cache_key(views.audit) != eligible_cache_key(views.compose)
```

- [ ] Define `views` fixture with identical principal/snapshot/explicit filters but different mode policy. Compile from exact snapshot membership/batches before any channel LIMIT. Key must be:

```python
def eligible_cache_key(view):
    return (view.snapshot_id, view.policy_epoch, view.scope_digest,
            view.eligibility_policy_digest, view.eligibility_request_digest)
```

- [ ] Separate source-candidate/context/structural/proxy/chunk/asset/vector ID sets and full representation/group dependencies. Intersect collection/source floors, granted uses, source/child review, withdrawal, mode/lane, typed facets and proxy policy. Check historical kind mappings before lane filtering. Accepted parent never exposes rejected child. Provider upload eligibility is separate from readable eligibility; denied upload cannot be bypassed through capsule, asset proxy, graph support or record.
- [ ] Implement explicit revisions > all > current. With dates, filter all retained editions first; half-open effective interval and publication cutoff are independent. Latest applicable date wins; tied/overlapping qualifying winners warn, unknown permissive editions supplement without replacing known winner. Null end remains open. Add exact boundary/cutoff-only/unknown/tie/current default tests.
- [ ] Recheck policy epoch before every external dispatch and delivery; if changed, discard affected results and retry eligibility once, second change `SCOPE_CHANGED`. No SQLite read transaction across provider calls. Tests cover warm audit→compose, proxy-policy change, global vs missing scope, new access floor, rejection events, filters with mixed-scope OR, source-range/group dependencies and withdrawal mid-query. Run `uv run --locked pytest tests/retrieval/test_eligibility.py tests/retrieval/test_temporal.py tests/retrieval/test_policy_cache.py -q`; commit with message `feat: compile complete policy and temporal eligibility`.

### Task 28: Exact identifiers and safe contextual FTS search

**Files:** Create `src/tychora/retrieval/lexical_query.py`, `src/tychora/adapters/sqlite/lexical.py`, `src/tychora/ports/lexical.py`, `tests/retrieval/test_exact.py`, `test_lexical.py`.

**Interfaces:** `lexical_expression(text: str, stopwords: frozenset[str]) -> str|None`; `LexicalIndex.search(view: EligibilityView, query: str, candidate_limit: int, ctx) -> ChannelCandidates`; `CorpusStore.lookup_identifiers(namespaced: tuple[Identifier,...], view, limit: int) -> ChannelCandidates`. ChannelCandidates includes independent source/asset ranked pools, matching window IDs, statistics epoch and completeness. Dependencies: 19, 26–27.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_exact.py tests/retrieval/test_lexical.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run malicious MATCH-syntax test:

```python
from tychora.retrieval.lexical_query import lexical_expression

def test_user_query_cannot_supply_fts_operators():
    expression = lexical_expression('alpha OR beta NEAR(secret)', frozenset())
    assert expression is not None
    assert 'NEAR(' not in expression
    assert '"alpha"' in expression
```

- [ ] Implement explicit identifier namespaces/case rules and free-text registered extractor routing (32 candidates/16 identifiers). `exact_only` uses equality lookup without lexical/semantic fallback or expansion. For FTS select <=32 distinct terms by frequency/length/first occurrence, up to four explicit quoted phrases <=8 terms within same budget; escape and OR quoted clauses. Empty expression returns no candidates. SQL kernel stays in a MATCH scoring context:

```sql
WITH scored AS MATERIALIZED (
  SELECT r.rowid, r.chunk_id,
         bm25(lexical_fts,5.0,3.0,2.0,1.0,0.5) AS score
  FROM lexical_fts JOIN lexical_records r ON r.rowid=lexical_fts.rowid
  JOIN eligible_lexical_rows e ON e.rowid=r.rowid
  WHERE lexical_fts MATCH ?
)
SELECT chunk_id, score FROM scored ORDER BY score, chunk_id;
```

- [ ] Materialize eligible rows from exact selected batches in a short read transaction; group windows by typed unit/asset owner before applying independent 80-owner caps and dense ranks. Capture statistics epoch in same transaction. No raw user field interpolates identifiers/SQL. Temp eligible tables are owner-connection scoped and cleared per request; concurrent operations serialize use of that connection or use isolated owner-controlled connections.
- [ ] Test punctuation IDs, 100 windows from one owner, matching end window, diacritics, quoted phrases, stopwords, hidden high-score rows, old snapshot with new unpublished physical FTS rows and ranking-cache invalidation on epoch. Exact match absence stays empty in exact mode. Run two files; commit with message `feat: retrieve exact and contextual lexical evidence before limits`.

### Task 29: Rank fusion, shared graph admission and lane reservations

**Files:** Create `src/tychora/retrieval/{fusion,admission,graph}.py`, `tests/retrieval/test_fusion.py`, `test_admission.py`.

**Interfaces:** `fuse(channels: Mapping[str,tuple[RankedCandidate,...]], weights: Mapping[str,float]) -> tuple[FusedCandidate,...]`; `admit(candidates, exact_ids, graph_lists, lane_policy, limit: int=64) -> Admission`; `augment(view, fused, config, graph: GraphStore, ctx) -> GraphCandidates`. RankedCandidate uses typed key/dense rank/window IDs; FusedCandidate adds scores/signals/reasons; Admission records selected/discarded/reservation coverage. Dependencies: 22–23, 25, 27–28.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_fusion.py tests/retrieval/test_admission.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run no-long-document-vote test:

```python
import pytest
from tychora.retrieval.fusion import rrf_score

def test_rrf_uses_one_vote_per_channel():
    assert rrf_score({"lexical": 1, "text": 2}, {"lexical": 1., "text": 1.}) == pytest.approx(1/61 + 1/62)
```

- [ ] Implement best-window collapse per owner/channel, one-based ranks and exact/lexical/text/visual/graph weights `2/1/1/.7/.35`. Across text families take minimum rank, sort by rank/ID and rerank densely for one text vote. Stable tie-break by typed ID; hints add capped total .01 only when enabled.

```python
def rrf_score(ranks, weights):
    return sum(weights[channel] / (60 + rank) for channel, rank in ranks.items())
```

- [ ] Pick one top-five eligible source seed list, give applicable subsets without refill to code structural/semantic expansion. Share 32 new units/2,000 rows and 16/16 initial strategy allocations; round-robin structural first, one graph vote per target. Within 64 rerank candidates admit <=32 exact, <=8 graph-only (4/4 when both strategies), then requested lane reservations in configured order, then fused rank. Return unused slots and deduplicate every step.
- [ ] Test 64 strong text hits plus weak graph-only target, equal-score IDs, many text families, mixed-corpus seed list, same target from both strategies, reservation saturation and explicit missing-lane reasons. Graph-only admission guarantees assessment, not top result. Run `uv run --locked pytest tests/retrieval/test_fusion.py tests/retrieval/test_admission.py -q`; commit with message `feat: fuse evidence ranks and preserve bounded assessment diversity`.

### Task 30: Matched-window rerank representations and failure behavior

**Files:** Create `src/tychora/retrieval/rerank.py`, `tests/retrieval/test_rerank_inputs.py`, `test_degraded.py`.

**Interfaces:** `ranking_input(candidate: FusedCandidate, units: Mapping[str,EvidenceUnit], view: EligibilityView, counter: TokenCounter, budget: int=1200) -> RankingInput`; `rerank_candidates(query: QueryRequest, admission: Admission, ctx) -> RankedEvidence`. RankedEvidence retains input candidate/window/anchor identities and omitted-match counts. Dependencies: 19, 22, 27, 29.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_rerank_inputs.py tests/retrieval/test_degraded.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run end-anchor test:

```python
def test_rerank_representation_keeps_end_match(long_candidate, units, view, counter):
    from tychora.retrieval.rerank import ranking_input
    item = ranking_input(long_candidate, units, view, counter, budget=1200)
    assert "END_MARKER" in item.text
    assert item.anchor_ranges[0].char_start > 0
    assert counter.count(item.text) <= 1200
```

- [ ] Build strongest matched window first (channel rank, weight, offset), append nonoverlapping matched windows as budget permits, then exact parent/import context <=160 tokens with dependencies. Never use unrelated whole-unit prefix for a retrieved end match. For graph-only use valid target-support span, otherwise labelled unit fallback. Slice canonical source via exact range:

```python
excerpt = unit.text[anchor.char_start:anchor.char_end]
if excerpt != ranking_range.text:
    raise TychoraError("INTEGRITY_ERROR", "Ranking range is not source text.")
```

- [ ] Preserve smaller explicit subrange/truncation metadata when a window itself exceeds provider budget; do not rewrite it. Check all upload dependencies before remote dispatch. In permissive degraded mode retain allowed successful fused results with channel failure status; strict required rerank fails. Denied upload never sends a subset disguised as a complete source. Text reranker does not score image pixels; asset ranking remains independent.
- [ ] Test two disjoint matches, graph support/no support, parent context dropped before winner, upload denial, unavailable preview, timeout, duplicate indices and no remote fallback in offline mode. Run both tests; commit with message `feat: rerank exact matched representations with explicit degradation`.

### Task 31: Exact segment assembly and independent visual selection

**Files:** Create `src/tychora/retrieval/{anchors,segments,visuals}.py`, `tests/retrieval/test_segments.py`, `test_visual_selection.py`.

**Interfaces:** `assemble_segments(ranked: RankedEvidence, view: EligibilityView, config: ResolvedConfig, counter: TokenCounter) -> Selection`; Selection includes provisionally selected segments, reservation priority, omitted anchors and lane coverage. `select_visuals(assets: tuple[AssetCandidate,...], segments: tuple[Segment,...], limit: int=3) -> tuple[VisualEvidence,...]`. Dependencies: 13, 25, 27, 30.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_segments.py tests/retrieval/test_visual_selection.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run source-substring test:

```python
def test_segment_keeps_winning_range_and_never_bridges_denied_gap(segment_case):
    f = segment_case
    result = f.assemble(allowed={"left", "right"}, forbidden={"gap"})
    assert len(result.segments) == 2
    for segment in result.segments:
        for excerpt in segment.excerpts:
            assert excerpt.text == f.units[excerpt.unit_id].text[excerpt.char_start:excerpt.char_end]
    assert all("gap" not in s.context_only_unit_ids for s in result.segments)
```

- [ ] Implement typed anchors from retrieved window/assertion support/exact unit/fallback; no fabricated indexed IDs. Preserve winning range, boundary-aligned expansion, disjoint intervals, one eligible ordinal gap and at most one eligible neighbor each end. Same extraction/boundary only, cap 1,200 rendered segment tokens, explicit incomplete/full-fetch reference. Code parent/import excerpts separate and <=160 tokens; deduplicate overlapping original file ranges. Table rows cite actual fragments, never a fabricated whole-table bbox/page.
- [ ] Apply final lane reservation then strongest-match rank, <=8 segments, default <=2 per document/code file, never entire repository as one bucket. Provisional text budget reserves visual space:

```python
visual_tokens = min(200 * visual_limit, total_tokens // 10)
text_tokens = total_tokens - visual_tokens
```

- [ ] Select <=3 visuals: best native multimodal first; distinct companion of strongest selected text second; remaining asset RRF. Native wins when limit is one. Keep reasons and no fabricated source-link score. Use original assets/captions plus labelled noncitable proxies. No text re-selection loop after visuals; unused reserve may remain.
- [ ] Test distant windows, winning match near 10,000-token unit end, large exact lookup, hidden gap, table continuation strict/permissive, duplicate class/method body, relevant image with weak/no proxy, image-only query, companion already native, and tight limit/reservations. Run both files; commit with message `feat: assemble bounded source excerpts and original visual evidence`.

### Task 32: Record/decision attachment, pure formatting and exact response fitting

**Files:** Create `src/tychora/retrieval/response.py`, `src/tychora/formatting/{render,hierarchical,budget}.py`, `tests/formatting/test_render.py`, `test_response_budget.py`, `tests/fixtures/results/canonical.json`, `hierarchical.json`, `agent.txt`.

**Interfaces:** `ResponseAssembler.assemble(selection: Selection, records: tuple[StructuredRecord,...], checks: RelationChecks, request: QueryRequest, ctx) -> DeliveredResult`; `DeliveredResult` has canonical QueryResult, exactly verified render target bytes and operational counts (not self-referential DTO fields). `format_result(result: QueryResult, output_profile: OutputProfile, *, counter: TokenCounter|None=None) -> str`; `render_unchecked(result, profile) -> bytes` internal pure helper. RelationChecks contains complete/partial/not_evaluated and eligible assertions/decisions. Dependencies: 5, 25, 27, 31.

**Red/green command:** `uv run --locked pytest tests/formatting -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run aggregate delivery test; fixed fixture counters use real `cl100k_base` provisioned locally, never model downloads:

```python
def test_mcp_canonical_and_envelope_are_counted_together(response_case):
    delivered = response_case.assemble(targets=("canonical-json@1", "agent@1"), tokens=900)
    assert sum(response_case.counter.count(b.decode()) for b in delivered.payloads) <= 900
    assert sum(len(b) for b in delivered.payloads) <= 1048576
    assert delivered.result.budget_contract.max_tokens == 900
```

- [ ] Before fitting, run bounded `get_assertions` separately from rank expansion using remaining 2,000-row budget and <=100 assertions. Disabled graph checks are not_evaluated; exhausted budget partial. Attach eligible structured records, exact conflicts, version ambiguities and recorded decision requirements; dependencies include embedded quotes/targets. Evaluate configured required lanes/kinds/schemas and conditional request context; bounded absence is never a corpus-wide claim.
- [ ] Implement canonical JSON, hierarchical-evidence@1, Markdown and escaped agent envelope. Labels/aliases are fixed renderer parameters; reject collisions/invalid tags/unsupported negotiation. Preserve canonical IDs, citations, source-vs-derived flags, coverage/check semantics. `answerability` aliases deterministic coverage, not truth. Renderer kernel:

```python
import html

def envelope(tag: str, body: str) -> str:
    if not tag or not tag.replace("_", "a").replace("-", "a").isalnum() or not tag[0].isalpha():
        raise TychoraError("INVALID_REQUEST", "Invalid envelope tag.")
    return f"<{tag}>\n{html.escape(body, quote=False)}\n</{tag}>"
```

- [ ] Materialize runtime IDs/timing before last count. Count actual delivered canonical/hierarchical/text representations in aggregate, including MCP text echo if unavoidable; 6,000 tokens/1 MiB default, lowerable. Keep counters operational to avoid self-reference. Omit in finite spec order: optional trace, optional full records with stubs, context-only excerpts, additional nonwinning intervals/nonreserved visuals, entire nonreserved then reserved owners. Never rewrite literal fields, strip required owner records alone, or discard the last match to simulate successful empty output.
- [ ] After every omission recompute citations/requirements/gaps/status from retained owners; partial relation checks never become complete by dropping owners. If mandatory zero-hit core or last matched owner cannot fit, `BUDGET_TOO_SMALL`; later pure reformat exceeding cap returns `OUTPUT_BUDGET_EXCEEDED` without pruning. Final verified bytes delivered unchanged. Test long feedback records, localized labels, mandatory warnings, required-lane output omission, exact immutable quotes, tiny budget, new cached run ID length, and pure formatting with IO/provider methods set to raise.
- [ ] Run `uv run --locked pytest tests/formatting -q`; compare frozen canonical→hierarchical/agent fixtures allowing only declared volatile fields. Retrieval equivalence remains a separate Task 42 gate. Commit with message `feat: fit complete evidence responses and deterministic projections`.

### Task 33: Query orchestration, cache isolation and retained diagnostics

**Files:** Create `src/tychora/retrieval/{query,cache}.py`, `src/tychora/application/query.py`, `src/tychora/adapters/sqlite/history.py`, `tests/retrieval/test_query.py`, `test_caches.py`, `tests/operations/test_history.py`.

**Interfaces:** `QueryService.search(request: QueryRequest, scope: RequestScope, ctx: ExecutionContext) -> DeliveredResult`; `QueryCaches` exposes query-embedding/result LRU get/put/purge with injected monotonic clock; `HistoryRepository.record(result, metrics, retained_payload: ArtifactRef|None) -> None`, `purge(expired_before) -> int`. Dependencies: 20–32.

**Red/green command:** `uv run --locked pytest tests/retrieval/test_query.py tests/retrieval/test_caches.py tests/operations/test_history.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run cache invalidation test:

```python
def test_filter_change_reuses_embedding_but_retrieves_again(query_case):
    f = query_case
    a = f.search(question="verification", project="alpha")
    b = f.search(question="verification", project="beta")
    assert f.provider.query_embedding_calls == 1
    assert f.lexical.calls == 2
    assert a.run_id != b.run_id
    assert not ({s.source_id for s in a.segments} & {s.source_id for s in b.segments})
```

- [ ] Define `query_case` with real corpus/FTS and recording synthetic provider/index ports; assertions test mechanics only. Orchestrate validate/pin→compile eligibility→parallel exact/lexical/authorized vector channels→fusion/graph→rerank→assembly→checks/records→fit→policy revalidation/delivery. `exact_only` bypasses all expansions/rerank; image-only runs visual plus linked context with no invented text query. Request text >2,000 provider tokens fails actionable validation; no hidden generation. Use shared deadline/cancellation and bounded owner queues.
- [ ] Implement query-embedding LRU 256 entries/5-minute TTL, exact prepared input/model/options/endpoint/space/workspace/principal/upload-scope/non-secret credential-binding identity. Result LRU initial 128 entries/5-minute TTL (implementation memory default, configurable), key includes snapshot/policy/lexical epochs/full request/config/render/budget. Pin resulting memory within max 64 MiB configured total; entry exceeding cap bypasses cache. Filter-only changes reuse query vector but not result. Kernel expiration:

```python
if entry is None or clock.monotonic() >= entry.expires_at:
    cache.pop(key, None)
    return None
```

- [ ] Cache hits still revalidate policy, regenerate run ID/timing, refit current targets and count actual bytes. History defaults to IDs/counts/timings/fingerprints for 30 days; no text/passages/provider bodies. Optional full local history is separate, disabled by default; purge history differs from cache purge and never deletes provenance or durable idempotency. Replay checks retained request/current access/snapshot/projection/config/provider availability before execution; missing inputs explained without attempting providers.
- [ ] Test second policy change, withdrawal before TTL, lexical epoch changes without membership change, credential/provider swap, offline cache miss, strict partial failure, cache TTL/LRU/purge, zero optional history, retained replay limitations and code-sensitive context hash. Run targeted files; commit with message `feat: orchestrate evidence retrieval with scoped caches and history`.

### Task 34: Scoped discovery, exact evidence/assets and relation inspection

**Files:** Create `src/tychora/application/{discovery,inspection}.py`, `tests/application/test_discovery.py`, `test_inspection.py`.

**Interfaces:** `DiscoveryService.discover(request: DiscoverRequest, scope: RequestScope) -> DiscoveryResult`; `InspectionService.inspect(request: InspectRequest, scope, ctx) -> InspectionResult`; `relations(request: RelationRequest, scope, ctx) -> RelationResult`. Dependencies: 5, 25, 27, 33.

**Red/green command:** `uv run --locked pytest tests/application -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run zero-count kind discovery test:

```python
def test_empty_configured_kind_remains_discoverable(discovery, reader):
    from tychora.contracts import DiscoverRequest
    result = discovery.discover(DiscoverRequest(subject="kinds"), reader)
    standard = next(k for k in result.items if k.key == "standard")
    assert standard.description
    assert standard.examples
    assert standard.source_count == 0
```

- [ ] Implement separate definitions and scoped counts; expose facets/operators, mode/kind/relations, schemas, installed/required/missing capabilities, language dialects/reference forms, models/config origin, per-stage source/index coverage and limits. No model import/download or DB creation during configuration-only discovery; corpus counts explicitly require an opened owner. Cursor pagination uses stable typed keys; unknown subjects fail with available subjects.
- [ ] Exact inspection accepts only registered IDs and retained authorized ranges; return source text/canonical IDs/locators/original or registered assets after complete dependency checks. Kernel remains exact source slicing:

```python
if not (0 <= start <= end <= len(unit.text)):
    raise TychoraError("INVALID_REQUEST", "Range lies outside the source unit.")
text = unit.text[start:end]
```

- [ ] Original reads check binding tombstones (Task 39), not raw hash existence. Full parent/file inspection cannot leak denied child; explicitly partial permitted ranges stay noncontiguous. Relations semantic/structural use existing graph budget/overlay. Diagnostics omit unsafe reference alternatives/support disclosures and hidden counts, label scope limitation. A run with no retained request cannot replay; graph GC requires rebuild despite retained manifest.
- [ ] Test hidden collection counts, zero kinds, missing adapters without import, asset traversal path attempts, unauthorized/unknown IDs, removed original, scoped code resolution rates, source-range restriction and graph inspection limit. Run `uv run --locked pytest tests/application -q`; commit with message `feat: expose scoped corpus discovery and exact evidence inspection`.

### Task 35: Primary sync/async Python API and public-only adapter example

**Files:** Create `src/tychora/api.py`, `src/tychora/clients/{sync,async_}.py`, `src/tychora/application/services.py`, extend `src/tychora/__init__.py`; create `tests/api/test_public_sync.py`, `test_public_async.py`, `test_scope.py`, `examples/knowledge_adapter.py`, `docs/api.md`.

**Interfaces:** `Tychora.open(path: str|Path, *, scope: RequestScope|None=None, config: ResolvedConfig|None=None, providers: ProviderBindings|None=None, token_counter: TokenCounter|None=None) -> Tychora`; context manager closes only owned resources. `AsyncTychora.open(...) -> AsyncTychora` is a synchronous factory returning an async context manager with equivalent parameters. `connect(endpoint: str, credential: str, workspace_id: str) -> client` for each class is implemented through Task 36 remote transport. Public `ProviderBindings` is a typed injection bundle for text/visual/rerank/vision ports, no DB handles. Dependencies: 26, 33–34.

| Client namespace | Exact sync signature; async equivalent awaits result |
| --- | --- |
| client | `discover(request: DiscoverRequest|Mapping) -> DiscoveryResult`; `search(request: QueryRequest|Mapping, *, scope: RequestScope|None=None, timeout: float|None=None) -> QueryResult` |
| sources | `add(source: SourceInput|Mapping, *, idempotency_key: str|None=None) -> JobHandle`; `sync(source_id: str, *, idempotency_key: str|None=None) -> JobHandle` |
| evidence | `get(unit_id: str, *, start: int|None=None, end: int|None=None) -> InspectionResult` |
| assets | `get(asset_id: str) -> InspectionResult`; `read(asset_id: str) -> bytes` bounded and registered |
| relations | `traverse(request: RelationRequest|Mapping) -> RelationResult` |
| jobs | `get(job_id: str) -> JobHandle`; `wait(job_id: str, *, timeout: float|None=None) -> JobHandle`; `cancel(job_id: str) -> JobHandle`; `events(job_id: str, *, after: int=0) -> Iterator[JobEvent]` (async iterator for async client) |
| evaluations | `run(request: EvaluationRequest|Mapping) -> JobHandle` |
| management | `run(request: MaintenanceRequest|Mapping, *, idempotency_key: str|None=None) -> MaintenanceResult` |
| formatting | module export `format_result(result: QueryResult, output_profile: OutputProfile, *, counter: TokenCounter|None=None) -> str` |

**Red/green command:** `uv run --locked pytest tests/api -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run public-export-only lifecycle test:

```python
from tychora import Tychora, SourceInput, QueryRequest

def test_public_api_ingests_text_without_cli_or_temporary_input(tmp_path, counter):
    with Tychora.open(tmp_path / "knowledge", token_counter=counter) as kb:
        job = kb.sources.add(SourceInput(type="text", source_key="manual", collection_id="default",
                                         kind="manual", text="Record verification outcomes."))
        assert kb.jobs.wait(job.id).state == "complete"
        result = kb.search(QueryRequest(question="verification", modalities=("lexical",)))
        assert result.segments
        unit = kb.evidence.get(result.segments[0].matched_unit_ids[0])
        assert unit.text == "Record verification outcomes."
```

- [ ] Define open semantics: explicit `open` can initialize an empty workspace using documented local lexical configuration and default collection, but never downloads models/constructs remote providers; existing workspace compatibility checks do not auto-migrate. Dependency construction occurs only in `build_services(owner, config, providers, counter) -> Services`. Thin client methods call services and unwrap canonical results:

```python
def search(self, request, *, scope=None, timeout=None):
    parsed = QueryRequest.from_mapping(request) if isinstance(request, Mapping) else request
    effective = self.scope if scope is None else self.scope.narrow(scope)
    return self.services.query.search(parsed, effective, self.context(timeout)).result
```

- [ ] Define `QueryRequest.from_mapping` in contracts as the documented normalization boundary; `self.context(timeout)` makes ExecutionContext. Async methods use owner queues/native async providers and bounded offload, never nest `asyncio.run`. Sync methods in an event loop are documented blocking; async is the responsive interface. Inject per-client state only, no global workspace/logging mutation, prints, exits or undocumented SDK return types.
- [ ] Add equivalent async example/test, simultaneous different scopes, cancellation/deadline, closed-client errors and event-loop heartbeat during blocking extraction. Framework-neutral adapter uses public search and maps each segment to `{id,text,metadata}` retaining unit/revision/locator/role/derived/assets/run fields; framework limit counts segments, not neighbors. It does not import Agno or reembed. Run `uv run --locked pytest tests/api -q`; commit with message `feat: expose stable sync and async evidence APIs`.

### Task 36: Authenticated versioned HTTP, durable events and local delegation

**Files:** Create `src/tychora/interfaces/http/{app,auth,routes,events,client}.py`, extend `application/runtime.py`, create `tests/transports/test_http.py`, `test_delegation.py`, `docs/http.md`.

**Interfaces:** `create_app(owner: WorkspaceOwner, services: Services, auth: LocalAuth) -> FastAPI`; `HTTPClient`/`AsyncHTTPClient` implement the operation signatures from 35 without access to stores. `LocalAuth` validates token/principal capabilities/origin and yields immutable RequestScope. All route contracts below are schema-generated; dependencies: 3, 8–9, 35.

| Routes | Behavior |
| --- | --- |
| `GET /v1/discovery`, `/v1/discovery/{subject}` | Definitions, scoped collections/kinds/config/capabilities; bounded cursor |
| `POST /v1/sources`, `GET /v1/sources`, `GET /v1/sources/{id}` | Add returns 202 + job, scoped lists/status |
| `GET /v1/sources/{id}/revisions`, `POST /v1/sources/{id}/sync` | Retained revisions; write capability for sync |
| `GET /v1/evidence/{id}`, `/v1/assets/{id}`, `/v1/assets/{id}/content` | Exact bounded registered inspection/bytes |
| `POST /v1/relations` | Typed bounded semantic/structural inspection |
| `POST /v1/queries`, `GET /v1/queries`, `GET /v1/queries/{id}` | Search and scoped retained history; explicit replay POST action requires dependencies |
| `GET /v1/jobs`, `/v1/jobs/{id}`, `/v1/jobs/{id}/events` | Status and reconnectable SSE |
| `POST /v1/jobs`, `/v1/jobs/{id}/cancel`, `/v1/jobs/{id}/retry` | Typed maintenance/cancel/retry with scoped idempotency |
| `POST /v1/evaluations`, `GET /v1/evaluations`, `/v1/evaluations/{id}` | Jobs and comparable reports |
| `GET /v1/settings`, `PUT /v1/settings` | Effective redacted settings; owner-only validated activation with previous config preserved on failure |

**Red/green command:** `uv run --locked pytest tests/transports/test_http.py tests/transports/test_delegation.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run real HTTP request tests through HTTPX ASGI client plus loopback subprocess tests for lifecycle/delegation:

```python
async def test_reader_cannot_add_sources(http_reader):
    response = await http_reader.post("/v1/sources", json={"type": "text", "source_key": "x",
        "collection_id": "default", "kind": "manual", "text": "source"})
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN_SCOPE"
```

- [ ] Implement loopback bind only, per-install protected credentials, server-granted scope intersection, separate read/write/manage capabilities, strict Host/Origin checks before dispatch and bounded request/upload/response sizes. Permit originless authenticated native clients; browser origins must exactly match configured loopback origin. No permissive CORS wildcard. Asset IDs resolve bindings; local source paths only within registered ingest roots and write capability.
- [ ] Map semantic errors consistently: invalid/budget 400, scope 403, missing 404, busy/profile/idempotency 409, original removed 410, capability/index/provider 503, deadline 504. Returned error bodies use sanitized 8-KiB envelope. Search defaults canonical JSON, explicit hierarchical profile/media type negotiates projection, unsupported combinations 406. Deliver ResponseAssembler's exact verified bytes:

```python
return Response(content=delivered.payloads[0], media_type=selected_media_type)
```

- [ ] Host owner in FastAPI lifespan, one Uvicorn worker. SSE Last-Event-ID returns strictly greater durable sequence; expiration signals authoritative state refresh, disconnect does not cancel job. Idempotency header binds canonical intent. Handshake verifies workspace/protocol/token before local CLI/Python delegation; reject stale/untrusted runtime endpoints and mismatched UUID. No secrets in URL/logs.
- [ ] Test auth/origin/DNS rebinding/Host, scoped writes, source path restrictions, content negotiation, cursor limits, SSE reconnect, duplicate request/conflict, service restart, two clients and busy standalone owner. Run `uv run --locked pytest tests/transports/test_http.py tests/transports/test_delegation.py -q`; commit with message `feat: serve authenticated evidence APIs and resumable events`.

### Task 37: Real MCP transports, discovery resources and output accounting

**Files:** Create `src/tychora/interfaces/mcp/{server,tools,resources}.py`, `tests/transports/test_mcp_stdio.py`, `test_mcp_http.py`, `docs/mcp.md`.

**Interfaces:** `build_mcp(services: Services, principal: RequestScope) -> MCPServer`; tools `discover`, `search`, `inspect`, `add_sources`, `manage_job` use public DTO schemas. Reader lacks write tools; `manage_job` actions are separately capability-checked and do not imply workspace management. Resources `tychora://schemas/{name}`, `tychora://kinds/{id}`, `tychora://evidence/{id}`, `tychora://assets/{id}` resolve registered/scoped records. Dependencies: 3, 32, 35–36.

**Red/green command:** `uv run --locked pytest tests/transports/test_mcp_stdio.py tests/transports/test_mcp_http.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run real SDK client test, not direct function invocation:

```python
async def test_mcp_reader_surface_and_structured_result(mcp_reader):
    tools = await mcp_reader.list_tools()
    assert {t.name for t in tools} >= {"discover", "search", "inspect"}
    assert "add_sources" not in {t.name for t in tools}
    result = await mcp_reader.call_tool("search", {"request": {"question": "verification"}})
    assert result.structured_content["schema_version"] == "1"
```

- [ ] Implement official pinned SDK tool/resource registration with descriptions/examples from resolved discovery schemas. No duplicated taxonomy. Validate returned canonical result and exact block accounting; canonical-only default must suppress SDK automatic text echo or count it. Rendered text adds its exact bytes/tokens to canonical content; inspect actual wire blocks in tests.
- [ ] Construct streamable HTTP app once and explicitly own SDK session lifespan inside host lifespan:

```python
mcp_app = mcp_server.streamable_http_app()

@asynccontextmanager
async def lifespan(app):
    async with mcp_server.session_manager.run():
        yield
```

- [ ] Integrate that session lifetime with owner/services rather than replacing Task 36 lifespan; close sessions before jobs/storage. Mount `/mcp`, share auth/Origin/grants. Stdio stdout is protocol-only; logs/errors/progress to stderr. Stdio owner also exposes protected private control endpoint for sibling delegation. Registered assets may return resource references; never arbitrary filesystem paths.
- [ ] Run actual stdio subprocess initialization/tool/resource/cancellation and Streamable HTTP reconnect/restart/mounted lifespan tests with `mcp.Client`; use APIs qualified in Task 3. Test write denial, unknown IDs, wrong scope, both render blocks and token cap after transport serialization. Run `uv run --locked pytest tests/transports/test_mcp_stdio.py tests/transports/test_mcp_http.py -q`; commit with message `feat: expose evidence services through official MCP transports`.

### Task 38: Five daily CLI verbs and discoverable maintenance

**Files:** Extend `src/tychora/interfaces/cli.py`; create `src/tychora/interfaces/commands/{init,add,search,inspect,serve,manage}.py`, `tests/cli/test_commands.py`, `test_output.py`, `docs/cli.md`.

**Interfaces:** `tychora init`, `add PATH`, `search QUESTION`, `inspect SUBJECT [ID]`, `serve`; maintenance under `manage setup|sync|remove|rebuild|export|import|health|evaluate|purge-originals|purge-history|purge-cache|migrate`. CLI only maps DTOs/public client. Dependencies: 35–37.

**Red/green command:** `uv run --locked pytest tests/cli -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run a machine-output test:

```python
import json
from typer.testing import CliRunner
from tychora.interfaces.cli import app

def test_json_discovery_contains_no_progress_noise(cli_workspace):
    result = CliRunner().invoke(app, ["--workspace", str(cli_workspace), "inspect", "kinds", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1"
```

- [ ] Add common workspace/profile/offline options and simple output selection; structured advanced query/request files contain tuning knobs. Use ordinary `add` to run all configured stages, with job/progress output. `inspect` noun list includes kinds, facets, relations definitions, sources, capabilities, config, evidence, assets, run, job, diagnostics. No manual text/vector/graph activation chores. `init` explicitly selects local versus configured providers and never downloads models implicitly. `manage setup --asset NAME` selects a manifest asset; `--bundle PATH` imports an explicitly supplied local bundle. An offline search whose configured tokenizer is absent fails with setup guidance before touching providers, never downloads it.
- [ ] Implement error/progress routing, canonical `--json`, explicit profile `--format json|markdown|agent`, conflicting switches invalid. Exit map kernel:

```python
exit_code = 2 if error.code in {"INVALID_REQUEST", "BUDGET_TOO_SMALL", "OUTPUT_BUDGET_EXCEEDED"} else 130 if error.code == "CANCELLED" else 1
print(error.to_json(), file=sys.stderr)
raise typer.Exit(exit_code)
```

- [ ] `serve` defaults HTTP+MCP loopback; `--stdio` uses protocol stdout and private delegation endpoint. Help/completion/config inspection perform no model imports/downloads/db creation. Add concrete help examples for local docs, code, exact lookup, kinds and original inspection. Maintenance commands call typed jobs, including original-removal plan preview and explicit confirmation flag from Task 39.
- [ ] Test subprocess help/no runtime side effects, shell completion, all public examples, progress on stderr, JSON errors, unknown nouns, request-file/passage/image input, Ctrl-C=130, busy owner delegation and no network offline. Run `uv run --locked pytest tests/cli -q`; commit with message `feat: provide a compact discoverable command line`.

### Task 38a: Configured runtime composition through public interfaces

**Files:** Add `src/tychora/application/default_bindings.py`; extend `api.py`, `application/{services,settings,sources,owner,query}.py`, qualified native/provider adapters, `tests/api/test_configured_runtime.py`, `docs/{api,cli}.md`. Dependencies: 20–26, 35–38. Task 39 owns explicit collection upload consent; Task 39a owns separately selected ingestion vision.

**Interfaces:** Ordinary `Tychora.open` and `AsyncTychora.open`, CLI and local HTTP/MCP hosts compose enabled configured text, image, rerank, Chroma and Ladybug ports after acquiring one owner. Caller-injected `ProviderBindings` retain their ownership and activation behavior. `env:` credentials resolve at first authorized operation; `secret:` references require an explicit runtime-only resolver. Settings activation rebuilds automatically owned ports, changes non-secret query-cache binding identity on rollover, preserves indexed representation identity, and retires old runtimes only after drain. Offline keeps native storage and disables remote ports. Model assets remain verified and lazy; no implicit download, consent, or fallback embedding.

**Coverage:** Start RED at the default public path, then qualify scoped mocked-provider add/search and zero-payload denial, native text/image separation and graph persistence, CLI add/search, a server query, missing credential/asset/dependency errors, offline no-network behavior, settings rollover/rollback/cleanup, affected suites and scoped Ruff/ty. Record exact results in the Task 38a report and commit only scoped code/docs/tests.

### Task 39: Withdrawal and explicit shared-original removal

**Files:** Create `src/tychora/application/removal.py`, extend `adapters/files/references.py`, `adapters/sqlite/corpus.py`, `application/services.py`; create `tests/operations/test_removal.py`, `tests/security/test_original_access.py`.

**Interfaces:** `RemovalService.remove_source(source_id: str, scope: RequestScope) -> JobHandle`; `purge_originals(source_id: str, revision_ids: tuple[str,...], *, confirm_original_removal: bool=False, scope: RequestScope) -> MaintenanceResult`. Plan enumerates bindings, reference cut and separate retained derived content; result distinguishes deleted/shared/pending-lease. Dependencies: 7–9, 26–27, 34–38.

**Red/green command:** `uv run --locked pytest tests/operations/test_removal.py tests/security/test_original_access.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run shared-payload test using real stores and two same-byte original bindings:

```python
import pytest
from tychora.contracts.errors import TychoraError

def test_removal_is_per_revision_binding(shared_originals):
    f = shared_originals
    f.withdraw("a")
    f.purge("a", confirm=True)
    with pytest.raises(TychoraError) as err:
        f.original("a")
    assert err.value.code == "ORIGINAL_REMOVED"
    assert f.original("b") == b"shared original"
```

- [ ] Implement source withdrawal and policy epoch in one transaction. Original purge requires withdrawn source, explicit revisions and management capability; false flag returns plan only. Validate revision original union (root original/repository file/original-role binding), stop conflicting publication, revalidate plan at execution. Do not erase normalized text/assets/vectors/history/backups or imply full erasure.
- [ ] Commit tombstones before unlink; mark only unreferenced payloads pending. Per-SHA guard recomputes live typed roots/leases immediately before deletion. Tombstone insertion uses:

```sql
INSERT INTO original_payload_tombstones(revision_id, artifact_sha, removal_job_id, removed_at)
VALUES(?,?,?,?);
```

- [ ] Live shared root restores payload state present; active reader/build/backup defers deletion; unlink registered hash path then fsync directory then state deleted timestamp. Crash pending+absent reconciles deletion, present+absent is corruption. New required binding shares guard and verifies/rematerializes bytes; old tombstone never cleared by identical reingestion. Original serving checks authorized revision binding, not direct hash.
- [ ] Test crashes before/after tombstone/physical unlink/fsync/final SQL, active lease, new reference race, shared extraction dependency, terminal job-capture provenance-only reference, management-only sharing counters and hidden tombstone nondisclosure. Run two test files; commit with message `feat: withdraw sources and durably release original bindings`.

### Task 39a: Public selective vision and document composition

**Files:** Extend `contracts/{requests,operations}.py`, `configuration/models.py`, `application/{sources,discovery,owner,default_bindings,vision_selection}.py`, the existing protected vision port/adapter, public Python/CLI/HTTP/MCP facades, and `docs/{api,cli,http,mcp}.md`; add focused contract, ingestion, and public API tests. Dependencies: 13–14, 35–39.

**Interfaces:** A disabled-by-default `providers.vision` setting qualifies Anthropic with an explicit model and lazy credential reference. A `SourceInput.vision` selection carries retained original/extraction identities, parser-native image reference, pixel crop, task/schema, and optional exact source-text intervals. Policy-scoped, bounded `sources.selections(source_id, revision_id, extraction_id)` exposes selectable image dimensions and context ranges. The trusted source service derives the crop, proof, context and allowed target IDs from retained parser facts, applies local OCR where selected, and invokes the protected provider dispatcher only after current principal/collection upload checks. Observations enter the existing document composition plan before the first canonical extraction commit; selection stays in captured job intent while stable source registration remains suitable for ordinary sync.

**Coverage:** Synthetic public Python/CLI/HTTP/MCP requests verify canonical parity and CLI continuation beyond 20 selectors; configured mocked Anthropic calls, crop/context and malformed output fallback; offline local OCR with zero remote construction; stale original/extraction and exact registration checks; consent/current-scope denial and reopening; first-ingest original preservation with an extraction-bound incomplete diagnostic, fresh-add identity, verified derived provenance and normal sync. Run focused tests, affected suites, scoped Ruff/ty and diff checks; report residual limits in the Task 39a report and its fix report.

### Task 40: Canonical backup with a consistent payload closure

**Files:** Create `src/tychora/application/backup.py`, `src/tychora/adapters/files/archive.py`, `tests/operations/test_backup.py`, `tests/security/test_archive.py`.

**Interfaces:** `BackupService.export(destination: Path, scope: RequestScope, *, mode: str='canonical', ctx: ExecutionContext) -> BackupReceipt`. Receipt binds `backup-manifest.json` v1, SQLite hash, required payload inventory, captured epochs/config/snapshots/tombstones and omitted projection rebuild requirements. Dependencies: 7–9, 26, 33, 39.

**Red/green command:** `uv run --locked pytest tests/operations/test_backup.py tests/security/test_archive.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run nested-reference closure test:

```python
def test_backup_includes_nested_registry_and_retained_present_payload(backup_case):
    f = backup_case
    receipt = f.export()
    members = {a.sha256 for a in receipt.manifest.artifacts}
    assert f.nested_schema_sha in members
    assert f.unreferenced_but_present_sha in members
    assert receipt.manifest.omitted_channels == ("text", "image", "graph")
```

- [ ] Implement owner-only whole-workspace export, no overwrite of completed destination. Pause publication/reference/deletion/GC, reconcile pending deletions, then use SQLite backup API (not live file copy). Derive transitive required closure plus every registered present/delete_pending artifact from the copied database, verify schema versions, acquire complete payload lease before releasing barrier:

```python
with owner.maintenance_barrier():
    corpus.reconcile_pending_deletions()
    corpus.connection.backup(backup_connection)
    inventory = references.from_backup(backup_connection)
    lease = artifacts.pin_payloads(inventory.required_shas)
    lease.__enter__()
```

- [ ] Define `maintenance_barrier`, `reconcile_pending_deletions`, `references.from_backup` here using Task 7 typed roots and Task 39 states. Copy under lease into private destination staging, verify all hashes/sizes, write manifest and completion receipt, fsync files/directories, atomic publish. Release lease in finally only after filesystem workers actually exit. Omit native Chroma/Ladybug files and runtime credentials/locks/temp uploads; copied lexical data stays. Warn that present tombstoned bytes at cut can still be included.
- [ ] Test concurrent publish/withdraw/purge during copy, nested missing/corrupt required payload, logically unreferenced present artifacts, pending-but-absent reconciliation, unknown typed schema, cancellation/crash without valid receipt and source directory removal after export. Unsupported exact-native replay mode fails clearly. Run `uv run --locked pytest tests/operations/test_backup.py tests/security/test_archive.py -q`; commit with message `feat: export consistent canonical workspace backups`.

### Task 41: Safe restore, explicit migrations and verified index recovery

**Files:** Create `src/tychora/application/{restore,recovery,maintenance}.py`, extend `adapters/files/archive.py`, `adapters/sqlite/migrations.py`, `application/services.py`; create `tests/operations/test_restore.py`, `test_migrations.py`, `tests/security/test_restore.py`.

**Interfaces:** `RestoreService.import_backup(source: Path, destination: Path, scope: RequestScope, ctx) -> RestoreReceipt`; `RecoveryService.reconcile(ctx) -> RecoveryReport`; `MaintenanceService.run(request: MaintenanceRequest, scope, key, ctx) -> MaintenanceResult`; `migrate(target_version: int, scope, ctx) -> JobHandle`. Dependencies: 6–9, 24, 26, 39–40.

**Red/green command:** `uv run --locked pytest tests/operations/test_restore.py tests/operations/test_migrations.py tests/security/test_restore.py -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run clean-destination no-provider restore test:

```python
def test_restore_preserves_identity_but_not_native_readiness(restore_case):
    f = restore_case
    restored = f.restore_without_original_workspace()
    assert restored.workspace_id == f.backup.workspace_id
    assert restored.open_original() == f.original_bytes
    assert restored.batch_state("text") == "pending"
    assert restored.sealed_receipt("text") == f.backup.sealed_receipt("text")
    assert f.provider_calls == 0
```

- [ ] Validate archive paths/symlinks/duplicate entries, limits (initial 100,000 entries/20 GiB expanded total/100:1 decompression ratio, lowerable), versioned complete manifest/hash/closure and destination empty. Inspect SQLite schema objects against trusted packaged schema before querying imported user tables; disable extensions/trusted schema, reject unexpected triggers/views/objects. Never execute archive-supplied migration/pack/code. Extract to private staging and activate only after integrity/FK checks and durable receipt.
- [ ] Preserve IDs/policy/tombstones at cut; generate fresh runtime credentials. Keep immutable receipts/bindings; set native projection operational availability pending:

```sql
UPDATE index_batches SET state='pending'
WHERE family_id IN (SELECT family_id FROM index_families WHERE channel IN ('text','image','graph'));
```

- [ ] Reconcile jobs already published; mark remaining copied queued/running/cancelling failed with interrupted-by-restore, suspend pending deletion, never resume providers automatically. Exact/lexical under policy remains usable; strict missing channels fail. Rebuild verifies same record/input/value digests and separate physical verification receipt; absent vector cache plus authorized remote recompute requires new family creation UUID and snapshot, never overwrite historical values. Graph rebuild applies original decisions/content needed for identity or creates new generation.
- [ ] Migrations use trusted packaged checksum/version sequence, explicit backup/preflight, transaction where supported and failure-preserved workspace. Startup checks schema/integrity/active bindings/backends; health jobs do deeper scans. GC retains active/previous/pinned generations and typed artifact roots; stale child staging persists until exit or explicit fencing. Test corrupt/malicious backup, unknown SQL trigger, path traversal, pending deletion, altered vector rebuild, key replay after diagnostic expiry, unsupported schema and failed migration rollback. Run operations/security suites; commit with message `feat: restore canonical workspaces and recover projections safely`.

### Task 42: Versioned gold datasets, metrics and controlled ablations

**Files:** Create `src/tychora/evaluation/{dataset,metrics,runner}.py`, `src/tychora/adapters/sqlite/evaluations.py`, `tests/evaluation/test_metrics.py`, `test_runner.py`, `tests/fixtures/evaluation/{manifest.json,cases.jsonl}`, `docs/evaluation.md`; wire `application/services.py` evaluations.

**Interfaces:** `load_dataset(path: Path) -> EvaluationDataset`; dataset immutable version/family/split/cases with expected+forbidden source/revision/unit/asset/definition/file IDs, spans, graded relevance and paths. `metrics(case: GoldCase, result: QueryResult) -> MetricSet`; `EvaluationService.run(request: EvaluationRequest, scope, ctx) -> JobHandle`. `recall(expected: set[str], returned: set[str]) -> float|None`, `mrr`, `ndcg`, `span_coverage` have explicit denominators. Dependencies: 18, 33–35, 41.

**Red/green command:** `uv run --locked pytest tests/evaluation -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run non-perfect-empty metric test:

```python
import pytest
from tychora.evaluation.metrics import recall, ndcg

def test_empty_denominator_is_not_perfect_quality():
    assert recall(set(), set()) is None
    assert recall({"a", "b"}, {"a"}) == 0.5
    assert ndcg([3, 1], [3, 1]) == pytest.approx(1.0)
```

- [ ] Implement metric kernels with counts/N/A, never fake perfect scores:

```python
import math

def recall(expected, returned):
    return len(expected & returned) / len(expected) if expected else None

def ndcg(grades, ideal_grades):
    actual = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))
    ideal = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal_grades))
    return actual / ideal if ideal else None
```

- [ ] Add document/unit recall, MRR, nDCG, version/locator precision, forbidden-object leakage, exact span completeness, text-to-image relevance and relation/path precision. Code file ranks are first distinct `(source,revision,path)` occurrence in actual selected segments; symbol ranks use definition key, context-only mentions never count. Resolution rates and judged edge precision remain separate; empty extracted references N/A.
- [ ] Construct public synthetic integrity cases for exact IDs, historic cutoff, mode/global/unknown/review restrictions, proxy/image, table splits, conflicting assertion, long end match, repeated symbols, overloads, exploratory entry point and hidden parent/support. Keep related editions/derivatives/feedback/repos in same dev/test family. Independently label semantic document/code/visual datasets before model comparison; do not treat generated expected answers or hash vectors as semantic quality.
- [ ] Run lexical, hybrid, reranked, structural-only, semantic graph and visual variants with fixed dataset/split/snapshot/config/policy/statistics epochs; maintenance barrier pauses lexical writes/GC/rebuild, policy change invalidates comparison. Persist per-case results and hardware/model/tokenizer/usage inputs. Compare contextual vs independent code models with no preset auto-switch. Integrity release gate zero exact-ID/wrong-version/forbidden-source/invalid-locator failures; semantic thresholds require reviewed first benchmark. Run `uv run --locked pytest tests/evaluation -q` and an explicit local integrity evaluation CLI command; commit with message `feat: evaluate evidence integrity and retrieval quality by dataset`.

### Task 43: Crash, security, concurrency and performance acceptance

**Files:** Create `src/tychora/evaluation/benchmarks.py`, `tests/acceptance/{test_crash_matrix,test_concurrency,test_budget_parity}.py`, `tests/security/{test_prompt_data,test_scope_paths,test_provider_boundary,test_history_privacy}.py`, `tests/performance/test_cache_states.py`, `docs/security.md`, `docs/performance.md`.

**Interfaces:** `benchmark(requests: tuple[QueryRequest,...], states: tuple[str,...], client_factory, clock) -> BenchmarkReport` records environment/counts/p50/p95/stages/memory/throughput/index bytes/provider usage. Shared fixture `FaultInjector.hit(point: str) -> None` enables subprocess crash at named boundary and is no-op when absent; no public hidden testing bypasses. Dependencies: 35–42.

**Red/green command:** `uv run --locked pytest tests/acceptance tests/security tests/performance -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run four-state contract test:

```python
def test_warm_result_is_not_reported_as_uncached_search(benchmark_case):
    result = benchmark_case.run_states()
    assert result["warm_query_embedding"].query_embedding_calls == 0
    assert result["warm_query_embedding"].retrieval_calls > 0
    assert result["warm_result"].retrieval_calls == 0
    assert result["warm_result"].provider_calls == 0
    assert result["warm_result"].policy_revalidations > 0
```

- [ ] Implement cold-owner/warm-resources/warm-query-embedding-with-result-bypass/warm-result runs with same corpus/config/query/output caps. Record OS/page/provider cache conditions separately; process-cold does not mean disk-cold. Record actual parsed files/embedded windows/projected records for no-op/edit/delete/rename/dirty capture. Quantile method is fixed in report; p95 example:

```python
samples = sorted(samples)
p95 = samples[max(0, math.ceil(0.95 * len(samples)) - 1)] if samples else None
```

- [ ] Add subprocess kill points: artifact before SQL, mid-extraction, half embeddings, backend write before receipt, graph seal before publish, publish before event, tombstone before unlink, unlink before final SQL, backup before receipt and owner death with live parser child. Assert prior or fully new snapshot only, resumable artifacts, no unauthorized provider/response, stable idempotency and staged child safety. Include real native stores, not only injected failures.
- [ ] Exercise malicious source prompt text as data through agent wrapper, source/path/URL/archive/schema injection, oversized JSON/image/graph/facet filters, auth/origin/token redaction, hidden diagnostic side channels, shared context group upload and current policy changes. Check bounded queues/deadlines, two concurrent async queries while parser blocks, canonical payload leases surviving cancellation and actual all-transport response bytes.
- [ ] Run `uv run --locked pytest tests/acceptance tests/security tests/performance -q`, Ruff, ty and dependency audit against locked runtime. Record vulnerabilities and compatibility findings with actual fixes/retests; unresolved release-relevant failures block the affected artifact. Performance report uses measurements and sample sizes, no invented universal target or price without dated inputs. Commit with message `test: verify engine failure recovery security and runtime behavior`.

### Task 44: Public documentation and discovery-driven companion skills

**Files:** Create/update `README.md`, `docs/{api,cli,http,mcp,configuration,operations,security,evaluation,performance,distribution}.md`, `examples/quickstart.py`, `examples/knowledge_adapter.py`, `examples/profiles/workbench/` complete generic pack; create `src/tychora/resources/skills/{configure,add-sources,query,inspect-evidence}/SKILL.md`, `tests/docs/test_examples.py`, `tests/docs/test_skills.py`.

**Interfaces:** Only public clients/CLI/discovery; no workflow-specific private terminology. Dependencies: 35–43. Use `superpowers:writing-skills` and the applicable skill-creation instructions at execution time; test actual agent behavior for the packaged skills instead of judging frontmatter alone.

**Red/green command:** `uv run --locked pytest tests/docs -q`. Run once after adding each new failing test, then again after its minimal implementation. Initial expected failure is the named missing export/behavior, not unrelated fixture errors; final expected result is PASS for every listed case.

- [ ] Write/run executable quickstart acceptance test using the literal README commands via subprocess and temporary workspace, plus direct public Python example. Expected source evidence is the synthetic sentence from Task 35; preprovision the declared output tokenizer from the checked local fixture asset bundle before running offline examples; documents/code/visual workflows remain separately marked by required extras/assets.

```python
from pathlib import Path

def test_query_skill_starts_with_discovery_and_preserves_provenance():
    text = Path("src/tychora/resources/skills/query/SKILL.md").read_text()
    assert "discover" in text
    assert "revision" in text
    assert "derived" in text
```

- [ ] Write minimal successful paths: install base/full, explicit local init/setup, add source, inspect kinds/filters, search, exact read, use Python sync/async, connect MCP, inspect job/health, export/import and explicit original-removal plan. Explain scope/upload/unknown dates/derived records, half-open dates, historical profiles and deterministic coverage. Include commands users can copy, not tuning prose without examples:

```bash
tychora init ./knowledge --profile documents --offline
tychora --workspace ./knowledge manage setup --asset output-tokenizer
tychora --workspace ./knowledge add ./manual.md --kind manual
tychora --workspace ./knowledge inspect kinds
tychora --workspace ./knowledge search "How is verification recorded?" --json
```

- [ ] Provide a complete generic external pack using standard/feedback/template/regulation roles, custom predicate, project-global binding, historical mapping and hierarchical output. Explain wheel vs binary/Git/model assets. Document provider data boundaries, metadata-only 30-day history, cache purge vs history purge, original purge vs derived retention, backup cut and unavailable native channels. No claim of implemented dashboard/Agno/LLM answering.
- [ ] Skills instruct discovery→validated request→evidence inspection, preserve citations and original/derived status, never hardcode full kind enums, silently raise scope or promote feedback into rules. Add controlled skill scenarios: unfamiliar pack, missing capability, unclear source kind, partial evidence, restricted record and original-removed result; verify agent chooses explicit discovery/inspection and does not invent data. No private source material in fixtures/screenshots/examples.
- [ ] Run `uv run --locked pytest tests/docs -q`, link/code-example checks, skill behavioral checks per writing-skills, then commit with message `docs: explain evidence workflows and package companion skills`.

### Task 45: Installed wheel/sdist, dependency hygiene and release inventory

**Files:** Create `packaging/check_artifact.py`, `packaging/build_manifest.py`, `tests/release/test_installed_wheel.py`, `tests/release/test_resources.py`, `.github/workflows/check.yml`, `.github/workflows/build.yml`; modify `pyproject.toml`, `uv.lock`, `docs/distribution.md`.

**Interfaces:** `check_artifact(path: Path) -> ArtifactAudit` verifies explicit allowlist and absence of corpora/secrets/local paths; build manifest binds version/commit/lock/toolchain/features/dependencies/licenses/hash inventory. Dependencies: 1, 3, 42–44.

**Red/green command:** `uv run --locked pytest tests/release -q`. Run these through the clean installed-artifact harness described below; checkout-imported tests do not satisfy this task.

- [ ] Write/run a packaged-resource test installed outside checkout:

```python
from importlib.resources import files

def test_installed_package_contains_schema_and_generic_profiles():
    root = files("tychora")
    assert root.joinpath("resources/migrations/0001.sql").is_file()
    assert root.joinpath("profiles/documents/profile.toml").is_file()
    assert root.joinpath("resources/skills/query/SKILL.md").is_file()
```

- [ ] Configure explicit wheel and sdist include lists for source/resources/build metadata/public tests/docs as appropriate; exclude private local paths/corpora/credentials/db/model caches/reports. Include MIT and applicable third-party notices. Build with `uv build`, then inspect every wheel ZIP/tar member and metadata before installation. Audit kernel rejects unsafe members:

```python
if path.is_absolute() or ".." in path.parts or not allowed_member(path):
    raise ValueError(f"Unexpected distribution member: {path}")
```

- [ ] Implement `allowed_member` from explicit repository/package prefixes plus deny sensitive artifact types; scan contents for machine-specific paths and secret patterns without printing matching secrets. Include clean-room build with private source directories inaccessible, not only filename filtering. Declare tested package ranges; lock full release inputs and publish constraint/feature manifest. Use minimal base environment and locked full environment independently.
- [ ] Install wheel into a fresh temp venv from built artifact; run tests from a separate temporary working directory with checkout/PYTHONPATH absent. Run base import/help/offline lexical, full native adapters/model-preprovisioned PDF/DOCX/code, HTTP/MCP and backup/restore. Build sdist→wheel and repeat resource/import tests. No developer editable install stands in for artifact acceptance.
- [ ] CI runs locked install/lint/format/ty/unit/real qualification/security/artifact tests; dependency audit and license inventory use actual resolved stack. Publish checksums/build provenance only for generated artifacts; distribution/upload remains separate from local build. Run `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked ty check src tests`, `uv build`, installed artifact tests; commit with message `build: qualify wheel contents and reproducible release inputs`.

### Task 46: Qualify actual standalone executables per target

**Files:** Create `packaging/tychora.spec`, `packaging/frozen_main.py`, `packaging/hooks/{hook-tychora,hook-ladybug,hook-chromadb,hook-docling}.py`, `packaging/test_frozen.py`, `.github/workflows/binary.yml`; update `docs/distribution.md`, `docs/qualification.md`.

**Interfaces:** Final `tychora-VERSION-OS-ARCH` runs identical CLI/MCP/HTTP contracts without Python/uv/Node/compiler; system Git and selected model assets are explicit external requirements. Binary resource lookup goes through `importlib.resources`, writable cache via platformdirs. Dependencies: 3, 43, 45.

**Red/green command:** `uv run --locked python packaging/test_frozen.py --artifact dist/tychora --features packaging/release-features.json`. Before the build, missing final artifact is a failing gate; after the build, all advertised feature checks must pass on the actual executable.

- [ ] Write the frozen entry point and subprocess test before freezing. Verify `--help` in a clean directory/network-disabled environment, then assert base/full feature manifest equals installed wheel except packaging form. Entry point:

```python
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from tychora.interfaces.cli import main
    main()
```

- [ ] Build from installed release wheel plus locked full runtime, first onedir for dependency diagnosis then onefile final. Explicit hooks collect only required native Chroma/Ladybug/Tree-sitter/Docling/Torch/tokenizer binaries/resources/distribution metadata and Tychora packs/schemas/skills. Never collect whole environments or cached model weights. Test read-only install location, spaces in paths, supported executable temp filesystem/symlinks and bounded parser multiprocessing.
- [ ] Sanitize frozen native-library environment for external Git while keeping application libraries intact; missing Git disables repository operations only. Test real Markdown/PDF/DOCX/figure/code ingestion, actual graph/vector reopen/rollback/crash, canonical restore without source/native stores, external generic pack/hierarchical output parity, HTTP owner/delegation and real stdio framing. Provider contract/live checks remain separate and explicit.
- [ ] Run `uv run --locked pyinstaller packaging/tychora.spec` in native target CI; the spec provides named onedir/onefile build modes using a documented build environment selector. Run `uv run --locked python packaging/test_frozen.py --artifact dist/tychora --features packaging/release-features.json` on lowest supported baseline and current runner. `release-features.json` is produced by Task 45 build manifest. Assert parser child shutdown/cancellation, no native handles in children and no hidden interpreter dependency.
- [ ] Record compressed size/extracted disk/peak memory/cold start/warm service timing; unknown usability remains a failed or unevaluated gate, not silent adapter removal. Qualify macOS 15+ arm64 and Ubuntu 22.04/glibc 2.35+ x86_64 independently. Sign/notarize public macOS artifact only with configured release credentials; absent credentials block signed release, not local unsigned development artifact. Produce per-target hashes/licenses/provenance/feature report. Do not publish/push/upload as part of this implementation task without release authorization.
- [ ] Run the final binary acceptance suite, record each target as qualified/failed/not-run, and commit build definitions/report with message `build: qualify standalone evidence engine executables`.

## Spec-to-task coverage map

This is the self-review index, not a second specification. Numeric/semantic requirements remain in the linked engine spec. Each row names the implementing tasks and the gate that distinguishes implemented behavior from a design claim.

| Spec section / requirement | Tasks | Acceptance evidence |
| --- | --- | --- |
| Product boundary; five verbs; deterministic evidence | 1–2, 32, 35–38, 44 | Public API/CLI/MCP parity and no generator path |
| Core dependency direction; lazy extras; programmatic ports | 1–3, 6–8, 20–25, 35 | Import tests, type checks, native qualification |
| Six identities, relocation, revisions/extractions/windows | 2, 6–7, 15, 18–19 | Stable identity, same bytes/new edition, dirty repository tests |
| Dedicated graph/vector; no distributed transaction | 3, 23–26 | Real DB probes, sealed receipts and publication crash tests |
| Single owner, private delegation, queues/leases/cancellation | 8–9, 26, 35–38, 43 | Subprocess contention/delegation and cancellation/GC races |
| Packs, dependencies, strict schemas, origins, secrets | 4–5, 34, 44 | Complete external pack and malicious include/schema tests |
| Kinds vs formats/roles/lanes, records and descriptions | 5, 11–14, 18, 34 | Zero-count kinds, typed original records and derived distinction |
| Historical kind mapping / old schema / activation rollback | 5, 27, 36 | Missing/conflicting mapping, warm profile and scoped diagnostics |
| Typed facets/global/unknown/mixed-scope filters/hints | 5, 18, 27, 29 | Cross-channel and context dependency tests |
| Custom versioned relations, origin/supports and decisions | 5, 18, 24–25, 32 | Graph seal, ambiguous endpoints and live decision overlay |
| SQLite 39 tables/FTS, immutability and cross-record checks | 6, 9, 26–28, 39–42 | SQL plus repository constraints and migration checks |
| Original/artifact retention, parent/asset proxy dependencies | 7, 13, 18–19, 27, 39–41 | Transitive closure, child restriction and tombstone tests |
| Exact lexical/vector snapshots, statistics epoch | 19, 23, 26–28, 33, 42 | Unpublished entries excluded, FTS epoch A/B controls |
| Chroma partitions, unique owners, bounded approximate search | 23, 28 | Hundreds of windows/one owner, partitions, missing IDs/truncation |
| Graph full generations, support eligibility, pagination/GC | 24–26, 32, 41, 43 | Real sealed graph, hidden support, duplicate targets, leases |
| Native documents/rich PDF/DOCX/figures/geometry | 10–13 | Real parser fixtures plus manually audited locator expectations |
| Table fragment joining and strict/permissive derivation | 13, 31, 42 | Positive/negative continuation, original rows/locators retained |
| Selective OCR/vision and model assets | 12–14, 21, 27 | Explicit setup, small region, schema/target/upload checks |
| Contextual/independent embeddings, pixel embeddings, rerank | 19–23, 30 | SDK mapping/count/limits/model capability, live synthetic gates |
| Exact-first, safe FTS, RRF, no duplicate family votes | 28–30 | Direct equality, escaped terms, rank/admission tests |
| Shared graph/structural seeds, 16/16 and 4/4 reservations | 25, 29 | Mixed corpus and shared-budget fixtures |
| Late/end anchors, gaps, code spans, visual reservations | 30–31 | Exact source-range assertions, weak-proxy native image case |
| Required lanes/conditions/records/checks/conflict status | 5, 29, 32 | Empty vs incomplete check, required lane omission reasons |
| Exact final aggregate budgets and pure render compatibility | 32, 36–38, 43–44 | Actual transport blocks/bytes; tiny/long/localized/MCP cases |
| Search vs inspector source policy, original binding access | 27, 34, 39 | Withdrawal/original-removed/hidden descendant/asset tests |
| Query cache identity/TTL/history/replay limitations | 27, 33, 43 | Filter reuse, credential/epoch misses, four benchmark states |
| LanguageAdapter and initial Python/JS/TS/JSX/TSX | 15–18 | Real grammar capabilities and source encoding/ranges |
| File-qualified symbols/resolution/context diagnostics | 18, 25, 31, 34, 42 | Same-name/overload/parent/scope and gold edge precision |
| Sync/async API, external framework compatibility | 35, 44 | Public-only adapter, responsiveness and scope isolation |
| HTTP/MCP auth/lifespan/resources/SSE/errors | 36–38 | Actual requests/transports/reconnect, SDK echo accounting |
| Dashboard backend contracts, no implemented UI claim | 33–38, 42–44 | Scoped routes/events/coverage/evaluation/settings, dashboard=false |
| Durable idempotency incl changed path/defaults/retention | 9–10, 26, 36, 41 | One committed capture, key conflicts, restore and diagnostic expiry |
| Original removal vs shared payload/derived retained bytes | 7, 39, 41 | Tombstones, leases, re-materialization, per-binding status |
| Canonical backup cut/transitive closure/security/restore | 40–41, 43 | New clean destination, no provider, malicious archive/SQL rejection |
| Immutable rebuild receipts/new family for changed values | 19, 23–26, 41 | Vector/graph content digest verification and readiness distinction |
| Evaluation integrity/semantic/code/image/path slices | 42–43 | Versioned gold, N/A, no leakage, controlled ablations |
| Security/resource/provider/dependency audits | 3, 10, 14–15, 27, 36–37, 41, 43, 45 | Negative inputs, real failures, actual lock audit |
| Wheel/sdist, onefile native targets, Git/model boundary | 45–46 | Installed artifact and clean-target final binary acceptance |
| Docs/skills/distribution privacy and external packs | 44–46 | Public examples, skill scenarios, artifact allowlist/clean-room build |

## Executor's final acceptance checklist

- [ ] All tasks have their red/green evidence, final passing targeted checks and review/commit; no skipped native/live/platform gate is reported as passed.
- [ ] Run `uv run --locked ruff check .`, `uv run --locked ruff format --check .`, `uv run --locked ty check src tests`, and the complete non-live test suite with qualification dependencies/models explicitly provisioned.
- [ ] Run integrity evaluation and publish scoped per-case results. Run independently labelled semantic/code/visual comparisons with recorded controls; do not infer semantic quality from contract tests.
- [ ] Verify installed base wheel, installed locked full wheel, sdist rebuild and each final onefile artifact separately. Reports identify exact tested OS/Python/dependency/model/grammar versions.
- [ ] Recheck public artifacts, dependency/license inventory and no local data inclusion. No external release/upload is implied by build completion.
- [ ] If provider credentials, signed-release credentials, model assets or a target runner are unavailable, name the exact unexecuted gate and retain the honest partial qualification report. Continue every unaffected task rather than declaring the whole application blocked.
- [ ] Report implemented capabilities, tests/evaluation actually executed, residual failures and distribution targets actually qualified. Link reports and artifacts; never present this plan itself as implementation evidence.

## Plan preparation verification

The plan is an implementation handoff. Its code snippets and acceptance tests have not been executed as application tests during planning. Preparation checks cover task/dependency structure, spec coverage, link resolution, code-fence syntax, prohibited placeholder scan and public terminology. Runtime and artifact qualification belongs to the task gates above.
