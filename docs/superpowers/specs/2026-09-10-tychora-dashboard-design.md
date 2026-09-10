# Tychora — dashboard design

Date: 2026-09-10
Status: review draft; interface implementation follows the application services
Depends on: [Application design](2026-09-10-tychora-design.md) and [Engineering contracts](2026-09-10-tychora-engineering-design.md)

## 1. Purpose

The dashboard makes a local knowledge workspace understandable. Its central tasks are to see what is indexed, explain why a result appeared, inspect the original evidence, resolve ingestion problems, and compare measured retrieval behavior.

This is an inspection and operations interface. Search is an evidence workbench with a query input and saved runs. It does not require chat bubbles, streaming generated prose, an assistant persona, or an LLM-written answer. Query histories represent independent retrieval runs, not inferred conversations.

The dashboard runs against the versioned HTTP API. It contains no ingestion, ranking, graph authorization or evaluation logic duplicated from the backend. The Python API, CLI, MCP and dashboard should explain the same result identically.

## 2. Navigation and information hierarchy

Use a persistent workspace/collection selector and six destinations: Search, Sources, Jobs, Evaluations, Activity and Settings. Source relationships are available from evidence/source detail and within Search, rather than as a disconnected full-corpus graph. A compact global status indicator links to active jobs and actionable failures.

Search is the default page when evidence is available. An empty workspace starts with a short explanation and Add sources. Navigation labels use the registered human-readable kind labels; implementation names appear only in diagnostic details where they help explain a decision.

Core browser routes:

| Route | Purpose |
| --- | --- |
| `/search` | New query and evidence results |
| `/runs/:runId` | Run metadata, with query/trace/result inspection when retained |
| `/sources` | Source inventory and coverage |
| `/sources/:sourceId/revisions/:revisionId` | Versioned evidence, assets and extraction diagnostics |
| `/jobs/:jobId` | Ingestion/rebuild/evaluation progress and recovery |
| `/evaluations/:evaluationId` | Metrics, cases and configuration comparison |
| `/activity` | Query/job history and operational analytics |
| `/settings` | Profiles, kinds, providers, storage health and history policy |

Stable object IDs support deep links. Ordinary navigation URLs do not embed queries, passages, source text or secrets. Full query text is not written into the browser URL/history merely to make a search shareable.

## 3. Search and evidence workbench

At the top, provide a question/passage field, selected collections and an optional image input. Show common filters as compact controls populated from discovery: kinds, dates, revision selection and review state. Advanced settings are in a secondary panel. The interface explains the active retrieval profile and offline/provider choice before a user submits potentially sensitive content.

The main result area has three coordinated regions: result list, evidence reader and collapsible details. On narrower screens these become navigable panels with state preserved. A result row shows title, source kind/role, revision, exact locator, short excerpt and why it matched. Dense technical scores live in details; a score is not presented as a probability that the answer is correct.

For code, show separately labelled parent-declaration/import excerpts and structural selection reasons; a sibling returned as matched evidence remains distinct from context-only declarations. Each excerpt keeps its own exact source span.

The evidence reader shows the complete returned segment, visually distinguishing directly matched units from added context. Original quotations are distinguishable from generated descriptions and OCR. A citation control copies a stable citation with source/revision/evidence IDs. Users can export the complete response as JSON or the deterministically formatted agent envelope.

For PDF evidence, open the original page with highlighted bounding boxes when available; label physical page and printed page separately. For a figure, show the source crop, caption and surrounding passage together, with full-page access. For code, show syntax-highlighted source at the indexed snapshot with line numbers, symbol and revision/dirty-snapshot status. For DOCX without reliable page geometry, show structured source content and original-file access rather than invented page numbers.

Selecting a result can open Relationships: a bounded local graph plus a synchronized accessible edge list. Relation type, direction, provenance, confidence and revision scope are visible. Expanding one node is an explicit bounded request; do not fetch the entire corpus graph. Distinguish declared source links from parser-inferred or model-proposed links. No graph animation or layout should obscure selection and reading.

### Explain a query

The run-details panel shows snapshot, policy/lexical-statistics epochs, enabled/completed channels, candidate counts, retained/excluded counts, stage timings, models and response budget. Original scope/filter values appear for live results or retained payloads; metadata-only history displays their fingerprints. Detailed candidate explanations are permission-scoped. Excluded-source names, snippets or hidden graph neighbours are not leaked through debug counts or explanations.

States distinguish no matches, incomplete channel coverage, missing configured index, unknown dates and unavailable original artifacts. Provide a relevant action: relax a visible filter, inspect coverage, resume an index job or register the missing artifact. The application does not suggest increasing access privileges as a generic fix.

