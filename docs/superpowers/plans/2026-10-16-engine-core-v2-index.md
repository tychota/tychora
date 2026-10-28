# Engine Core v2: Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute phases in the order below; each phase is its own branch and pull request and must leave `main` green.

**Goal:** Make every add and every query cost proportional to the work it adds or reads, not to the corpus, while delivering the shared contracts (boundary keys, vector families, query trace, usage ledger, aggregates, background phases, dashboard endpoints, repository layer, error reasons, benchmark harness) that sub-projects 2 to 7 consume.

**Architecture:** Seven phases, each shippable on its own. Phase 1 lays the foundations every later phase measures and builds on (benchmark harness, error reasons, repository ports, format 2 and the workspace rebuild path). Phases 2 and 3 rewrite the write side (snapshot pages and per-batch vectors; write-time structure facts). Phase 4 rewrites the read side on top of them (lanes, eligibility v2, query path). Phases 5 to 7 add the observability contracts, the incremental graph, and the parity and rebuild features.

**Tech Stack:** Python 3.12, Pydantic 2 contracts, SQLite (WAL, FTS5, STRICT tables), Chroma 1.5.9, Ladybug 0.20.3, FastAPI and MCP 2.2.0, Typer, pytest with pytest-asyncio, uv, the code-health checker `tools/health` (sub-project 0).

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` ("the spec"). Also binding: `docs/superpowers/specs/2026-10-15-tychora-v2-program-design.md` ("the programme", including its resolved decisions), `docs/superpowers/specs/2026-10-15-code-health-design.md` ("code health", ratchet obligations) and `docs/superpowers/reviews/2026-10-15-ingestion-query-flow-review.md` ("the flow review", finding IDs D1–D8, C1–C5, Q1–Q6).

## Phase plans

| # | Plan file | Name | Detail | Depends on |
| --- | --- | --- | --- | --- |
| 1 | `2026-10-16-engine-core-v2-phase-1-foundations.md` | Benchmark harness, error reasons, repository ports, format 2, workspace rebuild | Fully detailed | Sub-project 0 merged |
| 2 | `2026-10-16-engine-core-v2-phase-2-snapshot-pages.md` | Membership pages, publication of new batches only, per-batch vector artifacts, family identity, retention and GC | Fully detailed | 1 |
| 3 | `2026-10-16-engine-core-v2-phase-3-write-time-structure.md` | Boundary keys and section levels, linear windowing, persisted representation facts | Fully detailed | 1 |
| 4 | `2026-10-16-engine-core-v2-phase-4-query-path.md` | Lanes and read pool, snapshot catalog, eligibility v2 and cache, de-revalidated query path, stop words, strict mode, editions, per-request narrowing | Fully detailed | 2, 3 |
| 5 | `2026-10-16-engine-core-v2-phase-5-trace-usage-aggregates.md` | Query trace, usage ledger and prices, aggregates, background phases, dashboard endpoints, purge commands | Task granularity, to be expanded before execution | 2, 4 |
| 6 | `2026-10-16-engine-core-v2-phase-6-incremental-graph.md` | Graph contributions, live graph, compaction, degree table, neighbours and impact tools, hubs endpoint, edge weights | Task granularity, to be expanded before execution | 2, 4, 5 |
| 7 | `2026-10-16-engine-core-v2-phase-7-families-parity-rebuild.md` | Vector family options and local query encoder, `manage rebuild index`, connectors, catalog import, `max_upload_rank`, ingestion pipeline split, final verification | Task granularity, to be expanded before execution | 1–6 |

Phases 2 and 3 are independent and may run in parallel branches; everything else is sequential. Sub-project 2 (chunking v2) may start after phase 3. Sub-project 6 (dashboard v2) may start its fixture-driven work after phase 5 has merged the trace, usage and endpoint contracts.

**Before executing phases 3 to 7**, expand the plan with the writing-plans skill against the code as it stands after the previous phase: replace each task's outline with bite-sized TDD steps carrying real code and exact commands. The outlines fix files, interfaces and tests; they are not executable as written.

## Global Constraints

Every task of every phase implicitly includes these.

- Python `>=3.12,<3.13`; no new runtime dependency without an exact pin in `pyproject.toml` and `uv.lock` updated with `uv lock`. The ONNX runtime for the local query encoder (phase 7) goes into a new optional extra, never into the base install.
- New format: `schema_version` 2 in `workspaces`, one fresh `src/tychora/resources/migrations/0001.sql`. Opening a format-1 workspace fails with `PROFILE_INCOMPATIBLE` reason `workspace.format_old`, naming `tychora manage rebuild workspace --from`. No dual-read code. Format-1 export archives are refused with the same reason.
- Identity formulas for source, revision, extraction, unit and chunk are unchanged (`src/tychora/contracts/identity.py`). Snapshot, family and vector record identities change as the spec says and nowhere else.
- The identity, provenance, upload-consent and offline-first principles of `HANDOFF.md` are non-negotiable. Every provider call passes the existing owner upload authorization (`application/owner.py:513-561`).
- Ports speak in contracts, IDs, plain scalars, tuples and opaque `Cursor` values. No `sqlite3`, Chroma or Ladybug type, and no parameter named `sql`, `where` or `match`, crosses a `Protocol` in `src/tychora/ports/` (spec §4.13).
- Paging is keyset-based through `Cursor`; never `OFFSET` or `rowid`.
- Times are timezone-aware `datetime` in UTC, serialised RFC 3339; costs are decimal strings in USD.
- Every new `TychoraError` site passes `reason=` from the closed registry in `src/tychora/contracts/error_reasons.py` (phase 1). `reason=None` stays legal only in untouched code.
- Code health (sub-project 0): no metric of `quality/baseline.json` may get worse; new functions are at most 80 physical lines and cognitive complexity 15; no new function-body imports; no new `ignore_imports` entries. Each phase ends with `uv run --locked python -m tools.health check` green and, where values improved, `uv run --locked python -m tools.health update` committed.
- Comments and docstrings say what the code does, in present tense. They never cite plan tasks, plan files or review identifiers (gate G15 matches `\b[Tt]ask ?[0-9]+\b`).
- The default test suite stays green on a clean machine without model assets: `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`. Asset-dependent tests keep their markers. Network is blocked in ordinary tests by `tests/conftest.py`.
- Public repository rule: benchmark and example material is public or synthetic; no private corpus, customer context or private project name is committed.
- Kept fixes stay green: `tests/retrieval/test_segments.py` (including the Docling neighbour-context regression tests), `tests/application/test_upload_batches.py`, `tests/ingestion/test_docling_mapping.py`.

## Prerequisites before phase 1

1. Sub-project 0 (code health) is merged on `main`: `tools/health/`, `quality/baseline.json`, the import-linter contracts in `pyproject.toml`, gate G24 (reasonless integrity errors) and the code gold set (G22, criterion S8). The code gold set has no `baseline.json` yet: a whole-repository code extraction (about 19k rows, 19.6 MB) exceeds the evaluation retained-binding budget (16,384 rows, 16 MiB), so its test is an expected failure. Phase 1 makes repository sources fit (per-file extractions or verifying only cited units, not a larger budget), turns the expected failure into a pass, commits the baseline and investigates `definition_recall` near 0 (informational run with a 4x budget: file_recall 0.87, file_mrr 0.71, definition_recall 0.04 lexical). A search over the full repository also still takes about 13 s because each search reloads and prepares the whole extraction; phase 1's benchmark gate covers it.
2. The Docling neighbour-context fix (`retrieval/segments.py:392,640,659`, programme §2 "Immediate fix") is merged on `main` with its regression tests.
3. `git log -1 --format=%H b91d808` resolves: the E6 retrieval baseline is measured at that commit (phase 1, task "Evaluation baseline").

If any prerequisite is missing, stop and report; do not work around it.

## Carried over from phase 1

Phase 1 leaves out interface members that nothing calls yet, because the dead-code gate (G12) refuses members without a caller. The index names stay binding; the first phase that calls a member adds it under its exact name:

- `UnitOfWork`, `ReadTransaction`, `WriteTransaction` protocols in `ports/repositories.py`; `WorkspaceOwner.uow`.
- `WorkspaceRepository.format_version`, `bump_policy_epoch`, `bump_lexical_epoch`; `ArtifactRepository.require`.

Phase 4 also routes the remaining raw `lexical_statistics_epoch` reads (`application/query.py`, `adapters/sqlite/publication.py`, `retrieval/lexical.py`) through the workspace repository; the phase-1 guard only pins `policy_epoch` reads. Phase 4 further owns the code-gold `definition_recall` fix (output budget stubs drop `definition_key`), the full-repository code search (about 12 s per search at phase-1 HEAD) and the query p95 measured above budget on `bench-small`.

Phase 4 added `WorkspaceRepository.bump_policy_epoch` and `ArtifactRepository.require` with their first callers; `WorkspaceRepository.format_version` and `WorkspaceOwner.uow` still wait for theirs.

## Format changes across phases

The format is declared once (phase 1: `SCHEMA_VERSION = 2`) and the DDL of `0001.sql` grows in phases 2 to 7. Each DDL change updates `SCHEMA_CHECKSUM` in `src/tychora/adapters/sqlite/migrations.py` in the same commit. A workspace created on an intermediate commit fails to open after a later phase with `PROFILE_INCOMPATIBLE` reason `workspace.format_old`; it is recreated with `tychora manage rebuild workspace --from <old-path>`, which phase 1 delivers precisely so that every later DDL change has a recovery path. No release is cut between phases (the `binary.yml` and `build.yml` release jobs are not triggered for intermediate tags).

## Review Focus (programme-wide)

These failure modes cut across phases; each phase plan repeats the ones it owns and adds the pinning test to the owning task.

1. **A workspace created by an earlier commit is opened by a later one.** Expect `PROFILE_INCOMPATIBLE` reason `workspace.format_old` with the rebuild command named, never a raw `sqlite3` error or `INTEGRITY_ERROR`. Owner: phase 1 (format check), every DDL-changing phase (checksum bump test).
2. **A crash between a batch write and publication.** Expect `building` batches invisible to queries and collected by GC; a retried add succeeds without duplicate rows. Owner: phase 2 (lexical, vector), phase 6 (graph contributions).
3. **A concurrent add while queries run.** Expect queries to see either the old or the new snapshot, never a mix; no `WORKSPACE_BUSY` below the queue bounds. Owner: phase 4.
4. **A caller with a narrowed scope reads a trace, usage row or aggregate.** Expect no count, ID or title from outside the scope. Owner: phase 5 (trace, usage, aggregates), phase 6 (hubs, neighbours).
5. **A corpus where one bucket or one document dominates** (a 3,000-page book; 500 revisions of one source). Expect page size and publication time bounded as in E3, and no artifact over 1 MiB. Owner: phase 2.

## Cross-phase interfaces

Names and types below are the contract between phases. A phase plan may add private helpers but must not rename or re-type these. If a phase finds one wrong, it fixes this index and every dependent plan in the same pull request.

### Produced by phase 1

```python
# src/tychora/contracts/error_reasons.py
@dataclass(frozen=True, slots=True)
class Reason:
    code: str                 # a key of contracts.errors.ERROR_HTTP_STATUS
    reason: str               # dotted, e.g. "payload.hash_mismatch"
    retryable: bool
    operator_action: str      # one sentence shown to operators
    http_status: int | None = None   # overrides the code's status (416 for range.*)

