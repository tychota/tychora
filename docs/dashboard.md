# Local evidence workbench

The bundled dashboard uses the same authenticated services as Python and HTTP clients. One connection represents one workspace with multiple named collections. Knowledge pages use your selected active collections; workspace administration uses your complete granted authority, still capped by the server. Restricted credentials never become owners through navigation.

## Start and connect

Follow [getting started](getting-started.md) to install the server and prepare a workspace and verified output tokenizer. Start the owner with `tychora --workspace ./knowledge serve --port 8765` (or `python -m tychora.interfaces.http.server ./knowledge --port 8765`). At startup it prints one **launch link** such as `http://127.0.0.1:8765/dashboard/#launch=…`; open it within two minutes and the dashboard connects without pasting anything. Use the numeric loopback host: `localhost` is a different authority.

Need another link, for a second browser or after the first expired? While the owner runs:

```bash
tychora --workspace ./knowledge manage dashboard-link          # print a fresh link
tychora --workspace ./knowledge manage dashboard-link --open   # macOS: also open it
```

Each link is single-use, expires after **120 seconds**, and stops working when the owner exits. The code lives only in the URL fragment (after `#`), which browsers never send to the server, write to request logs or put in a `Referer`. The dashboard removes the fragment from the address bar and history before it contacts the server, exchanges the code once for the owner's bearer token, then connects. An expired, reused or mistyped link shows a generic error and the normal connection form.

### Remember on this browser

By default the token stays in the tab's memory: reloading the page or restarting the server needs a new launch link. Tick **Remember on this browser** (on the connection form, or in the sidebar after a launch link) to keep it in this browser's `localStorage` for this exact origin (`http://127.0.0.1:PORT`) under `tychora.dashboard.v1.rememberedToken`. Later loads of that origin then connect automatically, including after the owner restarts, because the owner token is now stable across restarts. Remembering is always opt-in and the box starts unchecked.

- **Disconnect** clears workspace data from memory and aborts requests and streams, but keeps a remembered token; the page does not reconnect until you reload it. It does not cancel an accepted durable job: reopen its job URL and reconnect to inspect the persisted outcome.
- **Forget this browser** (shown while remembered), or unticking the sidebar box, removes the stored token; Forget also disconnects.
- If the server rejects a remembered token (for example after rotation), the dashboard deletes it, explains why and shows the connection form. If the server is merely unreachable, the token is kept and **Retry** is offered.

### Pasting a token

You can still connect by pasting the bearer token from the owner's private runtime record. Do not print the full record or place tokens in shell arguments or reports. On macOS, this copies only the token without printing it:

```python
import json
import subprocess
from pathlib import Path

record = json.loads(Path("knowledge/.runtime/endpoint.json").read_text())
subprocess.run(["pbcopy"], input=record["token"].encode(), check=True)
```

Paste into **Bearer token**, connect, then clear the clipboard. If the port was omitted at startup, the same record holds the actual authority.

### Credential storage and revocation

| What | Where | Lifetime |
| --- | --- | --- |
| Owner bearer token | `<workspace>/.runtime/owner-token` (owner-only `0600` in a `0700` directory) and the `endpoint.json` record while serving | Until rotated |
| Launch codes | Owner process memory only, as SHA-256 digests | 120 s, one use, at most eight outstanding, lost on exit |
| Dashboard token, default | Tab memory | Until reload, Disconnect or tab close |
| Dashboard token, remembered | `localStorage` of `http://127.0.0.1:PORT` | Until Forget, rotation or clearing site data |

Tokens never go in cookies, `sessionStorage` or URLs; only the short-lived launch code appears in a URL fragment. Queries, imported contents, files and configuration drafts always stay in browser memory.

To revoke every remembered browser and copied credential, stop the owner and run `tychora --workspace ./knowledge manage rotate-token` (or delete `.runtime/owner-token` while stopped); the next start uses a new token and rejects the old one. If the owner refuses to start because the token file is unsafe (wrong owner, mode, link or contents), inspect it, then rotate or delete it: Tychora never silently replaces a suspicious file.

Remembering is a convenience trade-off. Anyone who can use your OS account and browser profile can read the remembered token and act with full owner authority on that workspace while it is served. Do not remember on shared or untrusted machines or browser profiles. Pages from other origins cannot read it, and the server binds only to loopback with exact Host and Origin checks. Because the origin includes the port, a fixed `--port` keeps the remembered token usable across restarts.