## 4. Sources and revisions

The inventory is a sortable/filterable table with source title, kind, collection, current revision, extraction status, text/image/graph coverage, review state and last update. Coverage uses counts plus labels such as 8 of 10 eligible figures indexed; a single green status must not imply every stage or asset is complete.

Source detail contains Overview, Revisions, Evidence, Assets and Diagnostics tabs. Revision comparison distinguishes source changes from changes in extraction/model configuration. It exposes immutable source hashes, available dates and policy, while avoiding raw filesystem paths unless explicitly requested by the local user. Multiple revisions alone do not imply conflict. Code diagnostics expose language/dialect support, syntax coverage and resolved/ambiguous/unresolved reference rates with denominators and scope limits; a resolved name is not presented as a verified edge.

Add sources supports selected files or a registered local source root. A browser may upload chosen files, but it cannot browse arbitrary server paths. Before queuing, show recognized formats, suggested kind, metadata requirements, destination collection, processing mode and external providers that will receive content. Values supplied by the user stay explicit; suggestions do not silently promote a document's trust status.

The result of Add is a job receipt. The source remains visible as pending while prior active evidence stays usable. Repeating the action with the same idempotency key returns the existing job. A multi-source submission shows each source job's success/failure and supports retrying failed stages. This UI submission is distinct from an immutable index batch; each snapshot publication remains atomic. Removing a source first withdraws it from retrieval; physical artifact deletion is a separately explained destructive action.

## 5. Jobs and recovery

Display pipeline stages and actual completed/total units when the total is known. Use an indeterminate state for discovery or external work with no reliable total. Separate waiting for workspace ownership, parsing, normalizing, embedding, graph indexing, verification and activation; users should see the cause of a stall.

A job detail page shows source scope, snapshot target, configuration fingerprint, elapsed time, reusable artifacts, warnings and typed failures. Retry resumes from the last valid checkpoint. Cancellation is cooperative and the UI explains when already-running work is finishing safely. Source data and the previous active snapshot remain available after failed or cancelled indexing.

Server-sent events have durable monotonically increasing per-job sequence IDs. Reconnection requests events after the last acknowledged ID; duplicate events are harmless. A disconnected browser does not cancel a background job. Refreshing reconstructs the page from persisted job state, not the browser's memory. Owner-service restart exposes interrupted jobs as resumable, rather than indefinitely running.

## 6. Evaluations

Show a list of evaluation runs with dataset version, split, corpus snapshot, retrieval configuration and completion status. Compare only compatible runs by default; check dataset/split, snapshot, policy/lexical-statistics epochs and projection availability, and label differences explicitly.

A comparison view includes retrieval quality, integrity failures, visual/graph metrics and cost/performance in distinct groups. Display changes against the selected baseline and link every aggregate to its cases. Never collapse integrity failures, recall and latency into one unexplained quality score. Code views separate file and file-qualified-symbol metrics, needle/exploratory cases and required-flow completeness under a fixed token budget.

Case detail shows the query when retained, expected/forbidden evidence IDs, retrieved rank order, exact source reader, relation paths, and why an assertion failed. Include the distinction between a correctly retrieved source and a sufficiently complete segment. Support human relevance labels with author/time and label provenance; editing a label creates a new dataset revision.

Graphs/plots have tabular alternatives and exportable data. Synthetic/offline-provider runs carry a visible label and do not imply semantic-quality validation. Runtime metrics specify corpus size, concurrency and hardware context. Performance comparisons label cold owner, warm resources, warm query embedding and warm result separately, with cache hits and provider-call counts. Warn when benchmark states differ.

## 7. Activity and analytics

Activity separates query runs from ingestion/maintenance/evaluation jobs. Filter by time, status, collection and configuration. Default persisted query history is metadata-only: timings, filter fingerprints, policy/lexical-statistics epochs, model/config fingerprints and evidence IDs, with no question or excerpt. Such a row says Query text not retained and cannot offer exact replay.

When full local history is enabled, expose the retention period and a purge action. Replay requires a retained request, current authorization and available/rebuildable snapshot projections. An old manifest may still exist after its graph generation was collected. If dependencies are unavailable, explain them and offer an explicitly labelled new run against the current snapshot where permitted. The view never reconstructs supposedly original text from a hash or result snippets.

Analytics covers p50/p95 stage and total latency, no-match/partial-result rate, ingestion throughput, cache reuse and source/index coverage. Provider token/image usage is displayed when reported. Monetary cost is shown only when pricing data and units are explicitly configured, labelled with the pricing date; absent prices mean unknown cost, not zero. Aggregate counts observe caller access scope.

