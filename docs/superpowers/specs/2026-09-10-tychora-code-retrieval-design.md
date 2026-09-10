# Tychora — code retrieval profile

Date: 2026-09-10
Status: engineering review draft; approved code-capability additions, application not implemented
Depends on: [Engineering](2026-09-10-tychora-engineering-design.md), [Storage](2026-09-10-tychora-storage-schema.md), [Algorithms](2026-09-10-tychora-pipeline-algorithms.md)

## 1. Required capabilities and experiment boundary

The first release must preserve code structure through ingestion, search and inspection. A matching method must retain its parent declaration and exact source location; relevant nearby definitions and methods in the same scope must be discoverable within a bounded context request. Structural proximity suggests candidates, not relevance or correctness.

Required: a language-adapter contract; revision-qualified file/symbol identities; deterministic structural-context selection; explicit code embedding profiles; reference-resolution diagnostics; file/symbol and exploratory-context evaluation; cold/warm/cache benchmarks. These capabilities share the generic evidence engine and five daily CLI verbs. They do not introduce code editing or generated answers.

Optional experiments: iterative graph score propagation with predicate/direction weights and decay; relevance/penalty-based greedy segment scoring; graph bridge interpolation. Each requires a named strategy fingerprint and an ablation against the required structural baseline. Neither a graph relationship nor a neighboring definition earns an automatic score bonus in the default profile.

## 2. Language adapter contract

`LanguageAdapter` is an ingestion port underneath the code parser, independent of database and transport clients. A registered adapter provides:

| Operation | Contract |
| --- | --- |
| `capabilities()` | Language/dialects, extensions, grammar package/version, supported declaration/reference forms and known limitations |
| `parse(file_bytes, options)` | Immutable syntax result, encoding/line map and diagnostics; no source execution |
| `definitions(syntax)` | Definition kind, name, qualified scope, original byte span, decorator/export span, declaration/header span, parent reference and optional overload discriminator |
| `references(syntax)` | Reference form, spelling, source span, owning scope, import binding and candidate-resolution inputs |
| `resolve(references, symbol_catalog)` | Deterministic target/candidate identities and reasons inside one indexed repository revision; no runtime introspection |
| `context_spans(definition)` | Exact parent-declaration and referenced-import spans with source dependencies; never a generated source summary |

The versioned adapter fingerprint includes grammar, extraction rules, resolver rules and options; the extraction/normalizer fingerprints include it. Changing resolution semantics creates a new extraction. Additional languages install explicitly through tested packages; parser/model downloads never occur during help or search.

Initial qualification targets are Python and JavaScript/TypeScript, including separately declared JSX/TSX support. Advertise a dialect only once its fixtures pass. The interface must accommodate receiver methods, implementation blocks, namespaces, overloads and definitions expressed through calls for later language adapters, without hard-coding those cases into the generic retriever.

Source lines are one-based inclusive; byte ranges are half-open in the captured file; normalized-text character ranges are half-open Unicode-codepoint offsets. Keep the mapping between them. Unsupported encodings or syntax produce explicit diagnostics and, where faithful decoding is possible, labelled text fallback. Syntax error recovery must not fabricate a resolved call. An empty or unsupported file is recorded in coverage, not silently presented as fully parsed.

## 3. Canonical storage mapping

Use the existing SQLite records and graph projection; no new adjacency tables or vector storage are required.

| Object | Canonical representation |
| --- | --- |
| File | `revision_artifacts` with relative path and captured blob; a non-searchable `code_file` container unit with empty text, `citable=0`, file metadata and original-artifact locator |
| Definition | `evidence_units` with original text, parent unit, ordinal and exact `locators`; `structured_records(record_type='code_symbol', schema_hash=...)` holds the validated symbol record |
| Search representation | `chunks` into the definition or uncovered source text; metadata/capsules never become quoted code |
| Exact identity | `identifiers` namespaces for file-qualified symbol identity and qualified/short-name aliases; aliases may return several definitions |
| Filters | Registered `code.language`, `code.path`, `code.symbol_kind`, `code.qualified_name` facets; source/revision scope remains mandatory |
| References | Immutable extraction diagnostic artifact with per-reference records and resolution outcomes; resolved relations also enter the assertion artifact |
| Structure | `contains` and `precedes` assertions in the graph generation, validated against canonical parent/order metadata |

