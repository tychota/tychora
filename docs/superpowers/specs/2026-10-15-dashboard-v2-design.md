# Dashboard v2

Date: 2026-10-15. Status: design, awaiting review. Sub-project 6 of the Tychora v2 programme (`2026-10-15-tychora-v2-program-design.md`). Depends on engine core v2 (query trace, usage ledger, aggregates, background phases), model defaults (price table, provider usage), kind ontology and presets (search modes, access labels) and Docling enrichment (enrichment phase status).

This sub-project is UX only: structure, flows, copy, interactions and the behaviour of visualisations. It does not change fonts, colours or visual style. New components reuse the current stylesheet's values as they are. A later visual pass, run with the `impeccable` skill, owns the design tokens, typography, colour palette, chart palette and dark mode. Section 4.6 states the hand-off that keeps that pass a restyle rather than a rewrite.

## 1. Intent and success criteria

The product owner's verdict on the current dashboard: "a lot of buttons everywhere, a lot of JSON, unclear instructions and a lot of spoilers." The intent is a dashboard a non-specialist can use to add documents, ask questions, check the cited place in the original, and understand why a result ranked and what it cost, plus the graph and index views needed to replace the two earlier projects.

| ID | Criterion | How it is measured | Programme criterion |
| --- | --- | --- | --- |
| U1 | A first-time user adds a PDF, sees it become Ready, searches, opens the cited passage highlighted on the PDF page, and sees the query's trace and cost without opening Developer details. | Playwright journey `first-run.spec.ts` on the model-free server; the test fails if any Developer details toggle is opened or any `<pre>` is visible. | 4 |
| U2 | Navigation has exactly five sections; each page has at most one primary button and at most two disclosures (one Developer details plus one page-specific "Options"). | Vitest structural test over every route: count of `.button-primary` ≤ 1, count of `details`/disclosure buttons ≤ 2. | 4 |
| U3 | Raw JSON is rendered only inside the `DeveloperDetails` component. | Source test: `JSON.stringify` and `<pre>` appear only in `components/DeveloperDetails.tsx` and `api/`. | 4 |
| U4 | Feedback within 400 ms (Doherty): every route shows a skeleton within 100 ms of navigation and every action shows a pending state within 100 ms. The initial route bundle is at most 120 KB gzip; graph and PDF code load only when used. A graph of 300 nodes and 100 edges is laid out and routed in the worker within 400 ms, and the page stays responsive meanwhile (no main-thread task above 50 ms). | Playwright timing on fixtures; `check:bundle` script reading Vite's manifest; a graph benchmark on synthetic fixtures of 100, 300 and 800 nodes. | 1 (perceived) |
| U5 | Zero serious or critical axe-core violations on every page in the current (light) theme; every chart has a table twin; every interactive visual is keyboard operable. The dark-theme sweep is added by the visual pass. | `@axe-core/playwright` sweep; unit tests per chart for the twin. | 4 |
| U6 | Each dashboard-owned capability of the replaced projects is present: compound code graph, symbol neighbourhood and impact view, index-status header, search modes (research, draft, audit, repair, lookup), as-of search with visible edition, review-required handling, visible access level, per-hit channel scores. | One named test per capability (section 6, parity table). | 5 |
| U7 | Code health: no dashboard source file above 300 lines; new components contain no colour, font or size literals (they read the CSS custom properties of section 4.6); dashboard duplication at or below the code-health baseline. | jscpd, a line-count test, a literal grep test scoped to new component files. | 6 |

## 2. Scope and non-goals

In scope: information architecture, page content and states, flows, copy and status vocabulary, interaction design, all visualisations listed in section 4.4 (including the in-house graph stack of 4.4.1), the document viewer, guided model setup, the data contracts the dashboard consumes, fixture-based development, removals, and test migration.

Non-goals:
- Visual style. Fonts, colours, spacing scale, radii, elevation, the chart palette, a design-token system and dark mode belong to the later visual pass with the `impeccable` skill. This sub-project neither changes them nor adds a theme switch.
- Generated answers or chat (programme non-goal). Search shows evidence.
- Any new authority or credential model. Launch links, remember-this-browser and token rotation stay exactly as implemented (`docs/dashboard.md`, "Start and connect").
- A corpus-wide graph of more than a few hundred nodes, embedding-space scatter and an edition diff view in the MVP (phase 2, section 8).
- Defining trace, ledger or aggregate schemas. Engine core v2 owns them; this document states what the dashboard reads.
- A charting, graph or component library dependency. Charts are custom SVG on two small d3 modules; the graph uses an in-house renderer and router on one constraint-layout library (4.4.1).

## 3. Current state

