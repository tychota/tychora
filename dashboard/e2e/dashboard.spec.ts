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
  await page.getByRole("link", { name: "Documents", exact: true }).click();
  await expect(page.getByText("Add a document to start reading")).toBeVisible();
  await screenshot(page, "sources-empty");
  await page.getByRole("link", { name: "Add documents", exact: true }).click();
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption({ label: "Default" });
  await page.getByText("Advanced source options", { exact: true }).click();
  await page
    .getByLabel("Source key", { exact: true })
    .fill("synthetic-browser-manual");
  await page.getByLabel("Kind", { exact: true }).fill("manual@1");
  await page
    .getByLabel("Content", { exact: true })
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
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
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
  await page.getByRole("link", { name: "Documents", exact: true }).click();
  await page.getByRole("button", { name: /Synthetic browser manual/ }).click();
  await page.getByRole("tab", { name: "History", exact: true }).click();
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
  const recallRow = page.getByRole("row").filter({
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
    page.getByText("Redacted effective configuration and origins", {
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("link", {
      name: "Edit model and processing configuration",
      exact: true,
    })
    .click();
  await page
    .getByText("Expert complete TOML activation", { exact: true })
    .click();
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
  await page.getByRole("link", { name: "Documents", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Add documents", exact: true }),
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
  await page.getByRole("button", { name: "Navigation", exact: true }).click();
  await page.getByRole("link", { name: "Search", exact: true }).click();
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
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
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

for (const mobile of [false, true]) {
  test(`named collection → simple text → exact content/chunks ${mobile ? "mobile" : "desktop"}`, async ({
    page,
    context,
  }) => {
    const title = mobile ? "Mobile field notes" : "Cedar field notes";
    const text =
      "Cedar verification 😀 <strong>remains inert</strong>.\nRetain the complete original passage.";
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/dashboard/collections");
    await connect(page);
    await page
      .getByRole("button", { name: "Create collection", exact: true })
      .click();
    await page.getByLabel("Collection name", { exact: true }).fill(title);
    await page
      .getByLabel("Slug", { exact: true })
      .fill(mobile ? "mobile-field-notes" : "cedar-field-notes");
    const createdResponse = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/v1/collections" &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Create", exact: true }).click();
    const created = await createdResponse;
    expect(created.status()).toBe(201);
    const collection = await created.json();
    await expect(
      page.getByRole("heading", { name: title, exact: true }),
    ).toBeVisible();
    await expect(page.locator(".scope-selector summary")).toContainText(title);
    await screenshot(
      page,
      `task4-${mobile ? "mobile" : "desktop"}-collections`,
    );
    const row = page
      .getByRole("row")
      .filter({ has: page.getByRole("link", { name: title, exact: true }) });
    await row.getByRole("button", { name: "Documents", exact: true }).click();
    await page
      .getByRole("link", { name: "Add documents", exact: true })
      .click();
    await page.getByLabel("Title", { exact: true }).fill(`${title} document`);
    await page.getByLabel("Content", { exact: true }).fill(text);
    await expect(
      page.getByRole("button", { name: "Index text", exact: true }),
    ).toBeDisabled();
    await page
      .getByLabel("Destination", { exact: true })
      .selectOption(collection.collection_id);
    await screenshot(page, `task4-${mobile ? "mobile" : "desktop"}-add-text`);
    await page.getByLabel("Content", { exact: true }).press("Tab");
    const acceptedResponse = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === "/v1/sources" &&
        r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Index text", exact: true }).click();
    const accepted = await acceptedResponse;
    expect(accepted.status()).toBe(202);
    const receipt = await accepted.json();
    await expect
      .poll(async () => {
        const response = await context.request.get(`/v1/jobs/${receipt.id}`, {
          headers: { Authorization: `Bearer ${owner}` },
        });
        return (await response.json()).state;
      })
      .toBe("succeeded");
    await page
      .getByRole("link", { name: "Open documents", exact: true })
      .click();
    await page
      .getByRole("button", { name: new RegExp(`${title} document`) })
      .click();
    await expect(
      page.getByRole("heading", { name: `${title} document`, exact: true }),
    ).toBeFocused();
    await page.getByRole("tab", { name: "Content", exact: true }).click();
    await expect(
      page.locator(".content-unit .evidence-text").first(),
    ).toContainText("Cedar verification 😀 <strong>remains inert</strong>.");
    expect(
      await page.locator(".content-unit .evidence-text strong").count(),
    ).toBe(0);
    await screenshot(page, `task4-${mobile ? "mobile" : "desktop"}-content`);
    await page
      .getByRole("tab", { name: "Content", exact: true })
      .press("ArrowRight");
    await expect(
      page.getByRole("tab", { name: "Chunks", exact: true }),
    ).toBeFocused();
    const chunks = page.locator(".content-unit .evidence-text");
    await expect(chunks.first()).toContainText(
      "Cedar verification 😀 <strong>remains inert</strong>.",
    );
    await screenshot(page, `task4-${mobile ? "mobile" : "desktop"}-documents`);
    await page
      .getByRole("button", { name: "Full unit", exact: true })
      .first()
      .click();
    await expect(
      page.getByRole("heading", { name: "Exact evidence", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".evidence-reader .evidence-text")).toContainText(
      "Cedar verification 😀",
    );
    await screenshot(page, `task4-${mobile ? "mobile" : "desktop"}-full-unit`);
    if (mobile) {
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBe(390);
      await page
        .getByRole("button", { name: "Back to documents", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: new RegExp(`${title} document`) }),
      ).toBeFocused();
      await page
        .getByRole("button", { name: "Navigation", exact: true })
        .click();
      await expect(
        page.getByRole("link", { name: "Models", exact: true }),
      ).toBeVisible();
    }
    expect(errors).toEqual([]);
    expect(
      await page.evaluate(() => ({
        local: localStorage.length,
        session: sessionStorage.length,
      })),
    ).toEqual({ local: 0, session: 0 });
  });
}

test("mobile source-only links continue the real inventory and preserve document identity", async ({
  page,
  context,
}) => {
  for (let i = 0; i < 21; i++) {
    const added = await context.request.post("/v1/sources", {
      headers: { Authorization: `Bearer ${owner}` },
      data: {
        type: "text",
        source_key: `synthetic-fix1-pagination-${i}`,
        collection_id: "default",
        kind: "document@1",
        format: "text",
        text: `Synthetic pagination record ${i}.`,
        metadata: { title: `Pagination fixture ${i}` },
      },
    });
    expect(added.status()).toBe(202);
    const receipt = await added.json();
    await expect
      .poll(async () => {
        const status = await context.request.get(`/v1/jobs/${receipt.id}`, {
          headers: { Authorization: `Bearer ${owner}` },
        });
        return (await status.json()).state;
      })
      .toBe("succeeded");
  }
  const headers = { Authorization: `Bearer ${reader}` };
  const inventoryResponse = await context.request.get(
    "/v1/documents?limit=100",
    { headers },
  );
  expect(inventoryResponse.status()).toBe(200);
  const inventory = await inventoryResponse.json();
  const target = inventory.documents.at(-1);
  const firstResponse = await context.request.get("/v1/documents?limit=20", {
    headers,
  });
  expect(firstResponse.status()).toBe(200);
  const first = await firstResponse.json();
  expect(first.next_cursor).not.toBeNull();
  expect(
    first.documents.some(
      (d: { source_id: string }) => d.source_id === target.source_id,
    ),
  ).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ["documents", "sources"]) {
    const path = `/dashboard/${route}/${target.source_id}`;
    await page.goto(path);
    await connect(page, reader);
    const detail = page.getByRole("region", {
      name: "Selected document",
      exact: true,
    });
    await expect(
      page.getByRole("region", { name: "Document inventory", exact: true }),
    ).toBeHidden();
    await expect(
      detail.getByRole("button", { name: "Load more documents", exact: true }),
    ).toBeVisible();
    await expect(
      detail.getByRole("button", {
        name: "Refresh document inventory",
        exact: true,
      }),
    ).toBeVisible();
    await detail
      .getByRole("button", { name: "Load more documents", exact: true })
      .click();
    await expect(
      detail.getByRole("heading", { name: target.title, exact: true }),
    ).toBeFocused();
    expect(new URL(page.url()).pathname).toBe(path);
    await detail.getByRole("tab", { name: "Chunks", exact: true }).click();
    await expect(
      detail.locator(".content-unit .evidence-text").first(),
    ).not.toBeEmpty();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBe(390);
    await screenshot(page, `task4-fix1-mobile-${route}-continued-detail`);
  }
});
