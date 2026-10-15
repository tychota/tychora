import {
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
import { EvidenceReader } from "../components/EvidenceReader";
import query from "../../contracts/query.json";
import evidence from "../../contracts/evidence.json";
import handshake from "../../contracts/handshake.json";
import fields from "../../contracts/task3-settings-fields.json";
import models from "../../contracts/task3-models.json";
import readiness from "../../contracts/task3-model-readiness.json";
import ocrQuery from "../../contracts/task5-ocr-included-query.json";
import ocrEvidence from "../../contracts/task5-ocr-evidence.json";
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
