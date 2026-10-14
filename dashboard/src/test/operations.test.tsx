import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import handshake from "../../contracts/handshake.json";
import sources from "../../contracts/sources.json";
import revisions from "../../contracts/source-revisions.json";
import receipt from "../../contracts/source-add.json";
import jobFixture from "../../contracts/job-status.json";
import type { JobStatus, Evaluation } from "../api/operations";
const job = jobFixture as JobStatus;
import historyPage from "../../contracts/query-history.json";
import run from "../../contracts/query-run.json";
import evaluation from "../../contracts/evaluation-detail.json";
import { App } from "../App";
import { OperationsRouter } from "../pages/OperationsRouter";
import { parseEvents, watchJob } from "../api/events";
import { ApiClient } from "../api/client";
import { compareEvaluations } from "../pages/evaluations/compare";
let requests: { path: string; init: RequestInit }[];
let respond: (path: string, init: RequestInit) => Response | Promise<Response>;
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
beforeEach(() => {
  requests = [];
  respond = (path) =>
    json(
      path === "/v1/handshake"
        ? handshake
        : path.startsWith("/v1/discovery")
          ? {
              definitions: [],
              counts: [],
              capabilities: [],
              capability_status: [],
              next_cursor: null,
            }
          : path === "/v1/settings"
            ? {
                profile: "local",
                values: { providers: { text: { provider: "none" } } },
              }
            : path.startsWith("/v1/sources")
              ? sources
              : path.startsWith("/v1/jobs")
                ? { jobs: [job], next_cursor: null }
                : historyPage,
    );
  vi.stubGlobal("fetch", async (path: string, init: RequestInit = {}) => {
    requests.push({ path, init });
    return respond(path, init);
  });
});
async function open(path: string) {
  history.replaceState({}, "", path);
  render(<App renderRoute={(path) => <OperationsRouter path={path} />} />);
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "private-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
}
it("parses split UTF-8, CRLF, comments and multiline data, ignoring duplicate acknowledged IDs", async () => {
  const bytes = new TextEncoder().encode(
    ': comment\r\nid: 7\r\nevent: job\r\ndata: {"stage":"é",\r\ndata: "sequence":7}\r\n\r\nid: 7\nevent: job\ndata: {}\n\nid: 8\nevent: job\ndata: {"sequence":8}\n\n',
  );
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      for (const byte of bytes) c.enqueue(Uint8Array.of(byte));
      c.close();
    },
  });
  const ids: string[] = [];
  await parseEvents(
    stream,
    async (e) => {
      ids.push(e.id);
    },
    new AbortController().signal,
  );
  expect(ids).toEqual(["7", "8"]);
});
it("reconnects after refresh with persisted sequence and keeps auth scoped", async () => {
  let streams = 0;
  let refreshed = 0;
  respond = () =>
    new Response(
      ++streams === 1
        ? 'id: 7\nevent: job\ndata: {"sequence":7,"state":"running"}\n\nevent: refresh\ndata: {}\n\n'
        : 'id: 8\nevent: job\ndata: {"sequence":8,"state":"succeeded"}\n\n',
      { headers: { "Content-Type": "text/event-stream" } },
    );
  await watchJob(
    new ApiClient("private-token", handshake.scope),
    "job",
    new AbortController().signal,
    {
      onEvent: async () => {},
      refresh: async () => {
        refreshed++;
        return { ...job, state: "running", latest_sequence: 7 };
      },
      delay: async () => {},
    },
  );
  expect(refreshed).toBe(1);
  expect(new Headers(requests[1].init.headers).get("Last-Event-ID")).toBe("7");
  expect(new Headers(requests[1].init.headers).get("Authorization")).toBe(
    "Bearer private-token",
  );
  expect(requests[1].init.cache).toBe("no-store");
});
it("stops event connections on authorization failure and session disposal", async () => {
  respond = () => json({ code: "FORBIDDEN_SCOPE", message: "Denied" }, 403);
  await expect(
    watchJob(
      new ApiClient("private-token", handshake.scope),
      "job",
      new AbortController().signal,
      {
        onEvent: async () => {},
        refresh: async () => job,
        delay: async () => {},
      },
    ),
  ).rejects.toMatchObject({ code: "FORBIDDEN_SCOPE" });
  expect(requests).toHaveLength(1);
  let signal: AbortSignal | undefined | null;
  respond = (_p, init) => {
    signal = init.signal;
    return new Response(new ReadableStream({ start() {} }));
  };
  const client = new ApiClient("private-token", handshake.scope);
  const pending = watchJob(client, "job", new AbortController().signal, {
    onEvent: async () => {},
    refresh: async () => job,
  });
  await waitFor(() => expect(signal).toBeDefined());
  client.dispose();
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
});
it("paginates sources, revisions and channels without claiming a current revision", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.includes("/channels")
      ? json({
          channels: [{ family_id: "f", channel: "lexical", state: "ready" }],
          next_cursor: path.includes("cursor=") ? null : "channels-more",
        })
      : path.includes("/extractions")
        ? json({
            extractions: revisions.revisions[0].extractions,
            next_cursor: null,
          })
        : path.includes("/revisions")
          ? json({
              ...revisions,
              next_cursor: path.includes("cursor=") ? null : "revisions-more",
            })
          : path.startsWith("/v1/sources?")
            ? json({
                ...sources,
                next_cursor: path.includes("cursor=") ? null : "sources-more",
              })
            : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(
    await screen.findByRole("button", { name: "Load more sources" }),
  );
  await waitFor(() =>
    expect(requests.some((r) => r.path.includes("cursor=sources-more"))).toBe(
      true,
    ),
  );
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Inspect revisions" }))[0],
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Load more revisions" }),
  );
  fireEvent.click(
    (await screen.findAllByRole("button", { name: "Inspect coverage" }))[0],
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Load more channels" }),
  );
  await waitFor(() =>
    expect(requests.some((r) => r.path.includes("cursor=channels-more"))).toBe(
      true,
    ),
  );
  expect(screen.queryByText(/current revision/i)).toBeNull();
});
it("requires confirmation, retains immutable source retry key, and links the durable receipt", async () => {
  const fallback = respond;
  let adds = 0;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? ++adds === 1
        ? Promise.reject(new Error("transport"))
        : json(receipt, 202)
      : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "manual" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Source text"), {
    target: { value: "héllo" },
  });
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByText("CONNECTION_FAILED");
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  const sent = requests.filter(
    (r) => r.path === "/v1/sources" && r.init.method === "POST",
  );
  expect(new Headers(sent[0].init.headers).get("Idempotency-Key")).toBe(
    new Headers(sent[1].init.headers).get("Idempotency-Key"),
  );
  expect(JSON.parse(sent[0].init.body as string)).toMatchObject({
    type: "text",
    text: "héllo",
  });
});
it("rejects encoded source requests at the byte limit before fetch", async () => {
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "large" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Source text"), {
    target: { value: "é".repeat(530000) },
  });
  await screen.findByText(/exceeds the 1 MiB/i);
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
  expect(
    requests.some((r) => r.path === "/v1/sources" && r.init.method === "POST"),
  ).toBe(false);
});
it("displays metadata-only activity and never offers replay for unretained text", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.startsWith("/v1/queries/") ? json(run) : fallback(path, init);
  await open("/dashboard/activity");
  await screen.findByText("Query text not retained");
  fireEvent.click(
    screen.getByRole("link", { name: historyPage.definitions[0].id }),
  );
  await screen.findByText("Run details");
  expect(
    screen.queryByRole("button", { name: "Replay retained query" }),
  ).toBeNull();
});
it("preserves settings draft on busy rejection", async () => {
  const fallback = respond;
  respond = (path, init) =>
    init.method === "PUT"
      ? json({ code: "WORKSPACE_BUSY", message: "Busy", retryable: true }, 409)
      : fallback(path, init);
  await open("/dashboard/settings");
  const draft = await screen.findByLabelText("Complete workspace TOML");
  fireEvent.change(draft, { target: { value: '[workspace]\nname="draft"' } });
  fireEvent.change(screen.getByLabelText("Activation profile"), {
    target: { value: "local" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Activate configuration" }),
  );
  await screen.findByText("WORKSPACE_BUSY");
  expect(draft).toHaveValue('[workspace]\nname="draft"');
});
it("shows recovery actions only for failed source jobs and sends service retry", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.endsWith("/retry")
      ? json({ ...receipt, state: "queued" })
      : path === `/v1/jobs/${job.id}`
        ? json({ ...job, state: "failed" })
        : fallback(path, init);
  await open(`/dashboard/jobs/${job.id}`);
  fireEvent.click(await screen.findByRole("button", { name: "Retry job" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) => r.path.endsWith("/retry") && r.init.method === "POST",
      ),
    ).toBe(true),
  );
});
it("labels evaluation context differences and only subtracts common numeric metrics", () => {
  const other = {
    ...evaluation,
    dataset_id: "different",
    hardware: { ...evaluation.hardware, offline: true },
    report: {
      ...evaluation.report,
      aggregate: {
        metrics: [
          { ...evaluation.report.aggregate.metrics[0], value: 0.5 },
          {
            name: "new",
            value: 1,
            sample_count: 1,
            denominator: 1,
            not_applicable_reason: null,
          },
        ],
      },
    },
  };
  const result = compareEvaluations(
    evaluation as Evaluation,
    other as Evaluation,
  );
  expect(result.differences.map((d) => d.field)).toContain("dataset_id");
  expect(result.differences.map((d) => d.field)).toContain("hardware");
  expect(result.deltas).toEqual([
    { name: "source_recall", left: 1, right: 0.5, delta: -0.5 },
  ]);
});
it("encodes selected file bytes and gives changed source intent a new key", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? json({ code: "WORKSPACE_BUSY", message: "Busy", retryable: true }, 409)
      : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "bytes" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Input type"), {
    target: { value: "bytes" },
  });
  fireEvent.change(screen.getByLabelText("Format"), {
    target: { value: "text" },
  });
  const file = new File([new Uint8Array([0, 255, 128])], "test.bin");
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => Uint8Array.of(0, 255, 128).buffer,
  });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByText("WORKSPACE_BUSY");
  const first = requests.find(
    (r) => r.path === "/v1/sources" && r.init.method === "POST",
  )!;
  expect(JSON.parse(first.init.body as string)).toMatchObject({
    type: "bytes",
    data: "AP-A",
  });
  fireEvent.change(screen.getByLabelText("Title"), {
    target: { value: "Changed intent" },
  });
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await waitFor(() =>
    expect(
      requests.filter(
        (r) => r.path === "/v1/sources" && r.init.method === "POST",
      ),
    ).toHaveLength(2),
  );
  const second = requests.filter(
    (r) => r.path === "/v1/sources" && r.init.method === "POST",
  )[1];
  expect(new Headers(first.init.headers).get("Idempotency-Key")).not.toBe(
    new Headers(second.init.headers).get("Idempotency-Key"),
  );
});
it("rejects oversized file bytes before reading or sending them", async () => {
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Input type"), {
    target: { value: "bytes" },
  });
  const file = new File(["small"], "large.bin");
  Object.defineProperty(file, "size", { value: 800000 });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  await screen.findByText("INPUT_BUDGET_EXCEEDED");
  expect(
    requests.some((r) => r.path === "/v1/sources" && r.init.method === "POST"),
  ).toBe(false);
});
it("continues extraction pages and does not invent absent counts", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.includes("/channels")
      ? json({ channels: [], next_cursor: null })
      : path.includes("/extractions")
        ? json({
            extractions: revisions.revisions[0].extractions,
            next_cursor: path.includes("cursor=") ? null : "extract-more",
          })
        : fallback(path, init);
  await open(
    `/dashboard/sources/${revisions.source_id}/revisions/${revisions.revisions[0].revision_id}`,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "Load more extractions" }),
  );
  await waitFor(() =>
    expect(requests.some((r) => r.path.includes("cursor=extract-more"))).toBe(
      true,
    ),
  );
  await screen.findAllByText(
    "No retrieval channels reported. Counts are unknown.",
  );
});
it("reconnects with the last consumed event and deduplicates events across streams", async () => {
  let streams = 0;
  const seen: number[] = [];
  respond = () =>
    new Response(
      ++streams === 1
        ? 'id: 7\nevent: job\ndata: {"sequence":7,"state":"running"}\n\nid: 8\nevent: job\ndata: {"sequence":8,"state":"running"}\n\n'
        : 'id: 8\nevent: job\ndata: {"sequence":8,"state":"running"}\n\nid: 9\nevent: job\ndata: {"sequence":9,"state":"succeeded"}\n\n',
    );
  await watchJob(
    new ApiClient("private-token", handshake.scope),
    "job",
    new AbortController().signal,
    {
      onEvent: async (e) => {
        seen.push(e.sequence);
      },
      refresh: async () => job,
      delay: async () => {},
    },
  );
  expect(seen).toEqual([7, 8, 9]);
  expect(new Headers(requests[1].init.headers).get("Last-Event-ID")).toBe("8");
});
it("bounds stream transport retries without leaking private transport details", async () => {
  respond = () => Promise.reject(new Error("private-token and private path"));
  const delays: number[] = [];
  await expect(
    watchJob(
      new ApiClient("private-token", handshake.scope),
      "job",
      new AbortController().signal,
      {
        onEvent: async () => {},
        refresh: async () => job,
        delay: async (ms) => {
          delays.push(ms);
        },
      },
    ),
  ).rejects.toMatchObject({ code: "CONNECTION_FAILED" });
  expect(requests).toHaveLength(5);
  expect(delays).toEqual([1000, 2000, 4000, 8000]);
});
it("keeps typed replay dependency failures visible for a retained query", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.endsWith("/replay")
      ? json(
          {
            code: "REPLAY_UNAVAILABLE",
            message: "Retained dependencies are unavailable.",
          },
          409,
        )
      : path.startsWith("/v1/queries/")
        ? json({
            ...run,
            records: run.records.map((r) => ({
              ...r,
              value: { ...r.value, request_retained: true },
            })),
          })
        : fallback(path, init);
  await open(`/dashboard/runs/${run.id}`);
  fireEvent.click(
    await screen.findByRole("button", { name: "Replay retained query" }),
  );
  await screen.findByText("REPLAY_UNAVAILABLE");
  expect(
    screen.getByText("Retained dependencies are unavailable.", {
      exact: false,
    }),
  ).toBeVisible();
});
it("blocks manager and writer forms for reader scope without issuing evaluation reads", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/handshake"
      ? json({
          ...handshake,
          scope: { ...handshake.scope, capabilities: ["read"] },
        })
      : fallback(path, init);
  await open("/dashboard/settings");
  await screen.findByText(
    "Configuration activation requires manage capability.",
  );
  expect(screen.queryByLabelText("Complete workspace TOML")).toBeNull();
  fireEvent.click(screen.getByRole("link", { name: "Evaluations" }));
  await screen.findByText(
    "Evaluation listing, inspection and submission require manage capability.",
  );
  expect(requests.some((r) => r.path.startsWith("/v1/evaluations"))).toBe(
    false,
  );
  fireEvent.click(screen.getByRole("link", { name: "Sources" }));
  await screen.findByText(
    "Adding and syncing sources requires write capability.",
  );
  expect(screen.queryByRole("button", { name: "Add source" })).toBeNull();
});
it("renders real evaluation details and compares mismatched runs with case drilldown", async () => {
  const fallback = respond;
  const second = {
    ...evaluation,
    evaluation_id: "second",
    dataset_id: "other",
  };
  respond = (path, init) =>
    path === "/v1/evaluations"
      ? json([
          {
            evaluation_id: evaluation.evaluation_id,
            dataset_id: evaluation.dataset_id,
            snapshot_id: evaluation.snapshot_id,
            created_at: evaluation.report.created_at,
            available: true,
          },
          {
            evaluation_id: "second",
            dataset_id: "other",
            snapshot_id: second.snapshot_id,
            created_at: second.report.created_at,
            available: true,
          },
        ])
      : path.endsWith("/second")
        ? json(second)
        : path.startsWith("/v1/evaluations/")
          ? json(evaluation)
          : fallback(path, init);
  await open("/dashboard/evaluations");
  fireEvent.click(
    await screen.findByLabelText(`Compare ${evaluation.evaluation_id}`),
  );
  fireEvent.click(screen.getByLabelText("Compare second"));
  await screen.findByText(
    "Identity or execution context differs. Arithmetic deltas do not establish comparable quality.",
  );
  expect(screen.getByText("Left case drilldown")).toBeInTheDocument();
});
it("shows evaluation in-flight state honestly until completion and sends supported options", async () => {
  const fallback = respond;
  let finish!: (response: Response) => void;
  respond = (path, init) =>
    path === "/v1/evaluations" && init.method === "POST"
      ? new Promise((r) => {
          finish = r;
        })
      : path === "/v1/evaluations"
        ? json([])
        : fallback(path, init);
  await open("/dashboard/evaluations");
  fireEvent.click(screen.getByRole("button", { name: "Submit evaluation" }));
  fireEvent.change(screen.getByLabelText("Registered dataset ID"), {
    target: { value: "registered" },
  });
  fireEvent.change(screen.getByLabelText("Snapshot ID"), {
    target: { value: "snapshot" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Run evaluation" }));
  await screen.findByText(
    "Waiting for the evaluation response. This is an active request.",
  );
  const request = requests.find(
    (r) => r.path === "/v1/evaluations" && r.init.method === "POST",
  )!;
  expect(JSON.parse(request.init.body as string)).toMatchObject({
    dataset_id: "registered",
    snapshot_id: "snapshot",
    split: "test",
    offline: true,
    repetitions: 1,
    seed: 0,
  });
  finish(
    json(
      {
        id: evaluation.evaluation_id,
        operation: "evaluate",
        state: "succeeded",
      },
      202,
    ),
  );
  await screen.findByRole("link", { name: "Inspect evaluation" });
});
it("clears secret-bearing settings draft after successful activation", async () => {
  const fallback = respond;
  respond = (path, init) =>
    init.method === "PUT"
      ? json({
          profile: "local",
          values: { providers: { text: { provider: "none" } } },
        })
      : fallback(path, init);
  await open("/dashboard/settings");
  fireEvent.change(await screen.findByLabelText("Complete workspace TOML"), {
    target: { value: 'secret = "private-secret"' },
  });
  fireEvent.change(screen.getByLabelText("Activation profile"), {
    target: { value: "local" },
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Activate configuration" }),
  );
  await screen.findByText(
    "Configuration activated. A rebuild has not been queued.",
  );
  expect(screen.getByLabelText("Complete workspace TOML")).toHaveValue("");
  expect(document.body.textContent).not.toContain("private-secret");
});
it("aborts active job events on page departure without cancelling the durable job", async () => {
  const fallback = respond;
  let streamSignal: AbortSignal | null | undefined;
  respond = (path, init) =>
    path.endsWith("/events")
      ? ((streamSignal = init.signal),
        new Response(new ReadableStream({ start() {} })))
      : path === `/v1/jobs/${job.id}`
        ? json({ ...job, state: "running" })
        : fallback(path, init);
  await open(`/dashboard/jobs/${job.id}`);
  await waitFor(() => expect(streamSignal).toBeDefined());
  fireEvent.click(screen.getByRole("link", { name: "Activity" }));
  await waitFor(() => expect(streamSignal?.aborted).toBe(true));
  expect(requests.some((r) => r.path.endsWith("/cancel"))).toBe(false);
});
it("stops a still-open stream as soon as a terminal event is acknowledged", async () => {
  let cancelled = false;
  respond = () =>
    new Response(
      new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(
            new TextEncoder().encode(
              'id: 9\nevent: job\ndata: {"sequence":9,"state":"succeeded"}\n\n',
            ),
          );
        },
        cancel() {
          cancelled = true;
        },
      }),
    );
  const controller = new AbortController();
  let complete = false;
  const pending = watchJob(
    new ApiClient("private-token", handshake.scope),
    "job",
    controller.signal,
    { onEvent: async () => {}, refresh: async () => job },
  ).then(() => {
    complete = true;
  });
  await waitFor(() => expect(complete).toBe(true), { timeout: 100 });
  expect(cancelled).toBe(true);
  await pending;
});
it("offers source retry only when retained inputs remain and retry budget is available", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === `/v1/jobs/${job.id}`
      ? json({ ...job, state: "failed", retry_count: 3, captured_inputs: null })
      : fallback(path, init);
  await open(`/dashboard/jobs/${job.id}`);
  await screen.findByText("failed");
  expect(screen.queryByRole("button", { name: "Retry job" })).toBeNull();
});
it("preserves ambiguous source retry identity after formatting-only metadata edits", async () => {
  const fallback = respond;
  let adds = 0;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? ++adds === 1
        ? Promise.reject(new Error("response lost"))
        : json(receipt, 202)
      : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "ambiguous" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Source text"), {
    target: { value: "same text" },
  });
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByText("CONNECTION_FAILED");
  fireEvent.change(screen.getByLabelText("Metadata JSON"), {
    target: { value: "{ }" },
  });
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  const sent = requests.filter(
    (r) => r.path === "/v1/sources" && r.init.method === "POST",
  );
  expect(sent[0].init.body).toBe(sent[1].init.body);
  expect(new Headers(sent[0].init.headers).get("Idempotency-Key")).toBe(
    new Headers(sent[1].init.headers).get("Idempotency-Key"),
  );
});
it.each(["running", "queued"])(
  "offers explicit service recovery for retained %s source jobs",
  async (state) => {
    const fallback = respond;
    let recovered = false;
    respond = (path, init) =>
      path.endsWith("/retry")
        ? ((recovered = true), json({ ...receipt, state: "queued" }))
        : path.endsWith("/events")
          ? new Response(new ReadableStream({ start() {} }))
          : path === `/v1/jobs/${job.id}`
            ? json({ ...job, state: recovered ? "succeeded" : state })
            : fallback(path, init);
    await open(`/dashboard/jobs/${job.id}`);
    fireEvent.click(
      await screen.findByRole("button", { name: "Resume or recover job" }),
    );
    await screen.findByText("succeeded");
    expect(
      requests.some(
        (r) => r.path.endsWith("/retry") && r.init.method === "POST",
      ),
    ).toBe(true);
  },
);
it("resumes a queued captured source even at its retry limit without claiming a new retry", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.endsWith("/events")
      ? new Response(new ReadableStream({ start() {} }))
      : path === `/v1/jobs/${job.id}`
        ? json({ ...job, state: "queued", retry_count: 3 })
        : fallback(path, init);
  await open(`/dashboard/jobs/${job.id}`);
  await screen.findByRole("button", { name: "Resume or recover job" });
});
it("displays an event once when status refresh fails and its unacknowledged sequence is replayed", async () => {
  const fallback = respond;
  let reads = 0;
  let streams = 0;
  respond = (path, init) =>
    path.endsWith("/events")
      ? new Response(
          ++streams === 1
            ? 'id: 7\nevent: job\ndata: {"sequence":7,"stage":"parse","state":"running"}\n\n'
            : 'id: 7\nevent: job\ndata: {"sequence":7,"stage":"parse","state":"running"}\n\nid: 8\nevent: job\ndata: {"sequence":8,"stage":"complete","state":"succeeded"}\n\n',
        )
      : path === `/v1/jobs/${job.id}`
        ? ++reads === 2
          ? Promise.reject(new Error("status connection lost"))
          : json({
              ...job,
              latest_sequence: 6,
              state: reads >= 4 ? "succeeded" : "running",
            })
        : fallback(path, init);
  await open(`/dashboard/jobs/${job.id}`);
  await waitFor(() =>
    expect(screen.queryAllByText("7 · parse")).toHaveLength(1),
  );
  await screen.findByText("8 · complete", {}, { timeout: 2000 });
  expect(screen.getAllByText("7 · parse")).toHaveLength(1);
  const streamsSent = requests.filter((r) => r.path.endsWith("/events"));
  expect(
    streamsSent.map((r) => new Headers(r.init.headers).get("Last-Event-ID")),
  ).toEqual(["6", "6"]);
});
it("discloses that evaluation inventory is recent, bounded before authorization and has no continuation", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/evaluations" ? json([]) : fallback(path, init);
  await open("/dashboard/evaluations");
  await screen.findByText(/100 most recent records before authorization/);
  expect(screen.queryByText(/complete authorized list/)).toBeNull();
  expect(screen.getByText(/older evaluations may be omitted/i)).toBeVisible();
});
it("sends a usable explicit default format for extensionless pasted text", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? json(receipt, 202)
      : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "no-extension" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Source text"), {
    target: { value: "Plain source text" },
  });
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  expect(
    JSON.parse(
      requests.find(
        (r) => r.path === "/v1/sources" && r.init.method === "POST",
      )!.init.body as string,
    ).format,
  ).toBe("text");
});
it.each([
  ["notes.md", "markdown"],
  ["manual.txt", "text"],
  ["scan.JPG", "jpeg"],
])("resolves %s selected bytes format as %s", async (name, format) => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? json(receipt, 202)
      : fallback(path, init);
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "no-extension" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Input type"), {
    target: { value: "bytes" },
  });
  const file = new File(["sample"], name);
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => new TextEncoder().encode("sample").buffer,
  });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  expect(
    JSON.parse(
      requests.find(
        (r) => r.path === "/v1/sources" && r.init.method === "POST",
      )!.init.body as string,
    ).format,
  ).toBe(format);
});
it("requires an explicit bytes format for unrecognized filenames and preserves deliberate overrides", async () => {
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Input type"), {
    target: { value: "bytes" },
  });
  const file = new File(["sample"], "unknown.bin");
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => new TextEncoder().encode("sample").buffer,
  });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  await screen.findByText(/Choose an explicit format for this file/);
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Format"), {
    target: { value: "markdown" },
  });
  expect(screen.getByLabelText("Format")).toHaveValue("markdown");
});
it.each(["file.constructor", "file.__proto__", "file.toString"])(
  "treats inherited object suffix %s as an unrecognized format",
  async (filename) => {
    const { fileFormat } = await import("../pages/sources/submission");
    expect(fileFormat(filename)).toBe("");
  },
);
it.each(["file.constructor", "file.__proto__"])(
  "keeps %s bytes behind the explicit format guard without rendering a nonstring",
  async (filename) => {
    await open("/dashboard/sources");
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    fireEvent.change(screen.getByLabelText("Input type"), {
      target: { value: "bytes" },
    });
    const file = new File(["sample"], filename);
    Object.defineProperty(file, "arrayBuffer", {
      value: async () => new TextEncoder().encode("sample").buffer,
    });
    fireEvent.change(screen.getByLabelText("Source file"), {
      target: { files: [file] },
    });
    await screen.findByText(/Choose an explicit format for this file/);
    expect(
      screen.getByRole("button", { name: "Submit source" }),
    ).toBeDisabled();
    expect(
      requests.some(
        (r) => r.path === "/v1/sources" && r.init.method === "POST",
      ),
    ).toBe(false);
  },
);

