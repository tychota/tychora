import { createHash } from "node:crypto";
import { mkdir, readFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";

const owner = "synthetic-dashboard-owner";
const reader = "synthetic-dashboard-reader";
const artifacts = "../.superpowers/verification/dashboard-e2e";
async function connect(page: Page, token = owner) {
  await page.getByLabel("Bearer token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
}
async function screenshot(page: Page, name: string) {
  await expect(
    page.getByText("Loading scoped workspace data…", { exact: true }),
  ).toHaveCount(0);
  await mkdir(artifacts, { recursive: true });
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
    window.scrollTo(0, 0);
  });
  expect(
    await page
      .getByRole("link", { name: "Skip to content" })
      .evaluate((link) => link.getBoundingClientRect().bottom),
  ).toBeLessThan(0);
  await page.screenshot({ path: `${artifacts}/${name}.png`, fullPage: true });
}

test.beforeEach(async ({ page, context }) => {
  const denyMocks = () => {
    throw new Error("Real-server journeys prohibit browser route mocks");
  };
  page.route = denyMocks;
  page.routeFromHAR = denyMocks;
  context.route = denyMocks;
  context.routeFromHAR = denyMocks;
});

// Real services only: these journeys deliberately never install page/context route handlers.
test("real ingestion, durable jobs, provenance, coverage, history and evaluations", async ({
  page,
  context,
}) => {
  const responses: { path: string; status: number }[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/v1/"))
      responses.push({ path: url.pathname, status: response.status() });
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/dashboard/");
  await page.getByLabel("Bearer token").fill("synthetic-invalid");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("FORBIDDEN_SCOPE");
  await page.getByLabel("Bearer token").fill("");
  await screenshot(page, "connection-error");
  await connect(page);
  await page.getByRole("link", { name: "Sources", exact: true }).click();
  await expect(
    page.getByText("No registered sources in this scope."),
  ).toBeVisible();
  await screenshot(page, "sources-empty");
  await page.getByRole("button", { name: "Add source", exact: true }).click();
  await page
    .getByLabel("Source key", { exact: true })
    .fill("synthetic-browser-manual");
  await page.getByLabel("Kind", { exact: true }).fill("manual@1");
  await page
    .getByLabel("Source text", { exact: true })
    .fill(
      "Synthetic browser verification: record cedar inspection outcomes and retain their exact provenance.",
    );
  await page
    .getByLabel("Title", { exact: true })
    .fill("Synthetic browser manual");
  await page.getByLabel("Publisher", { exact: true }).fill("Synthetic fixture");
  await page.getByLabel("Edition", { exact: true }).fill("1");
  await page
    .getByLabel("I confirm this source and processing destination.")
    .check();
  const acceptedPromise = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/sources" &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Submit source", exact: true })
    .click();
  const accepted = await acceptedPromise;
  expect(accepted.status()).toBe(202);
  const receipt = await accepted.json();
  const queued = await context.request.get(`/v1/jobs/${receipt.id}`, {
    headers: { Authorization: `Bearer ${owner}` },
  });
  expect((await queued.json()).state).toBe("queued");
  const streamPromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/v1/jobs/${receipt.id}/events`,
  );
  await page.getByRole("link", { name: "Follow job", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/dashboard/jobs/${receipt.id}$`));
  const stream = await streamPromise;
  expect(stream.status()).toBe(200);
  expect(stream.headers()["content-type"]).toContain("text/event-stream");
  await expect(
    page.locator(".compact-list li").filter({ hasText: "succeeded" }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("succeeded", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Live events" }),
  ).toBeVisible();
  await screenshot(page, "desktop-job");
  await page.reload();
  await expect(page.getByLabel("Bearer token")).toHaveValue("");
  await connect(page);
  await expect(
    page.getByText("succeeded", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("link", { name: "Search", exact: true }).click();
  await page.getByLabel("Question", { exact: true }).fill("cedar");
  const queryPromise = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/queries" &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  const queryResponse = await queryPromise;
  expect(queryResponse.status()).toBe(200);
  const query = await queryResponse.json();
  expect(query.segments.length).toBeGreaterThan(0);
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Exact evidence" }),
  ).toBeVisible();
  await expect(
    page.getByText("Exact locator", { exact: true }).first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Copy citation", exact: true })
    .click();
  const citation = await page.evaluate(() => navigator.clipboard.readText());
  expect(citation).toContain(query.segments[0].matched_unit_ids[0]);
  const downloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Export response JSON", exact: true })
    .click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  expect(JSON.parse(await readFile(downloadPath!, "utf8")).run_id).toBe(
    query.run_id,
  );
  await page
    .getByRole("button", { name: "Inspect original", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Retained original" }),
  ).toContainText("record cedar inspection outcomes");
  const originalDownloadPromise = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download original bytes", exact: true })
    .click();
  const originalDownload = await originalDownloadPromise;
  expect(await readFile((await originalDownload.path())!, "utf8")).toContain(
    "record cedar inspection outcomes",
  );
  const relationsPromise = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/v1/relations",
  );
  await page
    .getByRole("button", { name: "Explore relations", exact: true })
    .click();
  const relationsResponse = await relationsPromise;
  expect((await relationsResponse.json()).code).toBe("INDEX_INCOMPLETE");
  await expect(page.getByRole("region", { name: "Relations" })).toContainText(
    "INDEX_INCOMPLETE",
  );
  await screenshot(page, "desktop-search");
  await page.getByRole("link", { name: "Sources", exact: true }).click();
  await page
    .getByRole("button", { name: "Inspect revisions", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Inspect coverage", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Retrieval channels" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Open revision", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Extraction coverage" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Inspect original", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Retained original" }),
  ).toContainText("cedar");
  await screenshot(page, "desktop-coverage");
  await page.getByRole("link", { name: "Activity", exact: true }).click();
  await expect(page.getByText("Query text not retained").first()).toBeVisible();
  await page.getByRole("link", { name: query.run_id, exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Replay retained query" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Query text not retained", { exact: true }),
  ).toBeVisible();

  // Upload a declared synthetic gold case through real management HTTP, then run through the UI.
  // This is workflow coverage, not a retrieval quality benchmark.
  const payload =
    JSON.stringify({
      case_id: "cedar-case",
      family_key: "cedar-case",
      split: "test",
      benchmark: "integrity",
      label_provenance: "synthetic workflow fixture",
      query: { question: "cedar", modalities: ["lexical"] },
      expected_source_ids: [query.segments[0].source_id],
    }) + "\n";
  const datasetResponse = await context.request.post(
    "/v1/evaluations/datasets",
    {
      headers: { Authorization: `Bearer ${owner}` },
      data: {
        manifest_json: JSON.stringify({
          format: "tychora-gold@1",
          dataset_id: "synthetic-browser-v1",
          name: "Synthetic browser workflow",
          version_sha: createHash("sha256").update(payload).digest("hex"),
          case_count: 1,
        }),
        cases_jsonl: payload,
      },
    },
  );
  expect(datasetResponse.status(), await datasetResponse.text()).toBe(201);
  const dataset = await datasetResponse.json();
  await page.getByRole("link", { name: "Evaluations", exact: true }).click();
  await page
    .getByRole("button", { name: "Submit evaluation", exact: true })
    .click();
  await page
    .getByLabel("Registered dataset ID", { exact: true })
    .fill(dataset.dataset_id);
  await page.getByLabel("Snapshot ID", { exact: true }).fill(query.snapshot_id);
  await page
    .getByRole("textbox", { name: "Configuration variants JSON", exact: true })
    .fill(JSON.stringify([{ name: "baseline", profile: "local" }]));
  const evaluationPromise = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/evaluations" &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Run evaluation", exact: true })
    .click();
  const evaluationResponse = await evaluationPromise;
  expect(evaluationResponse.status()).toBe(202);
  const evaluation = await evaluationResponse.json();
  const secondResponse = await context.request.post("/v1/evaluations", {
    headers: { Authorization: `Bearer ${owner}` },
    data: {
      dataset_id: dataset.dataset_id,
      split: "test",
      snapshot_id: query.snapshot_id,
      variants: [{ name: "baseline", profile: "local" }],
      offline: true,
    },
  });
  expect(secondResponse.status()).toBe(202);
  const second = await secondResponse.json();
  await page
    .getByRole("link", { name: "Inspect evaluation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Aggregate metrics" }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "All evaluations", exact: true })
    .click();
  await page.getByLabel(`Compare ${evaluation.id}`, { exact: true }).check();
  await page.getByLabel(`Compare ${second.id}`, { exact: true }).check();
  await expect(
    page.getByRole("heading", { name: "Compare evaluations" }),
  ).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "Common numeric metric" }),
  ).toBeVisible();
  const detailResponses = await Promise.all(
    [evaluation.id, second.id].map((id: string) =>
      context.request.get(`/v1/evaluations/${id}`, {
        headers: { Authorization: `Bearer ${owner}` },
      }),
    ),
  );
  const details = await Promise.all(
    detailResponses.map(async (response) => {
      expect(response.status()).toBe(200);
      return response.json();
    }),
  );
  const recall = details.map(
    (detail) =>
      detail.report.aggregate.metrics.find(
        (metric: { name: string; value: unknown }) =>
          metric.name === "source_recall",
      ).value,
  );
  expect(typeof recall[0]).toBe("number");
  expect(typeof recall[1]).toBe("number");
  const recallRow = page
    .getByRole("row")
    .filter({
      has: page.getByRole("cell", { name: "source_recall", exact: true }),
    });
  await expect(recallRow.getByRole("cell")).toHaveText([
    "source_recall",
    String(recall[0]),
    String(recall[1]),
    String(recall[1] - recall[0]),
  ]);
  await screenshot(page, "desktop-evaluations");
  await page.getByRole("link", { name: evaluation.id, exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Aggregate metrics" }),
  ).toBeVisible();
  await expect(
    page.getByText("cedar-case · 1 runs", { exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(
    page.getByText("Redacted effective configuration", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Complete workspace TOML", { exact: true }),
  ).toBeVisible();
  await screenshot(page, "desktop-settings");
  expect(errors).toEqual([]);
  for (const path of [
    "/v1/handshake",
    "/v1/sources",
    `/v1/jobs/${receipt.id}`,
    "/v1/queries",
    "/v1/relations",
    "/v1/evaluations",
    "/v1/settings",
  ])
    expect(responses.some((response) => response.path === path)).toBe(true);
  expect(responses.some((response) => response.path.includes("/events"))).toBe(
    true,
  );
  expect(
    await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      cookies: document.cookie,
    })),
  ).toEqual({ local: [], session: [], cookies: "" });
  expect(page.url()).not.toContain(owner);
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(page.getByLabel("Bearer token")).toHaveValue("");
  await connect(page, reader);
  await expect(
    page.getByRole("button", { name: "Activate configuration", exact: true }),
  ).toHaveCount(0);
  // The owner's exclusive settings gate can report busy while read requests finish.
  await expect
    .poll(async () => {
      const forbidden = await context.request.put("/v1/settings", {
        headers: { Authorization: `Bearer ${reader}` },
        data: { profile: "local", workspace_toml: "" },
      });
      return forbidden.status();
    })
    .toBe(403);
  await page.getByRole("link", { name: "Sources", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Add source", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Evaluations", exact: true }).click();
  await expect(
    page.getByText(
      "Evaluation listing, inspection and submission require manage capability.",
    ),
  ).toBeVisible();
});

