# Engine Core v2, Phase 3: Write-Time Structure Implementation Plan

> **Status: task granularity, to be expanded before execution.** Files, interfaces and tests are fixed at task level. Before executing, expand each task with the superpowers:writing-plans skill into bite-sized TDD steps with real code and exact commands, against the code as it stands after phase 1 (and phase 2 if merged first).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every evidence unit a real section boundary and a level-tagged section path, count tokens once per interval, and persist at write time the representation facts that read paths re-derive today.

**Architecture:** Normalizers emit `boundary_key = "<scheme>:<native_ref>"` naming the enclosing section, table, article, file or figure, plus `attributes.section_path`. One resolver (`contracts/sections.py`) derives context and chunking keys from the path and the kind's `context_level`. Windowing tokenises each ownership interval once with offsets. `prepare_extraction` becomes linear (sort-and-sweep) and `commit_extraction` writes `representation_dependencies` and `ownership_intervals`, which phase 4 reads instead of re-validating.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, HF `tokenizers` (offsets), pytest, Hypothesis is not used: the property test is a seeded random loop (no new dependency).

**Spec:** `docs/superpowers/specs/2026-10-15-engine-core-v2-design.md` §4.8 (write side), §4.10 "Boundary keys", "Section levels and context level per kind", "Linear windowing". Index: `docs/superpowers/plans/2026-10-16-engine-core-v2-index.md`.

## Global Constraints

All constraints of the index apply. Phase-specific:

- Unit identity formulas are unchanged; extraction IDs change only because normalizer fingerprints bump to `docling-normalizer-v3` and `native-normalizer-v2`.
- `boundary_key` must not be a paragraph's own `native_ref` unless the unit is a section, article, table or file root.
- Windowing output is byte-for-byte identical to the current algorithm (lossless contract kept); only the counting algorithm changes.
- The named-level vocabulary is closed: `part`, `chapter`, `section`, `article`, `annex`, `clause`, `file`. Heading patterns that name levels are kind data owned by sub-project 5; this phase applies patterns given to it and ships none beyond tests.
- DDL changes update `SCHEMA_CHECKSUM` in the same commit.

## Review Focus

