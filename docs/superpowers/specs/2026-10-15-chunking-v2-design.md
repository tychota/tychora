# Chunking v2

Date: 2026-10-15. Status: design, awaiting review. Sub-project 2 of the Tychora v2 programme (`2026-10-15-tychora-v2-program-design.md`, "the programme"). Depends on sub-project 1 (engine core v2) for boundary keys, the new storage format and linear token counting; feeds sub-project 3 (rerank limits), 4 (sections from Docling headings) and 5 (per-preset sizes and identifier patterns).

## 1. Intent and success criteria

Tychora indexes fixed 512/800-token windows cut inside single evidence units, and every document paragraph is its own context group (flow review D5). Contextual embedding therefore sees one window at a time, neighbour context and adjacent merging never fire, and large books are cut into thousands of small, context-free pieces. Chunking v2 replaces this with the product owner's decision (programme §2): structure first, then semantic cuts inside a section, soft size about 300–1,500 tokens, hard maximum 2,000, a context unit as the `voyage-context-4` context group (by default the top-level chapter, with each kind declaring its own context level), sizes tunable per preset and validated on an evaluation set.

Voyage's published evidence supports keeping this range and tuning it rather than growing chunks. Its contextual-model study (voyage-context-3 announcement, 2025-07-23) reports that contextual embeddings are "less sensitive to variations in chunk size and deliver stronger performance with smaller chunks": retrieval quality varied by 2.06% across chunk sizes against 4.34% for the non-contextual `voyage-3-large`, and the contextual model led by 6.63% at 64-token chunks. Context comes from the group, not from chunk length, so small chunks are safe and bigger chunks are allowed but not shown to help. Voyage publishes no recommended range for `voyage-context-4`. The owner's 300–1,500 soft / 2,000 hard range is kept and tuned with the evaluation set (§6.2), which includes a smaller-chunk variant.

Success criteria (each tied to a programme criterion):

| # | Criterion | Programme |
| --- | --- | --- |
| S1 | Every indexable source codepoint is owned by exactly one chunk; every chunk is an ordered list of exact unit character ranges; no chunk crosses a chunking-section boundary; no chunk exceeds 2,000 tokens. Checked by a property test and by a publication-time invariant. | Identity principles |
| S2 | Chunk planning (tokenization, similarity pass, cut selection, group building) is linear in document length and takes ≤ 1 s p90 for a native PDF up to about 100 pages and ≤ 4 s for a 287-page book on the benchmark machine. | §5.1 (ingestion p90 < 30 s) |
| S3 | Rebuilding a workspace from retained originals reproduces identical chunk identifiers. Re-planning the pinned evaluation corpus on CI (Linux) and on macOS yields the same plans. | Identity, `manage rebuild` |
| S4 | On the test split of the evaluation set (§6.2), the recommended configuration is non-inferior to today's 512/800 baseline at every stage (lexical, hybrid, hybrid + rerank): the lower bound of the paired-bootstrap 95% interval of the recall@10 and nDCG@10 differences is ≥ −0.01. On the long-book subset, hybrid + rerank recall@10 improves by ≥ 0.05. | §5.3 |
| S5 | Citation precision (§6.2) of the recommended configuration is ≥ 0.85 × the baseline's. Bigger chunks must not turn citations into whole chapters. | §5.3, §1 ("exact place") |
| S6 | Code is chunked by symbol for Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Elixir, Bash, SQL, Lua, Scala, Dart, Haskell and Zig, and by structure for YAML, TOML, JSON and Markdown, from one declarative pack per language, with a line-based fallback for every other text file. Adding a language is one pack file plus its tests, with no code change. | Programme §1 (code), parity requirement |
| S7 | Neighbour context and adjacent-result merging apply to Docling documents. The regression tests added by the separate Docling fix (§3) pass unchanged on chunking v2. | Flow review D5 |

## 2. Scope and non-goals

In scope: the chunk planner (blocks, semantic boundary detection, cut selection), chunk plan identity and storage shape, context group construction and request packing, code chunking with declarative language packs, pattern-based identifier extraction at chunking time, the downstream changes that chunk owners require in reranking, segment assembly and lexical records, and the chunking evaluation set and harness.

Non-goals:

- Defining `boundary_key`, `section_path` or the storage format. Sub-project 1 owns those fields; this design states what it consumes (§4.1).
- Heading detection quality. Sub-project 4 turns on Docling heading hierarchy; this design benefits from it but does not depend on it for correctness.
- Model choice for text, code and rerank (sub-project 3). This design states the limits it needs.
- The identifier patterns themselves (sub-project 5 owns them per preset). This design owns running them.
- LLM-written section summaries in the capsule. They are non-deterministic, cost money per document and would make identity depend on a generative model; not in v2.
- Voyage auto-chunking (rejected by the product owner: no character offsets).
- Overlapping chunks. Lossless, disjoint ownership is kept; context comes from the context group and from lead ranges (§4.4).