REASONS: Mapping[str, Reason]                     # keyed by Reason.reason
# besides spec §4.13's list, phase 1 registers workspace.format_old, cursor.invalid, label.missing,
# label.ambiguous and rebuild.destination_not_empty
def reason_for(code: str, reason: str) -> Reason  # ValueError when unknown or registered under another code

# src/tychora/contracts/errors.py (changed)
class ErrorEnvelope(Contract):
    schema_version: str = "2"
    code: NonEmptyStr
    message: str
    retryable: bool = False
    reason: str | None = None
    run_or_job_id: Identifier | None = None
    diagnostic_refs: tuple[Identifier, ...] = ()

class TychoraError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool | None = None,
                 reason: str | None = None, diagnostic_id: str | None = None) -> None: ...
    code: str; message: str; retryable: bool; reason: str | None; diagnostic_id: str | None
    http_status: int          # property; Reason.http_status wins when set
    def envelope(self) -> ErrorEnvelope: ...
    def to_json(self) -> str: ...

# src/tychora/ports/repositories.py
class Cursor(Contract):
    sort_key: tuple[str | int, ...]
    last_id: str
    def encode(self) -> str: ...
    @classmethod
    def decode(cls, token: str) -> "Cursor": ...  # INVALID_REQUEST reason cursor.invalid

class WorkspaceRepository(Protocol):
    def workspace_id(self) -> str: ...
    def format_version(self) -> int: ...
    def policy_epoch(self) -> int: ...
    def lexical_epoch(self) -> int: ...
    def decision_watermark(self) -> int: ...
    def bump_policy_epoch(self) -> int: ...
    def bump_lexical_epoch(self) -> int: ...

class ArtifactRepository(Protocol):
    def get(self, sha256: str) -> ArtifactRef | None: ...
    def require(self, sha256: str) -> ArtifactRef: ...   # INTEGRITY_ERROR payload.missing
    def register(self, ref: ArtifactRef) -> bool: ...     # True when inserted
    def revive(self, ref: ArtifactRef) -> bool: ...       # released row back to present; caller verifies bytes (phase 2)

class ReadTransaction(Protocol):
    workspace: WorkspaceRepository
    artifacts: ArtifactRepository

class WriteTransaction(ReadTransaction, Protocol): ...

class UnitOfWork(Protocol):
    def read(self) -> AbstractContextManager[ReadTransaction]: ...
    def write(self) -> AbstractContextManager[WriteTransaction]: ...

# src/tychora/contracts/artifacts.py (changed)
class ArtifactRef: @classmethod def from_row(cls, row: Sequence[object]) -> ArtifactRef   # the one row constructor; upper layers use it

# src/tychora/application/owner.py, src/tychora/adapters/sqlite/corpus.py (changed)
WorkspaceOwner.uow: SqliteUnitOfWork; WorkspaceOwner.policy_epoch() -> int; WorkspaceOwner.decision_watermark() -> int; CorpusStore.uow

# src/tychora/adapters/sqlite/repositories/__init__.py
class SqliteTransaction:                      # what read()/write() yield; attributes workspace, artifacts (later phases add more)
class SqliteWorkspaceRepository: ...          # implements WorkspaceRepository
class SqliteUnitOfWork:                       # implements UnitOfWork over one connection in phase 1
    def __init__(self, db: sqlite3.Connection) -> None: ...
def artifact_ref(row: Sequence[object]) -> ArtifactRef   # alias of ArtifactRef.from_row for adapters; (sha, size, media_type, storage_key)

# src/tychora/adapters/sqlite/migrations.py
SCHEMA_VERSION = 2

# src/tychora/evaluation/bench/  (package: budgets, stages, results, corpus, synth, curation, harness)
# src/tychora/interfaces/bench.py (CLI entry point: python -m tychora.interfaces.bench; the evaluation
#   layer may not import the public client, so composition lives in interfaces)
#   fetch --corpus bench-v1|bench-small --cache <dir>
#   run   --corpus bench-v1|bench-small --suite ingest|query|scale|families
#         --profile offline|online [--out bench-results] [--cache <dir>]
class StageSample(Contract): stage: str; duration_ms: float; status: str
class StageSource(Protocol):                  # phase 5 adds the trace-backed implementation
    def query_stages(self, run_id: str) -> tuple[StageSample, ...]: ...
    def ingest_stages(self, job_id: str) -> tuple[StageSample, ...]: ...
QUERY_STAGE_BUDGETS_MS: Mapping[str, int]     # spec §4.12, keys listed in phase 1
INGEST_STAGE_BUDGETS_S: Mapping[str, float]
# result file: bench-results/<commit>/<suite>-<profile>.json, schema "tychora.bench-result" version 1