## Collections and documents

**Collections** owns named destinations, lifecycle and provider upload consent. Creation requires the server's explicitly managed owner, rather than an arbitrary credential with similar capabilities. UUIDs remain stable across title/slug edits. Archive preserves evidence, excludes the collection from ordinary search and blocks new indexing. Authorized retained revision inspection and durable jobs remain available. Reactivation restores eligible retained coverage. Static/restricted grants never expand after creation or consent changes.

Select one, several or all granted active collections for **Documents**, **Search**, **Explore** and exact-scope **Activity**. Add documents always names one destination. Inventory sorting/filtering describes loaded rows only; unknown counts/status stay unknown and continuation remains explicit.

**Documents** offers content previews, complete stored chunks, deliberate Full unit inspection, relationships, revisions/channels and safe retained-original access. Source-only direct links can require bounded inventory continuation. Explicit retained revision links retain their authorized inspection context, including archived collections.

**Add documents** supports pasted text, streamed PDFs, small other selected files and authorized server paths. The ordinary JSON request limit is **1 MiB including metadata and base64 expansion**; oversized requests are rejected before sending. Draft title/text/File/destination/kind and retry identity survive Models/Collections setup navigation in memory. Known missing/corrupt assets, unavailable packages, unsupported models, offline conflicts and missing collection consent/caller permission block processing. Incomplete bounded readiness remains unknown and delegates complete asset verification to the server; it does not prove installed assets or force an impossible full hash check in the browser.

Choose **Files** and a PDF, name its destination/title, then **Analyse PDF**. The browser sends the original File body without base64 or a whole-file array buffer. Upload is indeterminate until server-confirmed bytes return; **Cancel upload** interrupts it. Analysis visits every physical page locally and reports native, scanned/image-only, mixed, known blank and unknown content with actual capability blockers. Image presence does not prove text exists. During analysis **Stop waiting** stops the browser request; bounded server analysis may still finish and cleanup may remain busy until receipt expiry.

Review size, page count, destination and the captured native/local Docling OCR policy, then explicitly confirm that exact analysis and policy before **Ingest PDF**. The workspace default profile stays unchanged. A queued/accepted job is not indexed: follow its durable job, actual errors and retained processing-page counters through publication. OCR is derived, `needs_review` and noncitable; selected coverage distinguishes processed pages from detector-relative no-text-detected gaps. “Processed” does not establish complete transcription or accuracy. Search mode review policies can exclude indexed OCR; choose an authorized registered inclusion mode explicitly, without accepting the transcript. Full unit inspection shows physical page locators and retained input/derivation identities; visual OCR results need not have a text Segment or indexed-window anchor.

Changing file, destination, metadata or processing invalidates confirmation. Models setup/consent/config changes preserve File and draft in memory, then require a fresh receipt/upload/analysis and confirmation. Cleanup retains the original complete receipt scope; denied cleanup never widens authority and expiry remains the fallback. An uncertain submission keeps its receipt and retry key and checks for an accepted job before another attempt. Upload, analysis and durable ingestion failures are distinct; switching back to pasted text keeps it independently usable. Disconnect clears browser memory; accepted durable jobs survive.

PDF binary intake is at most **100 MiB (104857600 bytes)**, further narrowed by configured acquisition/processing limits. Descriptor/submission JSON remains capped at **1 MiB**. Receipts expire after **30 minutes**, with a **60-second upload idle deadline**, four active reservations per principal, eight per owner and **512 MiB** total reserved originals. Analysis is bounded to **1000 pages**, **40 million pixels** per image/raster, **100000 objects/operations** and **60 seconds**. The captured policy records actual OCR limits: currently 16-page passes, two CPU threads, 120-second SDK/180-second batch wall and 5400-second overall bounds, 256 MiB child output/aggregate metadata, 16 MiB retained composition, 1 GiB original-plus-derived budget (or lower owner allowance), and sampled 4 GiB child RSS cancellation. Independent composition limits can still fail a large document. These are finite bounds, not full-document accuracy or an OS memory sandbox.

