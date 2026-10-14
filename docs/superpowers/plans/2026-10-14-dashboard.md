# Tychora Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver a usable, bundled local dashboard against real Tychora HTTP services.

**Architecture:** A React/TypeScript client served at `/dashboard/` by the existing authenticated loopback server. A narrow static-resource admission path exposes only application code; all corpus operations retain current API authentication. Focused modules own API access, scope/session, streaming, evidence inspection and six destinations.

**Tech Stack:** Existing Python/FastAPI services; React, TypeScript, Vite; Vitest/Testing Library and Playwright. Pin resolved frontend dependencies in package-lock.json. No UI kit or remote runtime resources.

**Spec:** `docs/superpowers/specs/2026-10-14-dashboard-delivery-design.md` resolves `docs/superpowers/specs/2026-09-10-tychora-dashboard-design.md` against the engine.

## Global Constraints

- Serve the app at `/dashboard/`, with object-ID routes under that prefix and compiled resources under `/dashboard/assets/`.
- All `/v1` and `/mcp` behavior remains authenticated and scope-bound.
- Do not put the token in URLs, cookies, browser storage, logs, or bundled resources.
- Missing engine features are explicitly unavailable, never simulated.
- Node is a developer/build dependency only.
- No remote fonts, telemetry, or imported document resources.
- The owner selected a quiet, dense workbench with warm neutrals and a restrained accent.
- Commit the compiled static bundle so a Python wheel/source checkout works without Node.
- Frontend business logic must not duplicate retrieval, authorization, indexing or evaluation.
- Work only in `.worktrees/dashboard` on `feat/dashboard`; preserve other worktrees and existing evidence.

## Review Focus

- A public static request with foreign Host/Origin, duplicate headers or traversal cannot reach corpus data (Task 1).
- A late response after disconnect or scope change cannot display the prior scope's data (Tasks 2–3).
- Large or binary source uploads must remain exact or fail before submit, and retry preserves idempotency only for identical intent (Task 3).
- An SSE event split between chunks, duplicate sequence or expired cursor cannot lose recovery or loop after disconnect (Task 3).
- Metadata-only history, null traces and incompatible evaluation identities cannot be presented as replayable, explained or comparable quality (Tasks 2–4).

## Repository and verification

Baseline at `700df65`: `PYTHONPATH=src .worktrees/ci-fixes/.venv/bin/python -m pytest tests/transports/test_http.py tests/evaluation/test_evaluation_http.py -q` passed 41 tests, one upstream Starlette deprecation warning. In the new worktree run with `PYTHONPATH=src ../ci-fixes/.venv/bin/python ...` (the explicit source path selects this worktree). No live provider calls are authorized. Use synthetic local fixtures and configured offline profiles.

### Task 1: Secure dashboard hosting and verified HTTP contracts

**Files:** create `src/tychora/interfaces/http/dashboard.py`, `tests/transports/test_dashboard.py`; modify `src/tychora/interfaces/http/app.py`, `auth.py`, `routes.py`, `tests/evaluation/test_evaluation_http.py`. Create `dashboard/contracts/` fixtures and a focused regeneration helper under `dashboard/scripts/` if useful. Do not build frontend components yet.

**Interfaces:** consume `create_app(owner, services, auth)`, `LocalAuth`, `checked_headers`, inspection/query/job/evaluation services. Produce `register_dashboard(app)` and a narrowly checked static admission helper used by RawBoundary. Static root is `src/tychora/resources/dashboard/`. Exact dashboard pages `/dashboard/`, `/dashboard/search`, `/dashboard/sources`, `/dashboard/sources/{id}/revisions/{id}`, `/dashboard/jobs`, `/dashboard/jobs/{id}`, `/dashboard/evaluations`, `/dashboard/evaluations/{id}`, `/dashboard/activity`, `/dashboard/runs/{id}`, `/dashboard/settings`; accept only these application route shapes and files contained in `assets/`. An absent bundle returns a clear 503 development-build instruction without breaking API construction.