## 3. Current state

**Windows.** `indexing/windows.py:10-11` fixes `TARGET_TOKENS = 512` and `SOFT_CEILING_TOKENS = 800`. `window_unit` (`windows.py:82-128`) cuts each owned interval of one unit independently; a window never spans two units, and `chunk_id(unit_id, start, end, fingerprint)` (`contracts/identity.py:86-89`) bakes that in. The storage shape matches: `chunks(unit_id, ordinal_in_unit, char_start, char_end, …)` (`resources/migrations/0001.sql:182-194`). `_split_interval` (`windows.py:52-79`) counts `text[cursor:boundary]` for every sentence boundary: quadratic in unit length (C1).

**Boundaries.** Native, Docling and record normalizers set `boundary_key = native_ref` (`ingestion/normalize.py:100`, `ingestion/documents.py:98`, `ingestion/document_records.py:366`); only code uses the file path (`adapters/code/normalize.py:283,331,372`). `windows.py:99` derives `context_group_key` from it, so each paragraph is its own context group (D5).

**Groups.** `indexing/groups.py:39-50` builds a capsule (title, kind, heading/symbol paths) sent as input 0 of each group; `adapters/voyage/text.py:441` discards its vector (`stored[1:]`). `_split_joint` (`groups.py:126-162`) recomputes the capsule for every candidate prefix, and `_validate_windows` (`groups.py:64-72`) re-tokenizes every window (C1). Limits: `max_group_tokens ≤ 24,000`, `max_request_tokens ≤ 28,000` (`configuration/models.py:85-86`; qualified in `adapters/voyage/validation.py:89-90`). Voyage documents, verified 2026-10-15 at docs.voyageai.com/docs/contextualized-chunk-embeddings: without auto-chunking, at most 1,000 inputs, 32K total tokens and 16K chunks per request, order-preserving. The current caps sit inside these limits and are kept.

**Retrieval.** Candidates are typed owners `("unit", unit_id)` or `("asset", asset_id)`; `retrieval/rerank.py:394-408` and `application/reranking.py:428-432` reject a match whose window belongs to another unit. Segment assembly cuts excerpts at `min(1200, …)` (`retrieval/segments.py:481,507,854`). `_neighbor_context` (`segments.py:381-431`) and `_coalesce_boundary_matches` (`segments.py:609-700`) key on `boundary_key` and ordinals and additionally skip any unit with a `DocumentLocator` or `CodeLocator` (`segments.py:392,640,659`), so neither fires for Docling documents at all, independent of D5. This exclusion is a bug and is fixed separately, before chunking v2 starts: test-first, the Docling locator stops disqualifying a unit from neighbour context and adjacent merging, and regression tests pin that Docling results get neighbours and merge. Chunking v2 replaces both helpers (§4.11) and must keep that behaviour (S7). The rerank representation cap is 1,200 tokens (`adapters/voyage/validation.py:31`).

**Code.** Python and TypeScript/JavaScript have hand-written tree-sitter adapters (`adapters/code/python.py`, 801 lines; `adapters/code/typescript.py`, 947 lines). The normalizer emits nested `symbol` units and `code` units for uncovered spans (`adapters/code/normalize.py:310-385`), and windows are cut per owned interval like documents.

**Identifiers.** Rows are emitted only by normalizers for `numbered_provisions@1` and rule records (`ingestion/normalize.py:145-156`, `ingestion/document_records.py:394-404`) and for code symbols (`adapters/code/normalize.py:506-523`), at unit granularity. References inside running text ("see Article 12(3)") are not recognized.

**Evaluation.** Gold spans are `(unit_id, char range)` (`evaluation/dataset.py:23-25`) and the runner never rebuilds indexes (`docs/evaluation.md`, "Runs and ablations"), so chunking variants cannot be compared today without building separate workspaces. The executable gold set is five synthetic cases.

## 4. Design

### 4.1 Inputs consumed from engine core v2

Per extraction, the chunker receives the prepared units in ordinal order. Each unit has: `unit_id`, `unit_type`, `text`, ownership intervals (exact, partitioning the text, as today in `contracts/indexing.py:37-62`), `heading_path` (labels), `text_origin`, `citable`, and two fields that sub-project 1 owns:

- `section_path`: the ordered ancestors from the root to the innermost section, each entry `{key, level, depth}` (engine core §4.10); empty for preamble text.
- `boundary_key: str`: the innermost enclosing section, article, table or file key, in engine core's `"<scheme>:<native_ref>"` format.

The chunking section is engine core's derived key `chunk_section_key = section_key(section_path, depth:section_depth)` (the file for code, the record for record kinds), where `section_depth` is a per-preset parameter defined in §4.7; the stored `section_key` column of a chunk holds it. Using one resolver gives a single notion of "section" for chunking and segment adjacency. Headings deeper than `section_depth` stay inside the section and become strong cut preferences. Tables, captions and pictures carry their enclosing section's key; a table is never a boundary of its own in document kinds.