New qualified PDF extractions use the identity-bearing composition strategy `pdf-streamed-images@1`. Exact raw images remain declared and are verified one asset or original/crop pair at a time. The strategy allows **256 MiB of nonraw inputs**, **256 MiB of unique encoded inputs per verification group**, and **512 MiB of unique composition inputs plus generated products**, including the composed manifest. It intentionally replaces the old 256 MiB total-input rule for marked plans. Plans without the marker retain that old aggregate and their exact identities on replay/reuse. The **20 MiB region**, **40 million pixel**, **16 MiB plan/manifest**, semantic work/output and **1 GiB or lower whole-PDF retention** limits remain enforced. Encoded verification groups do not bound total process RSS. Settings discovery reports both strategies under `document_composition_inputs`.

Whole-original browser inspection has a separate existing **16 MiB payload / 24 MiB encoded response** ceiling. A retained/indexed PDF above that inspection limit can return `INVALID_REQUEST: Inspection payload exceeds its limit`; this does not mean the original was removed or fully downloaded. No expanded download endpoint is supplied. See [HTTP](http.md) for the actual intake contract.

New PDF analyses identify the coordinate rule as `text_coordinates: docling-assembly@1` in the confirmed processing policy. Captured native and OCR cell assembly independently prove final text ownership, including list markers, formula suppression and dehyphenation. Raw SDK spans remain exact retained evidence. Older retained extractions keep their original serialization, identities and recorded coordinate interpretation; they are not retroactively qualified by the new rule. A stale captured policy requires fresh analysis rather than silently changing an accepted retry. These changes do not raise capacity limits or establish full-document OCR accuracy.

## Search and exact evidence

Search starts with the query, named collection scope and one Search action. Registered filters, revision/date policy, retrieval mode, output budget, provider context and technical JSON are progressive details. Results are evidence, without a generated-answer/chat claim.

For a supported structured query, open **Filters & retrieval options**, enable **Structured request**, and edit **QueryRequest JSON**. It goes through the same selected collection scope and `/v1/queries` endpoint. For example:

```json
{
  "input": { "type": "passage", "text": "A passage to compare with retained evidence" },
  "hints": ["exact source context"],
  "context": { "purpose": "comparison" },
  "modalities": ["text"],
  "limit": 8,
  "trace": true
}
```

The contract also supports question input and image input with an existing `asset_ref`, plus the backend's profile, filters, revision/date, identifier, budget and rendering fields. The server remains authoritative for types, supported values and permissions. Invalid JSON, the ordinary **1 MiB** body limit, and typed server rejection preserve the editor draft. Structured mode hides the simple question and filters; **Search evidence** submits the JSON editor. Switching modes keeps both drafts. Result review labels and **Submitted QueryRequest** describe the body actually sent, including its effective default mode when omitted; editing a draft or simple controls does not relabel existing results. **Include review-required content** retries that submitted body with the selected authorized inclusion mode and leaves evidence review/citation status unchanged.

The selected reader has three deliberate representations:

- **Returned passage** displays exact query excerpt blocks, separate matched/context roles, returned bounds, omissions, completeness and structured records. It does not silently fetch a complete unit.
- **Stored chunks** resolves the actual returned window ID through the retained extraction API. Stored text/bounds may be larger than the clipped returned anchor. Null window IDs remain null. Stored token counts are reported counts; no tokenizer identity or active-index membership is inferred.
- **Full unit** explicitly fetches complete authorized evidence, or explains partial/unavailable inspection. Native citable evidence retains citation copying. Noncitable OCR retains provenance-reference access without becoming a verified source quote.

Text, including markup and non-BMP Unicode, renders literally; server text is not regenerated or sliced with browser UTF-16 offsets. Citations, locators, original bytes and response export preserve actual identities. Text/safe raster originals may render inline; other media remains downloadable. Missing/removed originals and bounded responses remain explicit.

A visible label describes whether the **submitted retrieval mode** includes review-required derived content. Inclusion is mode policy, not a nonexistent `review_state` query facet. Where excluded, the supported inclusion action switches to an authorized registered mode, discloses the policy change and preserves other query settings. It never accepts evidence or changes citable status. If no permitted mode exists, the UI explains that restriction. Unknown inventory never becomes an excluded-document count. All bundled compose/audit/lookup modes currently include `needs_review`; exclusion/inclusion qualification uses explicitly registered synthetic modes.

## Explore, jobs and activity

**Explore** selects actual source units or follows a unit from Search, then requests bounded relations. Lists and keyboard-accessible graph nodes contain only returned paths/provenance. The default UI requests at most 30 edges and two hops. Successful empty means no returned paths; typed missing-capability/error states are distinct. Truncation and continuation do not imply a complete corpus graph.

