import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import {
  expect,
  test,
  type Page,
  type APIRequestContext,
} from "@playwright/test";
import type { QueryResult, ExtractionWindowsPage } from "../src/api/contracts";
const scenario = process.env.TYCHORA_E2E_SCENARIO || "local";
const fixtures =
  process.env.TYCHORA_E2E_FIXTURE_DIR ||
  resolve(
    import.meta.dirname,
    "../../.superpowers/verification/dashboard-e2e/task5-fixtures",
  );
const artifacts =
  process.env.TYCHORA_E2E_ARTIFACT_DIR ||
  resolve(
    import.meta.dirname,
    "../../.superpowers/verification/dashboard-e2e/task5",
  );
const headers = { Authorization: "Bearer synthetic-dashboard-owner" };
async function api(request: APIRequestContext, path: string, data?: unknown) {
  const r =
    data === undefined
      ? await request.get(path, { headers })
      : await request.post(path, { headers, data });
  expect(r.ok(), `${r.status()} ${await r.text()}`).toBeTruthy();
  return r.json();
}
async function connect(page: Page) {
  await page.getByLabel("Bearer token").fill("synthetic-dashboard-owner");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
}
async function open(page: Page, path: string) {
  await page.goto(path);
  await connect(page);
}
async function job(
  request: APIRequestContext,
  id: string,
  state = "succeeded",
) {
  await expect
    .poll(async () => (await api(request, `/v1/jobs/${id}`)).state, {
      timeout: 120_000,
    })
    .toBe(state);
  return api(request, `/v1/jobs/${id}`);
}
async function capture(page: Page, name: string) {
  await mkdir(artifacts, { recursive: true });
  await page.screenshot({
    path: resolve(artifacts, `${name}.png`),
    fullPage: true,
  });
}
async function text(request: APIRequestContext, key: string, content: string) {
  const grant = await api(request, "/v1/handshake");
  const h = await api(request, "/v1/sources", {
    type: "text",
    source_key: key,
    kind: "document@1",
    collection_id: grant.default_collection_id,
    format: "markdown",
    metadata: { title: key },
    text: content,
  });
  return h;
}
test.beforeEach(async ({ page, context }) => {
  const deny = () => {
    throw new Error("Task5 journeys require actual HTTP, no browser mocks");
  };
  page.route = deny;
  page.routeFromHAR = deny;
  context.route = deny;
  context.routeFromHAR = deny;
});