A `code_symbol` record requires `relative_path`, `language`, `symbol_kind`, `name`, `qualified_name`, `declaration_span`, `parent_unit_id` (nullable), `overload_discriminator` (nullable), `definition_key`, `context_spans[]`, and `adapter_fingerprint`. Each persisted `context_spans` entry requires `source_unit_id`, canonical `char_start/char_end`, original-file byte/line locator, `reason` (parent declaration or referenced import), and complete `dependency_unit_ids`. Keep imports as original citable units in uncovered source regions; empty file containers cannot supply quoted text. The list is produced during normalization, then read without reparsing at query time. Spans include their byte/character coordinate domain and source-unit binding. Cross-record checks enforce the same extraction and parent as the evidence unit. A normalized file key is `(source_id, revision_id, relative_path)`; never use an absolute machine path.

`definition_key = H("code-definition-v1", revision_id, relative_path, language, qualified_name, symbol_kind, discriminator)`. The discriminator is the adapter's stable overload/signature key where available, otherwise the declaration's start byte. Repeated definitions of the same name therefore remain distinct. `unit_id` remains the exact extraction-qualified citation identity; neither key is promised to survive edits that move an otherwise indistinguishable definition. Evaluation stores both the expected definition key and exact evidence/ranges when applicable.

Nested definitions overlap in the original file. Build a source-range dependency map in the extraction artifact and bind every chunk, context span and provider input to the units whose text it covers. Full-class representations require every covered child to be permitted; an accepted parent cannot reveal a rejected method. Bounded declaration spans may remain usable when their ranges do not overlap the rejected body. Never silently remove a method and present the remaining text as a complete class.

Index windows can avoid covered child bodies by keeping parent declaration/direct-body intervals separate, preserving exact canonical ranges. If a stored representation includes a now-forbidden range, exclude it. Joint embeddings additionally depend on every member/capsule range of their contextual group; their manifests retain that complete dependency set. A policy change can therefore make an otherwise matching vector unavailable until an explicitly authorized rebuild. Lexical and exact inspection still check their own range dependencies. Full-file/parent fetches require permission for all contained ranges or return an explicit unavailable/partial response; no fabricated continuous quotation.

Assign `code.path` and `code.language` consistently to definitions, uncovered source units and file containers so all-unit facets can be evaluated on every context/support dependency. A file container may participate in scope/eligibility checks without being a quote or search candidate. Native `contains` assertions connect each definition to its immediate lexical parent; top-level definitions belong to the file container. `precedes` links adjacent definitions within the same immediate parent in source order. Include both endpoint units as required supports. Non-searchable file containers belong to a separate permitted structural-unit set; the graph may use that set for grounding, but returned search candidates must still pass source-evidence eligibility. The graph uses these sparse O(n) assertions; it does not materialize all same-class or same-file pairs.

## 4. Bounded structural context

The `code` preset enables structural context. Semantic calls/imports/inheritance expansion remains independently configurable. `exact_only` bypasses all context expansion; callers can explicitly inspect relationships afterward.

`GraphStore.structural_context(view, seed_unit_ids, limits)` returns candidate source units, relation paths, source-order distance and reasons: `same_parent`, `preceding_definition`, `following_definition`. It uses the same sealed generation, assertion decision watermark and eligibility overlay as semantic graph traversal. Sibling lookup is the explicit two-link pattern child → parent → child; this does not enable arbitrary two-hop semantic expansion. Hidden/rejected parents, intermediate definitions or required supports cannot connect visible results. Immediate adjacency never jumps over an ineligible definition.