# src/tychora/evaluation/dataset.py (changed)
class NativeRefAnchor(Contract): native_ref: NonEmptyStr
class CharRangeAnchor(Contract): char_start: NonNegativeInt; char_end: NonNegativeInt
class PageAnchor(Contract): page: PositiveInt
class GoldLabel(Contract):
    collection: NonEmptyStr; source_key: NonEmptyStr; edition_key: str | None = None
    anchor: NativeRefAnchor | CharRangeAnchor | PageAnchor
class GoldBinding(Contract): source_id: Identifier; revision_id: Identifier; unit_ids: tuple[Identifier, ...]
class GoldLabelResolver(Protocol):     # primitives only, so adapters can implement it without importing evaluation
    def source_ids(self, collection: str, source_key: str) -> tuple[str, ...]: ...
    def revision_ids(self, source_id: str, edition_key: str | None) -> tuple[str, ...]: ...
    def units_by_native_ref(self, revision_id: str, native_ref: str) -> tuple[str, ...]: ...
    def units_by_char_range(self, revision_id: str, char_start: int, char_end: int) -> tuple[str, ...]: ...
    def units_by_page(self, revision_id: str, page: int) -> tuple[str, ...]: ...
class GoldLabelError(ValueError): reason: Literal["label.missing", "label.ambiguous"]; detail: str
def resolve_gold_binding(label: GoldLabel, resolver: GoldLabelResolver) -> GoldBinding
def resolve_gold_label(label: GoldLabel, resolver: GoldLabelResolver) -> tuple[str, ...]  # unit IDs

# src/tychora/application/workspace_rebuild.py
def rebuild_workspace(old: Path, destination: Path, ctx: ExecutionContext, *,
                      open_target: Callable[[Path, RequestScope], RebuildTarget],
                      kind_map: Mapping[str, str] | None = None) -> WorkspaceRebuildReport
# src/tychora/api.py
def rebuild_workspace_from(old: Path, destination: Path, *, kind_map: Mapping[str, str] | None = None,
                           providers: ProviderBindings | None = None,
                           token_counter: TokenCounter | None = None) -> WorkspaceRebuildReport
```

Phase 1 also starts `tests/architecture/test_sql_free_modules.py` with `SQL_FREE_MODULES: tuple[str, ...]`; each later phase appends the modules it makes SQL-free (spec §4.13 list: `application/sources`, `owner`, `query`, `reranking`, `recovery`, `removal`, `restore`, `inspection`, `visual`, `indexing/coordinator`, `retrieval/visuals`).

### Produced by phase 2

```python
# src/tychora/contracts/snapshots.py
PAGE_BUCKETS = 256
def bucket_of(source_id: str) -> int          # int(sha256(source_id)[:2], 16)
class PageBatch(Contract):
    slot: NonEmptyStr; batch_id: Identifier; family_id: Sha256; count: NonNegativeInt
    receipt_digest: Sha256; input_manifest: ArtifactRef; vectors: ArtifactRef | None = None
class PageMember(Contract):
    source_id: Identifier; revision_id: Identifier; extraction_id: Identifier
    is_default: bool = True; batches: tuple[PageBatch, ...]
class MembershipPage(Contract):
    bucket: int; members: tuple[PageMember, ...]
    page_id: Identifier                         # validated: digest("membership-page-v2", bucket, members, batches)
    def manifest_document(self) -> dict: ...    # type "membership_page", schema_version "2"
    def manifest_bytes(self) -> bytes: ...
MAX_PAGE_BYTES = 4 * 1024 * 1024               # INTEGRITY_ERROR invariant.page_oversize above
class IndexFamily(Contract): ...               # moved here from contracts/publication.py, fields unchanged
class SlotFamily(Contract): slot: NonEmptyStr; family: IndexFamily; required: bool = True
def compute_page_id(bucket: int, members: Sequence[PageMember]) -> str
def membership_sha(page_ids: Sequence[str]) -> str
@dataclass(frozen=True)
class SnapshotBinding:                         # everything but the pages a snapshot identity binds
    workspace_id: str; base_snapshot_id: str | None; job_id: str; config_sha: str
    families: Sequence[SlotFamily]; root_batch_ids: Sequence[str] = ()
def snapshot_id_v2(binding: SnapshotBinding, page_ids: Sequence[str]) -> str

# src/tychora/contracts/publication.py (rewritten)
class NewBatch(Contract):
    slot: NonEmptyStr; batch_id: Identifier; family_id: Sha256; extraction_id: Identifier | None
    input_manifest: ArtifactRef; expected_count: NonNegativeInt
    receipt: LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt; vectors: ArtifactRef | None = None
class PublicationPlan(Contract):
    job_id; snapshot_id; workspace_id; base_snapshot_id; config_sha
    families: tuple[SlotFamily, ...]; member_count: NonNegativeInt; new_pages: tuple[MembershipPage, ...]
    reused_page_ids: tuple[Identifier, ...]; new_batches: tuple[NewBatch, ...]
    root_batches: tuple[PageBatch, ...] = ()   # corpus-wide graph generation only; removed by phase 6
    graph_watermark: NonNegativeInt | None = None   # removed by phase 6
    manifest: ArtifactRef                      # root document, type "snapshot", schema_version "2"
    page_ids: tuple[str, ...]                  # property: sorted new + reused
class RetainedBatch(Contract): ...             # interim rename of PublicationBatch rebuilt from SQL; removed by phase 4
class RetainedSlot(Contract): slot; family; required; batches: tuple[RetainedBatch, ...]   # interim; removed by phase 4
# CorpusStore.snapshot_slots(view: SnapshotView) -> tuple[RetainedSlot, ...]   interim loader; removed by phase 4
class SnapshotView(Contract):                  # phase 4 removes `members`
    snapshot_id; workspace_id; parent_snapshot_id; config_sha; membership_sha
    member_count: NonNegativeInt; page_ids: tuple[Identifier, ...]
    families: tuple[SlotFamily, ...]; members: tuple[SnapshotMember, ...]
    lexical_statistics_epoch: NonNegativeInt = 0

# src/tychora/ports/repositories.py (added; ReadTransaction/WriteTransaction gain these attributes)
class BatchRow(Contract):
    batch_id; family_id; slot; extraction_id: str | None; state: Literal["building","ready","failed","removed"]
    expected_count: int; actual_count: int | None; receipt_digest: str | None
    input_manifest: ArtifactRef; vectors: ArtifactRef | None
class SnapshotRepository(Protocol):
    def active_id(self) -> str | None: ...
    def view(self, snapshot_id: str) -> SnapshotView: ...
    def page(self, snapshot_id: str, bucket: int) -> MembershipPage | None: ...
    def page_manifests(self, page_ids: tuple[str, ...]) -> Mapping[str, ArtifactRef]: ...
    def insert_page(self, page: MembershipPage, manifest: ArtifactRef) -> None: ...
    def insert_snapshot(self, plan: PublicationPlan) -> None: ...
    def set_active(self, snapshot_id: str) -> None: ...
    def retained_ids(self, keep_last: int = 16) -> frozenset[str]: ...
    def collect(self, limit: int = 64, *, leased: frozenset[str] = frozenset(),
                include_unpublished: bool = False) -> GcReport: ...
    def footprint(self, snapshot_id: str) -> PublicationFootprint: ...
class BatchRepository(Protocol):
    def begin(self, batch: NewBatch) -> None: ...                 # state building
    def write_lexical(self, batch_id: str, records: tuple[LexicalRecord, ...]) -> None: ...
    def write_vectors(self, batch_id: str, rows: tuple[VectorRecordRow, ...]) -> None: ...
    def mark_ready(self, batch_id: str, actual_count: int) -> None: ...
    def count_ready(self, batch_ids: tuple[str, ...]) -> int: ...
    def get(self, batch_id: str) -> BatchRow | None: ...
    def find(self, family_id: str, extraction_id: str) -> BatchRow | None: ...
    def for_snapshot(self, snapshot_id: str, slot: str) -> tuple[BatchRow, ...]: ...
    def receipt(self, batch_id: str) -> LexicalBatchReceipt | BatchReceipt | GraphBatchReceipt | None: ...
    def record_ids(self, batch_id: str) -> tuple[str, ...]: ...
    def purge(self, batch_ids: tuple[str, ...]) -> tuple[str, ...]: ...