The chunker also receives the document's resolved kind and that kind's `context_level` (§4.10), which decides the context group (engine core's `context_key`, stored as the chunk's `context_group_key`). The context unit is never smaller than the chunking section: engine core's resolver narrows the chunking section when `section_depth` would cross a context unit.

### 4.2 Components

```
indexing/chunking/
  blocks.py      units → ordered blocks with token counts and atomicity
  similarity.py  pinned static embedding model → quantized gap scores
  planner.py     per-section cut selection (dynamic programming)
  code.py        code blocks from symbol units and language packs
  plan.py        ChunkPlan, Chunk, ChunkRange contracts, rendering, fingerprints
  identifiers.py pattern-based identifier extraction over units
indexing/groups.py  context groups and request packing (rewritten)
```

`indexing/windows.py` is deleted. Each module has one responsibility, takes immutable inputs and returns immutable outputs. None touches storage or providers.

### 4.3 Blocks

A block is the smallest thing the planner moves: `Block(unit_id, char_start, char_end, unit_type, tokens, atomic_run, attach_next, section_key, subsection_depth)`.

1. Every owned interval of every indexable unit becomes a block. Indexable means `citable` and `text_origin == "source"` and `unit_type` not in `{page_header, page_footer}`. Running heads and footers are listed in the plan as `excluded_units` with the reason `furniture`; repeating them in every chunk of a book would pollute lexical and vector representations.
2. Token counts come from one tokenizer call per block using the offset-returning API of the Hugging Face fast tokenizer already shipped for `voyage-context-4` (`resources/model-assets.json`, revision `8ca94607…`). The `TokenCounter` port (`ports/tokens.py:6-10`) gains engine core's `count_with_offsets(text)` (engine core §4.10, linear windowing), giving the character end offset of each token. Total work is one pass over the text (closes C1).
3. The chunking tokenizer is always `voyage-context-4`, regardless of the configured text provider, so offline and online workspaces get the same plan and enabling embeddings later does not re-chunk. Provider adapters still count their own request tokens at the trust boundary (`adapters/voyage/text.py:338-349`); the 2,000-token chunk cap is far from every provider's per-input limit (32K).
4. Atomic runs: a table; a code block; a formula; a contiguous run of `list_item` units sharing a parent; a caption together with the picture, picture proxy or table it captions (from `unit_assets` links with role `caption`). Cuts inside an atomic run are forbidden while the run is ≤ the hard maximum.
5. A heading block sets `attach_next`: the gap after a heading is never a cut, so headings start the chunk they introduce.
6. Oversized blocks (> hard maximum) are split before planning: tables at row boundaries (line ends inside the table text), other text at sentence boundaries, else at token boundaries, using the token offsets from step 2. Each piece except the first of a split table gets the table's header line as a lead range (§4.4).

### 4.4 Chunk plan contract

```
ChunkRange(unit_id, char_start, char_end, role: "owned" | "lead")
Chunk(chunk_id, section_key, ordinal_in_section, kind, ranges, token_count, cut_reason)
ChunkPlan(extraction_id, plan_fingerprint, plan_id, chunks, excluded_units, gaps)
```

- **Rendering.** A chunk's text is its ranges' exact substrings joined with `"\n\n"`, lead ranges first. This rendering (`chunk-render-v1`) is the text embedded, indexed lexically and counted. `token_count` is counted once on the rendered text after planning; if it exceeds the hard maximum (possible by a few tokens from merges at joins), the last owned block moves to the next chunk. This repair is deterministic and bounded.
- **Lead ranges** are non-owned context: at most 128 tokens per chunk, used only for a split table's header line and for the enclosing declaration line of a method chunk (§4.8). They are rendered and cited as context, never counted as ownership.
- **Lossless coverage.** Owned ranges of all chunks plus `excluded_units` partition every indexable unit's text exactly (S1); publication rejects a plan that does not.
- **`kind`** is `prose`, `table`, `list`, `code_block`, `figure` (caption plus visual), or for code `function`, `class`, `method`, `module`, `global`, `import`, `section` (a Markdown heading section or a structured-config table or top-level key), `fallback`.
- **`cut_reason`** for the gap that starts the chunk: `section_start`, `subheading`, `semantic`, `size`, `oversize_split`.
- **`gaps`** records every candidate gap with its quantized similarity and depth (§4.5). These records are for inspection and audit. Identity is defined by the chunks themselves.

Identity:

```
plan_fingerprint = digest("chunk-plan-v2", chunker_version, tokenizer_id,
                          boundary_model_id, boundary_model_sha256,
                          canonical(parameters), identifier_pattern_digest,
                          "chunk-render-v1")
chunk_id = digest("chunk-v2", extraction_id, plan_fingerprint,
                  [(r.unit_id, r.char_start, r.char_end, r.role) for r in ranges])
plan_id  = digest("chunk-plan-id-v2", extraction_id, plan_fingerprint,
                  [c.chunk_id for c in chunks])
```