Code facets declare filter applicability. `code.symbol_kind` and `code.qualified_name` constrain matched candidates only; `code.path`, `code.language`, source/revision/date/access/review and upload rules constrain every candidate, support and context span. Other facets default to all-unit scope. A class declaration may accompany a method-only result as labelled context, but never become a matched class result. Source-range permission dependencies use context eligibility, not candidate-selection facets.

The registry uses `filter_scope=matched_candidates|all_units`. Separate these constraints at request validation; reject an `or` mixing scopes rather than guessing its projection onto context. An `and` may contain separately scoped subtrees. Source authorization and mandatory date/use constraints cannot be reclassified as candidate-only filters.

Initial limits and admission order:

1. The coordinator chooses one top-five source-unit seed list after base exact/lexical/vector fusion. Structural selection receives only code definitions in that list; semantic selection receives its applicable subset. Neither strategy refills its subset or chooses five additional seeds in mixed corpora.
2. Per seed, consider the immediate preceding/following definition, then at most two nearest additional siblings excluding those neighbors and existing base candidates. Sibling order is source-ordinal distance, then preceding before following, then stable unit ID. Include a relation reason for every admitted candidate.
3. Structural and semantic expansion share **32 new unit candidates and 2,000 inspected eligible assertion rows** per request. Initially reserve 16 candidate positions for each enabled strategy; return unused positions to the other strategy in its deterministic order. With only one enabled, it may use all 32. Deduplicate against base hits and each other before consuming a position. Both operations receive the same remaining-budget object; each inspected assertion occurrence costs one work unit, so a sibling path with two contains assertions costs two (including repeated links in separate matches). Graph-internal scans/sorting remain bounded by the request deadline, not by a claim that output-row limits bound all database work. Exhaustion and work-limit truncation are distinct.
4. All expansion candidates enter the existing graph ranking channel once. Structural candidates use seed rank, structural priority (immediate neighbor before other sibling), source distance and ID. Interleave structural and semantic ranked lists round-robin, structural first, to assign the channel's dense ranks; candidates appearing in both get one vote and retain both reasons.
5. Within the existing eight graph-only rerank reservations, reserve four for each enabled strategy; return unused reservations and deduplicate before filling the rest of the 64-entry pool. When only one expansion strategy is enabled, it can use all eight. These are assessments, not final-result guarantees.

A bounded parent declaration/import context is different from a sibling candidate. For reranking representations and again for final selected definitions, include at most 160 tokens of exact context spans, measured with that stage's tokenizer: parent declaration first, then explicit imports actually referenced by the definition. Use the persisted context-span list and the appropriate provider/output tokenizer at each stage. Context is dropped before the matched excerpt when space is tight. Every context span and all its dependencies undergo eligibility and remote-upload checks before entering a reranker or response.

Returned segments retain the generic `text`, match anchors and citations, and may add `context_excerpts[] = {unit_id, char_start, char_end, text, locator, reason}`. These excerpts count against the existing 1,200-token segment and total response budgets. Keep noncontiguous parent/import excerpts separately labelled; do not concatenate them and claim one continuous source range. A sibling selected by the reranker is its own matched evidence; an added declaration/import is `context_only`. Deduplicate overlapping original-file ranges so a class and its matched method do not repeat the same body without adding evidence.

```mermaid
sequenceDiagram
    participant R as Retrieval coordinator
    participant G as Graph store
    participant S as Corpus store
    participant P as Reranker
    R->>R: Base candidates; shared five seeds; applicable code subset
    R->>G: Eligible structural context and optional semantic expansion
    G-->>R: Unit IDs, paths, reasons and truncation
    R->>R: Shared caps; deduplicate; reserved admission
    R->>S: Anchors and permitted declaration/import spans
    S-->>R: Exact representations and dependencies
    R->>P: Same bounded textual rerank pool
    P-->>R: Ranked candidate indices
    R->>S: Hydrate selected source ranges
    R-->>R: Budget text plus separately labelled context excerpts
```