- **Structure.** Nine navigation entries (`dashboard/src/App.tsx:25-35`). Large pages: `pages/Search.tsx` 895 lines with nine buttons, `pages/Documents.tsx` 506, `components/EvidenceReader.tsx` 471. Runtime dependencies: React 19.2 only.
- **JSON and disclosures.** `components/JsonDetails.tsx` is used about 45 times in 20 files; 15 `<details>` disclosures, four in `Search.tsx`. The document detail expands into the raw source record (hashes, `processing_state`, `channel_readiness`).
- **Status that says nothing.** Every document shows "Processing: unknown" because the field is a constant (`src/tychora/contracts/collections.py:77`) and `latest_job` is always `None` (`collections.py:82`). Sizes are raw bytes, types `application/octet-stream`, timestamps ISO microseconds.
- **Explore** asks for a seed from a dropdown, prints its hash, exposes four technical controls, answers a fresh workspace with the raw code `INDEX_INCOMPLETE`, and draws no graph (`components/Relations.tsx` renders chips). The contract allows `max_hops ≤ 2`, `max_edges ≤ 100` with a cursor (`contracts/operations.py:296-308`).
- **Copy** hedges ("Retained lineage and channel observations do not establish current job state…"); `docs/dashboard.md` shares the register.
- **Missing data.** The query trace is never populated (flow review C5). Run telemetry is `RunMetrics` (`duration_ms`, `candidate_count`, `cache_hit`; `adapters/sqlite/history.py:26-35`). No usage or cost is recorded. Available: job events with `timestamp`, `stage`, `state`, counters (`contracts/operations.py:322-329`); PDF `physical_page`, `page_width`, `page_height`, `raw_bbox` (`contracts/documents.py:90-93`); repository commit and dirty flag (`ingestion/acquire.py:371-387`).
- **Styles.** `styles.css` is 1,434 lines with two conflicting `:root` blocks (lines 2-15 hex, 938-950 OKLCH), 41 hex literals, no dark mode. Typography is offline system fonts (Iowan Old Style / Palatino headings, Avenir Next / Segoe UI text, SF Mono / Consolas code). This sub-project leaves all of it as it is; the visual pass reworks it.
- **Earlier graph experience.** The product owner has used a Cytoscape view with the fcose layout on a code graph (files as compound nodes holding symbols). Highlighting worked well; node placement and edge routing did not: groups overlapped or sprawled, and edges crossed through unrelated files. Section 4.4.1 is designed against those two failures.

## 4. Design

### 4.1 Information architecture

Five sections in a left sidebar (a top bar below 768 px). A profile menu at the bottom holds remember/forget, disconnect and new-launch-link help (a theme choice is added by the visual pass), replacing the "Workspace identity" disclosure. One compact scope control ("All collections ▾") in the header of Library, Search and Insights replaces the full-width collection fieldset; it shows access labels and hides archived collections unless asked.

| Section | Purpose | Primary action | Replaces |
| --- | --- | --- | --- |
| Home | Is my knowledge base healthy and what needs me? | Add documents | Collections landing |
| Library | What is in it, is it ready, what does it say? | Add documents | Documents, Sources, AddDocuments, Jobs (inline), Collections (as filter) |
| Search | Find evidence and see why it ranked | Search | Search, Activity (as History) |
| Insights | What does it cost, how fast is it, how is it connected? | Run evaluation (Evaluations tab only) | Explore, Evaluations, Activity analytics |
| Settings | Models, access, data, this browser | Apply changes | Models, Settings, Collections administration |

Routes: `/dashboard/{home, library, library/{source_id}, library/add, search, search/runs/{run_id}, insights/{usage|performance|ingestion|graph|graph/symbol/{unit_id}|evaluations}, settings/{models|access|data|browser|advanced}}`. Every old route redirects in `routing.tsx`; a durable job link printed by the CLI opens the Library drawer of the job's source.

### 4.2 Pages

**Home.** Five stat cards: Documents, Ready (%), Needs attention (links to filtered Library), Last added, Cost last 7 days (links to Insights). A "Needs attention" list (up to five, each with a one-sentence reason and one action: Retry, Allow provider, Review). "Recent searches" (up to five). With no documents, Home shows a three-step checklist "Choose a model → Add a document → Ask a question", ticking steps already done: the first-run path of U1.