class FamilyRepository(Protocol):
    def get(self, family_id: str) -> IndexFamily | None: ...
    def nonce(self, channel: str, identity_sha: str) -> str | None: ...
    def create(self, family: IndexFamily, nonce: str, identity_sha: str) -> None: ...
# identity_sha: the configuration digest bound into family_id (retained config_sha for lexical;
# the text/image/graph family-config digest otherwise)

# src/tychora/contracts/vector_batches.py (pure; also defines vector_value_sha, moved from indexing/receipts.py; importers updated)
VECTOR_BATCH_MAGIC = b"TYVB0001"
VECTOR_BATCH_MEDIA_TYPE = "application/vnd.tychora.vector-batch.v1"
class VectorBatchRow(Contract): record_id: Identifier; input_sha: Sha256; values: tuple[float, ...]
class DecodedVectorBatch(Contract): family_id: Sha256; dimension: PositiveInt; rows: tuple[VectorBatchRow, ...]
def encode_vector_batch(family_id: str, dimension: int, rows: Sequence[VectorBatchRow]) -> bytes
def decode_vector_batch(raw: bytes) -> DecodedVectorBatch   # verifies each value_sha

# src/tychora/configuration/models.py (changed)
class ResolvedConfig(Contract): ...
    @cached_property
    def retained_sha(self) -> str: ...   # sha256 of the canonical retained registry document; not a field
```

Schema added by phase 2: `membership_pages`, `page_members`, `page_batches`, `snapshot_pages`, the views `snapshot_members` and `snapshot_batches`, transitional `snapshot_root_batches(snapshot_id, slot, batch_id)`, `snapshots.job_id`, `snapshots.member_count`, `index_families.identity_sha`, `index_families.creation_nonce`, `index_batches.vectors_sha`, `vector_records.row_index` (replaces `embedding_cache_sha`). Lexical triggers stop bumping `lexical_statistics_epoch` per row. Chroma metadata gains `revision_id`, `extraction_id`, `owner_kind`, `owner_id`.

### Produced by phase 3

```python
# src/tychora/contracts/sections.py
SectionLevel = Literal["part", "chapter", "section", "article", "annex", "clause", "file"]
class Depth(Contract): n: PositiveInt
ContextLevel = SectionLevel | Depth | Literal["document"]
DEFAULT_CONTEXT_LEVEL = "depth:1"
def parse_context_level(text: str) -> ContextLevel        # "article", "depth:2", "document"
class SectionEntry(Contract): key: NonEmptyStr; level: SectionLevel | None; depth: NonNegativeInt  # depth 0 = document root, depth == index in the path
BoundaryScheme = Literal["file", "section", "table", "article", "figure"]
def boundary_key(scheme: BoundaryScheme, native_ref: str) -> str   # "<scheme>:<native_ref>"
ROOT_SECTION_KEY = "section:#root"
def section_key(section_path: Sequence[SectionEntry], level: ContextLevel) -> str
def context_key(section_path: Sequence[SectionEntry], context_level: ContextLevel) -> str
def chunk_section_key(section_path: Sequence[SectionEntry], section_depth: int, context_level: ContextLevel) -> str
class HeadingLevel(Contract): pattern: NonEmptyStr; level: SectionLevel   # kind TOML key heading_levels; matched with the `regex` package under a timeout
def parse_heading_levels(value: object) -> tuple[HeadingLevel, ...]; def kind_heading_levels(kind: Mapping) -> tuple[HeadingLevel, ...]
def heading_level_for(levels: Sequence[HeadingLevel], text: str) -> SectionLevel | None
def dump_section_path(path: Sequence[SectionEntry]) -> list[dict]
def section_path_of(attributes: Mapping, boundary: str) -> tuple[SectionEntry, ...]   # no stored path: the boundary key alone
def normalizer_definition(kind: Mapping) -> dict   # the kind without context_level; digested by every normalizer fingerprint

# src/tychora/ingestion/sections.py (added)
class Placement; class Placeable; def place_units(...) -> ...; ARTICLE_NORMALIZERS
def native_normalizer_fingerprint(kind: Mapping, registry: Registry) -> str   # "native-normalizer-v2"; re-exported by ingestion/native.py

# src/tychora/configuration/registry.py (changed)
class Registry: def context_level(self, kind_id: str) -> ContextLevel: ...   # kind TOML key context_level, default "depth:1"

# src/tychora/ports/tokens.py (changed; TokenCounter itself is unchanged)
@runtime_checkable
class OffsetTokenCounter(TokenCounter, Protocol):
    def count_with_offsets(self, text: str) -> tuple[tuple[int, int], ...]: ...  # char span per counted token; additive contract
# Neither BenchCounter nor the Voyage counter implements it: both keep the counted split (identical windows).

# src/tychora/indexing/windows.py (changed)
def window_unit(prepared, counter, *, context_level: ContextLevel = Depth(n=1)) -> tuple[SearchWindow, ...]
# src/tychora/contracts/indexing.py (changed): SearchWindow.context_key; context_group_key = digest("context-group-v1", extraction_id, context_key)
def dependency_digest(dependency_ids: Iterable[str]) -> str
class OwnerDependencies(Contract): owner_id; dependency_ids: frozenset[str]
class IntervalRow(Contract): unit_id; char_start: int; char_end: int; dependency_digest: Sha256
class RepresentationFacts(Contract): extraction_id; dependencies: tuple[OwnerDependencies, ...]; intervals: tuple[IntervalRow, ...]
# src/tychora/indexing/intervals.py (added): def overlapping_pairs(...)   # sort-and-sweep
# src/tychora/indexing/representations.py (added; facts are computed in indexing, not in the adapter)
class ChunkSpan(NamedTuple): chunk_id; unit_id; char_start; char_end
def representation_facts(rows, prepared: PreparedExtraction, chunks: Iterable[ChunkSpan]) -> RepresentationFacts

# src/tychora/ports/repositories.py (added; IntervalRow re-exported from contracts/indexing.py)
class UnitRow(Contract): unit_id; extraction_id; boundary_key; ordinal: int; text_sha; char_count: int
class ChunkRow(Contract): chunk_id; unit_id; extraction_id; char_start: int; char_end: int; context_group_key: str   # not tychora.contracts.corpus.ChunkRow
class RepresentationRepository(Protocol):
    def units(self, unit_ids: tuple[str, ...]) -> tuple[UnitRow, ...]: ...
    def chunks(self, chunk_ids: tuple[str, ...]) -> tuple[ChunkRow, ...]: ...
    def dependencies(self, owner_ids: tuple[str, ...]) -> Mapping[str, frozenset[str]]: ...
    def intervals(self, unit_ids: tuple[str, ...]) -> tuple[IntervalRow, ...]: ...   # document order
    def write(self, facts: RepresentationFacts) -> None: ...
    # neighbours(unit_id, radius) -> tuple[UnitRow, ...] (same boundary_key): added by phase 4, its first caller
# ReadTransaction.representations; CorpusStore.commit_extraction(bundle, facts: RepresentationFacts | None = None) -> str
# src/tychora/adapters/sqlite/migrations.py: SCHEMA_CHECKSUM changed with 0001.sql; every earlier format-two checksum is in EARLIER_FORMAT_TWO_CHECKSUMS
# ProjectionCoordinator.prepare(..., *, on_stage: Callable[[str], None] | None = None) emits windowing, normalize_commit, embedding
```

Schema added: `representation_dependencies(owner_id, dependency_id)` (owners: units, chunks, assets, vector groups, and interval dependency sets keyed by their `dependency_digest`), `ownership_intervals(unit_id, char_start, char_end, dependency_digest)`. Normalizer fingerprints become `native-normalizer-v2`, `docling-normalizer-v3`, `docling-record-normalizer-v3` and `code-normalizer-v2`. Reasons added: `kind.section_levels_invalid`, `kind.unknown`, `tokens.atomic_limit`. Job stages added: `windowing`, `normalize_commit`, `embedding`. Dependency added: `regex>=2024.11.6`.

### Produced by phase 4

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
# also as built: contracts/graph.py AssertionQuery (one request value for get_assertions); retrieval/compiler.py EligibilityInputs
#   (the public compile request); application/query_plan.py QueryBindings, QueryPlan(explicit), plan_for; retrieval/response.py budget_stub
```