## 5. Embedding profile and cache behavior

Code embeddings remain a declared family choice, with the same exact/lexical and rerank paths. `code-contextual` uses the pinned contextual model and coherent source groups. `code-independent` uses an ordinary code model, embedding each capsule plus window independently; the adapter preserves the group-to-window output mapping but declares `contextualization=independent`. It must never describe independent vectors as jointly contextualized.

For initial qualification compare `voyage-context-4` against the currently documented `voyage-code-4`, at 1,024 dimensions, on identical code units and output budgets. Keep `voyage-code-3` available as an explicitly selected compatibility/baseline model. The documentation lists these as distinct model choices; availability and SDK/tokenizer limits must pass real capability checks. This is a comparison requirement, not a claim that one model has won. [Voyage text models](https://docs.voyageai.com/docs/embeddings), [contextual models](https://docs.voyageai.com/docs/contextualized-chunk-embeddings).

Keep `code-contextual` as the initial default candidate until that comparison supports an explicit preset revision. Model/representation changes create a new family; no hidden per-query model switching, cross-space cosine comparison or double text-channel vote. Independent inputs are keyed by exact capsule/window bytes and provider options; joint inputs include the entire contextual group hash. Declaration/import dependencies supplied in capsules participate in upload policy checks and cache invalidation.

Add an owner-local **query-embedding cache**, separate from the final-result cache. Default: LRU, 256 entries, five-minute TTL, in memory only. Keys include workspace, principal/upload-scope digest, non-secret provider credential-binding ID, endpoint/model, space/representation fingerprint, dimensions/dtype/input type/options and exact prepared input hash. Key material and vectors are not query-history payloads and are not logged. Purging a workspace's query caches releases both caches; expiry/owner shutdown removes entries. Ordinary query-history retention is configured independently.

A filter change may reuse an identical query embedding but must re-run eligibility and retrieval. Final-result caching still requires the full snapshot/policy/statistics/filter/configuration key and delivery-time policy check. A cached result gets a new run ID and current run timings, with `cache_hit=true` and a permitted originating-run reference; it never reuses the old operation's identity. Offline mode permits no remote calls or provider construction, including on cache misses; report unavailable channels or use explicitly configured local capabilities.

## 6. Resolution diagnostics

Each extracted reference records `reference_id`, source unit/span, language/reference form, spelling, owning scope, adapter fingerprint, and exactly one state: `resolved`, `ambiguous`, `unresolved`. Resolved means one selected target with a stated static-resolution rule; it does not mean a human verified the relationship. Ambiguous records retain candidate IDs/reasons without inventing a selected endpoint. Cap stored alternatives at 20 with an explicit truncation flag and build-level candidate count; unknown external targets remain unresolved.

Keep unsupported reference syntax and parser recovery diagnostics separate from the extracted-reference denominator. Report counts/rates by language and reference form: `resolved/extracted`, `ambiguous/extracted`, `unresolved/extracted`; empty groups are N/A. Rates sum to one only within the declared extracted set. Independently measure gold-labelled edge precision/recall, including target and predicate correctness; a uniquely resolved wrong target must fail that evaluation.

Diagnostics are immutable extraction artifacts accessible through source/job inspection with current scope checks. Restricted target names/counts are not exposed to another scope; aggregate rates must be computed from permitted records, with unavailable fields labelled. Omit reference records whose candidate/support disclosures cannot be authorized from public diagnostic rates; label the result scope-limited without revealing omitted counts. Global build counts belong to owner-authorized diagnostics. This prevents a resolution report from becoming an access-control side channel.

## 7. Evaluation and performance acceptance

Gold cases tag language, `needle` (specific definition/behavior) or `exploratory` (several units/relationships), and expected/forbidden file, definition and exact source identities. Keep related repositories, editions and derivatives in the same development/test family.

Report file Recall@k/MRR using `(source, revision, path)` and symbol Recall@k/MRR using `definition_key`; deduplicate multiple windows of one definition. For each case, walk returned segments in their actual order and assign first-occurrence ranks to distinct file/definition IDs. Report both budgets and counts; a file hit cannot satisfy a symbol target. Context-only mentions or a call spelling do not count as a retrieved definition. Separate exploratory required-unit/relationship recall, returned-unit precision and exact-span completeness under the same final token budget; more context is not automatically better.

Required fixtures:

- Same short name in two files, repeated qualified names across repositories/revisions, overloads and name occurrences in comments/calls; only the expected definition counts.
- Nested classes, decorated/exported definitions, async functions, long methods with an end-of-body match, uncovered top-level statements, and partial syntax with faithful text fallback.
- Relevant distant sibling plus irrelevant adjacent method; structural candidates are assessed, and final precision/completeness is measured against the baseline without structural expansion.
- An entry-point query requiring its downstream implementation, a hidden parent/support, an accepted class containing a rejected method, a method-only facet with permitted class-header context, a mixed-corpus shared seed list, a large class, and a tight budget. Verify shared expansion caps and no lost winning anchor or leaked relationship.
- A correct local target, an external unresolved target, two plausible targets, and an unambiguous wrong resolution. Diagnostic rates and gold correctness must remain distinguishable.
- Incremental no-op, one-file edit, deletion, rename and dirty capture. Count parsed files, embedded windows and written projection records separately; full graph generation cost remains visible.

Benchmark the same published corpus, query set, configuration and output budget in four states:

| State | Resources / caches | Required instrumentation |
| --- | --- | --- |
| Cold owner | Fresh process; no database/provider handles; empty query-embedding/result caches | Startup plus request time; existing indexes, not re-ingestion |
| Warm resources | Open handles/provider clients; exact input absent from both caches | Embedding/rerank request counts and stage times |
| Warm query embedding | Same resources and cached exact embedding; result cache bypassed | Zero query-embedding calls; retrieval/rerank still executed as configured |
| Warm result | Valid final-result entry | No retrieval/provider calls; policy revalidation and new run record still occur |

Record OS page cache and any provider/model cache conditions separately; process-cold does not imply disk-cold or provider-cold. Report sample counts, p50/p95, stage times, provider calls and token/image volume, cache hits, selected units and output tokens. Do not compare warm-result latency with cold uncached quality runs as if the algorithm improved.

Instrument cache keys and calls with synthetic inputs: changed query/model/options/context must miss; filter changes can reuse embeddings but not invalid result entries; changed revision, policy/statistics epoch or scope invalidates results. Withdrawal before TTL expiry cannot return old evidence. Expiry, explicit cache purge and history purge have separate tests. Cached and bypassed runs must satisfy the same evidence/authorization contract.

The baseline release gate is all deterministic identity, range, policy, budget and cache-isolation cases passing plus a published code retrieval comparison. Semantic improvements require independently labelled cases; no numerical quality promise follows from fake embeddings. Add optional weighted-graph and greedy-segment experiments only as separate configuration rows, reporting ranking, context quality and latency together.

## 8. Interfaces and dashboard

The public Python clients expose structural context through `relations.traverse` with a typed `relation_view='structural'` option and through the code search profile; MCP uses its existing typed `inspect` tool. Discovery lists supported languages/dialects/reference forms, available code embedding profiles and context limits. Exact file/symbol inspection uses registered IDs and original snapshots.

The dashboard adds language/parser/resolution coverage to source diagnostics; structural selection reasons and separately highlighted parent/import excerpts to the reader; file/symbol and needle/exploratory slices to evaluations; and explicit benchmark/cache-state labels to performance comparisons. These are backend contracts now; UI implementation remains a later phase.
