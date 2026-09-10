# Tychora — extraction, indexing and retrieval algorithms

Date: 2026-09-10
Status: engineering review draft
Parent: [Engineering design](2026-09-10-tychora-engineering-design.md)
Code specialization: [Code retrieval profile](2026-09-10-tychora-code-retrieval-design.md)

All numeric defaults below are initial evaluation settings, not claims of optimality. Store their complete configuration digest with runs/builds. Search returns evidence, never an LLM-written answer or review.

## 1. Source acquisition and deterministic normalization

### Native documents

Markdown is parsed into block tokens with `markdown-it-py`; preserve headings, paragraphs, fenced code, lists, tables and source line ranges. Do not turn a heading inside a code fence into document structure. Identified rules and template slots remain one semantic evidence unit even when their indexing windows are split. Plain text preserves paragraph breaks and line references. Contextual titles/labels enrich indexes but never replace original quote text.

Docling receives immutable registered PDF/DOCX/image originals and an explicit conversion configuration. Retain its complete structured JSON and available page/figure/table images. Map `self_ref`, hierarchy, reading order and provenance into normalized units. A source locator records page number, page dimensions, bounding-box values and coordinate origin. Store Docling `charspan` with its coordinate domain; it is not automatically a raw PDF byte offset. DOCX pagination remains unknown unless a separate layout-preserving renderer supplies it. [Docling model](https://docling-project.github.io/docling/concepts/docling_document/), [figure export](https://docling-project.github.io/docling/_generated/examples/export_figures/).

The normalizer emits `ExtractionArtifact {revision_id, parser_fingerprint, units, assets, assertions, diagnostics}`. Database insertion happens later in the owner process. Reject duplicate native references/IDs, invalid geometry, impossible ranges and missing source dependencies before committing normalized records. An optional unsupported feature yields an explicit diagnostic; it must not silently degrade a table into confident prose.

### Tables across pages

Store every physical table fragment, its cell grid, header rows, caption, page and bbox. A logical table is a relation over retained fragments. Row evidence preserves actual fragment locators and repeats column labels only in its indexing representation.

Proposed continuation heuristic:

1. Hard reject nonadjacent pages, incompatible column counts, an intervening same/higher-level heading, or an explicit different table number.
2. Score adjacent compatible fragments: matching normalized headers `0.35`, matching caption/table number `0.20`, same heading path `0.15`, normalized horizontal overlap >=0.9 `0.10`, preceding fragment in bottom 20% and following in top 20% `0.10`, explicit continuation marker `0.10`.
3. Score >=0.85 becomes a high-confidence derived continuation; 0.60–0.85 becomes proposed; lower remains separate. Missing features contribute zero. These are heuristics, not calibrated probabilities or human acceptance.
4. Drop a repeated header from the assembled data only when normalized column labels match exactly; retain the original fragment. A derived logical table never overwrites either source table or manufactures a source-wide bbox.

High-confidence continuation may supply context in ordinary retrieval with its derivation visible. Strict provenance profiles expose the separate fragments until confirmation. Maintain positive/negative fixtures for coincidentally similar adjacent tables and headers repeated in unrelated sections.

### Figures, OCR and feedback

An asset retains actual pixels/hash, original caption and source links. `asset_proxies` binds each caption-copy/OCR/description/value proxy unit to exactly one asset; `asset_proxy_dependencies` records every original unit supplied as context. Proxy units are never citable. Original caption units remain independent source evidence. Proxy windows enter the asset candidate pool only. Native text/local OCR is attempted before configured remote vision; failed/ambiguous regions create a diagnostic and optional small crop for interpretation. Each remote task declares a response schema and source candidates.

Vision cache key: `H(image_bytes_sha, task, model, prompt_schema_version, supplied_context_sha)`. Cache literal transcription separately from interpretation. Inferred annotation targets must be one of supplied candidate IDs; otherwise remain unresolved. Native annotation target geometry is preferred when available. Derived numbers, descriptions and proposed feedback have review state `needs_review` and are not presented as verified quotations.

### Code

1. Obtain Git-tracked and permitted untracked paths using NUL-delimited read-only Git commands; apply Git ignore rules and workspace excludes. Never execute repository code, hooks, filters or submodules. Read blobs/files directly under a bounded registered root.
2. Capture each included file's bytes/hash and a sorted repository manifest; record commit and dirty status. Verify files did not change during capture; retry a moving file and fail a persistently unstable capture rather than mixing revisions.
3. Use grammar-specific Tree-sitter extraction for Python and TypeScript/JavaScript definitions, qualified symbols, decorators, docstrings, imports, calls and inheritance. Cache parse artifacts by file hash + grammar/parser fingerprint.
4. Preserve byte/line ranges and hierarchy; split large definitions for indexing while retaining the complete source unit and scope. Uncovered meaningful source regions get text units; parser errors are recorded.
5. Resolve symbols within the same indexed repository revision: exact qualified symbol first, explicit import binding second, unique same-module/scope name third. Ambiguous matches remain proposed/unresolved. Never resolve an old revision's import to the latest target by accident.
6. Cross-repository resolution requires an explicit revision-qualified dependency mapping. Cross-document identifier links with multiple possible editions remain unresolved unless the source/date binding selects one.

The code-profile LanguageAdapter contract is required: it defines declaration/parent/import spans, file-qualified definition keys, per-reference resolution outcomes and discoverable syntax capabilities. Index native `contains` and immediate same-scope `precedes` assertions for bounded structural context; do not construct all sibling pairs. Store symbol records/facets and immutable diagnostic artifacts as mapped in that profile.

Assertions such as `calls`, `imports`, `inherits`, `contains`, `precedes`, `cites`, `reviews`, `supports` and `supersedes` have type, direction, method and exact support evidence. Parser inference does not become a human decision.

## 2. Search windows and contextual groups

A canonical evidence unit is a source object. A chunk is an index window into it. Initial text windows target 512 tokenizer tokens, a soft ceiling of 800, and zero overlap. Use paragraph/sentence boundaries or AST child boundaries where possible. A long atomic unit can produce several windows with exact Unicode-codepoint offsets into its normalized source text. The evidence fetch API can still return the entire original unit.

`boundary_key` identifies an allowed assembly boundary: document section/article, physical/logical table, or source file/parent symbol. `context_group_key` groups ordered windows from one extraction and coherent section/file scope. Neither grouping crosses source revisions or unrelated documents. A deterministic context capsule contains title, kind and heading/symbol path. Provider input is an ordered nested list: `[capsule, window1, window2, ...]`; discard the capsule vector and map the remaining group/chunk indices exactly.

For code, omit mutable commit labels from the embedding capsule so unchanged file/scope content can reuse an embedding cache; revision-qualified citation identity remains outside the representation. Cache keys include the whole group text/context digest, provider/model, output dimensions and representation version. Changing one group's context invalidates all vectors affected by that joint contextualization.

Initial contextual provider settings: `voyage-context-4`, 1,024 dimensions, float32; SDK `contextualized_embed(..., input_type='document', enable_auto_chunking=False)`. Queries use the same model with `input_type='query'`. Validate SDK parameter mapping in a provider contract test; do not assume a REST field name from a guide is identical to its SDK keyword.

The current guide lists 32K total prechunked tokens versus 120K for auto-chunking, while generic constraints elsewhere still state 120K. Start at 24K/group and 28K/request, including capsule tokens, and never enable provider rechunking to evade a limit. Also respect documented group/chunk counts. Exact installed-tokenizer counting is required; if token counting is unavailable, refuse that configuration rather than rely on a character estimate at the hard boundary. [Contextual guide](https://docs.voyageai.com/docs/contextualized-chunk-embeddings), [API reference](https://docs.voyageai.com/reference/contextualized-embeddings-api).

Multimodal inputs pair locator/caption context with actual image pixels using `voyage-multimodal-3.5`, 1,024 dimensions and `truncation=False`. Start at 32 assets and 100K estimated multimodal tokens per batch, while enforcing the provider's per-image/per-input/request bounds. Oversized images are rejected or receive an explicitly recorded derived resize; never silently discard an image by truncation. Text-vector dimensions matching image-vector dimensions do not make them the same space. [Multimodal guide](https://docs.voyageai.com/docs/multimodal-embeddings), [API reference](https://docs.voyageai.com/reference/multimodal-embeddings-api).

Code may use the explicit contextual or independent-window embedding profile defined in the code specification. Joint contextualization and ordinary code-model inputs have different cache/representation semantics; both preserve one vector per indexed window.

Every response must have the expected number of entries, unambiguous index mapping, the configured dimension, finite values and nonzero norm. Normalize to unit length under an explicitly recorded cosine policy before storage; record the input/output hashes. Failed validation saves no completed receipt. Retry transient transport/rate failures with bounded exponential backoff; invalid payload/model/access errors fail without blind retries.

## 3. Eligibility before candidate selection

Resolve and pin the validated registry/mode before compiling eligibility. Kind-to-lane mapping, uses, review rules, typed facets and conditional evidence requirements come from the [configuration specification](2026-09-10-tychora-configuration-design.md). An unknown mode or predicate fails validation; profile selection cannot widen the granted scope.

Compile `EligibilityView {snapshot_id, policy_epoch, scope_digest, selected_batch_ids, revision_ids, source_unit_ids, context_unit_ids, structural_unit_ids, proxy_unit_ids, chunk_ids, asset_ids, vector_record_ids, blocked_assertion_ids}` once per request. Resolve source/collection membership, all current access floors, allowed uses, source/child review state, withdrawal, date policy and registered facets. Unknown/malformed filters fail closed.

Reviewable source material and derived retrieval descriptions have separate eligibility rules. The generic profile accepts original units in `unreviewed` or `accepted` state; `needs_review` and rejected originals require explicit policy or remain excluded. Strict verified evidence requires `accepted` state. An asset proxy in `unreviewed`, `needs_review` or `accepted` state may locate the original only when `allow_derived_proxies` is enabled and its asset, source and **all** original context dependencies pass their policies. Rejected proxies never participate. Proxy eligibility never upgrades it into a quoted source. Strict profiles default to accepted proxies only. Index visibility and model-upload permission are separate: a locally accessible unit may still be forbidden to a remote reranker.

Queries choose `current`, `all`, or explicit revision IDs. Explicit IDs take precedence and still undergo every policy/date filter; `all` keeps all qualifying retained editions. Undated `current` uses the snapshot's declared default. With either effective date or publication cutoff, evaluate all retained editions **before** choosing the current historical edition: apply half-open effective intervals and the publication cutoff, then prefer the latest applicable effective start (if requested) or publication date, with publication date as secondary ordering. Preserve tied/overlapping winners and warn about ambiguity. Null effective end is open-ended, not an unknown date. Strict mode excludes unknown start/publication dates only when that field is needed by the requested predicate; permissive mode reports unknown editions separately without replacing a known dated winner. Latest is never an arbitrary maximum edition string.

`context_unit_ids` applies all-unit filters/current policy; `source_unit_ids` is its matched-candidate subset after selection-only facets. Permitted non-searchable file containers belong only to `structural_unit_ids`; they may ground a graph scope but cannot become a quoted search result. Graph grounding/support checks use context and structural eligibility; matched-candidate hydration additionally applies selection-only facets. Code symbol-kind/name facets are matched-candidate selectors; path/language and authorization/date/use filters also apply to context/support. Validate filter scopes and reject mixed-scope OR expressions as specified by the code profile.

Use matched-candidate IDs to constrain exact/FTS/ANN results, and context-eligible IDs for labelled neighbours/support. Both sets obey mandatory policy/date/source constraints. Every source/provider representation also requires all overlapping range dependencies; joint embeddings require their complete group dependencies. Apply these views to assets, graph nodes, required relation supports and final citations. Check before every candidate LIMIT and again before output/provider dispatch. Graph path count limits cannot hide an unauthorized intermediate node and still return its downstream target.

## 4. Hybrid search and graph augmentation

```mermaid
sequenceDiagram
    participant C as Python / CLI / MCP / HTTP
    participant R as Retrieval service
    participant S as Corpus and lexical store
    participant P as Provider ports
    participant V as Vector store
    participant G as Graph store
    C->>R: QueryRequest and granted RequestScope
    R->>S: Pin snapshot; compile eligibility
    S-->>R: Allowed units, entries and policy epoch
    par Exact and lexical
        R->>S: Exact IDs and filtered BM25
        S-->>R: Ranked source candidates
    and Text and image embeddings
        R->>P: Authorized query inputs
        P-->>R: Separate query vectors
        R->>V: ANN restricted to eligible record IDs
        V-->>R: Text and visual candidates
    end
    R->>R: Deduplicate; weighted RRF
    opt Structural or semantic graph profile
        R->>G: Eligible bounded structural context and/or semantic expansion
        G-->>R: Supported candidate IDs and paths
        R->>R: Add one graph rank contribution
    end
    R->>P: Rerank bounded textual representations
    P-->>R: Scores mapped by original candidate index
    R->>S: Exact source units, allowed neighbours and assets
    R->>R: Assemble; budget; revalidate policy
    R-->>C: Structured evidence, citations, coverage and trace
```

| Parameter | Initial value |
| --- | ---: |
| Exact candidate cap / explicit identifiers per request | 32 / 16 |
| BM25 candidates | 80 distinct owners per text/asset candidate pool |
| Text ANN candidates per configured text family | 80 distinct owners per text/asset candidate pool |
| Image ANN candidates | 24 distinct assets |
| RRF constant | 60 |
| Channel weights: exact / lexical / text / visual / graph | 2.0 / 1.0 / 1.0 / 0.7 / 0.35 |
| Textual rerank pool | 64; up to 32 exact and 8 graph-only reserved slots |
| Graph search augmentation | Shared 5 seeds / 32 new units / 2,000 eligible assertion rows; semantic 1 hop, structural named parent/sibling patterns |
| Explicit relationship inspection maximum | 2 hops, 100 edges |
| Final text segments | 8 |
| Maximum segment / rendered textual context budget | 1,200 / 6,000 tokens |
| Diversity | 2 segments per document or code file by default |
| Final visual assets | 3; one native multimodal and one selected-text companion reservation |

Request text (question or supplied passage) is limited to 2,000 provider-query tokens initially. Longer input returns an actionable limit error, allowing the calling agent to split it or issue several explicit queries; no hidden summarization, truncation or LLM query rewriting is introduced. Image-only queries run the visual channel and inspect linked source context without inventing a text query.

### Exact and lexical channels

Identifier namespaces define normalization and case sensitivity. A structured identifier request avoids regex guessing; free text additionally runs registered extractors for conventional rule/article/symbol IDs. `exact_only=true` bypasses semantic expansion, graph augmentation and reranking. No exact hit means an empty result with coverage, not lexical fallback.

Build a bounded FTS MATCH expression from application-tokenized terms; user text is never raw MATCH syntax. Select at most 32 distinct terms after a versioned profile stop-word list: descending frequency, then descending token length, then first occurrence. Include up to four explicitly quoted phrases of at most eight terms each within the same 32-term budget. Escape embedded quotes, quote each term/phrase, and join clauses with **OR**. Phrases receive adjacency semantics; arbitrary user operators never do. Identifiers also use the equality channel independently. An empty lexical expression yields no lexical candidates, not an invalid MATCH. Persist the generated expression only when query-payload retention permits it.

Use FTS5 `unicode61 remove_diacritics 2`; populate identifiers, title, headings, body and context separately. Score with `bm25(lexical_fts, 5.0,3.0,2.0,1.0,0.5)` ascending. Within one short read transaction, capture the statistics epoch and materialize scored matching rows joined to eligible chunks and exact selected batches. Group by typed owner (unit/asset), retaining the best score and matching window IDs **before** the candidate LIMIT. Use a materialized scoring CTE to avoid evaluating the FTS auxiliary function outside its MATCH context. Return independently limited text and asset lists with dense integer ranks. Exact-code identifiers retain a dedicated equality path because tokenization can split punctuation. [SQLite FTS5](https://www.sqlite.org/fts5.html).

ANN budgets likewise count unique owners. Search source-window and asset-proxy eligible sets as separate pools, with bounded adaptive record fetching/deduplication described in the storage contract. All pools share the per-family work budget. Record retrieved-window and distinct-candidate counts; hitting the work limit is explicit truncation, never a claim that 80 unique candidates were searched.

### Rank fusion

For candidate x, use `RRF(x) = sum(weight[channel] / (60 + rank[channel,x]))`. Ranks are one-based; absent channels contribute zero. Collapse multiple matching windows to the best rank per evidence unit in each channel before fusion, preventing long sources from gaining votes solely through repeated chunks. Keep supporting window IDs for later assembly. An explicitly enabled registered relevance-hint strategy may add its bounded bonus after base RRF; it never changes eligibility or creates a channel vote. Tie-break with stable candidate ID. The channel weights are configuration defaults; the RRF reference does not establish their optimal values. [RRF paper](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf).

Candidates have typed keys: unit or asset. Original text units enter the textual ranking. Caption/OCR/description proxy chunks explicitly owned by an asset map to that asset's ranking, rather than pretending the description is an authoritative text unit. Image ANN adds a visual vote to asset candidates; it does not add cosine values to text-vector scores. For each text embedding family, sort unique candidates by best cosine distance and assign integer ranks 1..n. Across families take each candidate's minimum integer rank, sort by that value and stable ID, and assign a fresh dense rank 1..n for the single text-channel vote. This explicit rank merge avoids extra votes merely from configuring more models; record both family ranks and the merged rank.

### Optional graph expansion

The coordinator selects one list of the five best eligible source-unit seeds after initial fusion. Semantic and structural strategies receive applicable subsets of that same list, with no refilling. GraphStore resolves their grounded entities internally and returns canonical target unit IDs; internal graph keys never require ad hoc hydration. Expand allowed predicates one hop, validating every grounding/support and excluding currently blocked assertion IDs. Paginate eligible assertion rows until 32 new units or the 2,000-row work cap, deduplicating against initial candidates. Use a one-row lookahead to distinguish exhaustion from truncation. Rank returned unique targets by path length, confirmed-before-recorded status, descending heuristic confidence and stable ID; contribute once as the graph channel. Several paths to the same target do not multiply votes.

Reserve up to eight graph-only candidates **inside** the 64-entry rerank pool after exact admission. Then fill from ordinary fused rank. This reservation matters: a graph-only RRF score of `0.35/61` would otherwise lose even to a rank-80 single-channel text candidate. It guarantees assessment, not final selection. Report graph additions, admitted/discarded counts and relation/support paths without exposing hidden-object counts. Graph proximity is a reason to inspect evidence, not a factual inference.

The default document profile leaves graph rank augmentation off; exact relationship inspection remains available. The code preset enables bounded structural-context selection; calls/imports/inheritance rank expansion remains optional. Structural and semantic strategies share the existing seed/candidate/work/rerank budgets. The code profile specifies 16/16 new-candidate and 4/4 rerank reservations, deterministic interleaving and returning unused slots. They contribute once to the graph channel, never twice for the same unit. Measure against semantic-disabled and structural-disabled baselines. No automatic community-summary or entity-extraction LLM is required for this graph capability.

### Text reranking and visual selection

Build the 64-entry rerank pool in this order: up to 32 distinct exact candidates; up to eight graph-only candidates not already admitted (shared by structural/semantic strategies when both are enabled); then optional profile lane reservations from remaining candidates; remaining positions by fused textual rank. Deduplicate at every admission and return unused reservations to the fused pool. In normal hybrid mode reservation guarantees assessment, not automatic top position. `exact_only` remains deterministic lookup.

Each representation includes source kind/title/heading and exact matched windows, capped at 1,200 provider tokens. Start at its strongest matched window (best channel rank, channel weight as tie-break, then source offset), then include nonoverlapping supporting windows in relevance order while budget permits. Never take an unrelated prefix of the entire unit. If a window itself is too large, take an explicitly flagged exact subrange of that window. Report omitted-match counts and canonical ranges. Graph-only candidates use the assertion's supported target span where available, otherwise a labelled leading source excerpt. For code, permitted exact parent/import context spans may share this representation budget; they never displace a winning match. These are ranking inputs, not final quotations.

Use a configured Voyage text reranker. Initial candidate `rerank-3` is preview and must pass capability/access validation before activation; do not silently replace it if unavailable. Keep query <=2,000 tokens and `truncation=False`, validate supported per-input and repeated-query aggregate limits, and reduce the batch or reject a too-large request before sending. Returned indices must map one-to-one to supplied candidates; missing/duplicate/out-of-range indices fail validation. Scores order that query's textual candidates and are not calibrated probabilities. [Reranker guide](https://docs.voyageai.com/docs/reranker), [API](https://docs.voyageai.com/reference/reranker-api).

A text-only reranker does not inspect image pixels. Select the independent `visuals` list after text assembly: reserve the best eligible native multimodal hit first when that channel returned one; reserve one distinct image companion of the strongest selected text segment when available (segment rank, explicit figure/table/caption link priority, source ordinal/asset ID); fill remaining positions by asset RRF. Cap the union at three and deduplicate by asset ID. With a caller budget of one, the native multimodal reservation takes precedence; unused reservations return to the fused asset pool. An asset already admitted can satisfy several reasons without consuming another slot.

Record `selection_reasons` as a set of `multimodal_match`, `proxy_match`, `source_link`, and `reserved_multimodal`/`reserved_companion` where applicable. These admission rules preserve a pixel-based match with a weak proxy; they do not assert its factual relevance. Source-linked companions do not acquire a made-up similarity score.

## 5. Source segment reconstruction

RSE-style assembly uses canonical anchor ranges to locate relevant evidence and returns coherent exact source spans. [dsRAG](https://github.com/D-Star-AI/dsRAG) motivates this distinction; the deterministic algorithm here is explicitly specified:

Before assembly, normalize each candidate into `AnchorRange {unit_id, char_start, char_end, origin, indexed_window_id?}`. Use a retrieved window when available; otherwise a validated graph target-support span; for exact-only lookup, the full identified unit; finally a labelled source-unit range for a graph target lacking a narrower support span. Origins are `index_window`, `assertion_support`, `exact_unit` or `unit_fallback`. Exact and graph anchors without an indexed window retain an empty window-ID list; never fabricate one.

1. For each ranked unit, select its strongest anchor using retained channel rank/weight/offset where available. Begin the exact excerpt at that anchor, not at an unrelated prefix. Expand left/right to paragraph, sentence or AST boundaries only while the 1,200-token rendered segment budget permits. If the anchor itself exceeds the budget, keep a labelled subrange with `match_truncated=true`; whole-unit exact anchors use a leading excerpt with an explicit full-unit fetch reference.
2. Admit additional nonoverlapping anchored intervals in relevance order. Nearby ranges may coalesce if their intervening source text is eligible and fits; distant matches remain separate excerpts of the same unit. Store canonical `[char_start,char_end)` ranges and omitted-match counts. A skipped match is never silently called covered.
3. Group admitted intervals by extraction/boundary key. Merge adjacent matched units only if all included spans fit. Fill at most one missing ordinal only when the **entire** gap unit is eligible, shares the boundary and fits; otherwise keep separate segments. Add at most one neighbour at each end only with the remaining budget. Never bridge a denied unit or cross source revisions/file scopes.
4. Preserve table headers, row-to-fragment locators, figure captions and AST context. A reconstructed table cites each physical fragment; it has no invented single physical page. Large neighbours never displace the actual matched interval.
5. Code segments can attach separately labelled `context_excerpts` for exact parent declarations and relevant imports (at most 160 tokens within the segment budget), as defined in the code profile. Siblings that survive reranking are matched evidence, while added declarations/imports remain context-only. Never present noncontiguous spans as one continuous quotation. Deduplicate overlapping original-file ranges.
6. Deduplicate overlapping segments. Rank by strongest matched candidate, then number of distinct matched units, then source order/ID; added context does not increase relevance. Multiple disjoint matches from one long unit can therefore produce separate segments, subject to the same diversity limit.
7. Apply an enabled profile lane-reservation policy first within all existing budgets, then fill by rank. Select up to eight segments, generally two per document or per code file; a whole repository is not one diversity bucket. Exact lookup may relax diversity, never request budgets. Missing required source roles are reported as coverage gaps, not manufactured quota matches.
8. Enforce 6,000 rendered text tokens including headings, citations, warnings and textual visual proxies. Before selecting text, reserve `min(200 * visual_limit, floor(total_budget / 10))` tokens for visual labels/proxies (600 by default). Select text once within the remainder, then choose visuals using those selected segments and fit their labelled proxies within the reservation. Unused space may remain; no circular companion/text re-selection is needed. A source excerpt carries `complete=false`, exact ranges and a full-unit fetch reference. Never trim away the winning match merely to retain surrounding context.

After selection, perform configured bounded relation/decision inspection independently of graph rank expansion, using the remaining shared assertion-work budget. Preserve its complete/partial/not-evaluated status; empty conflicts never stand for an unperformed check. Before rendering, materialize authorized structured-record attachments keyed to their source units, lane IDs, unmet evidence requirements and recorded decision requirements. Apply the same dependency rules to embedded quotations, target references and record fields; a structured record cannot bypass source restrictions. Hierarchical rendering is a separate pure projection, specified in the configuration contract.

`matched_unit_ids` and `context_only_unit_ids` are distinct; canonical anchor ranges/origins and available indexed window IDs are retained in details. Returned quotes are source substrings, not capsule text or generated OCR interpretations. Derived descriptions are in labelled fields with original assets attached.

Output token accounting uses a configured, recorded tokenizer (initially `cl100k_base` for the textual envelope), and callers may provide a compatible counter through the Python API. This is an explicit budgeting convention, not a promise that every agent model counts identically. Image bytes are separate registered resources with count/size limits; inline image requests have a separate visual-input budget.

## 6. QueryResult example and error semantics

```json
{
  "schema_version": "1",
  "run_id": "run_example",
  "snapshot_id": "snapshot_example",
  "coverage": {"status": "complete", "completed_channels": ["exact", "lexical", "text"], "warnings": []},
  "segments": [{
    "segment_id": "segment_example",
    "source_id": "source_example",
    "revision_id": "revision_example",
    "extraction_id": "extraction_example",
    "kind": "standard",
    "role": "normative_reference",
    "text": "Record the verification outcome and its supporting evidence.",
    "matched_unit_ids": ["unit_requirement"],
    "context_only_unit_ids": [],
    "complete": true,
    "citations": [{"unit_id": "unit_requirement", "physical_page": 12, "source_ref": "section-4"}],
    "signals": ["exact", "lexical", "text", "rerank"]
  }],
  "visuals": [],
  "relations": [],
  "trace": {"rerank_candidates": 16, "tokenizer": "cl100k_base"}
}
```

This is synthetic contract data, not a source quotation. `coverage.status=complete` means enabled required retrieval stages completed; it does not mean the question is answered or the evidence is true. A valid zero-hit search is complete with zero matches. Channel failure yields `partial` only if the configured profile permits it; strict mode returns a typed failure. A missing optional channel is reported unavailable, not falsely completed. No result is labelled “supported” merely because some text matched.

No-match, incomplete-index, denied-scope and provider-failure conditions are separate. Diagnostics do not reveal restricted source names, excluded passages, hidden relationship counts or secrets. Cached queries include complete query/image hash, snapshot, policy epoch, lexical statistics epoch, filters, scope, model/profile fingerprints and budgets; in-memory result-cache default TTL is five minutes. Query embeddings use a separate bounded cache keyed by exact prepared input and provider/space/scope identity. Filter-only changes may reuse embeddings but not bypass fresh eligibility. Result-cache hits create new run IDs and revalidate policy; see the code profile for the shared cache contract. Offline execution disables external provider construction/calls rather than merely omitting the API key.

## 7. Evaluation and algorithm acceptance

Configuration compatibility uses synthetic packs to verify lane/mode exclusions, custom relations, scoped/global sources, records and exact renderer snapshots. Rendering equality is checked on fixed canonical results; retrieval migration separately compares expected/forbidden evidence and date semantics. Profiles cannot promise identical rankings after changing models/chunking.

A gold case contains request, split/family key, expected and forbidden source/revision/unit/asset IDs, graded relevance, required exact spans, locator expectations and permitted relation paths. Separate code/documents/visuals and strict-policy slices. Dataset edits create new immutable versions. Split related editions, derivatives and feedback events together to prevent near-duplicates from crossing development/test boundaries.

| Metric | Calculation / gate |
| --- | --- |
| Recall@k | Relevant returned IDs / all gold relevant IDs |
| MRR | Mean reciprocal first relevant rank, zero if absent |
| nDCG@k | Sum `(2^grade-1)/log2(rank+1)`, normalized by ideal ordering |
| Version precision | Correct-version matches / returned version-labelled matches |
| Policy leakage | Forbidden returned objects / returned objects; zero failures required |
| Segment completeness | Required gold source spans covered / required spans |
| Locator precision | Correct audited locators / returned audited locators |
| Visual recall | Relevant returned asset IDs / expected relevant asset IDs |
| Relationship precision | Valid returned assertions/paths / judged returned assertions/paths |
| Runtime | p50/p95 stage and total duration, memory, index bytes, throughput and retry work |

Report sample counts and N/A for empty denominators; no automatic perfect scores for empty evaluations. Compare lexical-only, hybrid, reranked, graph-augmented and visual-linked variants on the same snapshot/configured split and policy/statistics epoch. Pause lexical mutations/GC/rebuild during the comparison; policy changes invalidate comparability. Save per-case outputs so metric changes can be inspected in the dashboard. A snapshot alone does not freeze corpus-wide FTS scoring statistics.

Initial integrity gate is no exact-ID, wrong-version, forbidden-source or invalid-locator regression on the reviewed integrity suite. Semantic thresholds are set from the first independently labelled benchmark; do not derive them from deterministic fake embeddings. Report image text-to-image relevance, not only image-to-itself retrieval. Table continuity and symbol resolution get negative examples as well as happy paths. Required algorithm cases include: many windows owned by one unit; a matching window near the end of a 10,000-token unit; two distant matches in one unit; 64 strong textual candidates plus a graph-only target; a top native image with no useful proxy; cutoff-only historical retrieval; and a relation whose support or status changes during a query.

Code acceptance additionally separates file and file-qualified-definition metrics, needle/exploratory queries, resolution rates from gold edge correctness, and four cold/warm/cache benchmark states. Use the identities, denominators and fixtures in the [Code retrieval profile](2026-09-10-tychora-code-retrieval-design.md).

Record hardware, corpus size, concurrency, provider identities, model capability records and tokenizer with every performance run. Provider cost is unknown unless dated pricing inputs and units are explicitly available. An algorithm change is accepted through a reviewed quality/latency comparison, not through adding complexity alone.