The cut decisions are the ranges, so they are part of chunk identity, and `plan_id` binds the whole plan. The plan is stored as one content-addressed artifact per extraction and fingerprint. `manage rebuild` and re-projection reuse the stored plan whenever `extraction_id` and `plan_fingerprint` match, so identifiers survive hardware changes even if a similarity value would round differently elsewhere (S3).

Storage shape, which sub-project 1 writes in its schema: `chunks(chunk_id, extraction_id, plan_id, section_key, ordinal_in_section, kind, token_count, context_group_key)` and `chunk_ranges(chunk_id, ordinal, unit_id, char_start, char_end, role)`, with an index on `(unit_id, char_start)` so a unit range maps to its owning chunk.

### 4.5 Semantic boundary detection

**Model choice: a pinned local static embedding model, `minishlab/potion-multilingual-128M` (MIT, 256 dimensions, 101 languages, distilled from BGE-M3), loaded through `model2vec` at a pinned version and listed in `resources/model-assets.json` with revision and SHA-256 under a new `chunking` selection.**

| Option | Offline | Deterministic | Cost | Latency for 100 pages (~50K tokens) | Verdict |
| --- | --- | --- | --- | --- | --- |
| Voyage `voyage-4-lite` or `voyage-context-4` in non-contextual mode | No; needs upload consent | Remote, revision not pinnable | ~$0.001 per document | 1–3 s of network round trips | Rejected: chunking would depend on consent and connectivity, and the base profile is provider-free |
| `voyage-4-nano` open weights (Apache-2.0, about 0.34B parameters, 32K context, verified on Hugging Face; community ONNX and int8 LiteRT builds exist) | Yes | Yes, if pinned | Free | No published CPU timings. Estimated 20–100 ms per 128-token paragraph on a laptop CPU with ONNX int8, so roughly 8–40 s for 400 paragraphs, and attention cost grows quadratically with window length | **Evaluated alternative** (variant V7, §6.2); not the default because it alone would consume most of the 30 s ingestion budget |
| Static model (`potion-multilingual-128M`) | Yes | Yes: token lookup plus mean pooling, no attention | Free | Tens of milliseconds | **Chosen (default)** |

The deciding factor is speed. The detector scores every adjacent gap of every section, and a static model is a lookup and a mean, about 1,000 times faster than `voyage-4-nano` per paragraph (microseconds against tens of milliseconds). A static model is weaker than a transformer at absolute similarity. Here it only has to detect *relative drops* between adjacent windows of the same section, and structure does most of the work. Multilingual coverage matters because the presets include French regulatory and knowledge material. The evaluation compares semantic cuts against size-only cuts (variant V4 in §6.2) to prove the model earns its place, and runs `voyage-4-nano` as the boundary model (variant V7). `voyage-4-nano` replaces potion only if V7 improves WindowDiff on the boundary subset and hybrid + rerank nDCG@10 on dev while keeping S2. The `boundary_model_id` and its digest are already in `plan_fingerprint`, so the switch is a re-projection with new chunk identities.

**Gap scores.** Within a section, token vectors of the boundary model are accumulated as a prefix sum (float64). For each candidate gap *i*, the left window is the last `W` boundary-model tokens before the gap and the right window the first `W` after it, clipped to the section (default `W = 256`). Each window vector is computed in O(1) from the prefix sums, and *s_i* = cosine(left, right) is rounded half-to-even to an integer in thousandths. Depth is *d_i* = max(0, μ_i − s_i), where μ_i is the mean of *s* over up to two gaps on each side within the section (integer division). A section with one gap gets *d* = 0. A gap directly before a heading deeper than `section_depth` gets *d* = 1000 (structural preference). The whole pass is O(section tokens), and integer quantization makes the cut-selection step platform-independent.

No paragraph-embedding cache is built: recomputing costs less than reading a cache, and the stored plan already makes rebuilds free. A cache keyed by `(boundary_model_sha, block text SHA-256)` is added only if profiling on the benchmark shows the similarity pass above 5% of ingestion time.

### 4.6 Cut selection

For each section, blocks *b₁…bₙ* with tokens *tⱼ*. Allowed gaps exclude gaps inside atomic runs and after `attach_next` blocks. A chunk of *T* tokens costs (all integer arithmetic):

- `∞` if *T* > `hard_max`;
- `P_small·(soft_min − T) // soft_min` if *T* < `soft_min`, unless the chunk is the whole section;
- `P_large·(T − soft_max) // (hard_max − soft_max)` if *T* > `soft_max`;
- `P_target·|T − target| // (soft_max − soft_min)` otherwise.

