import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import {
  expect,
  test,
  type Page,
  type APIRequestContext,
} from "@playwright/test";
const scenario = process.env.TYCHORA_E2E_SCENARIO || "local";
const fixtures = process.env.TYCHORA_E2E_FIXTURE_DIR!;
const artifacts = resolve(
  import.meta.dirname,
  "../../.superpowers/verification/dashboard-e2e/task6",
);
const headers = { Authorization: "Bearer synthetic-dashboard-owner" };
async function api(request: APIRequestContext, path: string, data?: unknown) {
  const response =
    data === undefined
      ? await request.get(path, { headers })
      : await request.post(path, { headers, data });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}
async function connect(page: Page) {
  await page.goto("/dashboard/documents/add");
  await page.getByLabel("Bearer token").fill("synthetic-dashboard-owner");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByRole("button", { name: "Disconnect" })).toBeVisible();
}
async function capture(page: Page, name: string) {
  await mkdir(artifacts, { recursive: true });
  await page.screenshot({
    path: resolve(artifacts, name + ".png"),
    fullPage: true,
  });
}
test("task6 streamed PDF above JSON cap with retry and actual OCR review", async ({
  page,
  request,
}) => {
  test.skip(
    scenario !== "ocr",
    "Explicit installed Docling/OCR/tokenizer qualification required",
  );
  test.setTimeout(240000);
  const grant = await api(request, "/v1/handshake");
  const data = await readFile(resolve(fixtures, "task6-mixed.pdf"));
  expect(data.length).toBeGreaterThan(1048576);
  await connect(page);
  await page
    .getByLabel("Title", { exact: true })
    .fill("Synthetic streamed PDF");
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption(grant.default_collection_id);
  await page.getByText("Advanced source options", { exact: true }).click();
  await page
    .getByLabel("Source key", { exact: true })
    .fill("synthetic-pdf-recovery");
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page
    .getByLabel("Source file", { exact: true })
    .setInputFiles(resolve(fixtures, "task6-mixed.pdf"));
  const analyseResponse = page.waitForResponse(
    (r) => r.url().endsWith("/analyse") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Analyse PDF", exact: true }).click();
  const analysed = await (await analyseResponse).json();
  expect(analysed.state).toBe("analysed");
  expect(analysed.received_bytes).toBe(data.length);
  expect(analysed.analysis.policy_sha256).toMatch(/^[a-f0-9]{64}$/);
  await expect(
    page.getByText("Mixed · 3 physical pages", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Ingest PDF" })).toBeDisabled();
  await page.getByText("Advanced source options", { exact: true }).click();
  await capture(page, "desktop-analysis");
  await page
    .getByRole("checkbox", { name: /confirm this exact analysis/ })
    .check();
  const submission = page.waitForResponse((r) => r.url().endsWith("/submit"));
  await page.getByRole("button", { name: "Ingest PDF" }).click();
  const accepted = await (await submission).json();
  expect(accepted.id).toBeTruthy();
  await expect(
    page.getByText(/Queued or accepted does not mean indexed/),
  ).toBeVisible();
  await page.getByRole("link", { name: "Follow job", exact: true }).click();
  await expect(
    page.getByText("PROVIDER_FAILURE", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry job", exact: true }).click();
  await expect
    .poll(async () => (await api(request, `/v1/jobs/${accepted.id}`)).state, {
      timeout: 150000,
    })
    .toBe("succeeded");
  const events = await api(request, `/v1/jobs/${accepted.id}/events/page`);
  expect(
    events.some((e: { counters: { name: string; count: number }[] }) =>
      e.counters.some((c) => c.name === "accepted_pdf_pages" && c.count === 3),
    ),
  ).toBe(true);
  await expect(
    page.getByText("succeeded", { exact: true }).first(),
  ).toBeVisible();
  await capture(page, "desktop-job");
  const excluded = await api(request, "/v1/queries", {
    question: "violet",
    mode: "review-excluded",
    limit: 8,
  });
  const included = await api(request, "/v1/queries", {
    question: "violet",
    mode: "review-included",
    limit: 8,
  });
  expect(excluded.visuals).toHaveLength(0);
  expect(included.visuals.length).toBeGreaterThan(0);
  const visual = included.visuals.find((v: { proxies: { origin: string }[] }) =>
    v.proxies.some((p) => p.origin === "ocr"),
  );
  const proxy = visual.proxies.find(
    (p: { origin: string }) => p.origin === "ocr",
  );
  const evidence = await api(request, `/v1/evidence/${proxy.proxy_id}`);
  expect(evidence.evidence.citable).toBe(false);
  expect(evidence.evidence.review_state).toBe("needs_review");
  expect(evidence.evidence.locators[0].physical_page).toBe(1);
  expect(
    evidence.records.some(
      (r: { record_type: string }) => r.record_type === "vision_derivation",
    ),
  ).toBe(true);
  const original = await api(request, "/v1/inspect", {
    subject: "revisions",
    id: visual.revision_id,
    original: true,
    view: "original",
  });
  expect(original.truncated).toBe(false);
  expect(
    createHash("sha256")
      .update(Buffer.from(original.payload, "base64url"))
      .digest("hex"),
  ).toBe(createHash("sha256").update(data).digest("hex"));
  const content = `/v1/sources/${visual.source_id}/revisions/${visual.revision_id}/extractions/${evidence.evidence.extraction_id}`;
  const windows = await api(
    request,
    content + `/windows?unit_id=${proxy.proxy_id}`,
  );
  expect(windows.windows.length).toBeGreaterThan(0);
  const units = await api(request, content + "/units");
  const coverage = await api(
    request,
    `/v1/sources/${visual.source_id}/revisions/${visual.revision_id}/extractions`,
  );
  const diagnostics = await api(request, "/v1/inspect", {
    subject: "diagnostics",
    id: evidence.evidence.extraction_id,
  });
  expect(coverage.extractions[0].stages.pdf_coverage.page_count).toBe(3);

  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "actual-streamed-pdf.json"),
    JSON.stringify(
      {
        analysed,
        accepted,
        events,
        excluded,
        included,
        evidence,
        windows,
        units,
        coverage,
        diagnostics,
      },
      null,
      2,
    ),
  );
  for (const [device, width] of [
    ["desktop", 1440],
    ["mobile", 390],
  ] as const) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/dashboard/search");
    await page.getByLabel("Bearer token").fill("synthetic-dashboard-owner");
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await page.getByLabel("Question", { exact: true }).fill("violet");
    await page
      .getByText("Filters & retrieval options", { exact: true })
      .click();
    await page
      .getByLabel("Retrieval mode", { exact: true })
      .selectOption("review-excluded");
    await page
      .getByRole("button", { name: "Search evidence", exact: true })
      .click();
    await expect(
      page.getByText("No matching evidence", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", {
        name: "Include review-required content",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Inspect returned ocr proxy", exact: true })
      .first()
      .click();
    await expect(
      page.getByRole("region", { name: "Returned proxy passage" }),
    ).toContainText(proxy.text);
    await capture(page, device + "-ocr-passage");
    await page.getByRole("button", { name: "Full unit", exact: true }).click();
    await expect(
      page.getByRole("region", { name: "Evidence reader" }),
    ).toContainText("needs_review · not citable");
    await expect(
      page.getByRole("region", { name: "Evidence reader" }),
    ).toContainText("physical page 1");
    await capture(page, device + "-ocr-unit");
    await page
      .getByRole("button", { name: "Stored chunks", exact: true })
      .click();
    await expect(
      page.getByRole("region", { name: "Stored proxy chunks" }),
    ).toContainText(windows.windows[0].text);
    await capture(page, device + "-ocr-chunks");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  // Throttle a genuine File-body HTTP upload, then cancel it through the UI.
  await page.setViewportSize({ width: 1440, height: 1000 });
  await connect(page);
  await page.getByLabel("Title", { exact: true }).fill("Cancelled PDF");
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption(grant.default_collection_id);
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page
    .getByLabel("Source file")
    .setInputFiles(resolve(fixtures, "task6-mixed.pdf"));
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: 0,
    downloadThroughput: 10000000,
    uploadThroughput: 32768,
  });
  await page.getByRole("button", { name: "Analyse PDF" }).click();
  await expect(page.getByText("Uploading PDF…", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Cancel upload", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Upload stopped/)).toBeVisible();
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: -1,
  });
  await capture(page, "desktop-upload-cancelled");
  // A new destination invalidates an analysed receipt; cleanup uses its old scope.
  const second = await api(request, "/v1/collections", {
    title: "PDF alternative",
    slug: "pdf-alternative",
  });
  await connect(page);
  await page.getByLabel("Title", { exact: true }).fill("Destination renewal");
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption(grant.default_collection_id);
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page
    .getByLabel("Source file")
    .setInputFiles(resolve(fixtures, "task6-native.pdf"));
  await page.getByRole("button", { name: "Analyse PDF" }).click();
  await expect(
    page.getByText("Native text · 1 physical pages", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("checkbox", { name: /confirm this exact analysis/ })
    .check();
  const cleanup = page.waitForResponse(
    (r) =>
      r.request().method() === "DELETE" && r.url().includes("/pdf-intakes/"),
  );
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption(second.collection_id);
  expect((await cleanup).status()).toBe(200);
  await expect(
    page.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Analyse PDF" })).toBeEnabled();
});
test("task6 actual PDF analysis and typed failure matrix", async ({
  request,
  page,
}) => {
  test.skip(scenario !== "ocr", "Explicit qualified scenario");
  test.setTimeout(120000);
  const grant = await api(request, "/v1/handshake");
  for (const mode of ["scan", "native", "blank", "invalid"]) {
    const bytes = await readFile(resolve(fixtures, `task6-${mode}.pdf`));
    const prepared = await api(request, "/v1/pdf-intakes", {
      source_key: `matrix-${mode}`,
      collection_id: grant.default_collection_id,
      kind: "document@1",
      metadata: { title: `Synthetic ${mode}` },
      expected_size_bytes: bytes.length,
    });
    const path = `/v1/pdf-intakes/${prepared.intake_id}`;
    expect(
      (
        await request.put(path + "/content", {
          headers: { ...headers, "Content-Type": "application/pdf" },
          data: bytes,
        })
      ).ok(),
    ).toBe(true);
    const response = await request.post(path + "/analyse", { headers });
    if (mode === "invalid") {
      expect(response.status()).toBe(400);
    } else {
      const result = (await response.json()).analysis;
      if (mode === "scan") expect(result.requires_ocr).toBe(true);
      if (mode === "native") expect(result.requires_ocr).toBe(false);
      if (mode === "blank")
        expect(result.blockers.join(" ")).toContain("INDEX_INCOMPLETE");
    }
    await request.delete(path, { headers });
  }
  const bytes = await readFile(resolve(fixtures, "task6-native.pdf"));
  const prepared = await api(request, "/v1/pdf-intakes", {
    source_key: "synthetic-pdf-partial",
    collection_id: grant.default_collection_id,
    kind: "document@1",
    metadata: { title: "Synthetic partial timeout" },
    expected_size_bytes: bytes.length,
  });
  const path = `/v1/pdf-intakes/${prepared.intake_id}`;
  await request.put(path + "/content", {
    headers: { ...headers, "Content-Type": "application/pdf" },
    data: bytes,
  });
  const analysis = (await api(request, path + "/analyse", {})).analysis;
  const handle = await api(request, path + "/submit", {
    analysis_sha256: analysis.analysis_sha256,
    policy_sha256: analysis.policy_sha256,
  });
  await expect
    .poll(async () => (await api(request, `/v1/jobs/${handle.id}`)).state)
    .toBe("failed");
  expect((await api(request, `/v1/jobs/${handle.id}`)).error_code).toBe(
    "DEADLINE_EXCEEDED",
  );
  await page.goto(`/dashboard/jobs/${handle.id}`);
  await page.getByLabel("Bearer token").fill("synthetic-dashboard-owner");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(
    page.getByText("DEADLINE_EXCEEDED", { exact: true }),
  ).toBeVisible();
  await capture(page, "desktop-synthetic-partial-job");
  // Complete original effective grant must be preserved across managed expansion.
  const scopedHeaders = {
    ...headers,
    "X-Tychora-Scope": JSON.stringify({
      ...grant.scope,
      collection_ids: [grant.default_collection_id],
    }),
  };
  const old = await request.post("/v1/pdf-intakes", {
    headers: scopedHeaders,
    data: {
      source_key: "scope-fixture",
      collection_id: grant.default_collection_id,
      kind: "document@1",
      metadata: { title: "Scope" },
      expected_size_bytes: bytes.length,
    },
  });
  expect(old.ok()).toBe(true);
  const oldPath = `/v1/pdf-intakes/${(await old.json()).intake_id}`;
  await api(request, "/v1/collections", {
    title: "Grant expansion",
    slug: "grant-expansion",
  });
  expect((await request.get(oldPath, { headers })).status()).toBe(403);
  expect(
    (await request.get(oldPath, { headers: scopedHeaders })).status(),
  ).toBe(200);
  expect(
    (await request.delete(oldPath, { headers: scopedHeaders })).status(),
  ).toBe(200);
});

test("task6 skip link stays outside viewport except during keyboard focus", async ({
  page,
}) => {
  await page.goto("/dashboard/");
  const measure = () =>
    page.locator(".skip-link").evaluate((element) => ({
      focused: document.activeElement === element,
      top: element.getBoundingClientRect().top,
      bottom: element.getBoundingClientRect().bottom,
      position: getComputedStyle(element).position,
      scrollY,
    }));
  const before = await measure();
  expect(before.focused).toBe(false);
  expect(before.bottom).toBeLessThanOrEqual(0);
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
  const focused = await measure();
  expect(focused.top).toBeGreaterThanOrEqual(0);
  await page.keyboard.press("Tab");
  const after = await measure();
  expect(after.focused).toBe(false);
  expect(after.bottom).toBeLessThanOrEqual(0);
  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "skip-link-focus-bounds.json"),
    JSON.stringify({ before, focused, after }, null, 2),
  );
});