test("narrow-screen keyboard, empty query and reconnect", async ({
  page,
  context,
}) => {
  const seed = await context.request.post("/v1/sources", {
    headers: { Authorization: `Bearer ${owner}` },
    data: {
      type: "text",
      source_key: "synthetic-mobile-manual",
      collection_id: "default",
      kind: "manual@1",
      format: "text",
      text: "Synthetic mobile cedar evidence.",
      metadata: {
        title: "Mobile fixture",
        publisher: "Synthetic fixture",
        edition: "1",
      },
    },
  });
  expect(seed.status()).toBe(202);
  const seedJob = await seed.json();
  await expect
    .poll(async () => {
      const status = await context.request.get(`/v1/jobs/${seedJob.id}`, {
        headers: { Authorization: `Bearer ${owner}` },
      });
      return (await status.json()).state;
    })
    .toBe("succeeded");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/dashboard/");
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Bearer token")).toBeFocused();
  await connect(page, reader);
  await page
    .getByLabel("Question", { exact: true })
    .fill("zzznosyntheticmatchzzz");
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "No matching evidence" }),
  ).toBeVisible();
  await screenshot(page, "mobile-empty-search");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  await page.getByLabel("Question", { exact: true }).fill("cedar");
  await page.getByLabel("Question", { exact: true }).press("Tab");
  await expect(
    page.getByRole("button", { name: "Search evidence", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Exact evidence" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Inspect original", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Retained original" }),
  ).toContainText("cedar");
  await screenshot(page, "mobile-search");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  await page.reload();
  await expect(page.getByLabel("Bearer token")).toHaveValue("");
  await connect(page, reader);
  await expect(page.getByLabel("Question", { exact: true })).toHaveValue("");
});