Schema added: `review_current(target_kind, target_id, state, review_event_id)`, `sources.allowed_uses_override`, `vector_records.group_id`, indexes `representation_dependencies_dependency`, `units_declared_review` and `unit_assets_asset`; `SCHEMA_CHECKSUM` changed and `be4274cd…` (phase 3) joined `EARLIER_FORMAT_TWO_CHECKSUMS`. Resources added: `resources/stopwords/en@1.txt`, `fr@1.txt`, `LICENSE-snowball.txt`. Reasons added: `canonical.facts_missing`, `policy.use_unknown`, `policy.use_override_empty`, `source.absent`, `scope.capability_missing`, `request.limit_exceeded`, `mode.unavailable`, `profile.snapshot_mismatch`, `kind.historical_unavailable`, `facet.binding_unverified`, `policy.epoch_changed`, `identifier.namespace_unknown`, `scope.dependency_ineligible`, `edition.as_of_required`, `channel.not_configured`, `channel.input_unusable`.

### Produced by phase 5

```python
# src/tychora/contracts/usage.py
UsageOperation = Literal["embed_document","embed_query","embed_image","rerank","describe_image","ocr"]
class ProviderUsage(Contract):
    provider; model; operation: UsageOperation; input_tokens: int | None; output_tokens: int | None
    images: int | None; pixels: int | None; pages: int | None; latency_ms: int
    outcome: Literal["ok","failed","cancelled","denied"]; error_code: str | None
    batch: bool = False; units_source: Literal["provider","local"] = "provider"
class PriceDocument(Contract): version_id; effective_from: AwareDatetime; origin: Literal["builtin","user"]
                               prices: tuple[PriceEntry, ...]   # (provider, model, operation, unit, batch) -> USD per million, decimal string
def cost_usd(rows: Iterable[UsageRow], table: PriceDocument) -> Decimal | None

# src/tychora/contracts/trace.py
TraceStageName = Literal["eligibility","exact","lexical","text_embed","text_ann","image_embed","image_ann",
                         "graph","fusion","admission","rerank_facts","rerank","assembly","record"]
class QueryTrace(Contract): schema "tychora.query-trace", version 1, as spec §4.10 (<= 64 KiB, truncated flag)
class TraceRecorder: def stage(self, name: TraceStageName) -> AbstractContextManager[StageHandle]: ...

# repositories added
UsageRepository, TraceRepository, HistoryRepository (stage timings), AggregateRepository, PriceRepository
# src/tychora/evaluation/bench/stages.py
class TraceStageSource(StageSource)            # replaces the wall-clock source of phase 1
```

Schema added: `usage_events`, `price_tables`, `query_traces`, `query_stage_timings`, `query_runs.total_ms`, `document_status`, `collection_aggregates`, `extraction_layers`, `evidence_units.layer_id`, `assets.layer_id`, `index_batches.layer_id` with `UNIQUE(family_id, extraction_id, layer_id)`. Endpoints: `GET /v1/documents` (filter, sort, cursor), `GET /v1/collections/{id}/summary`, `GET /v1/usage`, `GET /v1/prices`, `PUT /v1/prices`, `GET /v1/metrics/queries`, `GET /v1/query-runs/{id}/trace`, ranged `GET /v1/assets/{id}/content`, `GET /v1/sources/{id}/revisions/{revision_id}/original`, `index_facts` on `GET /v1/sources/{id}`, `POST /v1/settings/providers/{role}/test`.

### Produced by phase 6

```python
# src/tychora/contracts/graph.py (changed)
def contribution_id(family_id: str, extraction_id: str, layer_id: str | None, relation_fingerprint: str) -> str
class GraphContribution(Contract): contribution_id; family_id; extraction_id; evidence_ids; entity_ids; assertion_ids; digest
class NeighboursRequest(Contract): target; direction: Literal["in","out","both"]; predicates; depth <= 3; per_predicate_limit <= 20; limit <= 100
class ImpactRequest(Contract): target; predicates = ("calls","imports","inherits"); depth <= 5; fan_out <= 50; limit <= 500
# ports/graph.py: GraphStore.write_contribution(...), GraphStore.compact(...), GraphStore.neighbours(...), GraphStore.impact(...)
```

Schema added: `graph_degrees(family_id, entity_id, contribution_id, predicate, direction, edges)`. Schema removed: `snapshot_root_batches`. MCP tools `neighbours`, `impact`; HTTP `POST /v1/relations/neighbours`, `POST /v1/relations/impact`, `GET /v1/relations/hubs`.

### Produced by phase 7

`VectorFamilySpec`, `QueryEncoder`, `LocalQueryEncoder` port and ONNX adapter; `SourceConnector` port, `ConnectorItem`, Git-history connector; `manage rebuild index`, `manage connector sync <name>`, `manage import-catalog <file.jsonl>`; `CollectionPolicy.max_upload_rank`; `SourceRegistrationRepository`; `execute_accepted` split into `capture`, `register`, `reuse_or_extract`, `project`, `publish`.

## Spec coverage by phase

| Spec section | Phase |
| --- | --- |
| §4.1 principles | all (each phase states the principle it applies) |
| §4.2 snapshot format v2, retention, GC | 2 |
| §4.3 publication, configuration `retained_sha` (D7) | 2 |
| §4.4 vector batches, family identity | 2; family options and local encoder | 7 |
| §4.5 graph contributions, compaction, degree table, fallbacks | 6 |
| §4.6 lanes, read pool, priorities, queue bounds | 4 |
| §4.7 snapshot cache, eligibility v2, cache, candidate selection, typed errors | 4 |
| §4.8 write-time representation facts | 3 (write side), 4 (read side, `CachedSelection`) |
| §4.9 strict mode, stop words, editions | 4 |
| §4.10 boundary keys, section levels, linear windowing | 3 |
| §4.10 query trace, usage ledger, aggregates, background phases | 5 |
| §4.10 benchmark corpus and harness | 1 (`families` suite: 7) |
| §4.11 rebuild `workspace` | 1; `native` from batch artifacts | 2; `index` | 7 |
| §4.12 performance budgets | 1 (encoded), asserted per the gates below |
| §4.13 repository layer | 1 (ports, workspace and artifact repositories), then per phase |
| §4.13 error reasons | 1 (registry), then every phase classifies the sites it touches |
| §4.13 dead code (purge commands, retention task) | 5 |
| §4.14 parity: per-query channels, review gating, generation policy, edition defects | 4 |
| §4.14 parity: stable gold labels | 1 |
| §4.14 parity: neighbours, impact, edge weights | 6 |
| §4.14 parity: connectors, bulk catalog import | 7 |
| §4.15 dashboard endpoints | 5 (hubs: 6) |
| §4.16 networked-store mapping | 1 (port-neutrality test), checked in every phase review |
| §5 migration (format, archives, API) | 1 (format, workspace rebuild), 2 (archives) |
| §6 testing table | each row is owned by the phase of its feature |
| §7 code-health ratchet | every phase; final check in 7 |
| Programme resolved decision 1 (`max_upload_rank`) | 7 |
| Programme resolved decision 4 (`rescore_top` is query-only) | 7 |
| Programme resolved decision 9 (Batch API under existing consent) | 5 (`usage_events.batch`) |
| Programme resolved decision 11 (one-time kind map in rebuild) | 1 (hook in `rebuild_workspace`), filled by sub-project 5 |

## Flow-review findings by phase

