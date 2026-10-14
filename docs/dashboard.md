# Local evidence workbench

The bundled dashboard has Search, Sources, Jobs, Evaluations, Activity and Settings, backed by the same authenticated services as the Python and HTTP clients. Node is required only to develop or rebuild the frontend; the wheel and executable resource collector include the compiled application.

## Start and connect

Install the `server` extra (from a checkout, `uv sync --locked --extra server`) and prepare a workspace and verified output tokenizer following [getting started](getting-started.md). Start one owner process:

```bash
python -m tychora.interfaces.http.server ./knowledge --port 8765
```

Open **http://127.0.0.1:8765/dashboard/**. Use the numeric loopback host: `localhost` is a different authority and is rejected. If you omit `--port`, the actual bound port is recorded privately in `knowledge/.runtime/endpoint.json`.

The running owner's `.runtime/endpoint.json` contains the actual `token` and `authority`. Do not print the whole record, paste it into a report, or copy the token into shell arguments. On macOS, this command reads that record and sends only the token directly to the local clipboard without printing it:

```bash
python - <<'PY'
import json
import subprocess
from pathlib import Path
record = json.loads(Path("knowledge/.runtime/endpoint.json").read_text())
subprocess.run(["pbcopy"], input=record["token"].encode(), check=True)
PY
```

Paste it into the dashboard's **Bearer token** input. Clear the clipboard after connecting (on macOS, `printf '' | pbcopy`). On other systems, use a trusted local clipboard tool or private editor to retrieve only the token. Keep the runtime directory and record owner-only; retrieve it only from the workspace you started. The server's client-side identity validation is described in [HTTP](http.md).

The dashboard keeps the token only in session component memory. It uses authenticated, scoped fetches and stores neither credentials nor workspace data in cookies, local storage or session storage. Refreshing requires reconnection. Disconnect clears workspace data and cancels pending requests and event streams; it does not cancel accepted source jobs. Opening a job URL and reconnecting restores its persisted status. Every API operation remains scope-bound and server-authorized.

## Supported workflows and limits

- **Search:** real retrieval, bounded result counts, registered facet/date/revision filters, exact evidence IDs and locators, stable citation copy and authoritative response JSON export. Inline original rendering is limited to text and safe raster images; other retained media is downloaded. PDFs have retained bytes and locators, without a bounding-box overlay.
- **Sources:** granted collections, bounded inventory, revision/extraction/channel coverage, original inspection, pasted text, user-selected files and explicit server paths within configured ingest roots. Review destination and enabled providers before confirming. The HTTP request limit is **1 MiB**, including metadata and JSON/base64 overhead; the form rejects an encoded request at or above that limit before sending it. File content base64 expansion means the usable raw file size is smaller. This interface is for small sources; it never silently truncates uploads.
- **Jobs:** durable receipt/status/events, authenticated SSE reconnection and server-supported recovery, retry and cancellation. Event counts are actual events, not estimated progress. Closing the page leaves durable work running.
- **Relations:** explicit exploration from selected evidence, at most 30 edges and up to two hops, accessible returned edge/provenance details. Missing channels and empty/truncated results remain explicit. This is not a full-corpus graph.
- **Evaluations:** managers can inspect real reports/cases, submit runs against registered datasets, and compare common numeric metrics. The list considers at most 100 recent records before authorization, with no continuation. Context differences are shown and arithmetic deltas alone do not establish comparable quality. Dataset registration and label editing use the API; the UI does not provide them.
- **Activity:** bounded retrieval metadata, separate from jobs. Default metadata-only history says **Query text not retained** and cannot replay. Replay appears only for reported retained requests. Trace generation, enforced expiry/purge and complete history configuration remain engine follow-ups.
- **Settings:** redacted effective configuration, origins, kinds and capabilities. Managers can explicitly activate complete workspace TOML with a registered profile. Activation may reject incompatible or busy changes and does not automatically queue a rebuild. Credentials remain references. Reader grants have no write/management controls.

One connection represents one workspace and its granted collection IDs. Collection lifecycle, global workspace discovery, aggregate evaluation analytics and automatic rebuild planning remain unavailable. Consult [implementation limits](review-assessment.md) before relying on unimplemented engine work.

## Develop and verify

The pinned frontend source and lockfile ship in the sdist. From `dashboard/`:

```bash
npm ci --ignore-scripts
npm test
npm run typecheck
npm run build
npm run check:build
npx playwright install chromium
TYCHORA_E2E_PYTHON=../.venv/bin/python npm run e2e
```

`build` writes deterministic production dependency notices and compiled resources into `src/tychora/resources/dashboard`. Commit rebuilt resources with source changes; `check:build` rejects drift, including new untracked resources. Playwright serves the ordinary `create_app` and compiled bundle from a disposable offline synthetic workspace, without browser API mocks or live providers. Browser screenshots and failure records live in ignored verification artifacts; traces are disabled to keep authorization headers out of recordings. An optional `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` supports an existing local browser; CI installs the pinned revision. Python packaging regression builds a wheel and sdist and checks resources, notices, frontend sources and exclusions. Executable resources use the existing `collect_data_files("tychora")`; this wheel check does not qualify platform executables.