Each internal cut costs `C_cut − d_i`. Dynamic programming over gaps finds the minimum total cost. Lookback is bounded by `hard_max` tokens, so the work is O(n·m) with m ≤ 2,000 / (minimum block tokens), capped at 512 blocks. Ties go first to fewer chunks, then to the lexicographically earliest cut positions. Default weights: `P_small = P_large = 1000`, `P_target = 50`, and `C_cut = τ` in thousandths. A semantic cut is therefore worth taking when its depth exceeds τ; otherwise only size forces cuts. Small sections stay whole: chunks never cross a section to reach `soft_min`.

### 4.7 Parameters per preset

Parameters live in the preset (sub-project 5 owns the preset files; this table is the initial content, to be confirmed by §6):

| Preset (prose documents) | section_depth | soft_min | target | soft_max | hard_max | τ | W |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `general` (default) | 2 | 300 | 900 | 1,500 | 2,000 | 80 | 256 |
| `software` | 2 | 300 | 800 | 1,500 | 2,000 | 80 | 256 |
| `regulatory-normative` | clause level (records) or 3 | 200 | 600 | 1,200 | 2,000 | 80 | 192 |
| `regulatory-administrative` | article level (records) or 3 | 200 | 600 | 1,200 | 2,000 | 80 | 192 |
| `knowledge` | 2 | 400 | 1,000 | 1,500 | 2,000 | 80 | 256 |

Two content types use the same parameters in every preset:

| Content | section_depth | soft_min | target | soft_max | hard_max | τ | W |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Code files | file | 100 | 400 | 1,000 | 2,000 | off | — |
| `visual` kind | n/a: each proxy or description unit is one atomic chunk | | | | 2,000 | off | — |

There is no images preset: `visual` is a kind in every preset. The example domain pack `quality-improvement` under `examples/` inherits the `general` values.

`hard_max` is capped at 2,000 by validation (owner decision) and `soft_min ≤ target ≤ soft_max < hard_max` is enforced at load. Parameters enter `plan_fingerprint`, so changing them is a re-projection with new chunk identities and never reinterprets old ones.

### 4.8 Code chunking and language packs

**Language packs.** A declarative pack per language (`profiles/code/languages/<language>.toml`, validated by a schema and digested into the code adapter fingerprint) states: grammar (from `tree-sitter-language-pack`, one pinned version), file suffixes, definition node types mapped to chunk kinds (`function`, `class`, `method`, `module`, `global`, `import`), container node types (which make children `method`s), the name field or node, import node types, and call node types for the graph projection. Elixir-style call definitions (`def`, `defmodule`) are expressed as `call_definitions = ["def", "defp", "defmodule", …]`. Packs cover Python, JavaScript, TypeScript/TSX, Go, Rust, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Elixir, Bash, SQL, Lua, Scala, Dart, Haskell and Zig (S6). SQL packs map statements (`CREATE TABLE`, `CREATE FUNCTION`, `CREATE VIEW`) to definitions and leave call node types empty where the grammar has none.

Structured-config and documentation packs use the same schema with `section` as the definition kind: YAML top-level keys, TOML tables, JSON top-level members and Markdown heading sections each become a `section` symbol, nested by depth. These packs declare no import or call node types. Markdown files reached through a code source use the pack; Markdown ingested as a document keeps the document path.

Adding a language is one pack file plus a fixture and a golden test; the schema and the generic extractor do not change. A pack is accepted only if its grammar is present in the pinned `tree-sitter-language-pack` version.

One generic extractor (`adapters/code/extract.py`) turns a pack plus a parse tree into definitions with exact byte and codepoint ranges, reusing `adapters/code/syntax.py` source maps. Python and TypeScript definition extraction moves onto the generic extractor; their adapters keep only import and call resolution (`adapters/code/resolve.py` and the language-specific resolution parts). The migration is gated by golden tests: for every existing code fixture, symbol tables (qualified names, kinds, ranges) must be identical before and after.

**Unknown languages.** A file with no pack becomes one `code` unit covering the file. Its blocks are maximal runs of lines separated by blank lines (split further at line ends when above `hard_max`), and its chunks have kind `fallback`. The planner is the same, with semantic scoring off.

**Code planning.** The section is the file. Blocks are the owned intervals of symbol and `code` units. A symbol's owned intervals form one atomic run. Uncovered top-level spans are classified by the pack as `import` (import nodes), `global` (top-level assignments and constants) or `module` (everything else, including the module docstring). Planning packs only adjacent siblings with the same parent and the same kind (for example, a run of small getters or the import block), so a chunk has one kind. A method chunk gets the first line of its enclosing class declaration as a lead range. Code groups follow today's rule (`groups.py:53-61`): `independent` with a code model, `joint` with `voyage-context-4`.

### 4.9 Identifier extraction at chunking time

