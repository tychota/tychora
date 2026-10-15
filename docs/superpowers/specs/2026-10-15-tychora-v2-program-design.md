# Tychora v2 programme

Date: 2026-10-15. Status: design, awaiting review.

This document is the roadmap for the next generation of Tychora. It records what the product owner wants, the decisions already taken, how the work is split into sub-projects, and how success is measured. Each sub-project has its own design document, implementation plan and review cycle; this document only fixes what they share.

## 1. Intended outcome

Tychora should be a local evidence engine that a person can fill with real material (books, regulations, code, scans, diagrams) and query in under a second, with citations that point to the exact place in the original. Its dashboard should let a non-specialist add documents, ask questions, see why an answer was chosen and what it cost, without reading JSON.

What the product owner said, in their words where possible:

- Fix the engine findings of the 2026-10-15 ingestion and query flow review (`docs/superpowers/reviews/2026-10-15-ingestion-query-flow-review.md`, called "the flow review" below) and track the work as superpowers specifications.
- Queries under one second; ingestion under 30 seconds in 90% of cases.
- No arbitrary chunking. Chunks must be cut properly (by structure, then by meaning) and embedded contextually with Voyage; small fixed chunks are a problem for big books.
- Use Docling "to the max", with a lot of enrichment.
- Modern defaults: Voyage `rerank-3`, Voyage 4-generation contextual embeddings, the current multimodal model, Claude Haiku 5.5 for vision.
- A small set of document-kind presets (code, law and norms, a tax and administrative compliance use case, books and articles, images) built on a deliberate ontology. Refined by the decisions below into five presets, with images as a kind rather than a preset.
- A much simpler dashboard with visualisations (graph, trace) and advanced functions (tracing, cost management).
- A big clean-up: clean infrastructure, no duplicated code (measured, e.g. jscpd), accurate and descriptive comments, good tests.
- Tychora must be able to replace two earlier projects of the product owner: a code-knowledge MCP server (code ontology, graph queries and visualisation) and a contextual-RAG proof of concept for a tax and administrative compliance practice (authority and internal lanes, confidentiality and clearance, query modes, yearly editions of guides). Their capabilities are inventoried privately; the public specifications describe them generically and record parity in each owning sub-project.

What this document assumes and the product owner may correct:

- Tychora remains alpha. Breaking the stored format is acceptable; existing workspaces are rebuilt from their retained originals.
- The identity, provenance, upload-consent and offline-first principles in `HANDOFF.md` remain non-negotiable.
- The public repository must never contain private corpora or customer context; examples and benchmarks use public or synthetic material.

## 2. Decisions already taken

| Topic | Decision | Source |
| --- | --- | --- |
| Compatibility | New workspace format; old workspaces are rebuilt (`manage rebuild` from retained originals); old test workspaces may be discarded. No dual-format code. | Product owner |
| Engine approach | "Storage core v2": redesign the hot layer (snapshot manifest, publication, eligibility, read connections, vector batches) behind the existing contracts; keep what the flow review lists under "What to keep". No full rewrite. | Proposed, accepted |
| Chunking | Structure first (section → paragraph, never crossing a section), then semantic cuts inside a section where adjacent-paragraph similarity drops; soft size about 300–1,500 tokens, hard maximum 2,000; the section is the `voyage-context-4` context group; sizes tunable per preset and validated on an evaluation set. Voyage auto-chunking is rejected because it returns no character offsets. | Product owner |
| Performance measurement | A pinned benchmark corpus (about 1,000 documents, about 50,000 chunks, public and synthetic). Query p95 < 1 s warm including Voyage embedding and reranking; local work alone < 300 ms. Ingestion p90 < 30 s per document for native PDFs up to about 100 pages; scanned documents and books reported separately with a per-page budget. | Product owner |
| Docling enrichment | Tiered and rich by default: always-on local enrichments; Haiku picture descriptions when online and the collection consents; formula and code enrichment per preset; heavy enrichment runs as a background phase after the document is searchable. | Product owner |
| Ontology | About twelve abstract kinds in a single-parent tree with SKOS-style `broader` and external mappings, expressed in TOML; not OWL2. Facets carry jurisdiction, issuer, binding level, origin, confidentiality and bibliographic type. About five presets. | Proposed, product owner: "let's try" |
| Dashboard | Five sections: Home, Library, Search, Insights, Settings. JSON only behind one "Developer details" toggle per page; one primary action per page. Visualisation stack researched before design. | Product owner |
| Code health | A baseline of automated quality gates before engine work, then a ratchet: every sub-project leaves each metric equal or better. | Product owner |
| Context unit | The `voyage-context-4` context group defaults to the top-level chapter; each kind declares its own context level (legislation: article or annex; standard: clause; code: file). | Product owner |
| Chunk cut detector | A pinned local static model (`potion-multilingual-128M`) scores adjacent paragraphs; `voyage-4-nano` is evaluated as an alternative. | Proposed, accepted after research |
| Embedding options | `voyage-context-4` remains the default text family. An optional "fast/offline query" family embeds documents with `voyage-4-large` and queries locally with the open-weight `voyage-4-nano` (same embedding space). Matryoshka dimensions and quantization are per-family options chosen by evaluation. `voyage-code-4` for code. | Product owner |
| Derived versus source text | Text reconstructed from a document's own content (tables merged across pages, reading order) is citable source text; only model-generated text (OCR, formula and code enrichment, picture descriptions) is derived and needs review. | Product owner |
| Presets | general (default), software, regulatory-normative, regulatory-administrative, knowledge; plus an example domain pack (quality improvement) showing how to build a custom ontology. No separate images preset: `visual` is a kind in every preset. | Product owner |
| Graph freshness | Incremental graph writes at publication (no full rebuild), with background compaction. | Product owner |
| Networked stores | Design engine core v2 so PostgreSQL, Qdrant and Neo4j adapters are possible (store-agnostic repository layer and contracts); implement them in a later sub-project 7. | Product owner |
| Graph visualisation | No Cytoscape: an own rendering, constraint-layout and edge-routing layer. | Product owner |
| Dashboard scope now | UX only: structure, flows, copy, interaction and visualisation behaviour. Fonts, colours and visual style are a later pass with the impeccable skill. | Product owner |
| Immediate fix | Neighbour context and adjacent-result merging skip Docling documents (`retrieval/segments.py`); fixed test-first before sub-project 2, which keeps the fix. | Product owner |

