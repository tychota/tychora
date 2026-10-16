import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { createRef } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { PdfIntake } from "../pages/sources/PdfIntakePanel";
import handshake from "../../contracts/collections-handshake.json";
import type { PdfIntakeDescriptor, PdfIntakeStatus } from "../api/contracts";
const descriptor: PdfIntakeDescriptor = {
  source_key: "synthetic.pdf",
  collection_id: handshake.scope.collection_ids[0],
  kind: "document@1",
  metadata: { title: "Synthetic" },
  expected_size_bytes: 2000000,
  format: "pdf",
};
const analysis = {
  original_sha256: "a".repeat(64),
  size_bytes: 2000000,
  page_count: 3,
  native_text_pages: [2],
  image_only_pages: [1],
  blank_pages: [3],
  unknown_pages: [],
  requires_ocr: true,
  blockers: [],
  proposed_policy: {
    version: "pdf-processing@1",
    mode: "local_ocr",
    native_pages: [2],
    ocr_pages: [1],
    blank_pages: [3],
    backend_identity: { parser: "docling" },
    runtime_inventory: {},
    model_inventory_sha256: "b".repeat(64),
    options: {},
    limits: {},
  },
  analysis_sha256: "c".repeat(64),
  policy_sha256: "d".repeat(64),
} as const;
const receipt = (state = "prepared", extra = {}) => ({
  intake_id: "e".repeat(64),
  state,
  byte_ceiling: 2000000,
  received_bytes: 0,
  expires_at: new Date(Date.now() + 1800000).toISOString(),
  analysis: null,
  job: null,
  error: null,
  ...extra,
});
let calls: { path: string; init: RequestInit }[];
let respond: (path: string, init: RequestInit) => Response | Promise<Response>;
const json = (v: unknown, status = 200) =>
  new Response(JSON.stringify(v), { status });