async function openSourceDraft() {
  await open("/dashboard/sources");
  fireEvent.click(screen.getByRole("button", { name: "Add source" }));
  fireEvent.change(screen.getByLabelText("Source key"), {
    target: { value: "manual" },
  });
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  fireEvent.change(screen.getByLabelText("Source text"), {
    target: { value: "Preserved source text" },
  });
  await screen.findByText("Active profile: local");
}
function changeSourceType(value: string) {
  fireEvent.change(screen.getByLabelText("Input type"), { target: { value } });
}
function confirmSource() {
  fireEvent.click(
    screen.getByLabelText("I confirm this source and processing destination."),
  );
}
function selectedPdf(size: number) {
  const file = new File(["sample"], "large.pdf");
  Object.defineProperty(file, "size", { value: size });
  return file;
}
function sourcePosts() {
  return requests.filter(
    (r) => r.path === "/v1/sources" && r.init.method === "POST",
  );
}
it("accepts preserved pasted text after rejecting a 50 MiB selected PDF", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? json(receipt, 202)
      : fallback(path, init);
  await openSourceDraft();
  changeSourceType("bytes");
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [selectedPdf(52428800)] },
  });
  await screen.findByText("INPUT_BUDGET_EXCEEDED");
  changeSourceType("text");
  expect(screen.queryByText("INPUT_BUDGET_EXCEEDED")).toBeNull();
  expect(screen.getByLabelText("Source text")).toHaveValue(
    "Preserved source text",
  );
  confirmSource();
  expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  expect(JSON.parse(sourcePosts()[0].init.body as string)).toMatchObject({
    type: "text",
    format: "text",
    text: "Preserved source text",
  });
});
it("shows rejected file size without presenting an empty payload as the encoded request", async () => {
  await openSourceDraft();
  changeSourceType("bytes");
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [selectedPdf(52428800)] },
  });
  await screen.findByText("INPUT_BUDGET_EXCEEDED");
  expect(screen.getByText(/Selected file:.*52,428,800 bytes/)).toBeVisible();
  expect(screen.getByText(/Encoded request: Unknown/)).toBeVisible();
  expect(
    screen.getByText(/smaller file.*registered-root file path/i),
  ).toBeVisible();
});
it.each(["success", "error"])(
  "ignores a deferred file read %s after changing the input type",
  async (outcome) => {
    const fallback = respond;
    respond = (path, init) =>
      path === "/v1/sources" && init.method === "POST"
        ? json(receipt, 202)
        : fallback(path, init);
    await openSourceDraft();
    let resolve!: (buffer: ArrayBuffer) => void;
    let reject!: (error: Error) => void;
    const pending = new Promise<ArrayBuffer>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    const file = selectedPdf(6);
    Object.defineProperty(file, "arrayBuffer", { value: () => pending });
    changeSourceType("bytes");
    fireEvent.change(screen.getByLabelText("Format"), {
      target: { value: "pdf" },
    });
    fireEvent.change(screen.getByLabelText("Source file"), {
      target: { files: [file] },
    });
    changeSourceType("text");
    confirmSource();
    expect(screen.getByRole("button", { name: "Submit source" })).toBeEnabled();
    changeSourceType("bytes");
    confirmSource();
    await act(async () => {
      if (outcome === "success") resolve(Uint8Array.of(0, 255, 128).buffer);
      else reject(new Error("file read failed"));
      await pending.catch(() => {});
    });
    expect(screen.queryByText("CONNECTION_FAILED")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Submit source" }),
    ).toBeDisabled();
    fireEvent.submit(
      screen.getByRole("button", { name: "Submit source" }).closest("form")!,
    );
    expect(sourcePosts()).toHaveLength(0);
    changeSourceType("text");
    confirmSource();
    fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
    await screen.findByRole("link", { name: "Follow job" });
    expect(JSON.parse(sourcePosts()[0].init.body as string).type).toBe("text");
  },
);
it.each([
  "missing file",
  "rejected file",
  "missing key",
  "missing kind",
  "invalid metadata",
  "unavailable settings",
])("guards programmatic source submission with %s", async (invalid) => {
  if (invalid === "unavailable settings") {
    const fallback = respond;
    respond = (path, init) =>
      path === "/v1/settings"
        ? json(
            { code: "WORKSPACE_BUSY", message: "Busy", retryable: true },
            409,
          )
        : fallback(path, init);
    await open("/dashboard/sources");
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    fireEvent.change(screen.getByLabelText("Source key"), {
      target: { value: "manual" },
    });
    fireEvent.change(screen.getByLabelText("Kind"), {
      target: { value: "manual@1" },
    });
    await screen.findByText("WORKSPACE_BUSY");
  } else {
    await openSourceDraft();
  }
  if (invalid === "missing file" || invalid === "rejected file") {
    changeSourceType("bytes");
    if (invalid === "rejected file") {
      fireEvent.change(screen.getByLabelText("Source file"), {
        target: { files: [selectedPdf(52428800)] },
      });
      await screen.findByText("INPUT_BUDGET_EXCEEDED");
    }
  } else if (invalid === "missing key" || invalid === "missing kind") {
    fireEvent.change(
      screen.getByLabelText(invalid === "missing key" ? "Source key" : "Kind"),
      {
        target: { value: "" },
      },
    );
  } else if (invalid === "invalid metadata") {
    fireEvent.change(screen.getByLabelText("Metadata JSON"), {
      target: { value: "[" },
    });
  }
  confirmSource();
  const button = screen.getByRole("button", { name: "Submit source" });
  expect(button).toBeDisabled();
  fireEvent.submit(button.closest("form")!);
  expect(sourcePosts()).toHaveLength(0);
});
it("preserves exact-body retry identity after a rejected file and returning to unchanged text", async () => {
  const fallback = respond;
  let adds = 0;
  respond = (path, init) =>
    path === "/v1/sources" && init.method === "POST"
      ? ++adds === 1
        ? Promise.reject(new Error("response lost"))
        : json(receipt, 202)
      : fallback(path, init);
  await openSourceDraft();
  confirmSource();
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByText("CONNECTION_FAILED");
  changeSourceType("bytes");
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [selectedPdf(52428800)] },
  });
  await screen.findByText("INPUT_BUDGET_EXCEEDED");
  changeSourceType("text");
  confirmSource();
  fireEvent.click(screen.getByRole("button", { name: "Submit source" }));
  await screen.findByRole("link", { name: "Follow job" });
  const sent = sourcePosts();
  expect(sent[0].init.body).toBe(sent[1].init.body);
  expect(new Headers(sent[0].init.headers).get("Idempotency-Key")).toBe(
    new Headers(sent[1].init.headers).get("Idempotency-Key"),
  );
});