## 3. Sub-projects

| # | Sub-project | Design document | Depends on |
| --- | --- | --- | --- |
| 0 | Code health baseline | `2026-10-15-code-health-design.md` | — |
| 1 | Engine core v2 (with query trace and usage ledger) | `2026-10-15-engine-core-v2-design.md` | 0 |
| 2 | Chunking v2 | `2026-10-15-chunking-v2-design.md` | 1 (boundary keys, linear windowing, format) |
| 3 | Model defaults | `2026-10-15-model-defaults-design.md` | 1 (usage ledger), 2 (chunk size drives rerank limits) |
| 4 | Docling enrichment | `2026-10-15-docling-enrichment-design.md` | 1 (background phase, format), 2 (sections), 3 (Haiku vision) |
| 5 | Kind ontology and presets | `2026-10-15-kind-ontology-design.md` | 1 (format), 4 (per-preset enrichment), 2 (per-preset chunk sizes) |
| 6 | Dashboard v2 | `2026-10-15-dashboard-v2-design.md` | 1 (trace, usage, aggregates, dashboard endpoints), 3 (price table, model registry), 5 (presets), 4 (enrichment status) |
| 7 | Networked stores (PostgreSQL, Qdrant, Neo4j) | Later; no design document yet | 1 (repository layer and store-agnostic contracts) |

Order of delivery: 0, 1, 2, 3, then 4 and 5 in either order, then 6. Sub-project 6 may start its static design and component work after sub-project 1 has fixed the trace and usage contracts, using recorded fixtures.

## 4. Contracts shared between sub-projects

These interfaces are owned by sub-project 1 and consumed elsewhere. Their exact schemas live in the engine core design; this section fixes their purpose so other designs can depend on them.

- **Boundary keys.** Every normalizer emits a `boundary_key` naming the enclosing section, article, table or file, never the individual paragraph (flow review D5). Chunking v2 groups and cuts within a boundary.
- **Query trace.** A versioned, scope-filtered trace attached to every query run: stages (eligibility, each channel, fusion, rerank, assembly) with start, duration and candidate counts, plus per-result rank and score per channel before and after fusion and reranking. Stored with run history under the existing retention rules; returned on request.
- **Usage ledger.** One row per provider call: time, provider, model, operation (embed, rerank, describe, OCR), units (tokens, images, pixels, pages), latency, outcome, collection, job or query reference. Cost is computed from a versioned, editable price table, never stored as the only truth.
- **Aggregates.** Bounded per-collection and per-document summaries (status, counts, coverage, chunk-size distribution) maintained at publication time, so dashboards never scan the corpus.
- **Background phases.** A document becomes searchable after its core phase; enrichment phases (picture description, formula/code enrichment) publish later as new derived evidence without changing original identities.
- **Benchmark corpus and harness.** A pinned, reproducible corpus and a measurement command shared by sub-projects 1–4; results are recorded per commit.

## 5. Success criteria for the programme

1. On the benchmark corpus: query p95 < 1 s warm (local share < 300 ms); ingestion p90 < 30 s per native PDF up to about 100 pages; no workspace size ceiling below 10,000 documents (the 16 MiB manifest ceiling observed on 2026-10-15 is gone).
2. Default strict search succeeds on ordinary queries over large corpora (the flow review's "verification outcome" probe and the 2026-10-15 real-PDF probes pass).
3. Retrieval quality on the evaluation set is equal or better than today at every stage, measured with the existing evaluation machinery, and chunk sizes are justified by those measurements.
4. A first-time user can add a PDF, see it become ready, ask a question, open the cited passage and see the query's cost and trace from the dashboard without opening developer details.
5. Every must-have capability of the two replaced projects (section 1) is present, with its owning sub-project recorded in that sub-project's design and verified by a test or a documented manual check.
6. Code-health gates are green and each metric is at or better than its baseline: duplication, layering contracts, function size, dead code, documentation rules, coverage and mutation score on core modules, and a default test suite that is green on a clean machine.

## 6. Non-goals

- A hosted or multi-user service. Networked stores are designed for in sub-project 1 and implemented in sub-project 7, outside this programme's first delivery.
- Generated answers. Tychora returns evidence; the calling agent writes the answer.
- OWL reasoning or a general ontology editor.
- Live provider qualification of model quality beyond the project's own evaluation set.

## 7. How the work is tracked

Each sub-project follows the superpowers cycle: design document (this directory), review by the product owner, implementation plan (`docs/superpowers/plans/`), execution with per-task reviews, and a final verification against its own success criteria plus the code-health ratchet. Progress is visible from the plans' checkboxes and commit history. Findings from the flow review are referenced by their identifiers (D1–D8, C1–C5, Q1–Q6) in the design that resolves them; the engine core design lists which identifiers it closes.