beforeEach(() => {
  calls = [];
  respond = (path, init) =>
    json(
      init.method === "DELETE"
        ? receipt("aborted")
        : path.endsWith("/content")
          ? receipt("staged", { received_bytes: 2000000 })
          : path.endsWith("/analyse")
            ? receipt("analysed", { analysis })
            : path.endsWith("/submit")
              ? { id: "job-1", operation: "add", state: "queued" }
              : receipt(),
    );
  vi.stubGlobal("fetch", (path: string, init: RequestInit) => {
    calls.push({ path, init });
    return respond(path, init);
  });
});
function fixture() {
  const file = new File(["synthetic"], "synthetic.pdf", {
    type: "application/pdf",
  });
  Object.defineProperty(file, "size", { value: 2000000, configurable: true });
  Object.defineProperty(file, "arrayBuffer", {
    value: vi.fn(() => {
      throw new Error("whole File read forbidden");
    }),
  });
  return {
    file,
    descriptor,
    client: new ApiClient("synthetic"),
    scope: handshake.scope,
    epoch: 1,
    ownerRef: createRef<ApiClient | null>(),
    onAccepted: vi.fn(),
  };
}
async function analyse() {
  fireEvent.click(screen.getByRole("button", { name: "Analyse PDF" }));
  await screen.findByText(/3 physical pages/);
}
it("uploads the raw File above the JSON cap and confirms the exact server hashes", async () => {
  const props = fixture();
  render(<PdfIntake {...props} />);
  await analyse();
  const put = calls.find((c) => c.init.method === "PUT")!;
  expect(put.init.body).toBe(props.file);
  expect(new Headers(put.init.headers).get("Content-Type")).toBe(
    "application/pdf",
  );
  expect(
    JSON.parse(new Headers(put.init.headers).get("X-Tychora-Scope")!)
      .collection_ids,
  ).toEqual([descriptor.collection_id]);
  expect(props.file.arrayBuffer).not.toHaveBeenCalled();
  expect(screen.getByText(/Mixed/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Ingest PDF" })).toBeDisabled();
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await waitFor(() =>
    expect(props.onAccepted).toHaveBeenCalledWith(
      expect.objectContaining({ id: "job-1" }),
      expect.any(ApiClient),
    ),
  );
  expect(
    JSON.parse(
      calls.find((c) => c.path.endsWith("/submit"))!.init.body as string,
    ),
  ).toEqual({
    analysis_sha256: analysis.analysis_sha256,
    policy_sha256: analysis.policy_sha256,
  });
});
it("abandons old analysis with its original owner when destination or grant changes", async () => {
  const props = fixture();
  const view = render(<PdfIntake {...props} />);
  await analyse();
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  view.rerender(
    <PdfIntake
      {...props}
      epoch={2}
      descriptor={{ ...descriptor, collection_id: "new-destination" }}
    />,
  );
  await waitFor(() =>
    expect(calls.some((c) => c.init.method === "DELETE")).toBe(true),
  );
  const deleted = calls.find((c) => c.init.method === "DELETE")!;
  expect(
    JSON.parse(new Headers(deleted.init.headers).get("X-Tychora-Scope")!)
      .collection_ids,
  ).toEqual([descriptor.collection_id]);
  expect(
    screen.queryByRole("checkbox", { name: /confirm this exact analysis/ }),
  ).toBeNull();
  expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeEnabled();
});
it("keeps same submission key after ambiguous rejection and recovers an accepted job", async () => {
  const props = fixture();
  let submissions = 0;
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/submit")
      ? ++submissions === 1
        ? json(
            { code: "WORKSPACE_BUSY", message: "Try again", retryable: true },
            409,
          )
        : json({ id: "job-retry", operation: "add", state: "queued" })
      : base(path, init);
  render(<PdfIntake {...props} />);
  await analyse();
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await screen.findByText(/Try again/);
  fireEvent.click(
    screen.getByRole("button", { name: "Retry confirmed submission" }),
  );
  await waitFor(() => expect(props.onAccepted).toHaveBeenCalled());
  const keys = calls
    .filter((c) => c.path.endsWith("/submit"))
    .map((c) => new Headers(c.init.headers).get("Idempotency-Key"));
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});
it("ignores a delayed no-job check after a confirmed retry is accepted", async () => {
  const props = fixture();
  let submissions = 0;
  let checks = 0;
  let finish!: (value: Response) => void;
  const base = respond;
  respond = (path, init) => {
    if (path.endsWith("/submit"))
      return ++submissions === 1
        ? json(
            { code: "WORKSPACE_BUSY", message: "Try again", retryable: true },
            409,
          )
        : json({ id: "accepted-retry", operation: "add", state: "queued" });
    if (
      path.endsWith(receipt().intake_id) &&
      init.method !== "DELETE" &&
      ++checks === 2
    )
      return new Promise((resolve) => {
        finish = resolve;
      });
    return base(path, init);
  };
  render(<PdfIntake {...props} />);
  await analyse();
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await screen.findByText(/Try again/);
  fireEvent.click(screen.getByRole("button", { name: "Check submission" }));
  fireEvent.click(
    screen.getByRole("button", { name: "Retry confirmed submission" }),
  );
  await waitFor(() =>
    expect(props.onAccepted).toHaveBeenCalledWith(
      expect.objectContaining({ id: "accepted-retry" }),
      expect.anything(),
    ),
  );
  await act(async () => finish(json(receipt("analysed", { analysis }))));
  expect(screen.queryByText(/No accepted job is confirmed yet/)).toBeNull();
  expect(props.onAccepted).toHaveBeenCalledTimes(1);
});
it("stops waiting for analysis without claiming server cancellation and ignores a late result", async () => {
  const props = fixture();
  let finish!: (v: Response) => void;
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/analyse")
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : base(path, init);
  render(<PdfIntake {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Analyse PDF" }));
  await screen.findByRole("button", { name: "Stop waiting" });
  fireEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
  await act(async () => finish(json(receipt("analysed", { analysis }))));
  expect(
    screen.queryByRole("checkbox", { name: /confirm this exact analysis/ }),
  ).toBeNull();
  expect(screen.getByText(/server analysis may still finish/)).toBeVisible();
});
it("blocks missing policy, capability errors and expiry without inventing readiness", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/analyse")
      ? json(
          receipt("analysed", {
            analysis: {
              ...analysis,
              blockers: ["CAPABILITY_MISSING: Verified model absent"],
              proposed_policy: null,
              policy_sha256: null,
            },
          }),
        )
      : base(path, init);
  render(<PdfIntake {...props} />);
  await analyse();
  expect(
    screen.getByText("CAPABILITY_MISSING: Verified model absent"),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Ingest PDF" })).toBeDisabled();
});
it("does not read or reserve oversized PDF files", () => {
  const props = fixture();
  Object.defineProperty(props.file, "size", { value: 104857601 });
  render(<PdfIntake {...props} />);
  expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeDisabled();
  expect(calls).toHaveLength(0);
});
it("recovers an accepted-before-response job without a second preparation", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/submit")
      ? Promise.reject(new TypeError("response lost"))
      : (init.method ?? "GET") === "GET"
        ? json(
            receipt("submitted", {
              job: { id: "recovered", operation: "add", state: "running" },
            }),
          )
        : base(path, init);
  render(<PdfIntake {...props} />);
  await analyse();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await waitFor(() =>
    expect(props.onAccepted).toHaveBeenCalledWith(
      expect.objectContaining({ id: "recovered" }),
      expect.any(ApiClient),
    ),
  );
  expect(calls.filter((c) => c.path === "/v1/pdf-intakes")).toHaveLength(1);
});
it("keeps ambiguous submission bound to retry rather than preparing a second job", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/submit") || (init.method ?? "GET") === "GET"
      ? Promise.reject(new TypeError("lost response"))
      : base(path, init);
  render(<PdfIntake {...props} />);
  await analyse();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await screen.findByText(/Submission was not confirmed/);
  expect(screen.getByRole("button", { name: "Analyse again" })).toBeDisabled();
});
it("expires exact confirmation and renews with a new receipt instead of reanalysing old policy", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/analyse")
      ? json(
          receipt("analysed", {
            analysis,
            expires_at: new Date(Date.now() - 1).toISOString(),
          }),
        )
      : base(path, init);
  render(<PdfIntake {...props} />);
  await analyse();
  expect(screen.getByRole("button", { name: "Ingest PDF" })).toBeDisabled();
  expect(await screen.findByText(/Upload expired/)).toBeVisible();
});
it("does not widen receipt authority after denied cleanup", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    init.method === "DELETE"
      ? json({ code: "FORBIDDEN_SCOPE", message: "Grant revoked" }, 403)
      : base(path, init);
  const view = render(<PdfIntake {...props} />);
  await analyse();
  view.rerender(<PdfIntake {...props} epoch={2} />);
  await waitFor(() =>
    expect(calls.filter((c) => c.init.method === "DELETE")).toHaveLength(1),
  );
  expect(
    calls
      .filter((c) => c.init.method === "DELETE")
      .every((c) => new Headers(c.init.headers).has("X-Tychora-Scope")),
  ).toBe(true);
});
it("cancels raw upload and ignores its late completion", async () => {
  const props = fixture();
  let finish!: (v: Response) => void;
  const base = respond;
  respond = (path, init) =>
    path.endsWith("/content")
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : base(path, init);
  render(<PdfIntake {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Analyse PDF" }));
  await screen.findByText("Uploading PDF…");
  fireEvent.click(screen.getByRole("button", { name: "Cancel upload" }));
  await act(async () => finish(json(receipt("staged"))));
  expect(calls.some((c) => c.path.endsWith("/analyse"))).toBe(false);
  expect(screen.getByText(/Upload stopped/)).toBeVisible();
});
it("shows readable actual retained OCR input provenance without a native quote", async () => {
  const { EvidenceReader } = await import("../components/EvidenceReader");
  const fixture = (await import("../../contracts/ocr-evidence.json")).default;
  respond = () => json(fixture);
  render(
    <EvidenceReader client={new ApiClient("synthetic")} unitId={fixture.id} />,
  );
  expect(
    await screen.findByRole("heading", { name: "OCR input and derivation" }),
  ).toBeVisible();
  expect(screen.getByText(/Uncalibrated OCR/)).toBeVisible();
});
it("reports denied old-receipt cleanup after changing input without erasing the new draft", async () => {
  const props = fixture();
  const base = respond;
  respond = (path, init) =>
    init.method === "DELETE"
      ? json(
          {
            code: "FORBIDDEN_SCOPE",
            message: "Old receipt no longer authorized",
          },
          403,
        )
      : base(path, init);
  const view = render(<PdfIntake {...props} />);
  await analyse();
  view.rerender(
    <PdfIntake
      {...props}
      descriptor={{ ...descriptor, metadata: { title: "Changed title" } }}
    />,
  );
  expect(
    await screen.findByText(/Its expiration remains the fallback/),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeEnabled();
});
it("retains ambiguous receipt and key across a route remount before allowing a new preparation", async () => {
  const props = fixture();
  const pendingRef = createRef<
    import("../pages/sources/pdfIntake").PendingPdfSubmission | null
  >();
  const base = respond;
  let submissions = 0;
  respond = (path, init) =>
    path.endsWith("/submit")
      ? ++submissions === 1
        ? Promise.reject(new TypeError("lost"))
        : json({ id: "remount-recovered", operation: "add", state: "queued" })
      : (init.method ?? "GET") === "GET"
        ? Promise.reject(new TypeError("offline"))
        : base(path, init);
  const view = render(<PdfIntake {...props} pendingRef={pendingRef} />);
  await analyse();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await screen.findByText(/Submission was not confirmed/);
  const key = new Headers(
    calls.find((c) => c.path.endsWith("/submit"))!.init.headers,
  ).get("Idempotency-Key");
  view.unmount();
  render(<PdfIntake {...props} pendingRef={pendingRef} />);
  expect(screen.getByRole("button", { name: "Analyse again" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(
    screen.getByRole("button", { name: "Retry confirmed submission" }),
  );
  await waitFor(() =>
    expect(props.onAccepted).toHaveBeenCalledWith(
      expect.objectContaining({ id: "remount-recovered" }),
      expect.any(ApiClient),
    ),
  );
  expect(
    new Headers(
      calls.filter((c) => c.path.endsWith("/submit"))[1].init.headers,
    ).get("Idempotency-Key"),
  ).toBe(key);
});
it("allows fresh preparation after original-scope recovery is permanently denied", async () => {
  const props = fixture();
  const owner = props.client
    .withGrant(props.scope)
    .narrow([descriptor.collection_id]);
  const pendingRef = createRef<
    import("../pages/sources/pdfIntake").PendingPdfSubmission | null
  >();
  pendingRef.current = {
    owner,
    file: props.file,
    status: receipt("analysed", {
      analysis,
      expires_at: new Date(Date.now() - 1).toISOString(),
    }) as PdfIntakeStatus,
    submission: {
      analysis_sha256: analysis.analysis_sha256,
      policy_sha256: analysis.policy_sha256,
    },
    descriptor,
    key: "original-key",
  };
  respond = () =>
    json({ code: "FORBIDDEN_SCOPE", message: "Original grant revoked" }, 403);
  render(<PdfIntake {...props} pendingRef={pendingRef} />);
  fireEvent.click(screen.getByRole("button", { name: "Check submission" }));
  await screen.findByText(/Original grant revoked/);
  expect(screen.getByRole("button", { name: "Analyse again" })).toBeEnabled();
  expect(calls).toHaveLength(1);
  expect(new Headers(calls[0].init.headers).has("X-Tychora-Scope")).toBe(true);
});
