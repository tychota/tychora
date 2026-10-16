import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { QueryResult } from "../api/contracts";
import { ApiClient } from "../api/client";
import { Search } from "../pages/Search";
import { OperationsRouter } from "../pages/OperationsRouter";
import { ModelDraftProvider } from "../pages/models/draft";
import { EvidenceReader } from "../components/EvidenceReader";
import { Relations } from "../components/Relations";
import query from "../../contracts/query.json";
import evidence from "../../contracts/evidence.json";
import handshake from "../../contracts/handshake.json";
import fields from "../../contracts/settings-fields.json";
import models from "../../contracts/models-list.json";
import readiness from "../../contracts/models-readiness.json";
import ocrQuery from "../../contracts/ocr-included-query.json";
import ocrEvidence from "../../contracts/ocr-evidence.json";
import windows from "../../contracts/extraction-windows.json";
import jobStatus from "../../contracts/job-status.json";

const state = vi.hoisted(() => ({
  session: {} as Record<string, unknown>,
  draft: { setupReturn: null },
}));
vi.mock("../session", () => ({ useSession: () => state.session }));
vi.mock("../pages/documents/draft", () => ({
  useAddDocumentDraft: () => ({ draft: state.draft }),
}));
let calls: { path: string; init: RequestInit }[];
let response: (path: string, init: RequestInit) => unknown;
let result: QueryResult = structuredClone(query) as QueryResult;
const modes = [
  {
    id: "exclude",
    label: "Native only",
    definition: {
      allowed_uses: ["lookup"],
      allow_derived_proxies: false,
      derived_proxy_policy: "deny",
      derived_proxy_review_states: [],
    },
  },
  {
    id: "include",
    label: "Review evidence",
    definition: {
      allowed_uses: ["lookup"],
      allow_derived_proxies: true,
      derived_proxy_policy: "allow",
      derived_proxy_review_states: ["needs_review"],
    },
  },
];
beforeEach(() => {
  calls = [];
  result = structuredClone(query) as QueryResult;
  const client = new ApiClient("synthetic", handshake.scope);
  state.session = {
    client,
    adminClient: client.credentialOnly(),
    handshake,
    refreshGrant: vi.fn(async () => undefined),
  };
  response = (path) =>
    path.startsWith("/v1/discovery")
      ? {
          definitions: path.includes("profiles") ? modes : [],
          counts: [],
          capabilities: [],
          next_cursor: null,
        }
      : path === "/v1/settings/fields"
        ? fields
        : path === "/v1/settings"
          ? {
              profile: "local",
              values: { default_mode: "exclude", providers: {} },
            }
          : path === "/v1/models"
            ? models
            : path.startsWith("/v1/models/readiness")
              ? readiness
              : path === "/v1/queries"
                ? result
                : path.includes("/windows")
                  ? windows
                  : path.startsWith("/v1/evidence/")
                    ? evidence
                    : { paths: [], truncated: false, next_cursor: null };
  vi.stubGlobal("fetch", async (path: string, init: RequestInit = {}) => {
    calls.push({ path, init });
    const value = response(path, init);
    return value instanceof Response
      ? value
      : new Response(JSON.stringify(value));
  });
});
it("loads actual retained stage events for a terminal job", async () => {
  const old = response;
  response = (path, init) =>
    path.includes("/events/page")
      ? [
          {
            job_id: jobStatus.id,
            sequence: 1,
            stage: "published-real-stage",
            state: "succeeded",
            timestamp: "2026-01-01T00:00:00Z",
            counters: [{ name: "accepted_pages", count: 3 }],
            diagnostic_refs: [],
          },
        ]
      : path === `/v1/jobs/${jobStatus.id}`
        ? { ...jobStatus, state: "succeeded" }
        : old(path, init);
  render(<OperationsRouter path={`/dashboard/jobs/${jobStatus.id}`} />);
  expect(await screen.findByText(/published-real-stage/)).toBeInTheDocument();
  expect(screen.getByText("accepted_pages: 3")).toBeInTheDocument();
});
it("selects actual visual-only OCR without inventing a Segment or citable quote", async () => {
  result = structuredClone(ocrQuery) as QueryResult;
  const proxy = result.visuals[0].proxies[0];
  const old = response;
  response = (path, init) =>
    path.startsWith("/v1/evidence/") ? ocrEvidence : old(path, init);
  render(<Search />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "OCR" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Inspect returned ocr proxy" }),
  );
  expect(
    screen.getByRole("region", { name: "Returned proxy passage" }).textContent,
  ).toContain(proxy.text);
  expect(screen.queryByText("No matching evidence")).toBeNull();
  expect(screen.getByText("1 visual result")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Page 1" })).toBeInTheDocument();
  expect(calls.some((c) => c.path.startsWith("/v1/evidence/"))).toBe(false);
  expect(screen.getByRole("button", { name: "Stored chunks" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Full unit" }));
  expect(
    await screen.findByRole("button", { name: "Copy provenance reference" }),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Stored chunks" })).toBeEnabled(),
  );
});
it("invalidates an in-flight purge plan when its source target changes", async () => {
  let release!: (r: Response) => void;
  const fallback = globalThis.fetch;
  vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
    path === "/v1/jobs"
      ? new Promise<Response>((resolve) => {
          release = resolve;
        })
      : fallback(path, init),
  );
  render(<OperationsRouter path="/dashboard/settings" />);
  fireEvent.change(screen.getByLabelText("Purge source ID"), {
    target: { value: "first" },
  });
  fireEvent.change(screen.getByLabelText("Purge revision IDs"), {
    target: { value: "revision" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Plan original purge" }));
  fireEvent.change(screen.getByLabelText("Purge source ID"), {
    target: { value: "second" },
  });
  release(
    new Response(
      JSON.stringify({
        plan: {
          operation: "purge-originals",
          target_ids: ["first"],
          actions: [],
        },
      }),
    ),
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Plan original purge" }),
    ).not.toBeDisabled(),
  );
  expect(
    screen.queryByRole("button", { name: "Confirm original purge" }),
  ).toBeNull();
});
it("ignores an old expert activation after route remount and preserves the newer draft", async () => {
  let release!: (response: Response) => void;
  const fallback = globalThis.fetch;
  vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
    path === "/v1/settings" && init.method === "PUT"
      ? new Promise<Response>((resolve) => {
          release = resolve;
        })
      : fallback(path, init),
  );
  const route = (path: string) => (
    <ModelDraftProvider>
      <OperationsRouter path={path} />
    </ModelDraftProvider>
  );
  const view = render(route("/dashboard/models"));
  fireEvent.click(await screen.findByText("Expert complete TOML activation"));
  fireEvent.change(screen.getByLabelText("Activation profile"), {
    target: { value: "old-profile" },
  });
  fireEvent.change(screen.getByLabelText("Complete workspace TOML"), {
    target: { value: "old submitted draft" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Activate configuration" }),
  );
  expect(screen.getByLabelText("Complete workspace TOML")).toBeDisabled();
  view.rerender(route("/dashboard/settings"));
  view.rerender(route("/dashboard/models"));
  fireEvent.click(await screen.findByText("Expert complete TOML activation"));
  fireEvent.change(screen.getByLabelText("Activation profile"), {
    target: { value: "new-profile" },
  });
  fireEvent.change(screen.getByLabelText("Complete workspace TOML"), {
    target: { value: "new unsent draft" },
  });
  await act(async () => {
    release(new Response(JSON.stringify({ activated: true })));
  });
  expect(screen.getByLabelText("Complete workspace TOML")).toHaveValue(
    "new unsent draft",
  );
  expect(screen.getByLabelText("Activation profile")).toHaveValue(
    "new-profile",
  );
  expect(screen.queryByText(/Configuration activated\./)).toBeNull();
  expect(state.session.refreshGrant).not.toHaveBeenCalled();
});
async function search() {
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "synthetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  fireEvent.click(
    await screen.findByRole("button", { name: /Record synthetic/ }),
  );
}
it("defaults to exact returned blocks and never fetches full unit implicitly", async () => {
  const segment = result.segments[0];
  segment.excerpts[0].text = "Exact 😀 é <strong>safe</strong>\r\n";
  segment.context_excerpts = [
    { unit_id: "context", char_start: 7, char_end: 14, text: "Context" },
  ];
  segment.context_only_unit_ids = ["context"];
  segment.complete = false;
  segment.omissions = [{ reason: "budget", count: 1, unit_ids: [] }];
  render(<Search />);
  await search();
  const reader = await screen.findByRole("region", {
    name: "Returned passage",
  });
  expect(
    within(reader).getByText(segment.excerpts[0].text, {
      exact: true,
      normalizer: (value) => value,
    }),
  ).toBeInTheDocument();
  expect(within(reader).getByText(/Context only/)).toBeInTheDocument();
  expect(within(reader).getByText(/budget/)).toBeInTheDocument();
  expect(reader.querySelector("strong")).toBeNull();
  expect(calls.some((c) => c.path.startsWith("/v1/evidence/"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Full unit" }));
  expect(await screen.findByText("Exact evidence")).toBeInTheDocument();
});
it("resolves a clipped anchor by real chunk ID and shows larger server text", async () => {
  result.segments[0].anchors[0].indexed_window_id = windows.windows[0].id;
  render(<Search />);
  await search();
  fireEvent.click(screen.getByRole("button", { name: "Stored chunks" }));
  expect(
    await screen.findByText(windows.windows[0].text, { exact: true }),
  ).toBeInTheDocument();
  expect(
    calls.some((c) => c.path.includes(`chunk_id=${windows.windows[0].id}`)),
  ).toBe(true);
});
it("keeps a null window ID null and a structured-only segment selectable", async () => {
  result.segments[0].anchors[0].indexed_window_id = null;
  result.segments[0].matched_unit_ids = [];
  result.segments[0].context_only_unit_ids = [];
  result.segments[0].excerpts = [];
  result.segments[0].text = "";
  result.segments[0].structured_records = [
    {
      record_id: "r",
      unit_id: "u",
      record_type: "record",
      schema_id: "synthetic@1",
      value: { literal: "😀" },
      dependencies: [],
    },
  ];
  render(<Search />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "synthetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  fireEvent.click(
    await screen.findByRole("button", { name: /Structured evidence/ }),
  );
  expect(
    await screen.findByRole("region", { name: "Returned passage" }),
  ).toHaveTextContent("Structured records");
  fireEvent.click(screen.getByRole("button", { name: "Stored chunks" }));
  expect(
    await screen.findByText(/No stored chunk identity/),
  ).toBeInTheDocument();
  expect(calls.some((c) => c.path.includes("/windows"))).toBe(false);
});
it("shows submitted mode review policy and supported explicit inclusion", async () => {
  render(<Search />);
  await screen.findByText(/Review-required content.*excluded/);
  await search();
  fireEvent.click(
    screen.getByRole("button", { name: /Include review-required/ }),
  );
  await waitFor(() =>
    expect(calls.filter((c) => c.path === "/v1/queries").length).toBe(2),
  );
  expect(
    JSON.parse(
      calls.filter((c) => c.path === "/v1/queries").at(-1)!.init.body as string,
    ).mode,
  ).toBe("include");
  expect(
    screen.getByText(/Review-required content.*included/),
  ).toBeInTheDocument();
});
it("submits structured passage fields and retries the submitted body despite edited drafts", async () => {
  render(<Search />);
  await screen.findByText(/Review-required content.*excluded/);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "simple draft" },
  });
  fireEvent.change(screen.getByLabelText("Result limit"), {
    target: { value: "3" },
  });
  fireEvent.click(screen.getByLabelText("Structured request"));
  expect(screen.queryByLabelText("Question")).toBeNull();
  expect(screen.queryByLabelText("Result limit")).toBeNull();
  expect(screen.queryByLabelText("Retrieval mode")).toBeNull();
  expect(
    screen.getByText(/Search uses the QueryRequest JSON/),
  ).toBeInTheDocument();
  const body = {
    input: { type: "passage", text: "retained passage" },
    mode: "exclude",
    hints: ["specific"],
    context: { task: "comparison" },
    modalities: ["text"],
    exact_only: true,
  };
  fireEvent.change(screen.getByLabelText("QueryRequest JSON"), {
    target: { value: JSON.stringify(body) },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  await waitFor(() =>
    expect(calls.filter((c) => c.path === "/v1/queries")).toHaveLength(1),
  );
  expect(
    JSON.parse(
      calls.find((c) => c.path === "/v1/queries")!.init.body as string,
    ),
  ).toEqual(body);
  const editedDraft =
    '{"input":{"type":"image","asset_ref":"draft"},"mode":"include"}';
  fireEvent.change(screen.getByLabelText("QueryRequest JSON"), {
    target: { value: editedDraft },
  });
  fireEvent.click(screen.getByLabelText("Structured request"));
  expect(screen.queryByLabelText("QueryRequest JSON")).toBeNull();
  expect(screen.getByLabelText("Question")).toHaveValue("simple draft");
  expect(screen.getByLabelText("Result limit")).toHaveValue(3);
  fireEvent.change(screen.getByLabelText("Retrieval mode"), {
    target: { value: "include" },
  });
  expect(
    screen.getByText(/Review-required content.*excluded/),
  ).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: /Include review-required/ }),
  );
  await waitFor(() =>
    expect(calls.filter((c) => c.path === "/v1/queries")).toHaveLength(2),
  );
  expect(
    JSON.parse(
      calls.filter((c) => c.path === "/v1/queries").at(-1)!.init.body as string,
    ),
  ).toEqual({ ...body, mode: "include" });
  fireEvent.click(screen.getByLabelText("Structured request"));
  expect(screen.getByLabelText("QueryRequest JSON")).toHaveValue(editedDraft);
});
it("retains structured drafts after invalid JSON, size rejection and typed backend rejection", async () => {
  render(<Search />);
  fireEvent.click(screen.getByLabelText("Structured request"));
  const editor = screen.getByLabelText("QueryRequest JSON");
  fireEvent.change(editor, { target: { value: "{broken" } });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  expect(await screen.findByText(/valid JSON/)).toBeInTheDocument();
  expect(editor).toHaveValue("{broken");
  fireEvent.change(editor, {
    target: {
      value: JSON.stringify({
        input: { type: "question", text: "x".repeat(1048576) },
      }),
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  expect(await screen.findByText(/1 MiB/)).toBeInTheDocument();
  expect(calls.some((c) => c.path === "/v1/queries")).toBe(false);
  const old = response;
  response = (path, init) =>
    path === "/v1/queries"
      ? new Response(
          JSON.stringify({
            code: "INVALID_REQUEST",
            message: "Unsupported mode",
            retryable: false,
          }),
          { status: 422 },
        )
      : old(path, init);
  const draft =
    '{"input":{"type":"image","asset_ref":"asset"},"mode":"unknown"}';
  fireEvent.change(editor, { target: { value: draft } });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  expect(await screen.findByText(/Unsupported mode/)).toBeInTheDocument();
  expect(editor).toHaveValue(draft);
});
it("aborts a structured request and ignores its completion when scope changes", async () => {
  const original = state.session.client as ApiClient;
  let finish!: (value: Response) => void;
  let querySignal: AbortSignal | null | undefined;
  vi.stubGlobal("fetch", (path: string, init: RequestInit = {}) => {
    if (path === "/v1/queries") {
      querySignal = init.signal;
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    }
    return Promise.resolve(new Response(JSON.stringify(response(path, init))));
  });
  const view = render(<Search />);
  await screen.findByText(/Review-required content.*excluded/);
  fireEvent.click(screen.getByLabelText("Structured request"));
  fireEvent.change(screen.getByLabelText("QueryRequest JSON"), {
    target: { value: '{"input":{"type":"passage","text":"old scope"}}' },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  await waitFor(() => expect(querySignal).toBeDefined());
  original.dispose();
  state.session.client = new ApiClient("synthetic", {
    ...handshake.scope,
    collection_ids: ["new-scope"],
  });
  view.rerender(<Search />);
  await act(async () => finish(new Response(JSON.stringify(result))));
  expect(querySignal?.aborted).toBe(true);
  expect(screen.queryByRole("region", { name: "Search results" })).toBeNull();
  expect(screen.getByRole("button", { name: "Search evidence" })).toBeEnabled();
});
it("resolves an omitted request mode before sending when initial settings are delayed", async () => {
  const pending: ((value: Response) => void)[] = [];
  vi.stubGlobal("fetch", (path: string, init: RequestInit = {}) => {
    calls.push({ path, init });
    if (path === "/v1/settings")
      return new Promise<Response>((resolve) => {
        pending.push(resolve);
      });
    return Promise.resolve(new Response(JSON.stringify(response(path, init))));
  });
  render(<Search />);
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "default mode" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
  expect(calls.some((c) => c.path === "/v1/queries")).toBe(false);
  await act(async () => {
    for (const finish of pending)
      finish(
        new Response(
          JSON.stringify({
            profile: "local",
            values: { default_mode: "exclude" },
          }),
        ),
      );
  });
  await waitFor(() =>
    expect(calls.filter((c) => c.path === "/v1/queries")).toHaveLength(1),
  );
  expect(
    screen.getByText(/Review-required content.*excluded by exclude/),
  ).toBeInTheDocument();
  expect(
    JSON.parse(
      calls.find((c) => c.path === "/v1/queries")!.init.body as string,
    ),
  ).not.toHaveProperty("mode");
});
it("shows scoped empty relations separately from a typed unavailable channel", async () => {
  const client = state.session.client as ApiClient;
  const old = response;
  response = (path, init) =>
    path === "/v1/relations"
      ? { paths: [], truncated: true, next_cursor: null }
      : old(path, init);
  render(<Relations client={client} unitIds={["seed"]} />);
  fireEvent.click(screen.getByRole("button", { name: "Explore relations" }));
  expect(
    await screen.findByText(/No relation paths.*selected seed/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Relation view is truncated/)).toBeInTheDocument();
  expect(screen.queryByText(/unavailable relation channel/)).toBeNull();
  response = (path, init) =>
    path === "/v1/relations"
      ? new Response(
          JSON.stringify({
            code: "INDEX_INCOMPLETE",
            message: "Relation channel unavailable",
            retryable: false,
          }),
          { status: 409 },
        )
      : old(path, init);
  fireEvent.click(screen.getByRole("button", { name: "Explore relations" }));
  expect(
    await screen.findByText(/Relation channel unavailable/),
  ).toBeInTheDocument();
  expect(screen.queryByText(/No relation paths.*selected seed/)).toBeNull();
});
it("labels noncitable full evidence references without a citation action", async () => {
  const old = response;
  response = (path, init) =>
    path.startsWith("/v1/evidence/")
      ? {
          ...evidence,
          evidence: {
            ...evidence.evidence,
            text_origin: "ocr",
            citable: false,
            review_state: "needs_review",
          },
        }
      : old(path, init);
  render(
    <EvidenceReader
      client={state.session.client as ApiClient}
      unitId={evidence.id}
    />,
  );
  expect(
    await screen.findByRole("button", { name: "Copy provenance reference" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Copy citation" }),
  ).not.toBeInTheDocument();
});
it("guides offline and backend drafts through preview before activation", async () => {
  const old = response;
  response = (path, init) =>
    path === "/v1/settings/preview"
      ? {
          activation_allowed: true,
          blockers: [],
          changes: [],
          rebuild_queued: false,
        }
      : old(path, init);
  render(<OperationsRouter path="/dashboard/models" />);
  fireEvent.change(await screen.findByLabelText("offline"), {
    target: { value: "false" },
  });
  fireEvent.change(screen.getByLabelText("index.text.backend"), {
    target: { value: "chroma" },
  });
  expect(
    screen.getByRole("button", { name: "Activate reviewed changes" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Activate reviewed changes" }),
    ).not.toBeDisabled(),
  );
  const patch = JSON.parse(
    calls.find((c) => c.path === "/v1/settings/preview")!.init.body as string,
  );
  expect(patch.base_workspace_sha256).toBe(fields.base_workspace_sha256);
  expect(patch.changes).toContainEqual({
    path: "offline",
    action: "set",
    value: false,
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Activate reviewed changes" }),
  );
  await waitFor(() => expect(state.session.refreshGrant).toHaveBeenCalled());
});
it("keeps preview blockers and never activates an incompatible generation", async () => {
  const old = response;
  response = (path, init) =>
    path === "/v1/settings/preview"
      ? {
          activation_allowed: false,
          blockers: ["PROFILE_INCOMPATIBLE"],
          changes: [],
          rebuild_queued: false,
        }
      : old(path, init);
  render(<OperationsRouter path="/dashboard/models" />);
  fireEvent.change(await screen.findByLabelText("offline"), {
    target: { value: "false" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(await screen.findByText(/PROFILE_INCOMPATIBLE/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Activate reviewed changes" }),
  ).toBeDisabled();
});
it("runs measured workspace health with full administrative authority", async () => {
  const old = response;
  response = (path, init) =>
    path === "/v1/jobs"
      ? {
          plan: { operation: "health", target_ids: ["workspace"], actions: [] },
          recovery: {
            workspace_id: "workspace",
            verified_payloads: 3,
            native_pending_batches: 0,
            interrupted_jobs: 0,
            deletion_suspended: false,
          },
        }
      : old(path, init);
  render(<OperationsRouter path="/dashboard/settings" />);
  fireEvent.click(
    await screen.findByRole("button", { name: "Run health check" }),
  );
  expect(await screen.findByText(/verified_payloads: 3/)).toBeInTheDocument();
  expect(
    new Headers(calls.find((c) => c.path === "/v1/jobs")!.init.headers).has(
      "X-Tychora-Scope",
    ),
  ).toBe(false);
});
it("requires purge plan and explicit confirmation before destructive submission", async () => {
  const old = response;
  response = (path, init) =>
    path === "/v1/jobs"
      ? {
          plan: {
            operation: "purge-originals",
            target_ids: ["revision"],
            actions: ["remove original bindings"],
            estimated_bytes: 4,
          },
        }
      : old(path, init);
  render(<OperationsRouter path="/dashboard/settings" />);
  fireEvent.change(screen.getByLabelText("Purge source ID"), {
    target: { value: "source" },
  });
  fireEvent.change(screen.getByLabelText("Purge revision IDs"), {
    target: { value: "revision" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Plan original purge" }));
  expect(
    await screen.findByRole("button", { name: "Confirm original purge" }),
  ).toBeDisabled();
  fireEvent.click(screen.getByLabelText(/I confirm removal/));
  fireEvent.click(
    screen.getByRole("button", { name: "Confirm original purge" }),
  );
  await waitFor(() =>
    expect(calls.filter((c) => c.path === "/v1/jobs").length).toBe(2),
  );
  expect(
    JSON.parse(
      calls.filter((c) => c.path === "/v1/jobs").at(-1)!.init.body as string,
    ).confirm_original_removal,
  ).toBe(true);
});
it("registers bounded dataset bytes and displays the actual dataset receipt", async () => {
  const old = response;
  response = (path, init) =>
    path === "/v1/evaluations"
      ? []
      : path === "/v1/evaluations/datasets"
        ? { dataset_id: "synthetic-dataset", version_sha: "verified-sha" }
        : old(path, init);
  render(<OperationsRouter path="/dashboard/evaluations" />);
  fireEvent.change(screen.getByLabelText("Dataset manifest JSON"), {
    target: { value: "{}" },
  });
  fireEvent.change(screen.getByLabelText("Dataset cases JSONL"), {
    target: { value: "{}" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Register dataset" }));
  expect(await screen.findByText(/synthetic-dataset/)).toBeInTheDocument();
  expect(
    JSON.parse(
      calls.find((c) => c.path === "/v1/evaluations/datasets")!.init
        .body as string,
    ),
  ).toEqual({ manifest_json: "{}", cases_jsonl: "{}" });
});