**Library.**
- Table: Title, Collection, Kind, Status, Pages, Size ("10.5 MB"), Added (relative; absolute on hover). Types by name ("PDF", "Word", "Code repository"; octet-stream shows as "File"). Titles, never IDs. Filter chips for status, kind and collection; the text filter searches titles on the server, not only loaded rows.
- A row opens a right drawer (table plus flyout) with three tabs: **Read** (document viewer, 4.4.6), **About** (one facts card: kind, edition, published and in-force dates, access level, location, pages, size, versions; "Update from source"; "Remove from library" alone at the bottom in danger style), **Connections** (compact graph, 4.4.1). For a code symbol, Connections opens on its neighbourhood (4.4.7).
- Active jobs show "Processing · 40%" inline from the job stream (`api/events.ts` `watchJob`) and end "Ready — try a search", linking to Search scoped to that document (Peak–End). Repository sources show the index-status header (4.4.5).
- **Add documents** is one drop-zone wizard replacing `AddDocuments`, `SourceForm` and `PdfIntakePanel`: drop files, paste text, or give a folder, repository or URL; pick a collection (default last used, "New collection" inline) and kind (auto-detected among the preset's offered kinds, with only its required fields asked and defaults prefilled; the `general` preset, the default, requires only the title). It then uploads and analyses without asking and stops only for a real decision: scanned pages ("12 of 287 pages are images. Read their text locally with OCR?", default yes), missing provider consent, a file over the limit, or a blocked model. The server's PDF receipt → analyse → confirm protocol (`/v1/pdf-intakes/*`) is unchanged; the wizard confirms the analysed policy itself when nothing needs deciding.

**Search.**
- One query box and a mode switch whose options are the active preset's registered modes (`/v1/discovery`). The presets are `general` (default), `software`, `regulatory-normative`, `regulatory-administrative` and `knowledge` (kind ontology). The switch is hidden when the preset registers one mode. The `regulatory-administrative` preset registers the five below, and the other presets a subset; the mode decides the input:

| Mode | Input | Request mapping |
| --- | --- | --- |
| Research | Question | `input.type=question`, mode `research` (authority lane only) |
| Draft | Question plus optional section, year, project facets | mode `draft` (`compose` in presets that do not register `draft`), facets as filters |
| Audit | A pasted passage | `input.type=passage`, mode `audit` |
| Repair | A pasted passage plus finding IDs | mode `repair`, finding IDs as `exact_identifiers` |
| Lookup | An identifier (article, rule ID, case number) | mode `lookup`, `exact_only=true` |

- One "Options" disclosure: As of (`QueryRequest.as_of`), Published before (`cutoff`), Include passages that need review (switches to the registered inclusion mode, as today), Result count.
- Above results, a one-line **coverage summary** from `coverage` and `coverage_gaps`: "Evidence found in Authority and Standards. Nothing found in Feedback." Decision requirements appear as a **Decision needed** panel: "Two editions apply on 2024-03-01. Pick one to continue."; picking one reruns with that revision.
- Each result is a citation card: title, edition badge ("2024 edition"), locator ("p. 12 · §3.2"), excerpt with the match highlighted, lane, and badges only when they matter: "Needs review" (not citable; "Copy citation" becomes "Copy reference") and the access label when above public. "Open" opens the Library drawer at the passage; "Why here?" shows the hit's per-channel scores, fused rank and rerank score.
- Tabs: **Results**, **Trace** (4.4.2), **History** (replay only where the request was retained). The run header always shows "0.42 s · $0.0004 · 2 provider calls".
- The structured `QueryRequest` editor is removed; Developer details shows the submitted request and response read-only, with "Copy as curl".

**Insights.** A date range (7 days default) and five tabs: **Usage & cost**, **Performance**, **Ingestion** (4.4.3–4.4.4), **Graph** (4.4.1), **Evaluations** (existing list, comparison and form, restyled with comparison bars; primary action "Run evaluation").

**Settings.**
- **Models**: one card per role (text embeddings, image embeddings, reranking, picture descriptions, OCR) with provider, model, credential reference (`env:`/`secret:` only) and a **readiness checklist**: package installed, assets verified, credential reference set, collection consent granted, connection tested. "Test connection" (4.5) answers "Connected · 180 ms" or the provider error in one sentence. Edits collect in a draft; "Apply changes" shows the preview (changes, blockers, "requires rebuilding the text index, about 4.1 M tokens, about $0.49") and activates on confirm.
- **Access**: per collection, access label, archive state and a consent switch per provider; consent text names what is sent and whether batch processing keeps inputs at the provider (model defaults, open question 3).
- **Data**: backup, restore into a separate destination, retention with destructive confirmation.
- **This browser**: launch links, remember/forget, disconnect, `manage rotate-token`, as implemented.
- **Advanced**: TOML editor, price edits (new `user` price version through `PUT /v1/prices`), compatible-channel rebuild, health report.

### 4.3 Copy, status vocabulary and states

**Copy rules.**
1. Every instruction is one plain sentence in the second person stating what to do or what happened: "Drop PDFs, Word files or text here." Not what the system cannot guarantee.
2. Facts are humanised: sizes in KB/MB, relative times with absolute on hover, media types by name, counts with units.
3. Error text = what happened + what to do, one sentence each, with one action button. Error codes appear only in Developer details.
4. No IDs or hashes outside Developer details. Where an identity matters for copying, a "Copy ID" icon button carries it.
5. Lane, kind, mode and access names are the preset's labels, in the preset's locale when it declares one; the dashboard's own chrome is English.
6. Limits are stated when hit, not in advance ("This file is 120 MB; the limit is 100 MB.").

**Status vocabulary** (one module, `status/vocabulary.ts`, exhaustively typed over the backend literals). Document status comes from engine core v2's `document_status` (`state`, `phase`, `reason_code`); job state refines it while a job runs.

| Backend state | Label | Colour + icon | Reason line and action |
| --- | --- | --- | --- |
| `queued` | Queued | neutral, clock | "Waiting for another document to finish." |
| `processing` (job `running`) | Processing · n% | info, spinner | Stage in words from job counters: "Reading page 40 of 287" |
| `ready` + phase `enriching` | Ready · improving | success, check with dot | "Searchable now. Picture descriptions are still being added." |
| `ready` + phase `complete` or `core` | Ready | success, check | — |
| `needs_attention` | Needs attention | warning, triangle | Sentence for `reason_code` ("12 pages were read by OCR. Review them before citing."; "Allow Voyage for this collection to finish.") with its action |
| `failed` | Failed | danger, cross | Sentence for the error reason; Retry |
| job `interrupted` | Needs attention | warning, triangle | "Stopped before finishing." Retry |
| job `cancelled` | Cancelled | neutral, slash | "You cancelled this." |
| `removed` | Removed | neutral | Shown only with the "Removed" filter |
| collection archived | Archived | neutral, box | "Hidden from search. Restore to use it again." |
| evidence `needs_review` | Needs review | warning outline | "Read automatically. Check before citing." |
| evidence `rejected` | Rejected | neutral | Excluded from default search |
| evidence `accepted` | Reviewed | success outline | — |
| evidence `unreviewed` | (no badge) | — | — |

Errors map by code and engine core's dotted reason (`ErrorEnvelope` v2), falling back to the code alone: `INDEX_INCOMPLETE` / `channel.unavailable` → "The index is being rebuilt. This view will work when it finishes." (links the job); `PROFILE_INCOMPATIBLE` / `index.rebuild_required` → "These settings need a rebuild of the index."; `WORKSPACE_BUSY` / `owner.live` → "Tychora is finishing another task. Try again shortly."; `PROVIDER_FAILURE` → "Voyage didn't respond. Try again in a minute." Unknown codes read "Something went wrong." with details in Developer details. Colour is never the only signal: every status has an icon and a label.

**States.** One `ViewState` component gives every view four states: loading (layout-shaped skeleton after 100 ms), empty (one sentence plus the page's primary action, never a blank table), error (sentence, action, typed error in Developer details) and partial ("Showing 100 of 1,240. Load more.").

### 4.4 Visualisations

Charts are custom React SVG on `d3-scale` 4.0.2 and `d3-shape` 3.2.0 (ISC, about 21 KB gzip together). Each has `role="img"` with a summary label, a "Show as table" twin over the same rows, a pattern or dash per series in addition to colour (colours read from the custom properties of 4.6), and one tooltip shared by hover and arrow-key focus.

#### 4.4.1 Graph explorer

Decision: **an in-house graph stack in layers (model, layout, edge routing, rendering, focus, accessible twin) with WebCola 3.4.0 as the layout core, run in a Web Worker.** Cytoscape is not used.

Why not Cytoscape. The product owner's earlier Cytoscape and fcose view highlighted well, but placed nodes and routed edges poorly (section 3). Cytoscape's edge styles (straight, bezier, taxi) join endpoints without avoiding other nodes or groups, and fcose treats containment as a soft force, so files drift into one another. Both failures sit in parts Cytoscape does not let us replace, while the part that worked (highlighting) is simple to build. Cytoscape also costs about 134 KB gzip and renders only to canvas.

**Layers.**

| Layer | Module | Responsibility | Runs in |
| --- | --- | --- | --- |
| Model | `graph/model.ts` | Merges relation, neighbourhood and impact responses into one graph keyed by unit ID: nodes, edges, groups (file, document, section or unit; optional outer group by collection or kind), predicate and direction, and `truncated` flags. Expanding adds to the model; it never rebuilds it. | main thread |
| Layout | `graph/layout.worker.ts` | Positions nodes and group rectangles under constraints (below). | Web Worker |
| Edge routing | `graph/route.worker.ts` (same worker) | Turns each edge into a path that avoids groups it does not belong to (below). | Web Worker |
| Rendering | `graph/SvgScene.tsx`, `graph/CanvasScene.ts` | Draws one scene description: SVG up to 300 nodes, canvas above. Pan and zoom, label level of detail, hit testing. | main thread |
| Highlight and focus | `graph/focus.ts` | Computes the emphasised and dimmed sets for hover, selection, path and impact depth; both renderers consume it. | main thread |
| Accessible twin | `graph/GraphList.tsx` | DOM list and keyboard model (below). | main thread |

The worker receives the model as plain data and returns positions and paths, so the main thread never blocks (U4). A newer request cancels the one in flight. The worker has a 400 ms budget at 300 nodes; when a run exceeds 1 s it returns its best positions so far with `converged: false`, and the view shows them without a message.

**Layout core: WebCola versus d3-force.**

| Criterion | WebCola 3.4.0 | d3-force 3.0.0 plus custom constraints |
| --- | --- | --- |
| Licence | MIT | ISC |
| Size | about 23 KB gzip (80 KB minified, whole library including the router) | about 5.5 KB gzip |
| Compound containment | Native: hierarchical `groups` holding leaves and sub-groups, with padding; group bounds are computed | None; needs a custom grouping force, hull or rectangle collision between groups, and bounds computation |
| Non-overlap | Hard constraints solved by projection (VPSC) on node rectangles, including labels | `forceCollide` on circles only; rectangles and labels need custom code; overlap reduces but is not guaranteed |
| Other constraints | Separation and alignment constraints, flow along an axis, disconnected-component packing | None built in |
| Edge routing | Includes an orthogonal grid router aware of the group hierarchy, and obstacle-avoiding spline routing | None |
| Scale | Stress majorisation with a full distance matrix: good below about 100 nodes, usable at a few hundred, slow near 1,000 | Fast at thousands of nodes |
| Maintenance | Last release May 2019; repository still receives commits (2026); about 154,000 downloads a week; about 110 open issues | Stable, last release 2021; part of d3 |
| Worker use | The core modules (`layout`, `vpsc`, `gridrouter`, `geom`) use no DOM; only the d3 adaptors do | No DOM use |

Choice: **WebCola.** The two failures to fix are containment and routing, and WebCola solves containment and non-overlap as hard constraints and ships a group-aware router, where d3-force offers neither and the team would write and maintain a constraint solver. Its scale limit matches the explorer's bounds (requests of ≤ 100 edges, an explorer cap of 800 nodes, SVG up to 300). The maintenance risk is accepted because the package is small, stable, written in TypeScript and free of DOM dependencies in its core: it is pinned at 3.4.0, imported through its core modules only (`webcola/dist/src/layout`, `vpsc`, `gridrouter`, `geom`) so its d3 version 1 adaptor dependencies stay out of the bundle, and wrapped behind `graph/layout.worker.ts` so a later replacement touches one module. If a fix is ever needed, the module is vendored under `dashboard/vendor/webcola/` with its MIT notice.

**Layout constraints.**
- Containment: files contain symbols; documents contain sections, and sections contain units, as nested groups with padding for the group label. Group rectangles never overlap and nodes never leave their group.
- Non-overlap: node rectangles include their labels.
- Grouping: an optional outer level groups files or documents by collection or by kind ("Group by" chip: none, collection, kind).
- Direction: neighbourhood and impact views add a flow constraint along x (callers left of the focus, callees right; impact by depth in columns, 4.4.7). Other views have no flow constraint.
- Stable incremental layout: on Expand, existing nodes start at their current positions and stay fixed for the unconstrained and user-constraint phases; new nodes start beside the node that was expanded. Existing nodes are released only for the final non-overlap phase, which moves each by at most 40 px. Collapsing a group removes its nodes without moving the rest. The same model and options always give the same layout (seeded start positions), so revisiting a view does not reshuffle it.

**Edge routing.** Decision: **orthogonal routing between groups, short direct curves inside a group, no bundling.** Edges between nodes of the same group are drawn as short arcs inside the group's padding, because nothing lies in their way. Edges between groups are routed by WebCola's grid router over the group hierarchy, so they travel in the corridors between files, never across an unrelated file; parallel segments sharing a corridor are nudged apart and corners are rounded. Arrowheads mark direction and every edge keeps its predicate dash.
- Bundling is rejected because merged trunks hide which symbol an edge reaches, which defeats per-edge highlighting and the predicate dash.
- Obstacle-avoiding splines are rejected as the default because, around nested groups, they hug container borders and cross each other at shallow angles, and direction is harder to read. They stay available behind the same router interface for evaluation.
- Budget: when the graph has more than 300 inter-group edges or routing exceeds 200 ms, inter-group edges fall back to straight lines drawn underneath the groups, and selected or hovered edges are routed on demand.

**Rendering.** One scene description (positions, group rectangles, paths, label priority) feeds both renderers. SVG is used up to 300 nodes and makes every mark testable in jsdom; canvas is used above, with hit testing through a spatial index of node and group rectangles. Labels show by priority (focus, then group labels, then high-degree nodes) and by zoom level. Colours, dashes and glyphs come from the custom properties and the predicate table, so the visual pass restyles the graph without touching the layers.

**Highlighting and focus plus context.** Hover or selection emphasises the node, its incident edges and their other ends, and dims the rest to a low opacity without hiding it. Selecting two nodes highlights the shortest returned path between them. Predicate and direction filter chips dim, not remove, so the layout does not jump. A minimap shows the viewport when the graph is larger than the view. Reduced motion replaces animated transitions with an immediate update.

**Seeds and interaction.**
- **Seeds**: a document or search hit ("Connections"), a code symbol (4.4.7), or "Most connected", a paged list of the highest-degree units in scope (4.5).
- **Elements**: for code, files are rounded containers labelled with the path, and symbols are children with a kind glyph (function, class, method, module, constant, interface, test); for documents, documents contain sections and sections contain passages. Edges take a dash and a marker per predicate (calls solid, imports dashed, inherits dotted, supersedes double, contradicts with a cross marker), with a legend; colour is a secondary signal.
- **Interaction**: hover previews title, locator and first lines; click opens an inspector with the passage, its relations and "Expand" (one more hop, `max_edges=100`, merged into the model); "Load more" follows the cursor. When `truncated`, a note says "Showing part of the connections." Past 800 nodes, Expand is disabled with "This view is full. Collapse a group or start from another item."

**Keyboard and list twin.** A DOM list grouped by file or document, then by predicate, is the accessible representation; the SVG or canvas is `aria-hidden`. Arrow keys move a roving tabindex through the list and the view pans to keep the focused node visible and highlighted; Enter inspects, "+" expands, "−" collapses a group, "Escape" clears focus. The list and the drawing share the selection.

**Compact mode** (≤ 30 edges, no filters, no minimap) serves the Library "Connections" tab and the phase-2 search mini-graph.

#### 4.4.2 Query trace waterfall and rank flow

A waterfall: one row per stage (eligibility, each channel, fusion, graph expansion, rerank, assembly) on a shared millisecond axis, candidate counts in and out, provider calls nested with model, units and cost; the header gives total, local share and cost. Below, **rank flow**: for the top 20 results, rank per channel → fused → reranked → final, with gain/loss arrows (MVP table; phase-2 bump chart over the same rows). "Why here?" on a card shows its row.

#### 4.4.3 Cost, usage and latency

Usage & cost: daily cost stacked by provider; a table by model and operation (units, calls, failures, cost); top collections; the price table with effective dates. The server computes cost from the ledger and the price row effective at each call (model defaults 4.6); the dashboard never multiplies prices, and locally counted units show "estimated". Performance: daily p50 and p95 lines with a 1 s reference line, and median stage durations as a stacked bar.

#### 4.4.4 Ingestion and corpus health

Status donut from aggregates; page coverage strip (native, OCR, blank, unknown; replaces the text-only `components/PdfCoverage.tsx`); chunk-size histogram with the preset's soft and hard limits as reference lines; and a job timeline (one row per job, segments per stage from consecutive events, enrichment phases in a lighter tint after the core phase).

#### 4.4.5 Index-status header (code workspaces)

Branch, short commit and relative time, "Indexed with uncommitted changes." when the tree was dirty at capture, language histogram (stacked bar with table twin), file count, last sync and "Update from source".

#### 4.4.6 Document viewer

PDFs render with `pdfjs-dist` 6.4.299 in a lazy chunk; worker, cMaps, standard fonts and wasm ship in the package so it works offline. Only visible pages render, device pixel ratio capped at 2. Highlights are `raw_bbox` scaled by `page_width`/`page_height`, drawn as translucent rectangles with an outline, matched span stronger than context; a passage list beside the page scrolls to each. Text and code render with highlighted character ranges (code with line numbers), images with region boxes. Returned passage / Stored chunks / Full unit become this one viewer plus a "Show chunk boundaries" toggle and "Show full document".

#### 4.4.7 Symbol neighbourhood and impact (code workspaces)

Confirmed for the MVP. Engine core owns the queries, the MCP tools `neighbours` and `impact` and their HTTP endpoints (engine core §4.14); the dashboard shows them through `POST /v1/relations/neighbours` and `POST /v1/relations/impact`. The view opens from a symbol's "Connections" tab, from a code hit's "Show neighbourhood", or at `insights/graph/symbol/{unit_id}`.
- **Neighbourhood**: a segmented control "Callers · Callees · Both" (`direction` in, out, both), predicate chips (calls, imports, inherits), and depth 1, 2 or 3. The focus symbol sits in the middle; callers are laid out to its left and callees to its right in depth columns, with their files as groups. Each list-twin row carries the engine's reason string in words ("Called by `parse_header` in `io/reader.py`", depth 1).
- **Impact**: "What could this change affect?" runs the incoming closure over calls, imports and inherits with depth up to 5 and fan-out up to 50. Columns are depth bands right to left of the focus. The header summarises in one sentence: "34 symbols in 9 files may be affected. Searched 3 levels deep." When the engine reports truncation it adds "Stopped at 500 symbols; narrow the predicates to see the rest." A "Show as table" twin lists file, symbol, depth and the path of reasons, sortable and copyable as text.
- Per-predicate `truncated` flags show as "More not shown" on the predicate chip. Results come from the graph contributions of the pinned snapshot (engine core §4.5, written incrementally at each publication), and the header states the indexed commit (4.4.5), so a stale index is visible.

### 4.5 Data contracts consumed

Schemas are owned by engine core v2 (`2026-10-15-engine-core-v2-design.md` §4: query trace v1, usage ledger, aggregates, background phases, error reasons). Rows marked "engine core adds" are endpoints the programme assigns to engine core v2 for this dashboard: document title filter and sort, usage grouping by run and operation, query latency p50/p95, the "most connected" graph endpoint, HTTP Range for originals above 16 MiB, repository index facts and the provider connection test.

| View | Endpoint | Status |
| --- | --- | --- |
| Session, profile menu | `/v1/handshake`, `/v1/session/launch*` | exists |
| Home cards, Library status, corpus health | `GET /v1/collections/{id}/summary`; `GET /v1/documents?status=` rows carrying `document_status` (state, phase, reason code, pages native/OCR/blank, units, chunks, chunk histogram), replacing the constant `processing_state` | engine core |
| Library table and drawer | `GET /v1/documents` (title filter, sort, cursor), `/v1/sources/{id}`, `/revisions`, `/extractions/.../units`, `/windows` (serving chunks after chunking v2) | exists; engine core adds title filter and sort |
| Inline progress, timeline | `GET /v1/jobs`, `/v1/jobs/{id}/events` (SSE), `/events/page`; phase jobs from `extraction_layers` | exists |
| Add wizard | `POST /v1/sources`, `/v1/pdf-intakes/*` | exists |
| Viewer | `GET /v1/assets/{id}/content`; unit locators with page geometry | exists; engine core adds HTTP Range above 16 MiB |
| Search, History | `POST /v1/queries` with `trace: true`; `GET /v1/queries`, `/v1/queries/{run_id}` | exists; trace populated by engine core |
| Trace tab, run cost | trace v1 in the response or `GET /v1/query-runs/{id}/trace`; cost from `GET /v1/usage` filtered by `run_id` (each provider stage carries a `usage_id`) | engine core, including the `run_id` filter |
| Modes, kinds, access labels | `GET /v1/discovery/{subject}` for modes and kinds; kind discovery returns friendly labels, required fields, facet defaults and the preset's access labels (kind ontology §4.10) | exists; fields added by kind ontology |
| Usage & cost | `GET /v1/usage?group_by=day\|provider\|model\|collection&from&to` (units, computed cost, price version); price table read and `PUT /v1/prices` | engine core, including `group_by=run\|operation` |
| Performance | `GET /v1/metrics/queries?from&to&bucket=day` returning p50, p95 and per-stage p50 and p95 from the stored stage timings, with the stage budgets | engine core adds |
| Graph | `POST /v1/relations` (≤ 2 hops, ≤ 100 edges, cursor); `GET /v1/relations/hubs?collection&kind&predicate&direction&limit&cursor`, entities in scope (with their unit) ordered by degree | relations exist; engine core adds hubs |
| Symbol neighbourhood and impact | `POST /v1/relations/neighbours` (direction, predicates, depth ≤ 3, per-predicate limit ≤ 20, limit ≤ 100) and `POST /v1/relations/impact` (predicates, depth ≤ 5, fan-out ≤ 50, limit ≤ 500); nodes, edges, reason strings, depth and `truncated` flags | engine core adds (§4.14) |
| Index status | `index_facts` (commit, branch, dirty flag, per-language file and unit counts) on `GET /v1/sources/{id}` | commit and dirty captured; engine core adds branch, languages and the field |
| Settings | `/v1/settings` draft, preview, activate; `/v1/collections/{id}/consent`; `PUT /v1/prices`; `POST /v1/settings/providers/{role}/test` | engine core adds the test endpoint (engine core §4.15) |
| Evaluations | `/v1/evaluations*` | exists |

The connection test (engine core §4.15) makes one minimal call with a fixed synthetic input ("tychora connection test", or a 1×1 image for image roles) and the server-side credential, tests the settings draft or the active settings, records a normal ledger row (for example `embed_query`) with no collection, job or run, and sends no collection content, so it needs no consent. For a local query encoder it loads and verifies the asset instead.

**Fixture-based development.** When engine core v2 fixes the trace, usage, aggregates, hubs, neighbours and impact schemas (its contract tests record each endpoint's response as a fixture, engine core E10), `scripts/generate_contracts.py` (new `--trace`, `--usage`, `--aggregates` options) records example responses into `dashboard/contracts/`. A `FixtureClient` implementing the `api/client.ts` interface runs only under `vite dev` with `?fixtures=<scenario>` (`import.meta.env.DEV`, absent from builds). Scenarios: `empty`, `first-run`, `busy`, `code-repo` (including a neighbourhood, a truncated impact and an 800-node graph), `editions-and-modes` (modes, editions, decisions, review-required, restricted), `errors`. A dev-only gallery renders every component in every state, and a vitest test decodes every fixture with `api/contracts.ts`, so schema drift fails the build.

### 4.6 Components and visual hand-off

No visual style is designed here; the later `impeccable` pass owns it.
- **Hand-off contract.** New components never write a colour, font, size, radius or shadow literal. They read CSS custom properties named by role (surface, text, muted text, border, accent, danger, warning, success, info, focus, and six series roles for charts and graph predicates), declared once in `src/styles/roles.css` with values copied unchanged from the current stylesheet. Existing styles are not migrated to them. The visual pass then restyles by changing values, adds dark mode and the theme choice, and decides whether to fold `styles.css` into a token system.
- **What stays UX-owned.** Non-colour encodings, because they carry meaning: status icons and labels (4.3), predicate dashes and markers, series patterns, kind glyphs, and the rule that colour is never the only signal. Charts and tables use the current fonts with tabular figures where the current stack supports them.
- **Components**, one file and test each: `Button` (primary, secondary, quiet, danger), `StatusBadge`, `StatCard`, `DataTable`, `Drawer`, `Tabs`, `SegmentedControl`, `ViewState`, `DeveloperDetails`, chart primitives (`Axis`, `BarStack`, `Line`, `Donut`, `TableTwin`), the graph layers of 4.4.1 (`GraphModel`, layout and routing worker, `SvgScene`, `CanvasScene`, `GraphList`), `ScopeSelect`, `DropZone`. Each takes the current class names and look; only their structure and behaviour are new.
- **Layout**: primary action top right; destructive actions at the bottom of drawers, never next to the primary one (Fitts).

### 4.7 Security and consent

No new credential paths; fixtures never ship in builds. Usage views cover only the caller's authorised collections. Access labels appear on every collection chip and result card, and the profile menu states the caller's clearance ("You can read up to Confidential") from the handshake's access ceiling, so a missing result can be explained by access rather than absence. Developer details keeps today's redaction of tokens and receipt identifiers.

## 5. Migration and compatibility

Programme decision: one code path. The v2 dashboard replaces the current pages in place; there is no "classic" mode. Old routes redirect (section 4.1). Removal list:

| Remove or replace | Files | Replacement |
| --- | --- | --- |
| Nine-entry navigation, identity disclosure, sidebar remember controls | `App.tsx:25-35, 255-300` | Five sections; profile menu |
| `JsonDetails` (about 45 uses) | `components/JsonDetails.tsx` and callers | One `DeveloperDetails` per page |
| Structured `QueryRequest` editor, review relabelling, four disclosures | `pages/Search.tsx`, `pages/search/request.ts` | Mode-driven input; read-only request in Developer details |
| Returned passage / Stored chunks / Full unit triad | `components/EvidenceReader.tsx`, `ReturnedPassage.tsx`, `StoredChunks.tsx`, `VisualPassage.tsx` | One `DocumentViewer` |
| Three intake forms | `pages/AddDocuments.tsx`, `pages/sources/SourceForm.tsx`, `pages/sources/PdfIntakePanel.tsx` (protocol helpers `pdfIntake.ts`, `submission.ts`, `readiness.ts` kept) | `AddWizard` |
| Explore form and chip list | `pages/Explore.tsx`, `components/Relations.tsx` | `GraphExplorer` |
| Jobs page | `pages/Jobs.tsx`, `components/Operations.tsx`, `pages/OperationsRouter.tsx` | Inline progress, timeline, redirect |
| Activity page | `pages/Activity.tsx` | Search → History, Insights → Performance |
| Text-only coverage | `components/PdfCoverage.tsx`, `pages/sources/Coverage.tsx` | Coverage strip |
| Collections page | `pages/Collections.tsx` | Scope selector, Settings → Access |
| Models / Settings split | `pages/Models.tsx`, `pages/models/*`, `pages/Settings.tsx`, `components/Administration.tsx` | Settings sub-pages |

The duplicate `:root` blocks and hex literals in `styles.css` (lines 2-15, 938-950) stay; the visual pass removes them.

Kept unchanged: `session.tsx`, `credentials.ts`, `api/client.ts`, `api/events.ts`, `hooks/useResource.ts` (extended with the 100 ms skeleton delay). `docs/dashboard.md` is rewritten to the new structure and copy rules; the connection section stays.

## 6. Testing and verification

**Vitest (134 tests in seven files today).** `session.test.tsx` and `launch.test.tsx` stay unchanged. `workbench`, `documents`, `pdf-intake`, `operations` and `ocr-intake` (formerly `task5`, renamed by code health) tests are re-cut per new page, keeping every behavioural assertion that still applies (typed rejection preserves drafts, uncertain submission checks for an accepted job, OCR stays noncitable, scope denial) and deleting assertions about removed UI; the implementation plan carries an old → new test mapping so nothing is dropped silently. New: vocabulary exhaustiveness, table twins, the U2/U3/U7 structural tests, fixture decoding, and graph tests: model merge on expand; layout invariants run on the worker code in Node (no node outside its group, no two group rectangles or node rectangles overlapping, existing nodes moved at most 40 px by an expand, identical output for identical input); routing invariants (no inter-group path crosses a group that holds neither end, straight-line fallback past the budget); SVG scene snapshot on a small fixture; list-twin keyboard model.

**Playwright (12 tests in four specs).** Page objects (`e2e/pages/*.ts`) absorb selector churn. `dashboard.spec.ts` becomes `first-run.spec.ts` (U1) plus the existing narrow-screen keyboard and reconnect journeys; `ocr-intake`, `pdf-intake` and `request-and-restore` (formerly `task5`, `task6` and `task7`, renamed by code health) keep their scenarios (simulated Voyage, actual OCR inclusion, streamed PDF retry, typed restore failure) through page objects. Added: an axe sweep of every route in the current theme (U5), fixture-mode screenshots of each state, and the parity checks below.

| Capability (U6) | Test |
| --- | --- |
| Compound code graph, expand, keyboard | `graph.spec.ts` on a synthetic repository |
| Symbol neighbourhood and impact, with truncation | `neighbourhood.spec.ts` on the same repository |
| Index-status header with dirty warning | `code-repo.spec.ts` |
| Search modes and input mapping | `search-modes.test.tsx` (request bodies per mode) |
| As-of search with visible edition and decision | `editions.spec.ts` with two synthetic editions |
| Review-required handling | existing `ocr-intake` OCR journey |
| Access label visibility and restricted exclusion | `access.test.tsx` |
| Per-hit channel scores | `trace.test.tsx` on fixture |

**Bundle and performance.** `check:bundle` fails when the entry chunk exceeds 120 KB gzip or when webcola, the graph layers or pdfjs appear in it. `bench:graph` runs layout and routing in the worker on synthetic graphs of 100, 300 and 800 nodes and fails when the 300-node case exceeds 400 ms on the CI runner (U4).

## 7. Code-health ratchet

- No dashboard file above 300 lines (replacing `Search.tsx` at 895); one status vocabulary, one error-copy map and one `ViewState` replace per-page logic.
- New components hold no colour, font or size literals (U7); restructuring the 1,434-line stylesheet is left to the visual pass.
- Files in section 5 deleted; the code-health baseline's dead-code tool reports no unused dashboard exports.
- Each component file opens with a one-line responsibility comment.
- `scripts/notices.mjs` covers d3-scale, d3-shape and their ISC dependencies, webcola (MIT), and pdfjs-dist (Apache-2.0).

## 8. Effort, phasing, risks and open questions

**Effort** (ideal days including tests, states, table twins and keyboard support):

| Work | MVP | Phase 2 |
| --- | --- | --- |
| Base components and the role custom properties (no token system, no dark mode) | 1.5 | — |
| Shell, routing and redirects, vocabulary, copy | 3 | — |
| Home | 2 | — |
| Library table, drawer, inline progress | 4 | — |
| Add wizard | 3 | — |
| Document viewer (pdf.js, highlights, text ranges) | 4–5 | — |
| Search (modes, options, coverage, decisions, cards) | 4 | — |
| Trace waterfall and rank-flow table | 3 | bump chart 1 |
| Usage & cost, latency | 3 | — |
| Graph model, seeds, inspector, expand and merge | 2 | search mini-graph 1 |
| Layout worker (WebCola, containment, grouping, flow, stable incremental layout, budgets) | 4–5 | — |
| Edge router (grid router over groups, nudging, in-group arcs, fallback) | 3–4 | — |
| Renderers (SVG and canvas scenes, pan and zoom, labels, hit testing, minimap) | 4 | — |
| Highlighting, focus plus context, filter chips | 1.5 | — |
| Graph list twin and keyboard model | 1.5 | — |
| Symbol neighbourhood and impact view | 2 | — |
| Graph benchmark and invariant suite | 1 | — |
| Index-status header | 1.5 | — |
| Ingestion tab (donut, coverage strip) | 2 | timeline and chunk histogram 3 |
| Settings (guided models, checklist, test, access, data, browser) | 4 | — |
| Fixture client and gallery | 1.5 | — |
| Test migration, axe sweep, first-run journey | 4 | — |
| Embedding-space scatter (needs a projection endpoint) | — | 3 |
| Edition comparison view | — | 3 |
| Large corpus overview (measure the canvas scene at 2,000 nodes; for that view only, a d3-force layout without containment, and WebGL only if frames exceed 50 ms) | — | 3–6 |
| **Total** | **about 60–63** | **about 14–17** |

The graph stack costs about 19–21 days against 5–6 for the earlier Cytoscape estimate; the difference buys the containment, non-overlap, stable expansion and routing that the earlier view lacked. Removing the token and dark-mode work saves 1.5 days here; that work moves to the visual pass, estimated with the `impeccable` skill. MVP satisfies U1–U7. Static and component work (base components, shell, Home, Library, wizard, viewer, settings, and the graph layout and routing worker) starts after engine core v2 freezes its contracts, on fixtures; trace, usage and performance views integrate when engine core ships them.

**Risks and open questions** (each with its default):
1. Engine core additions for this dashboard (title filter and sort, `run_id` filter and run and operation grouping on usage, query metrics, hubs, Range, source index facts, connection test, `neighbours` and `impact` endpoints). Engine core v2 owns them; if one ships late, the dashboard hides the dependent element (metrics fall back to `duration_ms` percentiles computed from `GET /v1/queries`, at most 100 runs).
2. If hubs slip, "Most connected" is hidden and the explorer starts from documents and hits.
3. No Range support on asset content: PDFs above 16 MiB fall back to text highlights with "Page view unavailable for files above 16 MB."
4. Tychora has no findings model for Repair: finding IDs travel as exact identifiers and retrieve the feedback records carrying them.
5. pdf.js offline assets are unverified: the build bundles `cmaps/`, `standard_fonts/` and wasm, and an e2e test opens a CID-font PDF offline.
6. WebCola has had no release since 2019. Default: pin 3.4.0, import core modules only, keep it behind `graph/layout.worker.ts`, and vendor it if a fix is needed (4.4.1).
7. Layout quality is judged by eye as much as by invariants. Default: before the renderers are built, the layout and routing worker is run on three fixtures (a 300-node code repository, a regulation with sections, a mixed collection) and shown to the product owner as static SVG; the constraint weights and router settings are tuned there.
8. Layout time at 800 nodes may exceed the 1 s cap. Default: the worker returns its best positions at 1 s (4.4.1); if that is regularly unreadable, the explorer cap drops to 500 nodes.
9. The programme makes bounded impact analysis a must-have alongside the neighbourhood query. Default: if engine core ships `impact` after `neighbours`, the Impact control is hidden and Neighbourhood with depth 3 incoming stands in.

## 9. Flow-review findings

None closed by this sub-project alone. C5 (empty trace) and Q1/Q2 (query latency) are closed by engine core v2 and made visible here by the Trace and Performance tabs; the review's section 8 note that Activity cannot replay is handled by offering replay only for retained requests.