**Jobs** lists durable source/maintenance state and opens actual stage/events, counters, errors, retry budget and snapshots. Authenticated SSE reconnects with consumed sequence and deduplicates events; persisted status remains authoritative. Source add/sync recovery/retry requires retained capture and server-verified eligibility. Maintenance retry is unavailable because the source scheduler cannot execute it. Cancellation is a separate action from disconnect. Historical job DTOs do not expose source identity; their unavailable lineage is stated instead of guessed from artifact hashes. Document acceptance links use actual returned job identities.

**Activity** lists bounded retrieval metadata for the exact selected query scope, not a workspace-wide history union. An empty selection scope may have no matching history while other scopes retain runs. Metadata-only history says **Query text not retained** and cannot replay. Replay appears only for retained requests and may fail when dependencies are unavailable. Trace, expiry/history purge and unmeasured analytics are not operational controls.

## Models and workspace operations

**Models** is the canonical configuration editor. Extraction/OCR, text/image embedding and reranking are distinct sections. Catalog/package availability, configured model, current assets, credential references and collection upload consent are separate facts. Readiness GET is bounded; `not_checked`/incomplete is not installed, and historical setup receipts do not prove current assets. Explicit local setup reviews asset hashes/licenses and uses verified cache or an explicit server bundle, without automatic download.

Guided fields include workspace `offline`, text/image `index.*.backend`, actual provider/model/reference choices and supported processing/retrieval defaults. Set and inherit differ; inherit removes the override rather than setting null. Preview uses the exact current workspace digest, shows effective changes/blockers, and is required before guided activation. Successful preview does not guarantee runtime preparation. Busy/stale/incompatible changes remain typed and preserve drafts. The expert complete TOML path remains available. Credential **values must be supplied to the server process**; the UI edits only `env:`/`secret:` references and never verifies a live connection.

Retained indexed configuration can constrain query defaults, limits and network policy as well as embedding generations. Incompatible preview blocks activation with `PROFILE_INCOMPATIBLE`; no automatic rebuild is queued. Use a fresh workspace configured before first indexing or a separately qualified publication/migration path. Compatible-channel recovery cannot convert parser/model generations. See [configuration](configuration.md).

**Settings** displays workspace context and links canonical Models/collection-consent editors. Whole-workspace health reports measured payload/native/recovery facts; compatible-channel rebuild uses selected supported text/image/graph channels. Backup exports to an explicit **server path**, not a browser download. Restore verifies a server archive into a clean separate destination without switching the current connection. Missing archive gives a sanitized typed `NOT_FOUND`; paths stay editable. Completed receipts show their actual manifest/target/time; only actual returned jobs link to Jobs.

Advanced retention first requests the original-purge reference-cut plan, then requires explicit destructive confirmation. Derived evidence remains retained; shared/leased/pending bytes are distinguished in the actual result. No fake trace/history purge or model migration button is supplied. Server complete-workspace authorization governs global operations even while one knowledge collection is selected.

**Evaluations** registers manifest/cases bytes under the ordinary 1 MiB boundary, submits real dataset/split/snapshot/configuration variants, inspects measured report/cases and compares common numeric metrics. The list considers at most 100 recent records before authorization, without continuation. Context differences remain visible; numeric deltas alone do not establish comparable quality. Missing metrics/denominators and unavailable reports remain explicit. No quality charts are inferred from configuration.

## Reproducible remote-provider smoke journey

Use a **fresh disposable workspace before first indexing**. Provider/index settings are workspace-wide, consent is per collection. Prepare verified local `output-tokenizer`, `voyage-context-4` and `rerank-2.5` assets and installed required packages beforehand. No model download or user document is part of this smoke journey.

1. In Models set `offline=false`, `providers.text.provider=voyage`, `providers.text.model=voyage-context-4`, `providers.text.credential_ref=env:TYCHORA_SMOKE_VOYAGE_KEY`, `providers.rerank.provider=voyage`, `providers.rerank.model=rerank-2.5`, the corresponding reference, and `index.text.backend=chroma`. Review preview, then activate. Image indexing has its own supported backend field and is not implied by text configuration.
2. The named environment value belongs to the server process. Real usage must supply it privately before starting the owner. Never paste a key into Models or TOML. Catalog/readiness do not resolve or qualify credentials.
3. Separately grant the destination collection's Voyage upload consent. Confirm the current caller permission. Configuration alone must not grant it; denial and later revocation block dispatch.
4. Index a small synthetic text through Add documents, follow durable acceptance to success, query the text and inspect its exact passage/chunk/unit. Observe actual completed retrieval channels.
5. A successful rerank has no current response success marker. The qualified automated journey records actual invocation/model/candidate payload at the injected provider-transport boundary. Configuration, complete coverage and lack of warnings do not prove rerank execution or live provider qualification.

