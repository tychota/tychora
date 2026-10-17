# Dashboard delivery design

Date: 2026-10-14. Implements the owner's request to review the September draft and build the dashboard. Read alongside the [original design](2026-09-10-tychora-dashboard-design.md); this document resolves its assumptions against the current engine.

## Intent and decisions

Build a local evidence inspection and operations workbench for the person maintaining or querying a Tychora workspace. Success means a user can add a source, follow its durable job, search real evidence, inspect exact provenance and retained originals, explore bounded relations, inspect coverage, browse retrieval history, compare evaluation results, and inspect or activate configuration. There is no generated answer or conversation UI.

The owner selected a quiet, dense workbench with warm neutrals and a restrained accent. Use the existing forest-green identity, warm paper surfaces, compact tables, clear typography, and a spacious evidence reader. Persistent navigation names Search, Sources, Jobs, Evaluations, Activity, Settings. A split search reader becomes stacked panels on small screens. Use selectable text, labelled controls, visible keyboard focus, readable contrast, and text status labels. No remote fonts, telemetry, or imported document resources.

Three approaches were considered: a bundled React/TypeScript app, a separate frontend server, and server-rendered pages. Choose the bundled app: it supports coordinated reading and reconnectable job events without another production process. A separate server complicates same-origin authorization and distribution. Server-rendered pages would still need substantial client state for these workflows. Build with Vite; Node is a developer/build dependency only. Official implementation references: [React](https://react.dev/learn/build-a-react-app-from-scratch), [Vite deployment base](https://vite.dev/guide/build).

## Honest first delivery

The existing spec is a product direction, not proof of working endpoints. Ship all six navigation destinations backed by existing services, plus the small HTTP fixes necessary to make them work. Missing engine features are explicitly unavailable, never simulated. This delivery does not implement the unrelated engine backlog in HANDOFF.

Keep separate follow-up prerequisites for query trace generation, enforced history expiry/purge and full-history configuration, collection lifecycle/global workspace discovery, evaluation label revision/aggregate analytics, automatic settings rebuild planning, precise PDF overlay rendering, and a full-corpus graph. Do not expose a working control for any of those. Where useful, explain the concrete limitation close to its affected feature. Original PDF bytes and exact retained locators remain accessible without claiming rendered bounding-box highlighting. JSON export is authoritative; do not fabricate an agent envelope by concatenating snippets.

## Server and authorization

Serve the app at `/dashboard/`, with object-ID routes under that prefix and compiled resources under `/dashboard/assets/`. A request for the entry point or a real packaged static resource may omit a bearer token, but must still pass exact Host and optional Origin validation. GET/HEAD only. This narrow public surface contains application code, never workspace data or credentials. Traversal, missing assets, unknown application routes, duplicate control headers, and foreign hosts/origins must not escape containment or fall through to a resource.

All `/v1` and `/mcp` behavior remains authenticated and scope-bound. The connection form accepts the owner's existing local bearer token, stores it only in component memory, and calls `/v1/handshake`. Do not put the token in URLs, cookies, browser storage, logs, or bundled resources. Disconnect clears all prior workspace data and cancels outstanding requests/streams. Send `cache: 'no-store'`; sensitive responses receive `Cache-Control: no-store`. Static responses carry a restrictive CSP, `nosniff`, no-referrer, and frame protection. Use authenticated fetch for assets/events, never token-bearing image URLs or EventSource query parameters.

The handshake already supplies workspace ID, scoped collection IDs, capabilities, and the permitted default collection. Display one workspace and selectable granted collection IDs; do not invent collection labels or a global workspace registry. Narrow API calls using the complete handshake scope with the selected collection IDs. Never expand a grant in the browser. Unsupported write/manage controls are hidden or disabled with an explanation; the server remains authoritative.

## Search and inspection

Discover kinds and profiles/configuration, then send a real query with question input, selected scope, registered metadata-facet filters, date/revision controls and bounded result count. The engine has no generic kind-filter field: display kind labels and discovery counts without inventing a filter or filtering already-ranked results locally. Explain enabled provider choices before submission. Keep query content out of navigation URLs. Preserve input after errors. Separate no matches, partial coverage, typed errors, and unavailable trace details.

Render returned segments, evidence IDs, revisions, exact locators, match/context distinctions, coverage and warnings from the actual response contract. Selecting evidence loads scoped inspection. Use escaped text for imported content; never `dangerouslySetInnerHTML`. Copy a stable citation including source/revision/evidence identity and locator; allow response JSON download. Inspect retained originals via the existing inspection API and assets via authenticated content fetch. Decode base64url payloads correctly. Only display safe raster media inline; offer explicit downloads for other content and render text as text. Revoke object URLs when replaced or disconnected. Show ORIGINAL_REMOVED and unavailable artifacts without guessing.

Relationships use `POST /v1/relations` with explicit selected unit IDs, bounded hops/edges and direction. Render a small relationship view and an accessible edge list with returned predicate/provenance fields. Expansion is explicit and bounded; do not infer edges from similar names. Follow returned truncation/cursors honestly.

## Sources and jobs

Sources use bounded server pages and revision/extraction/channel continuations. Show returned coverage counts/statuses and truncation; absent counts mean unknown. A source detail identifies the selected revision and exposes original inspection and coverage diagnostics.

Add supports pasted text, user-selected files through the existing base64 bytes source contract, and explicit paths within registered roots. The browser does not enumerate server files. Show destination, kind, metadata, format, processing/provider disclosure and body-size limits before submit. Reuse an idempotency key for a retry of identical intent; changed intent gets a new key. Follow the accepted durable job receipt. Large uploads must be rejected locally with a clear message before crossing the server's existing 1 MiB request boundary; do not silently truncate. Source sync also returns a job.

Job pages load persisted status first and then stream authenticated SSE. Parse chunk boundaries, CRLF, multiline data, sequence IDs and refresh events. Reconnect with `Last-Event-ID`, deduplicate events, refresh persisted status on expired history, and stop retry loops when aborted/disconnected or authorization fails. A disconnected browser never cancels a job. Show actual progress only where the event supplies it. Retry/cancel actions use service endpoints and display typed errors. Keep terminal state recoverable by reload.

## Evaluations, activity and settings

Fix evaluation read endpoint serialization for dict/list service results and prove management authorization remains intact. Evaluations display real runs and cases; allow selecting two runs, show dataset/split/snapshot/configuration/policy differences and compare common numeric metrics with explicit labels. Do not claim compatible quality comparisons when identity or context differs. Expose supported evaluation submission using registered dataset identity. Offline/synthetic context is labelled when reported; unreported context is unknown.

Activity displays bounded query history separately from jobs. A metadata-only run says “Query text not retained.” Show replay only for retained requests and still surface `REPLAY_UNAVAILABLE` or missing-dependency responses. Trace null means unavailable. No fake analytics or purge controls.

Settings displays redacted effective configuration, origins, discovery capabilities and kind definitions. A manager can explicitly submit complete TOML plus registered profile through the existing activation API, with dirty input preserved on failure. Explain this API activates configuration, does not automatically queue a rebuild, and may reject incompatible/busy changes. Never turn provider enablement into upload consent. Credentials remain references and are not redisplayed.

## Contracts, packaging and verification

Use typed browser DTOs tied to actual Python contracts and checked fixtures generated through real HTTP; permissive unknown diagnostic maps are confined to expandable inspection details. Keep network, connection/scope, SSE, evidence rendering and each destination in focused modules. Avoid duplicating retrieval, ingestion, authorization, or evaluation logic.

Commit the compiled static bundle so a Python wheel/source checkout works without Node. The frontend lockfile and source are included in the sdist, with a reproducible build command and CI drift check. Verify packaged assets exist in a built wheel. Existing executable packaging must include resources through its established mechanism; do not claim cross-platform qualification from a wheel test.

Run focused Python transport/evaluation regressions, frontend typecheck/unit/component tests, production build, and real-server browser journeys using a temporary synthetic local corpus. Browser checks cover connect, add text, durable job, query, evidence/citation/original, coverage, relationships or honest missing-channel state, history, evaluations and settings. Inspect desktop and mobile screenshots plus keyboard navigation. Capture limitations and commands in the delivery report; do not claim the known upstream Linux caption or dependency advisory blockers are fixed.