| ID | Finding | Closed in |
| --- | --- | --- |
| D1 | Snapshot manifest embeds the corpus | 2 |
| D2 | Publication re-verifies the whole corpus | 2 |
| D3 | Full graph generation per add | 6 |
| D4 / Q4 | One owner thread for all work | 4 |
| D5 | Boundary keys defeat chunking and context | 3 |
| D6 | One CAS file per vector | 2 |
| D7 | Configuration serialized on hot paths | 2 (publication, segments), 4 (eligibility, query) |
| D8 | Rebase after configuration change has no path | 7 |
| C1 | Quadratic windowing | 3 |
| C2 | Quadratic ownership validation, re-run on reads | 3 (linear, persisted), 4 (read calls removed) |
| C3 | Bare `ValueError` from eligibility | 4 |
| C4 | No stop words | 4 |
| C5 | Exact-channel cap, editions, purge, family identity, busy owner, trace | exact cap and editions: 4; family identity: 2; busy owner: 1; purge and trace: 5 |
| Q1 / Q2 | Eligibility corpus scan, manifest parsed per query | 4 (Q2's manifest half: 2) |
| Q3 | Read-time validation of write-time invariants | 3 and 4 |
| Q5 | Result cache pickles documents | 4 |
| Q6 | Strict default fails on ordinary queries | 4 |
| §4.3 loops | `_vector_owners`, `_candidate_lanes` | 4 |
| §4.3 graph truncation marks coverage partial | `truncated` separate from `partial` | 4 |

Deferred by the spec and not planned here: `linked_visual_context` rank restart (sub-project 2); incremental `ResponseAssembler` measuring (revisited only if the 50 ms assembly budget is missed in phase 4's gate).

## Benchmark gates per phase

All gates run with `python -m tychora.interfaces.bench run …` (phase 1). "Offline" uses the zero-latency deterministic provider; it asserts local work only. A gate that fails blocks the phase's merge.

| Phase | Gate | Command | Pass condition |
| --- | --- | --- | --- |
| 1 | Harness works, baselines recorded | `bench run --corpus bench-small --suite ingest --profile offline` and `--suite query` | Result JSON written with every key of the result schema; `bench-results/baseline/evaluation-b91d808.json` committed (E6 baseline per stage) |
| 2 | E3 | `bench run --corpus bench-small --suite scale --profile offline` (replays 100 → 10,000 lexical-only documents) | 10,000 members publish; publication bytes per add at 10,000 ≤ 2× at 1,000 (see spec gap 8); largest snapshot or page artifact ≤ 1 MiB; publication p90 ≤ 0.5 s at 10,000 |
| 3 | C1, ingest windowing stage | `bench run --corpus bench-small --suite ingest --profile offline` | Windowing property test green; stage "windowing" p90 ≤ 1.0 s per 100-page native document; no regression > 20% on any other ingest stage |
| 4 | E1 local, E4, E5, E6, concurrency | `bench run --corpus bench-small --suite query --profile offline`; `--suite scale` | Local query share p95 < 300 ms (wall clock; per-stage §4.12 budgets are asserted from phase 5, when traces exist); latency at 10,000 ≤ 1.5× at 1,000; strict "verification outcome" succeeds at 20, 1,000, 10,000; evaluation metrics ≥ the phase-1 baselines (E6 lexical, measured at `b91d808`, in `bench-results/baseline/evaluation-b91d808.json`; code gold set G22, measured at phase-1 HEAD, in `tests/fixtures/evaluation/code-gold/baseline.json`) per stage; one 300-page add with 20 concurrent queries keeps query p95 ≤ 1.5× idle |
| 5 | E7, E10, harness switches to trace stages | `bench run --corpus bench-small --suite query --profile offline` | Every run has a trace and per-stage timings; every provider call in the run has one usage row; stage table comes from `TraceStageSource`; endpoint contract tests green and fixtures recorded |
| 6 | E11 | `bench run --corpus bench-small --suite scale --profile offline` with graph enabled (software preset fixture) | Graph bytes written per add at 10,000 ≤ 2× at 100; next query after publication sees new relations; traversal p95 within the graph budget after compacting 50% of contributions |
| 7 | E1 online, E2, E8, E9, families | `bench run --corpus bench-v1 --suite query --profile online` (weekly workflow, credentials); `--suite ingest --profile online`; `--suite families --profile online` | Query p95 < 1 s total; native PDF ≤ 100 pages ingestion p90 < 30 s; per-page budgets reported; every §4.14 parity row has a passing test; `tools.health check` green with the §7 deletions done |

On shared CI runners, `bench-small` fails only on a regression of more than 20% against the last result from the same runner class (spec §4.12 "Assertions").

**Phase 4 gate status: pending reference-runner run.** The verdict comes only from the `Benchmarks` workflow on the reference runner, dispatched by the owner. Strict "verification outcome" succeeded at 20, 1,000 and 10,000 documents. Concurrency acceptance passes locally: the client bridge no longer caps admission below the owner queue bounds (33 concurrent callers succeed), and rounds of 20 concurrent queries, sampled from the 300-page add until it settles and overlapping its stages past parsing, stay within 1.5× of the same 20 concurrent queries idle; an online-shaped variant with a synthetic reranker also checks that every writer task of a reranked query runs on the interactive lane. Code gold G22 is at or above `baseline.json` on both variants (see "Phase 4 as built").

## Code-health ratchet by phase

| Phase | Ratchet obligation (spec §7) |
| --- | --- |
| 1 | Reason registry exists; G24 lowered by every site classified in the touched modules; one `ArtifactRef` row constructor, one watermark read, one epoch read (duplication); port-neutrality test green |
| 2 | Deleted: per-row ownership SELECTs in publication, `pin_snapshot` payload pinning, per-vector CAS writes, manifest parse in `resolve_snapshot`. No module joins `SQL_FREE_MODULES` yet: `indexing/coordinator`, `application/removal` and `application/restore` keep extraction, visual and retention SQL until phase 4 (removal, restore) and phase 7 (coordinator) |
| 3 | Deleted: `_owners_by_locator`; quadratic `_locator_intervals` replaced by sort-and-sweep |
| 4 | Deleted: read-path `prepare_extraction` calls (6 sites), `_vector_owners` and `_candidate_lanes` loops, three `resolve_snapshot` calls per query, configuration dumps in `query`, `eligibility`, `segments`; `read_eligibility_facts`, `_prepare`, `EligibilityCompiler._compile` replaced by stage functions under 80 lines; `retrieval/eligibility.py` split in three; `adapters/sqlite/eligibility.py` no longer imports `retrieval` or `indexing`; `application/query`, `reranking`, `visual`, `inspection`, `owner`, `removal`, `restore`, `retrieval/visuals` join `SQL_FREE_MODULES`; interim `RetainedSlot`, `RetainedBatch`, `CorpusStore.snapshot_slots` deleted |
| 5 | Purge commands have callers; `application/recovery` joins `SQL_FREE_MODULES` |
| 6 | Deleted: per-add graph generations, `_graph_slot`'s corpus-wide read, corpus-wide `GraphManifest`, `snapshot_root_batches` |
| 7 | `indexing/coordinator` joins `SQL_FREE_MODULES`; `execute_accepted` split into five stages of at most 60 lines; `application/sources` joins `SQL_FREE_MODULES`; import-linter contract "rewritten modules import no `sqlite3`" enabled; 0 reasonless `INTEGRITY_ERROR` in touched modules; mutation score on `adapters/sqlite/repositories/` and `retrieval/compiler.py` at or above the code-health baseline; no function over 120 lines in touched modules |

## Phase 4 as built: documented changes and follow-ups

Decisions taken while executing phase 4 that the spec or later phases must take into account.

- **Spec wording (§4.7).** `EligibilityView.allows(dependency_ids)` takes no `kinds`: every persisted dependency set names one revision and units or assets of its extraction, so the revision grant and the exclusion sets decide alone (spec gap 30). `excluded_unit_ids` holds no non-citable unit, although §4.7 lists "non-citable" among exclusions: non-citable units are structural context that dependency sets name, and citability is a candidate rule applied in the search joins and the survivor check (spec gap 31). The spec text should be amended to match both.
- **Filter semantics, a documented change from v1.** A revision-metadata filter clause judges the revision. Owners that no unit reaches (unlinked assets, their vector records, revisions with zero units) are evaluated on revision metadata with empty unit values on every path, so admission no longer depends on the shape of the filter tree; v1 admitted such owners under a filtered query.
- **`review_states`.** A request's `review_states` narrows the mode's review states at every gate: revisions, units, assets and proxies. An accepted-only request returns nothing until units are reviewed, as with the `verified` profile.
- **`allow_degraded`.** It is a query-time setting and is excluded from the pinned-runtime compatibility comparison, as the lexical profile is: a configuration differing only in `allow_degraded` reuses the pinned snapshot.
- **Delivery truncation.** Omission by the output budget alone is a limit truncation: `delivery` is listed in `Coverage.truncated_channels` and the status stays `complete` (§4.9). An unmet declared requirement still makes the result `partial`.
- **Inventory and rebuild.** Resolved in the phase 4 final fix (ruling FR-2): the inventory reads `sources.allowed_uses_override` when the old schema has it, and `manage rebuild workspace --from` re-applies it with `set_allowed_uses_override` after the source is re-added. An override the new workspace refuses is reported as a skipped `uses_override_unapplied` item.
- **Guard query plan.** The read-path guard query's plan relies on SQLite having no `ANALYZE` statistics for the workspace (nothing in `src` runs `ANALYZE`). A phase that adds `ANALYZE` must re-check that plan.
- **Trace.** Per-stage query budgets (§4.12) are asserted from phase 5, when the query trace exists; phase 4's gate measures wall-clock totals only.
- **Code gold and stop words.** The built-in `code@1` profile selects `retrieval.lexical.stopwords = "none"` (a value §4.9's lexical profile already allows; documents profiles keep `builtin@1`). With the built-in lists, dropping "and" and "with" from code questions pushed five code-gold file metrics below `baseline.json`; with `none`, every metric is at or above the baseline and `definition_recall` rises to 0.3696 (lexical) and 0.1522 (lexical_structural). `tests/fixtures/evaluation/code-gold/README.md` ("Search time", `definition_recall` table, "Stop words") holds the values measured on a developer laptop: a search takes about 3.7 s median (7.4 s maximum) instead of 12 s.
- **`review_states` that admit nothing.** An empty `review_states` list is refused by the request contract, and states disjoint from the mode's are refused with `INVALID_REQUEST` `review.states_empty` (final fix M9), so an empty result always means no match.
- **Survivor bound.** The survivor check reads at most 200 fused candidates, chosen in admission order: exact keys (up to 32), then graph-only keys (up to 8), then fused rank. Candidates left unread are reported as a `survivors` entry in `Coverage.truncated_channels`; the status stays `complete`, as for other bounded reads (final fix I1).
- **Unavailable graph.** A configured graph with no pinned generation is listed in `Coverage.unavailable_channels` and makes the result `partial`; a graph the request selected itself is still refused with `graph.disabled`.
- **CPU lane (§4.6), deferred to phase 5.** Only the counter preflight runs on `run_cpu`. Segment assembly and response measuring and serialization (`query.py` `_assemble` and the assembler) run inline on the client bridge's event-loop thread, and a registered image query's decode (`_registered_pixels`, up to 40 MP) runs on the writer thread. Phase 5 owns moving them to the CPU lane, with the query trace that measures them.
- **Structural order after reopening.** `GraphTraversal._structural_order` reads the in-memory build of the process that built the graph generation, so a structural graph query in a reopened workspace fails with `INDEX_INCOMPLETE` "Canonical structural source order is unavailable" until the next build. This predates phase 4 (`e3dc00f`); phase 6 (incremental graph) replaces the generation and should persist the order.

### Phase 4 follow-ups (Low, parked by the task reviews)

- T6: `OwnerFact` fails validation on a NULL `index_batches.extraction_id`; only graph batches have one, and they hold no vector records.
- T7: the eligibility oracle shares `owner_facts` with the code under test, has no image records, and its fixture needs extras.
- T9: an absent kind is never reported truncated; `_enough` counts the metadata kind; concurrent adds that share record IDs are untested.
- T12b: the `_unreached` classification and the extraction-read assumption.
- T17: the plain query path raises on a pinned index rather than reporting it; the degraded test mutates a frozen config and its strict assertion should pin `CAPABILITY_MISSING`.
- T19: `tests/ingestion/test_tables.py::test_tables[docx]` is order-dependent (predates phase 4).
- T20: `references.py` splits the root list; `restore_rows` uses `fetchall` instead of streaming.
- T21: two load flakes in the default suite (the upgrade test's 5 s legacy-extraction wait; the cancelled collector's short sleeps).

## Spec gaps found while planning

Recorded here so the spec can be amended; each plan states the default it applies.

1. **Interim home of the corpus-wide graph generation.** The spec binds graph contributions in pages (§4.2, §4.5) but phase 2 lands before contributions (phase 6). Default: phase 2 binds the existing generation batch through a transitional `snapshot_root_batches` table that is part of the snapshot identity and is dropped in phase 6.
2. **Format evolution across phases.** The spec defines one fresh `0001.sql` but not how intermediate commits behave. Default: see "Format changes across phases" above.
3. **Stage names of the harness before the trace exists.** §4.10 says stage durations come from traces, which arrive in phase 5. Default: phases 1–4 record wall-clock totals and job-event stages through `StageSource`; phase 5 swaps in `TraceStageSource`.
4. **`bench-v1` content.** The spec fixes the mix but not the documents. Default: phase 1 commits a curation script with named public sources and licences, and the generated `benchmarks/bench-v1.toml`; the reference runner is the GitHub-hosted `ubuntu-22.04` runner class until a dedicated machine is named.
5. **Reference machine.** §4.12 assertions require "the reference runner", which is not identified. Default: as in gap 4; budget assertions apply only to results whose machine fingerprint matches `benchmarks/reference-machine.toml`.
6. **`rebuild workspace` target path.** §4.11 names `--from <old-path>` but not the destination. Default: the destination is the `--workspace` path given to the CLI; it must not exist or must be empty.
7. **Trace endpoint path.** §4.10 names `GET /v1/query-runs/{id}/trace`, while existing run routes are `/v1/queries/{run_id}` (`interfaces/http/routes.py:575-597`). Default: phase 5 implements the spec path only, as dashboard v2 §5 also cites it.
8. **E3 cannot hold against 100 members.** With 256 buckets, a page at 100 members holds under one member and at 10,000 about 40, so bytes written per add grow about 4–5× between 100 and 10,000 although they are bounded. Default: phase 2 gates 10,000 against 1,000 (about 1.7×) and reports the 100-member figure; the spec's E3 wording should be amended.
9. **Retention of a running job's base snapshot.** §4.2's rules omit it; GC could delete the base a job is about to rebase on. Default: phase 2 adds "the base snapshot of a running job" to the retention rules.
10. **Foreign keys to `snapshots`.** Jobs, child snapshots and expired query runs reference snapshots GC deletes. Default: phase 2 drops those foreign keys (identifiers stay, integrity checked by GC).
11. **Withdrawn sources inside pages.** A withdrawn source stays in its bucket's page until that page is next rewritten; eligibility already excludes it by policy. Default: accept; `manage gc` does not rewrite pages.
12. **Job checkpoint document.** Today the snapshot manifest doubles as the job's plan checkpoint. Default: phase 2 adds a `publication_plan` document (schema "2") for that purpose.
13. **Real-PDF probe queries.** E5 cites "the 2026-10-15 real-PDF probes", whose queries were never recorded. Default: phase 1's query set includes "verification outcome" only; the probe queries are added when the product owner supplies them (public documents only).
14. **Offline profile coverage.** No deterministic vector provider exists in `src`, so the offline profile measures lexical work only until phase 7's families suite adds a deterministic vector provider for benchmarks.
15. **Publication bytes per add.** Phase 1 approximates them by workspace growth; phase 2 replaces the approximation with `SnapshotRepository.footprint`.
16. **What `depth` counts.** §4.10 says "its heading depth (1 for a top-level chapter)". A document whose top headings are `##` would get depth 2 if the Markdown level were used. Answer: depth is the nesting depth (an entry's index in the path), so the outermost heading present is depth 1 whatever its markup level; the parser's level only decides nesting.
17. **Records below records.** "The record itself is the article" does not say what a numbered paragraph inside an article is. Answer: every provision or rule is its own boundary (`article:<its ref>`); a top-level one has level `article`, a nested one level `clause` with the enclosing records in its path. `annex` is assigned only through kind heading patterns: no records normalizer recognises annexes structurally today (`ingestion/normalize.py:533-540`).
18. **Units outside the scheme table.** Asset proxies and pixel-source units have no row. Answer: they keep `boundary_key = native_ref` and carry no `section_path`; `section_path_of` resolves such a unit to its boundary key, so its context group is unchanged.
19. **Tables split across pages.** Answer: each Docling table item is one `table:` boundary; its table and row views share it. Fragments are joined only by the reviewed `table-continuation` assertion (`ingestion/tables.py`), never by sharing a key, because a continuation can be rejected after ingestion.
20. **Identical windows need an additive tokenizer.** Counting a range from one tokenisation equals counting the range alone only when the tokenizer is additive at split positions; the spec's property test cannot hold for every tokenizer. Answer: `OffsetTokenCounter` states the contract, the property test checks it on additive counters, a counter without offsets or with unusable spans keeps the counted split. As built, neither `BenchCounter` (not additive) nor the Voyage counter implements `count_with_offsets`, so both keep the counted split and their windows stay identical; a Voyage offset counter needs an additivity qualification first (left to a later phase).
21. **`context_level` versus pinned kinds.** Kind envelopes are digest-pinned (`Registry.validate_activation`, `configuration/registry.py:209-236`) and `extraction_fingerprint` digests every kind (`configuration/resolve.py:540-551`), so changing a retained kind's `context_level` is refused today however the normalizer fingerprints are computed. Answer: this phase excludes `context_level` from normalizer fingerprints, so the extraction ID is stable; exempting the field from the envelope digest belongs to the kind ontology (sub-project 5), which owns per-kind values.
22. **What `dependency_digest` names.** Answer: the digest of an interval's dependency set, which is stored in `representation_dependencies` with that digest as `owner_id`; phase 4 reads intervals and resolves their sets in the same query as the candidates' own sets.
23. **Vector-group dependencies are not known at commit.** Context groups depend on the text provider configuration and are built after `commit_extraction`. Answer: their sets are written with the text batch, in the batch's transaction, keyed by group ID.
24. **Fingerprints the spec does not name.** Answer: `docling-record-normalizer-v3` and `code-normalizer-v2` bump too, since their output changes; composed documents follow automatically, because the composition fingerprint digests the base normalizer fingerprint.
25. **Root keys.** Answer: Markdown and Docling documents use `section:#root`; plain text and JSON records use `file:<source_key>` (text with no headings); a standalone image uses `figure:<ref>`; a code extraction uses `section:#root` above its `file:` entries, so `document` groups a whole repository.
26. **Measuring the windowing gate before traces.** Answer: the coordinator emits `windowing` and `normalize_commit` job stages; bench-small has no 100-page document, so a 300,000-character single-unit probe stands in, measured with the additive `OffsetCharacters` counter (not `BenchCounter`, which stays on the counted path); informative off the reference runner.
27. **`depth:N` deeper than the unit.** §4.10's fallback reads "the depth-1 ancestor". Answer: applied literally, then raised to the context key by `chunk_section_key`'s "never coarser" rule.
28. **Fact reads before facts exist.** Extractions committed by tests without facts, and any committed by an earlier commit of this branch, have no rows. Answer: the coordinator's reuse path writes the facts of an extraction it finds already committed, so every published member has them; how a read reports a missing set is phase 4's decision (suggested default: `INTEGRITY_ERROR` reason `canonical.row_invalid`).
29. **`query_only` and temp tables.** §4.6 requires `query_only` read connections and §4.7 a per-connection temp table "filled once per cache key". SQLite refuses temp writes under `query_only`, and a read task's `ROLLBACK` drops a temp table it created. Answer: bind ID lists through `json_each(?)` inside `IN (...)` (materialised once per statement), skip the restriction when every member is eligible, and cache each view's JSON text (Task 8).
30. **What `allows` needs.** The spec's `allows(dependency_ids, kinds)` leaves `kinds` undefined. Answer: none is needed. Every persisted dependency set names exactly one revision, its owner's, and units or assets of that revision's extraction; with exclusions closed under dependency, a set is readable when its revision is eligible and it names no excluded unit or asset (Tasks 6, 7).
31. **What `excluded_unit_ids` holds.** §4.7 says "rejected/blocked/non-citable for this mode". Answer: units and assets of eligible extractions that fail the mode's review states, the proxy policy or a unit-level (`all_units`) filter, plus every unit or asset depending on one of them (a recursive closure over `representation_dependencies`, which reproduces v1's fixpoint). Non-citable units are not excluded: they are structural context that dependency sets name; citability is a candidate rule applied in the search joins and the survivor check (Task 7).
32. **What `unit_filter` holds.** Answer: the units that pass a `matched_candidates` filter (candidate-only, as v1's candidate filter); the `all_units` filter's failures are exclusions (gap 3). Both are computed only when a request carries a filter, over the units of eligible extractions (Task 7).
33. **Graph eligibility before contributions.** §4.5 binds graph contributions (phase 6); the corpus-wide generation still filters traversal by unit lists. Answer: `GraphScope` lists the eligible units of eligible extractions (all not excluded, by gap 3's closure, so no dependency read), built only when a graph channel runs and cached with the view; phase 6 replaces it (Task 12).
34. **Blocked assertions.** v1 intersected assertions decided after the generation's watermark with the generation manifest, which it loaded per query. Answer: every assertion decided after the watermark is blocked; one outside the generation is never traversed, so the manifest is not read (Tasks 5, 12).
35. **Edition overlap and undated editions.** §4.9 says a decision arises "when winners' `[effective_from, effective_until)` intervals overlap" and that undated editions are "returned as `undated_candidates`". Answer: with `as_of`, an overlap decision arises when another edition in force declares an `effective_until` later than the winner's `effective_from` (an open-ended older edition is superseded by a later start, so it raises nothing — otherwise every amended document would demand a decision); undated editions are selected beside the winners and reported in their own decision; `revision_selector="applicable"` requires `as_of` (`edition.as_of_required`) (Task 16).
36. **Missing persisted facts.** Phase 3's gap 13 left the refusal to this phase, suggesting `canonical.row_invalid`. Answer: a dedicated `INTEGRITY_ERROR` reason `canonical.facts_missing` whose operator action names the workspace rebuild, because the remedy differs from a malformed row (Task 5).
37. **Retained inspection of archived collections.** v1 compiled exact retained inspection with archived collections treated as active and never cached the result. Answer: `compile(..., include_archived=True)` for explicit retained members only, never through the cache (Task 11).
38. **"Survivor check … in one query".** Answer: one statement reads the candidates', windows' and records' rows, one their dependency sets and, when a window's unit was not among the candidates, one more statement reads those units; at most three statements for at most 200 candidates (Tasks 8, 9).
39. **Global facets on every query.** v1 verified each member's global-facet binding artifact on every query. Answer: they are read and verified only when a request carries a filter, and only for the revisions it admits (Task 7).
40. **Stop-word lists.** §4.9 names `builtin@1` but not its content. Answer: the Snowball English and French lists (BSD 3-Clause, licence shipped beside them) without apostrophe forms, which the tokenizer splits, and without French "ai" and "eu", which collide with English acronyms in a combined list; the lexical profile is excluded from the pinned-configuration comparison because it only shapes query expressions (Task 14).
41. **Partitioned upserts.** §4.6 runs each partition as a separate writer task, but the coordinator's `finalize` upserts inside one writer task. Answer: `SourceService` submits each ≤ 2 MB partition as its own background task before `finalize`, which then finds every record present and only reads them back to build the receipt (Task 9).
42. **Vector hits outside the snapshot.** A family's collection holds records of unpublished or collected batches that v1 never saw (it queried by eligible ID lists). Answer: such hits are dropped silently by the canonical owner mapping (`is_admitted_record` checks the batch against the view), not refused (Task 9).