No query telemetry is sent to a hosted service by default. Browser caching of query bodies and original artifacts is disabled unless a separate explicit local-storage feature is introduced.

## 8. Settings and discovery

Settings groups workspace profile, semantic kinds/metadata, processing providers, storage/index health, and history. Each effective setting shows its origin: default, preset or workspace override. Secrets are entered/referenced through a dedicated control and never redisplayed in API responses or configuration exports.

Changing an embedding model/dimension, parser or graph representation shows the affected extraction, embedding family/batches or graph generation and estimated work if measurable. A saved change queues a rebuild and keeps the previous snapshot active until verification. It never relabels old vectors as if a new model had produced them.

The kinds view is usable documentation: descriptions, required metadata, example sources, typical queries and permitted roles. Zero-source kinds remain discoverable. Unsupported optional capabilities explain installation/configuration requirements without implying they are already available.

## 9. Backend contracts required before UI implementation

Use schema-versioned DTOs shared with/generated from the public contracts. Required endpoints include:

- Capabilities, kinds, filter schema, collections and scoped coverage.
- Source registration/list/detail, revision inventory, withdrawal and artifact inspection.
- Query execution and query-run detail with optional authorized diagnostics.
- Exact evidence/asset retrieval and bounded relation neighbourhood/path inspection.
- Job submission/status/cancel/retry and reconnectable event streams.
- Evaluation submission/list/detail/compare, case inspection and versioned label changes.
- History policy, purge operations and scoped aggregate metrics.
- Effective configuration, provider/index health and authenticated settings changes.

Pagination is stable and bounded. Responses distinguish inaccessible from missing objects without revealing hidden metadata. Error envelopes contain a machine code, short user-facing explanation, retryability and permitted next action. Jobs and runs include workspace/snapshot/configuration identity; query/evaluation runs also carry policy and lexical-statistics epochs for traceability.

Job events contain schema version, per-job sequence ID, timestamp, job ID, stage, state, optional completed/total counts and diagnostic references. Query traces contain stage names/durations/counts, model/configuration fingerprints and policy/lexical-statistics epochs; request/input payloads follow optional retention policy. Both event and snapshot endpoints are required because events alone are insufficient for reconnect and recovery.

The frontend can be a small TypeScript/React application consuming HTTP and SSE. Backend DTO generation and contract fixtures prevent drift. The [Code retrieval profile](2026-09-10-tychora-code-retrieval-design.md) defines these additional code-context, diagnostic and benchmark fields. The frontend is built separately in CI; its compiled static resources can ship inside the wheel/binary, so Python API/CLI/MCP use does not require a Node.js runtime. Kind/relation/mode settings and previewed output use the [configuration schemas](2026-09-10-tychora-configuration-design.md); package resources and eventual bundled static assets follow the [distribution contract](2026-09-10-tychora-distribution-design.md). Final visual styling and component selection happen in the UI implementation phase, without changing the workflows or data contracts in this specification.

## 10. Accessibility, security and interaction quality

All primary flows work by keyboard with visible focus and labelled controls. Status is conveyed with text as well as color. Dense tables support sensible reading order; graph relationships always have a list representation. Respect reduced-motion settings. Evidence readers keep text selectable and citation actions discoverable.

Handle desktop split panes and small screens with equivalent inspection functions. Loading is localized to the changing region; an updating job cannot reset an evidence reader's scroll or selection. Filters can be cleared individually, and errors preserve the user's input.

Render imported HTML/Markdown as untrusted content with script execution disabled and external-resource loading controlled. Do not automatically open source links, execute code, follow file URIs, or fetch remote assets embedded in retrieved documents. HTTP origin/authentication and artifact containment checks are enforced by the backend. The UI does not determine access privileges.

## 11. Acceptance journeys

1. Add a PDF with a table and figure, observe progress, search a passage, open its original page and export the citation.
2. Index a small code repository, find a symbol, inspect its indexed revision and traverse a documented import/call relation.
3. Interrupt indexing, continue using the previous active snapshot, retry only incomplete work, and observe safe activation.
4. Query with a restrictive date/kind filter, understand a no-match or missing-coverage result, and adjust only that filter.
5. Inspect graph evidence with hidden neighbours and verify that neither nodes, paths nor diagnostics reveal inaccessible material.
6. Compare two retrieval configurations on one dataset, open a regressed case and review exact expected/retrieved evidence.
7. Browse metadata-only query history, enable full local retention deliberately, replay an eligible run and purge it.
8. Complete source discovery, search, original inspection and job recovery using only keyboard navigation.

Before delivery, validate these journeys through real HTTP against the application service, then browser integration tests and human visual/accessibility review. Mock-only dashboards do not satisfy the data, recovery or evidence-inspection requirements.