test("task5 real exact passages, durable recovery, maintenance and dataset registration", async ({
  page,
  request,
}) => {
  test.skip(
    scenario !== "local",
    "Runs on the model-free disposable local server",
  );
  test.setTimeout(180_000);
  const recovery = await text(
    request,
    "synthetic-recovery",
    "Recover this synthetic transient source failure.",
  );
  await job(request, recovery.id, "failed");
  await open(page, `/dashboard/jobs/${recovery.id}`);
  await page.getByRole("button", { name: "Retry job", exact: true }).click();
  await job(request, recovery.id);
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(
    page.getByText("succeeded", { exact: true }).first(),
  ).toBeVisible();
  const content =
    "Anchorrare 😀 <strong>literal</strong> é " +
    "retained cedar provenance with exact long source text. ".repeat(180);
  await job(request, (await text(request, "synthetic-long-unit", content)).id);
  // Choose a measured budget that clips an anchor inside its actual retained chunk.
  let clipped: QueryResult | undefined;
  let retained: ExtractionWindowsPage | undefined;
  let budget = 0;
  for (const n of [6000, 5500, 5000, 4500, 4000, 3500, 3000]) {
    const measured = await request.post("/v1/queries", {
      headers,
      data: {
        question: "Anchorrare",
        limit: 1,
        budgets: { max_tokens: n, tokenizer_id: "cl100k_base" },
      },
    });
    if (
      measured.status() === 400 &&
      (await measured.json()).code === "BUDGET_TOO_SMALL"
    )
      continue;
    expect(measured.ok(), await measured.text()).toBeTruthy();
    const q = (await measured.json()) as QueryResult;
    await mkdir(artifacts, { recursive: true });
    await writeFile(
      resolve(artifacts, `clipping-budget-${n}.json`),
      JSON.stringify(q, null, 2),
    );
    const s = q.segments[0];
    const a = s?.anchors.find((a) => a.indexed_window_id);
    if (!s || !a) continue;
    const w = (await api(
      request,
      `/v1/sources/${s.source_id}/revisions/${s.revision_id}/extractions/${s.extraction_id}/windows?chunk_id=${a.indexed_window_id}&unit_id=${a.unit_id}`,
    )) as ExtractionWindowsPage;
    if (
      w.windows.some(
        (w) => w.char_start < a.char_start || w.char_end > a.char_end,
      )
    ) {
      clipped = q;
      retained = w;
      budget = n;
      break;
    }
  }
  expect(
    clipped,
    "a real query budget must clip its retained window",
  ).toBeDefined();
  await page.getByRole("link", { name: "Search", exact: true }).click();
  await page.getByLabel("Question", { exact: true }).fill("Anchorrare");
  await page.getByText("Filters & retrieval options", { exact: true }).click();
  await page.getByLabel("Response token budget").fill(String(budget));
  await page.getByLabel("Result limit").fill("1");
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await expect(
    page.getByRole("region", { name: "Returned passage" }),
  ).toContainText(clipped!.segments[0].excerpts[0].text);
  await capture(page, "desktop-returned-passage");
  await page
    .getByRole("button", { name: "Stored chunks", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Stored chunks" }),
  ).toContainText(retained!.windows[0].text);
  await capture(page, "desktop-stored-chunk");
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Evidence reader" }),
  ).toContainText(content);
  await capture(page, "desktop-full-unit");
  await page
    .getByRole("link", { name: "Open selected evidence in Explore" })
    .click();
  await page
    .getByRole("button", { name: "Explore relations", exact: true })
    .click();
  await expect(page.getByRole("region", { name: "Relations" })).toContainText(
    /INDEX_INCOMPLETE|No relation paths/,
  );
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  const health = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/jobs" &&
      r.request().postDataJSON()?.operation === "health",
  );
  await page.getByRole("button", { name: "Run health check" }).click();
  const hr = await health;
  expect(hr.request().headers()["x-tychora-scope"]).toBeUndefined();
  await expect(page.getByText(/verified_payloads:/)).toBeVisible();
  const paths = JSON.parse(
    await readFile(resolve(fixtures, "context.json"), "utf8"),
  );
  await page
    .getByLabel("Backup destination server path", { exact: true })
    .fill(paths.backup);
  await page
    .getByRole("button", { name: "Export backup", exact: true })
    .click();
  await expect(
    page
      .getByRole("region", { name: "Operation result" })
      .filter({ hasText: "Server result · export" }),
  ).toBeVisible();
  await page.getByLabel("Restore archive server path").fill(paths.backup);
  await page
    .getByLabel("Clean restore destination server path")
    .fill(paths.restore);
  await page
    .getByLabel("Confirm the archive and clean separate restore destination")
    .check();
  await page.getByRole("button", { name: "Restore verified backup" }).click();
  await expect(
    page
      .getByRole("region", { name: "Operation result" })
      .filter({ hasText: "Server result · import" }),
  ).toBeVisible();
  await capture(page, "workspace-maintenance");
  await page.getByRole("link", { name: "Evaluations", exact: true }).click();
  await page.getByText("Register evaluation dataset", { exact: true }).click();
  const cases =
    JSON.stringify({
      case_id: "anchor-case",
      family_key: "anchor",
      split: "test",
      benchmark: "integrity",
      label_provenance: "synthetic Task5 workflow",
      query: { question: "Anchorrare", modalities: ["lexical"] },
      expected_source_ids: [clipped!.segments[0].source_id],
    }) + "\n";
  await page.getByLabel("Dataset manifest JSON").fill(
    JSON.stringify({
      format: "tychora-gold@1",
      dataset_id: "task5-synthetic",
      name: "Task5 synthetic",
      version_sha: createHash("sha256").update(cases).digest("hex"),
      case_count: 1,
    }),
  );
  await page.getByLabel("Dataset cases JSONL").fill(cases);
  await page
    .getByRole("button", { name: "Register dataset", exact: true })
    .click();
  await expect(
    page.getByText(/Registered dataset task5-synthetic/),
  ).toBeVisible();
  const grant = await api(request, "/v1/handshake");
  await api(
    request,
    `/v1/collections/${grant.default_collection_id}/archive`,
    {},
  );
  const archived = await api(request, "/v1/queries", {
    question: "Anchorrare",
    limit: 1,
  });
  expect(archived.segments).toHaveLength(0);
  const retainedEvidence = await api(
    request,
    `/v1/evidence/${clipped!.segments[0].matched_unit_ids[0]}`,
  );
  expect(retainedEvidence.evidence.text).toBe(content);
  await api(
    request,
    `/v1/collections/${grant.default_collection_id}/reactivate`,
    {},
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, "/dashboard/search");
  await page.getByLabel("Question", { exact: true }).fill("Anchorrare");
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await page
    .getByRole("button", { name: "Returned passage", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await capture(page, "mobile-returned-passage");
  await page
    .getByRole("button", { name: "Stored chunks", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Stored chunks" }),
  ).toContainText(retained!.windows[0].text);
  await capture(page, "mobile-stored-chunk");
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Evidence reader" }),
  ).toContainText(content);
  await capture(page, "mobile-full-unit");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("task5 qualified simulated Voyage setup consent ingestion and reranking", async ({
  page,
  request,
}) => {
  test.skip(
    scenario !== "remote",
    "Opt in with TYCHORA_E2E_SCENARIO=remote and installed tokenizer assets; no live provider",
  );
  test.setTimeout(180_000);
  await open(page, "/dashboard/documents/add");
  await page
    .getByLabel("Destination", { exact: true })
    .selectOption({ label: "Default" });
  await page.getByLabel("Title", { exact: true }).fill("Simulated Voyage note");
  await page
    .getByLabel("Content", { exact: true })
    .fill("Voyage synthetic cedar note preserves exact evidence.");
  await page.getByRole("link", { name: "Models", exact: true }).click();
  await page.getByText("Reranking", { exact: true }).click();
  for (const [name, value] of [
    ["offline", "false"],
    ["providers.text.provider", "voyage"],
    ["providers.text.model", "voyage-context-4"],
    ["providers.text.credential_ref", "env:TYCHORA_SMOKE_VOYAGE_KEY"],
    ["providers.rerank.provider", "voyage"],
    ["providers.rerank.model", "rerank-2.5"],
    ["providers.rerank.credential_ref", "env:TYCHORA_SMOKE_VOYAGE_KEY"],
    ["index.text.backend", "chroma"],
  ]) {
    const input = page.getByLabel(name, { exact: true });
    if (await input.evaluate((e) => e.tagName === "SELECT"))
      await input.selectOption(value);
    else await input.fill(value);
  }
  await page
    .getByRole("button", { name: "Preview changes", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Activate reviewed changes" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Activate reviewed changes" }).click();
  await expect(
    page.getByText("Configuration activated. A rebuild has not been queued.", {
      exact: true,
    }),
  ).toBeVisible();
  await capture(page, "desktop-models");
  await page.setViewportSize({ width: 390, height: 844 });
  await capture(page, "mobile-models");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("link", { name: "Documents", exact: true }).click();
  await page.getByRole("link", { name: "Add documents", exact: true }).click();
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue(
    "Simulated Voyage note",
  );
  await expect(
    page.getByRole("button", { name: "Index text", exact: true }),
  ).toBeDisabled();
  // Credential-not-checked notice enters canonical Models and preserves the draft return record.
  await page
    .locator('a[href="/dashboard/models"]')
    .filter({ hasText: "Set up" })
    .first()
    .click();
  await page.getByRole("button", { name: "Review asset setup plan" }).click();
  await page
    .getByRole("button", { name: "Verify and provision selected local assets" })
    .click();
  await expect(page.getByRole("link", { name: /Follow job/ })).toBeVisible();
  await page
    .getByRole("link", { name: "Return to Add documents", exact: true })
    .click();
  await expect(page.getByLabel("Content", { exact: true })).toHaveValue(
    "Voyage synthetic cedar note preserves exact evidence.",
  );
  await page
    .locator('a[href*="/dashboard/collections/"]')
    .filter({ hasText: "Set up" })
    .first()
    .click();
  await page.getByText("Provider upload consent", { exact: true }).click();
  await page
    .getByRole("button", { name: "Grant voyage upload consent", exact: true })
    .click();
  await page
    .getByRole("link", { name: "Return to Add documents", exact: true })
    .click();
  const accepted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/sources" &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Index text", exact: true }).click();
  await job(request, (await (await accepted).json()).id);
  await page.getByRole("link", { name: "Search", exact: true }).click();
  await page.getByLabel("Question", { exact: true }).fill("cedar provenance");
  const queried = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/v1/queries" &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  const result = await (await queried).json();
  expect(result.segments.length).toBeGreaterThan(0);
  expect(result.coverage.completed_channels).toContain("text");
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await capture(page, "simulated-provider-search");
  const records = (
    await readFile(resolve(fixtures, "simulated-provider.jsonl"), "utf8")
  )
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  expect(
    records.some(
      (r) =>
        r.path === "/v1/contextualizedembeddings" &&
        r.payload.model === "voyage-context-4",
    ),
  ).toBeTruthy();
  expect(
    records.some(
      (r) =>
        r.path === "/v1/rerank" &&
        r.payload.model === "rerank-2.5" &&
        r.payload.documents.length > 0,
    ),
  ).toBeTruthy();
  const grant = await api(request, "/v1/handshake");
  await page.getByRole("link", { name: "Collections", exact: true }).click();
  await page
    .locator(`a[href="/dashboard/collections/${grant.default_collection_id}"]`)
    .first()
    .click();
  await page.getByText("Provider upload consent", { exact: true }).click();
  await page
    .getByRole("button", { name: "Revoke voyage upload consent", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Grant voyage upload consent",
      exact: true,
      includeHidden: true,
    }),
  ).toHaveCount(1);
  const posts = records.length;
  const denied = await request.post("/v1/sources", {
    headers,
    data: {
      type: "text",
      source_key: "denied-after-revoke",
      kind: "document@1",
      collection_id: grant.default_collection_id,
      format: "markdown",
      metadata: { title: "Denied" },
      text: "Synthetic denied upload",
    },
  });
  expect(denied.status()).toBe(202);
  const deniedJob = await job(request, (await denied.json()).id, "failed");
  expect(deniedJob.error_code).toBe("FORBIDDEN_SCOPE");
  expect(
    (await readFile(resolve(fixtures, "simulated-provider.jsonl"), "utf8"))
      .trim()
      .split("\n"),
  ).toHaveLength(posts);
});