Sub-project 5 defines identifier patterns per preset (for example: article references, case numbers, paragraph references), each with a namespace, a regular expression, a normalization template and a role (`defines` when matched in a heading or record identifier, `mentions` elsewhere). The chunking side:

- `identifiers.py` compiles the preset's patterns once and applies them to each indexable unit's text in the same pass as block construction. Matches are bounded at 10,000 per extraction (`LIMIT_EXCEEDED` diagnostic, not a failure), and a pattern is rejected at preset load if it contains backreferences or nested unbounded quantifiers.
- Matches become rows in the identifiers table: `(namespace, normalized_value, original_value, unit_id, char_start, char_end, role, origin = "pattern", plan_fingerprint)`. Normalizer-emitted identifiers keep `origin = "normalizer"`. The pattern-set digest is part of `plan_fingerprint`.
- The exact-identifier channel maps a row to the chunk owning `(unit_id, char_start)`. The lexical record's `identifiers` column holds the normalized values of the chunk's identifiers.

### 4.10 Context groups and request packing

- **Group = context unit, declared per kind.** Each kind in a preset (sub-project 5) declares `context_level` from engine core's single definition (engine core §4.10), one of:
  - `depth:N`: the enclosing heading at depth *N*. The default for every kind that declares nothing is `depth:1`, the top-level chapter.
  - a named section level (`part`, `chapter`, `section`, `article`, `annex`, `clause`, `file`), assigned by the normalizer from records, code files or the kind's heading patterns; for example `article` for legislation (annexes resolve to their own `annex` section) and `clause` for standards.
  - `document`: the whole document.

  Examples of declarations: legislation `article`, standards `clause`, books `depth:1` (chapter), code `file`. The chunker reads the level from the resolved kind passed with the extraction (§4.1) and takes the group key from engine core's resolver, `context_key = section_key(section_path, context_level)`, which falls back to the depth-1 ancestor and then to the document root when no ancestor matches. If the declared level is finer than `section_depth`, the resolver narrows the chunking section to the context unit, so a context group always contains whole chunking sections. `context_level` enters `plan_fingerprint` through the parameters.
- **Oversized units.** If the group's capsule plus chunks exceed `max_group_tokens` (24,000), it descends to child sections and packs consecutive children greedily up to the cap. A leaf section still over the cap is cut into consecutive slices at chunk boundaries. All slices of one context unit share the same capsule.
- **Capsule** = canonical JSON of `{title, kind, heading_path of the group's section}` (code: `{title, kind, path, symbol_path}`), at most 256 tokens; deeper heading labels are dropped from the end when over. It is computed once per group, not per candidate prefix (closes the C1 part in `groups.py:137`).
- **Derived text** published later by background phases (picture descriptions, formula text; programme §4) is chunked in its own plan increment. Each derived unit is one atomic chunk in its own context group with the section's capsule, so adding enrichment never changes the inputs, and therefore the embeddings, of existing groups.
- **Packing** keeps `groups.py:213-228` semantics: groups are packed in order into requests of ≤ 28,000 tokens, ≤ 1,000 inputs and ≤ 16,000 chunks. Upload authorization stays in batches of ≤ 2,048 dependencies (`docs/superpowers/2026-10-15-large-document-limits.md`). With section groups up to 24K tokens, a 287-page book (about 200K tokens) needs about 9–12 requests instead of thousands of single-paragraph groups.

### 4.11 Downstream changes

**Chunk owners.** Text candidates become `("chunk", chunk_id)`. Lexical and vector hits already land on chunks. Exact-identifier and graph hits on a unit map to the chunk owning that unit's range (first owning chunk for a split unit). Fusion collapses by chunk, which keeps the engine design's rule of not rewarding repetition. `rerank.py:394-408` and `reranking.py:428-432` check that matched ranges belong to the candidate chunk. Asset owners are unchanged.

**Eligibility.** A chunk is a candidate only if every owned range is in an eligible unit. Chunks never cross revisions, so revision-level eligibility (Q1) is unaffected. A rarely excluded unit (rejected review, blocked) makes its chunk ineligible for lexical and vector selection, and the chunk's other units stay reachable through exact and graph channels. This keeps "eligibility before candidate selection" without redaction logic.

**Rerank representation** = kind, title and heading-path header (≤ 400 tokens; deeper heading labels are dropped from the end when over) + `"\n\n"` + rendered chunk. The qualified `max_representation_tokens` is `hard_max + 400 = 2,400` (today 1,200, `validation.py:31`). Sub-project 3 derives it from the registry entry and keeps `max_candidates × (max_query_tokens + 2,400) ≤ 600,000`: with 64 candidates, queries up to 6,975 tokens fit, which is the limit sub-project 3 sets.