- [x] Write transport tests for unauthenticated app shell/static delivery with a temporary static fixture; authenticated API remains required; wrong Host/Origin/duplicate Host rejected; traversal/missing resources/unsupported methods cannot serve files; no-store and CSP/nosniff/referrer/frame headers set. Representative behavior:

```python
shell = await client.get("/dashboard/", headers={"Host": "127.0.0.1:8765"})
assert shell.status_code == 200
assert shell.headers["cache-control"] == "no-store"
denied = await client.get("/v1/handshake")
assert denied.status_code == 403
assert (await client.get("/dashboard/assets/absent.js")).status_code == 404
```

- [x] Add real-service HTTP evaluation list/detail tests after dataset registration/run, including reader denial. Before code, run focused new tests and record expected failures.
- [x] Implement a static-only bypass before bearer authentication with exact authority/header validation and safe containment; leave runtime admission for API unchanged. Do not bypass authentication for arbitrary `/dashboard` prefix requests. Add no-store to sensitive API responses at the boundary. Fix evaluation list/detail JSON serialization using JSONResponse for declared dict/list results.
- [x] Capture small real HTTP query/inspection/job/discovery/evaluation fixture examples from synthetic text evidence, with no credentials or filesystem paths in committed fixtures. Document actual response shape and missing fields. Use real public source/query calls, not invented JSON. Keep fixture generator usable by later browser tests.
- [x] Run `PYTHONPATH=src ../ci-fixes/.venv/bin/python -m pytest tests/transports/test_dashboard.py tests/transports/test_http.py tests/evaluation/test_evaluation_http.py -q`, Ruff on changed Python, and report commands/outcomes. Commit as `feat: serve dashboard assets within the local HTTP boundary`.

### Task 2: Connected workbench, real search and exact evidence reader

**Files:** create `dashboard/package.json`, `package-lock.json`, `tsconfig.json`, `vite.config.ts`, `index.html`, `src/main.tsx`, `src/App.tsx`, `src/styles.css`, `src/api/{client,contracts}.ts`, `src/session.tsx`, `src/components/{EvidenceReader,Relations,Status,JsonDetails}.tsx`, `src/pages/Search.tsx`, corresponding tests. Build output is `src/tychora/resources/dashboard/`.

**Interfaces:** consume Task 1 `/dashboard/` hosting and actual `/v1/handshake`, `/v1/discovery`, `/v1/settings`, `/v1/queries`, `/v1/evidence/{id}`, `/v1/inspect`, `/v1/assets/{id}/content`, `/v1/relations`. Produce exported `ApiClient` with `request<T>(path: string, init?: RequestInit): Promise<T>` and `blob(path: string, init?: RequestInit): Promise<Blob>`, `ApiError` with code/message/retryable, and `useSession()` exposing client, handshake, selected collections and disconnect. Every request adds in-memory bearer/scope, no-store and supports AbortSignal. Export shared reader/components for Task 3.

- [x] Establish pinned npm dependencies/scripts: `test` nonwatch, `typecheck`, `build`, later `test:e2e`. Vite base `/dashboard/`, no network resource dependencies, build directly to package resources. Configure jsdom + Testing Library. Write failing tests for connection denied/success, collection narrowing, stale response isolation, query typed errors preserving input, no matches, incomplete coverage, citation identity, text escaping and base64url original bytes. Example contract assertion:

```ts
expect(request.headers.get('Authorization')).toBe('Bearer test-token')
expect(request.cache).toBe('no-store')
expect(JSON.parse(request.headers.get('X-Tychora-Scope')!).collection_ids).toEqual([collection])
expect(window.localStorage.length).toBe(0)
```