The automated version uses **explicitly simulated Voyage transport**, real already-installed tokenizers, local Chroma, dashboard, authenticated HTTP and durable jobs. A synthetic server-only environment sentinel is accepted only by that transport. No actual provider transport is constructed or contacted; synthetic outputs do not qualify connectivity, credentials, live quality or paid service behavior.

## Develop and verify

Frontend dependencies/browser must already be installed for offline local verification. From the repository root:

```sh
npm --prefix dashboard test
npm --prefix dashboard run typecheck
npm --prefix dashboard run build
npm --prefix dashboard run check:build
```

Build writes deterministic notices and compiled package resources; commit them with source. Resource/distribution tests cover wheel/sdist inclusion, not platform executable qualification.

The default Playwright suite starts a disposable model-free local server. Its deterministic synthetic index/output counters have distinct declared identities, so ordinary indexing persists a window larger than the returned query excerpt; this qualifies clipped-anchor resolution, not real tokenizer sizing or retrieval quality. It binds neither the user's workspace nor the running preview. Specify an installed Chromium with `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`, a Python with server/test dependencies with `TYCHORA_E2E_PYTHON`, and an absolute `PYTHONPATH` (the config otherwise resolves the repository source path). Screenshots/fixtures live under ignored `.superpowers/verification/dashboard-e2e/`; traces are off and screenshots are taken after token input clears.

```sh
export PYTHONPATH="$(pwd)/src"
cd dashboard
TYCHORA_E2E_PYTHON=../.venv/bin/python npm run e2e
```

Model-dependent qualification is **opt-in**, separate from thin CI. An explicit selected scenario fails on missing prerequisites; an unselected qualified test is reported as skipped, not executed. Use a new output/fixture directory when preserving prior evidence. Paths below refer to already-verified local assets; no CI trigger/download is implied.

```sh
export TYCHORA_MODEL_CACHE=/path/to/already-verified/cache
TYCHORA_E2E_SCENARIO=remote npm run e2e -- --grep 'OCR intake: qualified simulated Voyage'
export TYCHORA_TEST_DOCLING_MODELS="$TYCHORA_MODEL_CACHE"
export TYCHORA_TEST_OCR_MODELS="$TYCHORA_MODEL_CACHE/rapidocr/3.9.2"
TYCHORA_E2E_SCENARIO=ocr npm run e2e -- --grep 'OCR intake: qualified actual OCR'
```

The OCR journey generates a synthetic raster/native PDF, performs streamed authenticated HTTP intake, runs actual local OCR, queries real registered exclude/include modes and verifies noncitable evidence, stored chunks and scope denial. This tests an operable path and provenance, not transcription accuracy or full-book capacity. The ordinary CI workflow intentionally installs only thin server/development dependencies and runs the model-free scenario; a separately provisioned runner can execute these explicit qualification commands.

The streamed-PDF browser qualification uses `TYCHORA_E2E_SCENARIO=ocr` and the same explicit installed asset/tokenizer prerequisites above, with `npm --prefix dashboard run e2e -- --grep 'PDF intake: '`. It generates a readable textured raster PDF larger than 1 MiB, uses the single browser form, verifies real analysis/hash confirmation, injected transient failure plus durable retry, actual OCR publication/provenance/page geometry/review modes and desktop/mobile returned-passage/full-unit/stored-chunk views. A genuine throttled upload is cancelled through the keyboard. Test-only transient/partial failures are labeled synthetic and do not qualify real timeout recovery or transcription quality. The model-free scenario skips qualified OCR tests explicitly; skipped tests are not successful local executions. Synthetic contract envelopes can be recaptured with `dashboard/scripts/generate_contracts.py --pdf`; `--pdf-evidence <ignored actual-streamed-pdf.json>` exports the actual qualified browser responses without repeating OCR. Receipts/expiry are redacted from committed examples.