**Segments.** A segment is the matched chunk's owned ranges, citations being one fragment citation per range. The hard-coded `1200` (`segments.py:481,507,854`) becomes `retrieval.segment_max_tokens` (default 2,000, validated ≥ every preset's `hard_max`). When the remaining output budget is smaller than the chunk, the segment is trimmed to whole owned ranges around the best anchor and marked truncated. `_neighbor_context` and `_coalesce_boundary_matches` are replaced by one chunk-adjacency rule that applies equally to native, Docling and code: two selected chunks with consecutive `ordinal_in_section` in the same section merge into one segment if the result fits `segment_max_tokens`. This rule has no locator-type exclusion, so it keeps the earlier Docling fix (§3), whose regression tests are carried over unchanged (S7). Code context (`_code_context`) and table continuations stay.

**Lexical records.** One record per chunk: `body` = rendered owned text, `headings` = heading path, `title`, `context` = lead ranges, `identifiers` as in §4.9. BM25 length normalization over bigger documents is measured, not tuned blindly (§6).

**Aggregates.** Per-document chunk count, token histogram (engine core's bins `[0,64,128,256,512,1024,1536,2048,∞)`, engine core §4.10) and cut-reason counts feed the programme's aggregates contract.

### 4.12 Errors, configuration, security

- A missing `chunking` asset (boundary model or tokenizer) fails ingestion with `CAPABILITY_MISSING` and names `manage setup`. There is no fallback planner: one code path.
- An oversized atomic unit that cannot be split (a single codepoint run over 2,000 tokens) fails as `INVALID_REQUEST` with the unit and offset, as today (`windows.py:45-48`).
- The planner is pure and runs off the owner thread (D4 is sub-project 1's, but the planner needs no database handle).
- No new upload: the boundary model is local. Capsules and chunks go to Voyage only through the existing consent-checked path. Identifier patterns are data from signed-off preset files and are bounded as described.

## 5. Migration and compatibility

Programme rule: new format, rebuild, no dual paths. `windows.py`, the `chunk-v1` identity, `chunks.ordinal_in_unit` and the unit-owner text candidate path are removed in the same change set that introduces chunk plans. Existing workspaces are rebuilt with `manage rebuild` from retained originals, which yields new chunk, embedding-record and lexical identities; unit, revision and source identities are unchanged unless sub-project 1 changes normalizer fingerprints. Before the switch, the baseline (variant V0) is measured once on the evaluation set at the last pre-v2 commit and its report is committed under `benchmarks/chunking/` with that commit hash, so later comparisons need no old code.

## 6. Testing and verification

### 6.1 Tests

- **Unit:** block construction (atomic runs, attach-next, furniture exclusion, oversize splitting with token offsets); quantized similarity and depth on fixed vectors; DP optimality against brute force on random sections of ≤ 12 blocks; tie-breaking; rendering and repair; fingerprint sensitivity to every parameter; identifier patterns (defines vs mentions, bounds, rejection of unsafe patterns); language-pack schema validation.
- **Property (Hypothesis):** for generated unit sequences, S1 holds: exact partition, no section crossing, every chunk ≤ `hard_max`, lead ranges ≤ 128 tokens.
- **Golden:** pinned plans for fixture documents (native Markdown, Docling JSON fixtures, one file per language pack and one unknown suffix) compared byte for byte on Linux and macOS CI (S3). Python and TypeScript symbol tables equal pre-migration output.
- **Integration:** ingest → publish → search on fixtures with joint groups (recorded Voyage responses) verifying one group per declared context unit (chapter, article, clause, file), chunk owners, neighbour context and segment merging for Docling documents (the carried-over regression tests, S7), exact-identifier hits on mentions.
- **Performance:** planning time and tokenizer call count on synthetic documents of 10K, 100K and 1M tokens; the call count must be `blocks + chunks + groups`, and time must grow ≤ 1.2× per 1× of text (S2). Benchmark-corpus timings are recorded with the programme harness.

### 6.2 Evaluation set and protocol

Corpus (public or synthetic, pinned by URL and SHA-256 under `benchmarks/chunking/`):

| Subset | Material | Gold queries |
| --- | --- | --- |
| Long book | Charles Darwin, *On the Origin of Species*, 6th ed., Project Gutenberg eBook #2009, converted to Markdown with chapter and section headings, plus its PDF rendering for the Docling path | 80 |
| Law and norms | Regulation (EU) 2016/679 in English and French (EUR-Lex, reuse permitted by Decision 2011/833/EU) | 60 |
| Articles | 10 arXiv papers under CC BY 4.0 (PDF) | 40 |
| Code | This repository at a pinned commit, plus one small public-domain or MIT sample repository per pack language | 40 |
| Boundary | 200 synthetic documents concatenating paragraphs from distinct sources with known topic boundaries | Pk and WindowDiff only |

Gold labels are spans in original-text coordinates `(source_key, char_start, char_end)`. A resolver step at registration maps them to `GoldSpan(unit_id, …)` against the pinned snapshot, which makes labels independent of chunking. This extends `evaluation/dataset.py` with a `GoldSourceSpan` that is resolved and retained alongside the unit spans. Queries are drafted by a person or with model assistance and every case is reviewed by a person; `label_provenance` records which. The split is dev/test by document family.

Metrics, at stages lexical, hybrid, and hybrid + rerank:

- Recall@5 and @10. A gold span counts as hit when returned citations in the top k cover ≥ 50% of its characters.
- nDCG@10 (graded) and MRR.
- Citation precision: cited characters in the top 10 that fall inside any gold span, divided by all cited characters.
- Output tokens per query.
- Pk and WindowDiff on the boundary subset.

Variants: V0 recorded baseline (512/800, per-paragraph groups); V1 new planner with semantic cuts off and sizes 0/512/512/800 (isolates the effect of sections and groups); V2 recommended (§4.7); V3 larger (soft 800–2,000, target 1,400); V4 V2 sizes with semantic cuts off; V5 V2 with the group widened to the whole document when it fits 24K; V6 smaller (soft 150–600, target 400), testing Voyage's finding that contextual models do well with small chunks; V7 V2 with `voyage-4-nano` as the boundary model. The chunking harness builds one workspace per variant (`tychora manage evaluate` runs on each), because the runner does not re-index.

Selection rule per preset: the variant with the best hybrid + rerank nDCG@10 on dev that satisfies S5. Ties go to larger chunks (fewer vectors, lower cost). τ is tuned on the boundary subset first (maximize WindowDiff improvement over size-only), then confirmed on dev. Acceptance is S4 and S5 on test. If V4 is non-inferior to V2 on test, semantic cuts are kept only if they improve the long-book subset, and the result is reported either way.

## 7. Code-health ratchet

- Delete `indexing/windows.py`, the per-candidate capsule recomputation and window re-tokenization in `groups.py`, the two unit-adjacency helpers in `segments.py` (≈ 150 lines of near-duplicate eligibility and gap checks) and the three `1200` literals.
- Python and TypeScript definition extraction moves to the generic pack-driven extractor; the expected reduction is ≥ 600 lines across `python.py` and `typescript.py`. jscpd duplication must not rise despite the new languages, because languages are data.
- Every new function ≤ 50 lines; modules in §4.2 import only `contracts`, `ports` and each other (an import-linter contract is added for `indexing.chunking`).
- Docstrings state invariants (partition, quantization, tie-break), not history. `docs/architecture.md` (chunk definition, line 58) and `HANDOFF.md` (window constants section) are updated in the same change.
- Mutation testing on `planner.py` and `blocks.py` must reach the core-module threshold from sub-project 0.

## 8. Risks and open questions

1. **`section_path` and `boundary_key` contract.** Resolved by engine core §4.10: the stored `boundary_key` stays the innermost enclosing key, and the chunking section is the derived `chunk_section_key = section_key(section_path, depth:section_depth)`, which chunking and segment adjacency both use (§4.1).
2. **Boundary model size.** `potion-multilingual-128M` stored as float32 is several hundred megabytes, and it would enter the minimum offline setup. Default: ship it in the `chunking` selection installed by `manage setup`. The evaluation also runs V2 with a smaller multilingual potion model, which replaces it if non-inferior.
3. **Grammar drift for Python/TypeScript** when moving to `tree-sitter-language-pack`. Default: the golden symbol-table gate blocks the migration of a language whose tables differ, and that language keeps its pinned standalone grammar wheel until fixed.
4. **Context level per kind.** The owner decided the context unit is the top-level chapter by default, with each kind declaring its own level (§4.10). Open: whether a kind's declared level should be re-tuned from evaluation. Default: keep the declared levels; V5 (whole document when it fits) is reported for the owner's information.
5. **Chunk-level eligibility exclusion** hides eligible sibling text from lexical and vector channels when one unit of a chunk is excluded. Default: accept, and count occurrences in the query trace; revisit if they are frequent in practice.
6. **Rerank aggregate limit** at large query lengths (§4.11) belongs to sub-project 3. Default: sub-project 3 derives `max_query_tokens` = 6,975 from the 2,400-token representation cap; until its registry entry is qualified, the current 2,000 stays.

## 9. Flow-review findings

Closed: **D5** (consumed here: groups operate on declared context units and segment adjacency and merging on sections; the Docling exclusion at `segments.py:392,640,659` is removed by the separate earlier fix, and this design keeps it removed); **C1** for windowing and grouping (offset-based single-pass counting, capsule once per group, no window re-count).

Partly addressed: **D6**. Section groups cut the number of requests, but one-file-per-vector storage is sub-project 1's. **Q5**. Chunk owners make rerank facts range-based; removing whole extractions from the cache is sub-project 1's.

Deferred to sub-project 1: D1–D4, D7, D8, C2–C5 and Q1–Q4, Q6. They concern storage, publication, eligibility and query orchestration, not chunk formation.