1. **A Markdown document with no headings, or text before the first heading.** Expect `section:#root` for those units and a working context group; pinned in Task 2.
2. **Heading levels that skip (H1 then H4) or go back up (H3 then H2).** Expect the nearest preceding heading at or above the unit's level, matching the existing `heading_path` computation; pinned in Task 2.
3. **A table split across pages in a Docling document.** Expect one `table:` boundary for every row and cell of the merged table; pinned in Task 3.
4. **A unit longer than 200 sentence boundaries** (records normalizers produce whole-section units). Expect token counting in fewer than 2× its length in tokens; pinned in Task 5.
5. **Overlapping locators from two units on the same page region** (captions over pictures). Expect the sort-and-sweep result to equal the quadratic result on every fixture; pinned in Task 6.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/tychora/contracts/sections.py` (create) | Section vocabulary, `SectionEntry`, `ContextLevel`, `boundary_key`, `section_key`, `context_key`, `chunk_section_key` |
| `src/tychora/ingestion/normalize.py` (modify `:100` and the heading-path computation) | Native Markdown and text boundaries and section paths |
| `src/tychora/ingestion/documents.py` (modify `:99`, `:165`), `ingestion/document_views.py:123`, `ingestion/assets.py:196`, `ingestion/vision.py:175` | Docling, picture, caption and image boundaries |
| `src/tychora/ingestion/document_records.py` (modify `:367`) | Record (article) boundaries |
| `src/tychora/adapters/code/normalize.py` (modify `:283,332,374`) | `file:` scheme for code |
| `src/tychora/indexing/windows.py` (modify `:52-79`, `:99`) | Linear splitting; context groups keyed by `context_key` |
| `src/tychora/indexing/groups.py` (modify `:64-72`, `:92`, `:179-196`) | Reuse counts; group by `context_key` |
| `src/tychora/ports/tokens.py` (modify) | `count_with_offsets` |
| `src/tychora/application/default_bindings.py` (modify the HF tokenizer counter near `:44-70`) | Offsets from `tokenizers.Encoding.offsets` |
| `src/tychora/indexing/representations.py` (modify `:661-731`) | Sort-and-sweep interval overlap |
| `src/tychora/adapters/sqlite/eligibility.py` (delete `_owners_by_locator` `:56-70`) | Dead after the sweep |
| `src/tychora/resources/migrations/0001.sql`, `src/tychora/adapters/sqlite/migrations.py` | `representation_dependencies`, `ownership_intervals`, checksum |
| `src/tychora/adapters/sqlite/repositories/representations.py` (create) | `RepresentationRepository` |
| `src/tychora/adapters/sqlite/corpus.py` (modify `commit_extraction` `:262`) | Persist dependencies and intervals in the commit transaction |
| `tests/contracts/test_sections.py`, `tests/ingestion/test_boundary_keys.py`, `tests/indexing/test_windows_linear.py`, `tests/indexing/test_interval_sweep.py`, `tests/storage/test_representation_facts.py` (create) | Tests |

---

### Task 1: Section vocabulary and resolver

**Files:**
- Create: `src/tychora/contracts/sections.py`
- Test: `tests/contracts/test_sections.py`

**Interfaces:**
- Consumes: nothing beyond `contracts/base.py`.
- Produces (exactly as the index): `SectionLevel`, `Depth`, `ContextLevel`, `parse_context_level(text) -> ContextLevel`, `SectionEntry(key, level, depth)`, `BoundaryScheme`, `boundary_key(scheme, native_ref) -> str`, `ROOT_SECTION_KEY = "section:#root"`, `section_key(section_path, level) -> str`, `context_key(section_path, context_level) -> str`, `chunk_section_key(section_path, section_depth, context_level) -> str`. A `section_path` always starts with the root entry `SectionEntry(key=<document root key>, level=None, depth=0)`.

Tests to write first:
- `test_parse_context_level_accepts_named_depth_and_document` and rejects `depth:0`, `depth:x`, unknown names (`ValueError`).
- `test_section_key_named_level_returns_innermost_match` (path root → part → chapter → article → clause; `article` returns the article key).
- `test_section_key_depth_returns_ancestor_at_depth`.
- `test_section_key_falls_back_to_depth_one_then_root` (no `annex` in path → depth-1 key; path with only root → root key).
- `test_section_key_document_returns_root_key`.
- `test_chunk_section_key_never_coarser_than_context_key` (seeded random paths, 1,000 cases: the chunk key's entry depth ≥ the context key's entry depth).
- `test_context_key_examples` for the four decision examples: legislation `article`, standard `clause`, book `depth:1`, code `file`.

Acceptance: `uv run --locked pytest tests/contracts/test_sections.py -v` passes. Commit `feat: add section levels and boundary key resolver`.

### Task 2: Native Markdown and text boundaries

**Files:**
- Modify: `src/tychora/ingestion/normalize.py:100` and the function that computes `heading_path` in the same module; bump the native normalizer fingerprint to `native-normalizer-v2` where it is derived (`ingestion/normalize.py:66-74`).
- Test: `tests/ingestion/test_boundary_keys.py`

**Interfaces:**
- Consumes: Task 1 resolver and `boundary_key`.
- Produces: native units with `boundary_key="section:<heading native_ref>"` (or `section:#root`, or `file:<path>` for text with no headings) and `attributes.section_path` as a list of `SectionEntry` dumps.

Tests: `test_markdown_paragraphs_share_enclosing_heading_key`; `test_text_before_first_heading_uses_root` (Review Focus 1); `test_skipped_and_rising_heading_levels` (Review Focus 2); `test_text_without_headings_uses_file_scheme`; `test_native_normalizer_fingerprint_is_v2`; and the contract test `test_boundary_key_is_never_a_paragraph_ref` over every fixture in `tests/fixtures/documents/` that the native normalizer accepts.

Acceptance: new tests pass; `uv run --locked pytest tests/ingestion tests/retrieval/test_segments.py -q` green (neighbour attach and adjacent merge now happen on a sectioned native fixture: add `test_neighbour_context_attaches_within_native_section` to `tests/retrieval/test_segments.py`). Commit `feat: emit section boundary keys for native documents`.

### Task 3: Docling, picture, caption, table and image boundaries

**Files:**
- Modify: `src/tychora/ingestion/documents.py:99,165`, `src/tychora/ingestion/document_views.py:123`, `src/tychora/ingestion/assets.py:196`, `src/tychora/ingestion/vision.py:175`; fingerprint `docling-normalizer-v3` in `ingestion/document_records.py` (`document_normalizer_fingerprint`).
- Test: `tests/ingestion/test_boundary_keys.py` (extend), `tests/ingestion/test_docling_mapping.py` (expected boundary keys updated).