- [x] Build the six-destination shell, connection form, disconnect, workspace ID and authorized collection selector. On authentication/session/scope change, abort and clear prior data; use keyed route subtree so unresolved requests cannot restore it. Keep token form cleared after connect. URLs contain route/object IDs only. Link unimplemented destinations to a clear temporary development message until Task 3 replaces them; final bundle may contain no such placeholder.
- [x] Implement typed contracts using Task 1 fixtures plus Python DTO definitions. Discovery provides kind labels, zero-source kinds, profile/provider context. Add accessible question/filter controls and bounded query request construction. Use existing filter schema rather than invented key names. Show live result metadata/coverage/warnings and null-trace explanation. Request failures preserve input and offer permitted retry.
- [x] Render coordinated result list and reader; use returned segments/locators to label matched versus context and original versus derived. Provide copy citation and full JSON export, original inspection with safe text/raster/download behavior, object URL cleanup, and explicit unavailable/removed states. All remote/imported markup is inert text.
- [x] Add bounded relation exploration with exact unit IDs, direction/predicate, truncation, accessible edge list, and a simple synchronized graph when real returned paths exist. Never derive edges from display text. On narrow screens stack the reader without removing operations.
- [x] Style using warm paper, dark forest-green navigation/text accents, restrained terracotta indicators, compact rows and open reader surface. Use locally available serif headings and readable local sans text; exact font selection can adapt to installed fonts with reliable fallback. Visible focus, semantic landmarks, skip link, labelled controls, reduced motion and text statuses are required. Avoid metric-card grids and decorative charts.
- [x] Run `npm --prefix dashboard test`, `npm --prefix dashboard run typecheck`, `npm --prefix dashboard run build`; inspect generated index/assets and commit source, lockfile and bundle as `feat: add the connected evidence search workbench`.

### Task 3: Sources, durable jobs, evaluations, activity and settings

**Files:** create `dashboard/src/pages/{Sources,Jobs,Evaluations,Activity,Settings}.tsx`, `dashboard/src/api/events.ts`, focused supporting modules and tests; modify `App.tsx`, contracts/styles/shared reader as needed. Keep individual page files focused; split forms/readers where needed.

**Interfaces:** consume Task 2 ApiClient/useSession and shared evidence reader. Use existing paginated `/v1/sources`, source revision/extraction/channel routes, `POST /v1/sources`, source sync, jobs status/events/retry/cancel, evaluations list/detail/submit, queries history/detail/replay, redacted settings read/activation and discovery. Events must use authenticated fetch with Last-Event-ID and AbortSignal. Preserve service field names and shape; verify in contracts before use.

- [x] Write failing component tests for source pagination/coverage continuations, source text/file submission and durable receipt, upload-size guard and stable idempotency retries, job recovery actions, SSE split/duplicate/refresh/abort, history without retained text, evaluation comparison mismatch, and failed settings activation preserving draft. Use actual Task 1 fixtures for response shape. Parser invariant example:

```ts
// Feed chunks that split both a field name and UTF-8 text.
// After id: 7 is acknowledged, duplicate 7 is ignored and reconnect carries 7.
expect(receivedIds).toEqual(['7', '8'])
expect(reconnectHeaders.get('Last-Event-ID')).toBe('8')
```