test("task5 qualified actual OCR mode exclusion inclusion and noncitable references", async ({
  page,
  request,
}) => {
  test.skip(
    scenario !== "ocr",
    "Opt in with TYCHORA_E2E_SCENARIO=ocr and installed pinned Docling/OCR assets",
  );
  test.setTimeout(180_000);
  const grant = await api(request, "/v1/handshake");
  const bytes = await readFile(resolve(fixtures, "synthetic-scan.pdf"));
  const receipt = await api(request, "/v1/pdf-intakes", {
    source_key: "synthetic-task5-scan",
    collection_id: grant.default_collection_id,
    kind: "document@1",
    expected_size_bytes: bytes.length,
    metadata: { title: "Synthetic OCR" },
  });
  const path = `/v1/pdf-intakes/${receipt.intake_id}`;
  const upload = await request.put(path + "/content", {
    headers: { ...headers, "Content-Type": "application/pdf" },
    data: bytes,
  });
  expect(upload.ok()).toBeTruthy();
  const analysis = (await api(request, path + "/analyse", {})).analysis;
  expect(analysis.blockers).toEqual([]);
  const accepted = await api(request, path + "/submit", {
    analysis_sha256: analysis.analysis_sha256,
    policy_sha256: analysis.policy_sha256,
  });
  await job(request, accepted.id);
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
  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "actual-ocr-initial-query.json"),
    JSON.stringify({ excluded, included }, null, 2),
  );
  expect(excluded.segments).toHaveLength(0);
  expect(excluded.visuals).toHaveLength(0);
  expect(included.visuals.length).toBeGreaterThan(0);
  const visual = included.visuals.find((v: { proxies: { origin: string }[] }) =>
    v.proxies.some((p) => p.origin === "ocr"),
  );
  const proxy = visual.proxies.find(
    (p: { origin: string }) => p.origin === "ocr",
  );
  const full = await api(request, `/v1/evidence/${proxy.proxy_id}`);
  expect(full.evidence.text_origin).toBe("ocr");
  expect(full.evidence.citable).toBe(false);
  expect(full.evidence.review_state).toBe("needs_review");
  const windows = await api(
    request,
    `/v1/sources/${visual.source_id}/revisions/${visual.revision_id}/extractions/${full.evidence.extraction_id}/windows?unit_id=${proxy.proxy_id}`,
  );
  expect(windows.windows.length).toBeGreaterThan(0);
  expect(windows.scope_limited).toBe(false);
  const emptyScope = { ...grant.scope, collection_ids: [] };
  const denied = await request.get(`/v1/evidence/${proxy.proxy_id}`, {
    headers: { ...headers, "X-Tychora-Scope": JSON.stringify(emptyScope) },
  });
  expect(denied.status()).toBe(403);
  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "actual-ocr-query-and-evidence.json"),
    JSON.stringify(
      {
        excluded,
        included,
        windows,
        inspection: full,
        deniedScopeStatus: denied.status(),
      },
      null,
      2,
    ),
  );
  await open(page, "/dashboard/search");
  await page.getByLabel("Question", { exact: true }).fill("violet");
  await page.getByText("Filters & retrieval options", { exact: true }).click();
  await page
    .getByLabel("Retrieval mode", { exact: true })
    .selectOption("review-excluded");
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  await expect(
    page.getByText("No matching evidence", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/Review-required content: excluded/),
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
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Copy provenance reference",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Evidence reader" }),
  ).toContainText("needs_review · not citable");
  await capture(page, "actual-ocr-included");
  await page
    .getByRole("button", { name: "Stored chunks", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Stored proxy chunks" }),
  ).toContainText(windows.windows[0].text);
  await capture(page, "actual-ocr-stored-chunks");
});