**Interfaces:**
- Consumes: Task 1.
- Produces: Docling units keyed by the nearest enclosing `SectionHeaderItem` (using the normalizer's `heading_level`), `table:<table native_ref>` for tables, rows and cells, the enclosing section for pictures and captions, `figure:<asset ref>` for standalone images with no section.

Tests: `test_docling_units_use_enclosing_section_header`; `test_docling_table_rows_and_cells_share_table_key` (Review Focus 3, multi-page table fixture from `tests/fixtures/documents/rich.pdf` mapping); `test_picture_and_caption_use_enclosing_section`; `test_standalone_image_uses_figure_scheme`; Docling variant of the paragraph-ref contract test; `test_neighbour_context_attaches_within_docling_section` in `tests/retrieval/test_segments.py` (keeps the programme's Docling neighbour fix covered).

Acceptance: `uv run --locked pytest tests/ingestion tests/retrieval/test_segments.py -q` green (asset-dependent Docling cases skip without assets). Commit `feat: emit section, table and figure boundary keys for Docling documents`.

### Task 4: Records and code boundaries

**Files:**
- Modify: `src/tychora/ingestion/document_records.py:367` (`article:` scheme; mark `article` and `annex` levels in `section_path`), `src/tychora/adapters/code/normalize.py:283,332,374` (`file:<relative path>`, level `file`), and the code representation checks that compare `unit.boundary_key` with the path: `src/tychora/indexing/representations.py:223,300,918`.
- Test: `tests/ingestion/test_boundary_keys.py`, `tests/code/test_structure.py` (expected keys).

**Interfaces:** Produces `article:<record native_ref>` and `file:<path>` keys; `representations.py` compares against `boundary_key("file", path)`.

Tests: `test_numbered_provisions_are_articles`; `test_rule_records_are_articles`; `test_code_units_use_file_scheme`; heading-pattern hook: `test_kind_heading_pattern_names_level` (a pattern `^Article \d+` passed through the kind definition marks `article`; a pattern change changes the normalizer fingerprint).

Acceptance: `uv run --locked pytest tests/ingestion tests/code tests/indexing -q` green. Commit `feat: emit article and file boundary keys`.

### Task 5: Linear windowing with token offsets (C1)

**Files:**
- Modify: `src/tychora/ports/tokens.py` (add `count_with_offsets`), `src/tychora/application/default_bindings.py` (HF tokenizer counter: `Tokenizer.encode(text, add_special_tokens=False).offsets`), `src/tychora/indexing/windows.py:14-79` (`_split_interval` binary-searches boundary positions in the offset array; `_count` reused), `src/tychora/indexing/groups.py:64-72,92` (reuse counts), every `TokenCounter` implementation in tests (`tests/indexing/test_publication.py` `RecordingCounter`, `tests/api/*` `CharacterCounter`, `tests/performance/engine_probe.py` `Counter`, `src/tychora/api.py:70` `_LazyOutputCounter`, `src/tychora/retrieval/segments.py:31` `OutputTokenCounter`).
- Keep the old implementation only inside the test module as the reference oracle.
- Test: `tests/indexing/test_windows_linear.py`

**Interfaces:**
- Produces: `TokenCounter.count_with_offsets(text) -> tuple[tuple[int, int], ...]` (one `(char_start, char_end)` per token); `window_unit(...)` signature unchanged.

Tests: `test_linear_windows_equal_reference_on_1000_random_texts` (seeded generator of sentences, headings, CJK and combining characters; compares full `SearchWindow` tuples); `test_long_unit_counts_tokens_in_linear_time` (Review Focus 4: a counting spy proves total characters tokenised < 2× unit length for a 200-boundary unit); `test_offsets_cover_text_without_gaps_for_character_counter`.

Acceptance: `uv run --locked pytest tests/indexing -q` green; `python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline` stage `windowing` p90 ≤ 1.0 s per 100-page native document. Commit `perf: count window tokens once per interval`.

### Task 6: Context groups keyed by `context_key`

**Files:**
- Modify: `src/tychora/indexing/windows.py:99` (`group_key = digest("context-group-v1", extraction_id, context_key(section_path, registry.context_level(kind_id)))`), `src/tychora/indexing/groups.py:179-196` (group boundary check uses the context key), kind validation in `src/tychora/configuration/registry.py` (`_validate_definitions`, the kind loop near `:394`: accept an optional `context_level` string validated by `parse_context_level`) and a new accessor on `Registry` (`configuration/registry.py:57`).
- Test: `tests/indexing/test_groups.py` (extend), `tests/configuration/test_registry.py` (extend).

**Interfaces:** Consumes Task 1 `context_key`; produces `Registry.context_level(kind_id: str) -> ContextLevel` (kind TOML key `context_level`, default `depth:1`), the only definition of the field (sub-project 5 sets values per kind; sub-project 2 consumes it). An invalid value is refused at registry load with `INVALID_REQUEST`.

Tests: `test_context_group_spans_top_level_chapter_by_default`; `test_context_level_article_groups_per_article`; `test_context_level_change_takes_effect_on_rebuild_without_renormalizing` (same extraction ID, different group keys).

Acceptance: `uv run --locked pytest tests/indexing tests/configuration -q` green. Commit `feat: key context groups on the kind context level`.

### Task 7: Sort-and-sweep ownership intervals (C2 write side)

**Files:**
- Modify: `src/tychora/indexing/representations.py:661-731` (`_locator_intervals` becomes a sweep over `(artifact, source_ref, start)`), delete `_owners_by_locator` (`src/tychora/adapters/sqlite/eligibility.py:56-70`) and its callers' use (replace by the sweep result).
- Test: `tests/indexing/test_interval_sweep.py`

**Interfaces:** `prepare_extraction(bundle) -> PreparedExtraction` unchanged in signature and output.

Tests: `test_sweep_equals_quadratic_overlap_on_fixtures` (Review Focus 5; the quadratic function is kept in the test as the oracle; run over every committed document and code fixture); `test_sweep_scales_n_log_n` (10,000 synthetic intervals finish under 1 s).

Acceptance: `uv run --locked pytest tests/indexing tests/retrieval -q` green. Commit `perf: compute ownership overlaps with a sort-and-sweep`.

### Task 8: Persisted representation facts

**Files:**
- Modify: `src/tychora/resources/migrations/0001.sql` (tables below), `src/tychora/adapters/sqlite/migrations.py` (`SCHEMA_CHECKSUM`), `src/tychora/adapters/sqlite/corpus.py:262` (`commit_extraction` writes both tables from `PreparedExtraction` in the same transaction).
- Create: `src/tychora/adapters/sqlite/repositories/representations.py`; add `RepresentationRepository`, `UnitRow`, `ChunkRow`, `IntervalRow` to `src/tychora/ports/repositories.py`; add `representations` to `ReadTransaction`.
- Test: `tests/storage/test_representation_facts.py`

```sql
CREATE TABLE representation_dependencies (
  owner_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
  PRIMARY KEY(owner_id, dependency_id)) STRICT, WITHOUT ROWID;
CREATE TABLE ownership_intervals (
  unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  char_start INTEGER NOT NULL CHECK(char_start >= 0),
  char_end INTEGER NOT NULL CHECK(char_end > char_start),
  dependency_digest TEXT NOT NULL,
  PRIMARY KEY(unit_id, char_start)) STRICT;
```

**Interfaces:** Produces `RepresentationRepository` exactly as the index (`units`, `chunks`, `dependencies`, `intervals`, `neighbours`). Chunk reads use an explicit column list (no `PRAGMA table_info`).

Tests: `test_commit_writes_dependencies_for_units_chunks_assets_and_groups` (equal to what `read_eligibility_facts` derives today for the same bundle, `adapters/sqlite/eligibility.py:377-478`); `test_commit_writes_intervals_matching_prepared_extraction`; `test_recommit_is_idempotent`; `test_neighbours_stay_within_boundary_key` (uses index `units_extraction_boundary`); `test_repository_reads_bind_id_tuples` (1,000 IDs in one call).

Acceptance: `uv run --locked pytest tests/storage tests/indexing tests/architecture -q` green, port-neutrality test green. Commit `feat: persist representation dependencies and ownership intervals at commit`.

### Task 9: Fixtures, gate and ratchet

**Files:** regenerate committed fixtures whose extraction IDs changed (`tests/fixtures/results/*.json`, `tests/fixtures/evaluation/*.json` via the scripts that produce them; list each in the expanded plan), `bench-results/` entry for this phase.

Steps: run `uv run --locked pytest tests -q -m 'not live and not qualification and not release'`; run the phase-3 gate (`python -m tychora.interfaces.bench run --corpus bench-small --suite ingest --profile offline`); run `uv run --locked python -m tools.health check`, then `update` for improved metrics (`_owners_by_locator` removed; `_locator_intervals` shorter); commit `chore: record phase 3 benchmark and health baseline`.

## Self-review (outline level)

- Spec coverage: boundary-key table rows (`file:`, `section:` native, `section:` Docling, picture and caption, `table:`, `article:`, `figure:`) → Tasks 2–4; `section_path` → Tasks 2–4; section levels and `context_level` → Tasks 1, 4, 6; `context_key`, `chunk_section_key` → Task 1 (consumed by sub-project 2); fingerprint bumps → Tasks 2, 3; paragraph-ref contract test → Tasks 2, 3; linear windowing and property test → Task 5; sort-and-sweep, `_owners_by_locator` deletion → Task 7; `representation_dependencies`, `ownership_intervals` → Task 8. The read-side removal of `prepare_extraction` calls is phase 4.
- Names match the index's "Produced by phase 3" block.