- [x] Build source inventory and revision detail with load-more cursors, coverage/extraction/channel continuations and diagnostics. Do not label source_key as a human title or an unordered revision as current. Let original inspection/reader reuse existing service behavior.
- [x] Add source form: text, chosen bytes file, or explicit registered-root file/folder/repository path; kind, collection, format, metadata JSON with useful common title/publisher/edition inputs. Discover and show selected kind requirements where provided. Show active processing/provider disclosure, explicit confirmation before source submit, and exact encoded request size below 1 MiB. Each immutable submission intent owns a crypto.randomUUID idempotency key; retain it on transport failure and replace it when intent changes. Successful receipt links to job detail. Do not execute source paths in the frontend.
- [x] Implement durable jobs list/detail and fetch-based SSE parser/reconnect. Load status first; on events update or refetch status without resetting unrelated reader state. Maintain increasing acknowledged sequence, ignore duplicates, handle refresh by reading status, use bounded retry backoff, stop terminal/auth/aborted streams, cancel on unmount. Expose service-backed retry/cancel appropriate to state and capability.
- [x] Implement evaluations list/detail and two-run metric comparison with case drilldown. Show explicit identity/context differences and only compute arithmetic deltas for matching numeric metric names; this is presentation, not a replacement evaluator. Allow manager submission of registered dataset ID/split/options supported by EvaluationRequest. The existing POST awaits evaluation completion despite 202: show in-flight request honestly, not a fictitious detached durable job. Do not expose dataset label editing.
- [x] Implement Activity's paginated query-run list and object-ID detail. Show “Query text not retained” for metadata-only rows; retained replay is explicit and typed failures stay visible. No unavailable trace or invented aggregate charts. Provide navigation to Jobs separately.
- [x] Implement Settings groups for effective profile/providers/origins, kinds, capabilities and unavailable history policy controls. Manager activation uses complete TOML + profile with explicit submit and no redisplayed secrets. Busy/incompatible failures preserve edits. Explain rebuild is not automatic. Reader cannot submit manager operations.
- [x] Run full frontend unit/type/build commands plus focused Python transport regressions when contracts changed. No unfinished nav placeholders remain. Commit as `feat: add dashboard operations and recovery views`.

### Task 4: Real browser journeys, distribution and delivery documentation

**Files:** create `dashboard/playwright.config.ts`, `dashboard/e2e/dashboard.spec.ts`, `dashboard/e2e/server.py` and small local fixture helpers; modify `pyproject.toml`, `.gitignore`, `.github/workflows/check.yml` or add focused dashboard workflow, `docs/http.md`, `docs/getting-started.md`, `HANDOFF.md`, frontend scripts, and add `docs/dashboard.md`. Add package resource regression under `tests/transports/` or existing packaging test area.

**Interfaces:** serve the built Task 2–3 bundle through the real `create_app` on a temporary workspace with synthetic offline corpus. Test-only server may use a fixed synthetic token and deterministic token counter; neither can enter production code. Frontend source/lock/scripts ship in sdist and compiled resources in wheel/executable resource collection.

- [x] Add real-server Playwright journeys: connect → add synthetic text → follow durable job → search → inspect source/locator → copy/export/original; source/revision coverage; bounded relation result or typed missing-channel; metadata-only history; actual evaluation list/detail/comparison; settings read and forbidden manager behavior. Explicitly assert API traffic is real and no browser route mocks replace these journeys.
- [x] Add reload/reconnect and narrow-screen keyboard coverage. Inspect desktop/mobile screenshots, empty/error states, focus order and horizontal overflow. Store screenshots/logs in ignored plan verification artifacts. Fix demonstrated issues through the same tested modules.
- [x] Extend packaging to include frontend sources in sdist and verify generated resources inside a built wheel. Ensure the existing executable collector includes dashboard resources; amend its manifest only if necessary. Add CI `npm ci --ignore-scripts`, frontend tests/type/build, reproducibility/drift check and browser checks with an offline local fixture. Do not change unrelated qualification gates.
- [x] Document concrete startup URL and safe local token retrieval from the actual owner record; never print a real token into artifacts or logs during verification. Explain session memory/disconnect, source size limits, enabled workflows and backend limitations. Update HANDOFF to distinguish implemented UI from outstanding engine work.
- [x] Run frontend tests/type/build/e2e, Python transport/evaluation tests, Ruff, typecheck appropriate to changed Python, and wheel-resource check. Run broader Python regression only when new failures or cross-boundary changes justify it. Record exact results and any pre-existing blockers. Commit as `test: verify dashboard journeys and packaged delivery`.

## Completion

Each task gets an independent spec/quality review and any material findings are fixed before dependent work. Final whole-branch review inspects the complete diff and documented capability limits. Keep `feat/dashboard` and its worktree available for review; do not publish, merge into main, delete retained worktrees, or claim upstream qualification blockers are closed.
