import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";

const artifacts = resolve(
  import.meta.dirname,
  "../../.superpowers/verification/dashboard-e2e/request-and-restore",
);
const headers = { Authorization: "Bearer synthetic-dashboard-owner" };
async function connect(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel("Bearer token").fill("synthetic-dashboard-owner");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeVisible();
}
async function capture(page: Page, name: string) {
  await mkdir(artifacts, { recursive: true });
  await page.screenshot({
    path: resolve(artifacts, `${name}.png`),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}
test.beforeEach(async ({ page, context }) => {
  const deny = () => {
    throw new Error(
      "Request and restore journeys require real HTTP, no browser interception",
    );
  };
  page.route = deny;
  page.routeFromHAR = deny;
  context.route = deny;
  context.routeFromHAR = deny;
});

test("Request and restore: real structured request, typed rejection and text-first reader", async ({
  page,
  request,
}) => {
  test.skip(
    (process.env.TYCHORA_E2E_SCENARIO || "local") !== "local",
    "Disposable local HTTP journey",
  );
  const grant = await (await request.get("/v1/handshake", { headers })).json();
  const text =
    "Coordinatecedar: a readable retained passage with exact source ownership. ".repeat(
      8,
    );
  const accepted = await (
    await request.post("/v1/sources", {
      headers,
      data: {
        type: "text",
        source_key: "restore-readable",
        kind: "document@1",
        format: "markdown",
        collection_id: grant.default_collection_id,
        metadata: { title: "Coordinate cedar" },
        text,
      },
    })
  ).json();
  await expect
    .poll(
      async () =>
        (
          await (
            await request.get(`/v1/jobs/${accepted.id}`, { headers })
          ).json()
        ).state,
    )
    .toBe("succeeded");
  await connect(page, "/dashboard/search");
  await page.getByText("Filters & retrieval options", { exact: true }).click();
  await page.getByLabel("Question", { exact: true }).fill("saved simple draft");
  await page.getByLabel("Result limit", { exact: true }).fill("4");
  await page.getByLabel("Structured request", { exact: true }).check();
  await expect(page.getByLabel("Question", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Result limit", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Retrieval mode", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    page.getByText(/Search uses the QueryRequest JSON/),
  ).toBeVisible();
  const body = {
    input: { type: "passage", text: "Coordinatecedar" },
    hints: ["retained passage"],
    context: { purpose: "restore-local-acceptance" },
    modalities: ["text"],
    previous_unit_ids: [],
    exact_identifiers: [],
    include_global: false,
    revision_selector: "current",
    trace: true,
    limit: 1,
  };
  const editor = page.getByLabel("QueryRequest JSON", { exact: true });
  await editor.fill(JSON.stringify(body, null, 2));
  await page.getByLabel("Structured request", { exact: true }).uncheck();
  await expect(page.getByLabel("Question", { exact: true })).toHaveValue(
    "saved simple draft",
  );
  await expect(page.getByLabel("Result limit", { exact: true })).toHaveValue(
    "4",
  );
  await page.getByLabel("Structured request", { exact: true }).check();
  await expect(editor).toHaveValue(JSON.stringify(body, null, 2));
  const query = page.waitForResponse(
    (r) => r.url().endsWith("/v1/queries") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  const response = await query;
  expect(response.request().postDataJSON()).toEqual(body);
  expect(
    JSON.parse(response.request().headers()["x-tychora-scope"]).collection_ids,
  ).toEqual([grant.default_collection_id]);
  expect(response.ok(), await response.text()).toBe(true);
  const result = await response.json();
  expect(result.segments.length).toBeGreaterThan(0);
  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "structured-request-result.json"),
    JSON.stringify(
      { request: body, status: response.status(), result },
      null,
      2,
    ),
  );
  await capture(page, "desktop-structured-request");
  const bad = { ...body, unsupported_field: true };
  await editor.fill(JSON.stringify(bad));
  const rejected = page.waitForResponse(
    (r) => r.url().endsWith("/v1/queries") && r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  const rejection = await rejected;
  expect(rejection.status()).toBe(400);
  const error = await rejection.json();
  expect(error.code).toBe("INVALID_REQUEST");
  await expect(editor).toHaveValue(JSON.stringify(bad));
  await expect(
    page.getByText("INVALID_REQUEST", { exact: true }),
  ).toBeVisible();
  await writeFile(
    resolve(artifacts, "structured-request-error.json"),
    JSON.stringify(
      { request: bad, status: rejection.status(), error },
      null,
      2,
    ),
  );
  await editor.fill(JSON.stringify(body));
  await page
    .getByRole("button", { name: "Search evidence", exact: true })
    .click();
  await page
    .getByRole("region", { name: "Search results" })
    .getByRole("button")
    .first()
    .click();
  await page.getByRole("button", { name: "Full unit", exact: true }).click();
  const reader = page.getByRole("region", { name: "Evidence reader" });
  await expect(reader.locator(".evidence-text")).toHaveText(text.trim());
  const metadata = reader
    .locator("details")
    .filter({ has: page.locator("summary", { hasText: "Source identities" }) });
  await expect(metadata).not.toHaveAttribute("open", "");
  expect(
    await reader.locator(".evidence-text").evaluate((el) => {
      const identities = el
        .closest("section")!
        .querySelector(".evidence-metadata")!;
      return Boolean(
        el.compareDocumentPosition(identities) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }),
  ).toBe(true);
  await capture(page, "desktop-full-unit");
  await page.setViewportSize({ width: 390, height: 844 });
  await capture(page, "narrow-full-unit");
  const summary = reader.getByText("Source identities", { exact: true });
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(metadata).toHaveAttribute("open", "");
  await expect(
    reader.getByText(result.segments[0].source_id, { exact: true }).first(),
  ).toBeVisible();
  await capture(page, "narrow-source-identities");
  await page.keyboard.press("Enter");
  await expect(metadata).not.toHaveAttribute("open", "");
});

test("Request and restore: missing archive remains editable after actual typed restore failure", async ({
  page,
}) => {
  test.skip(
    (process.env.TYCHORA_E2E_SCENARIO || "local") !== "local",
    "Disposable local HTTP journey",
  );
  const fixtureDirectory = process.env.TYCHORA_E2E_FIXTURE_DIR!;
  const paths = JSON.parse(
    await readFile(resolve(fixtureDirectory, "context.json"), "utf8"),
  );
  await connect(page, "/dashboard/settings");
  const archive = page.getByLabel("Restore archive server path", {
    exact: true,
  });
  const destination = page.getByLabel("Clean restore destination server path", {
    exact: true,
  });
  const missing = paths.backup + "-missing-restore";
  const target = paths.restore + "-missing-restore";
  await archive.fill(missing);
  await destination.fill(target);
  await page
    .getByLabel("Confirm the archive and clean separate restore destination")
    .check();
  const failure = page.waitForResponse(
    (r) =>
      r.url().endsWith("/v1/jobs") &&
      r.request().postDataJSON()?.operation === "import",
  );
  await page
    .getByRole("button", { name: "Restore verified backup", exact: true })
    .click();
  const response = await failure;
  expect(response.status()).toBe(404);
  const error = await response.json();
  expect(error.code).toBe("NOT_FOUND");
  await expect(page.getByText("NOT_FOUND", { exact: true })).toBeVisible();
  await expect(archive).toHaveValue(missing);
  await expect(destination).toHaveValue(target);
  await expect(archive).toBeEditable();
  await expect(destination).toBeEditable();
  await mkdir(artifacts, { recursive: true });
  await writeFile(
    resolve(artifacts, "missing-archive-error.json"),
    JSON.stringify(
      {
        status: response.status(),
        error,
        archive: missing,
        destination: target,
      },
      null,
      2,
    ),
  );
  await capture(page, "desktop-restore-error");
  await page.setViewportSize({ width: 390, height: 844 });
  await capture(page, "narrow-restore-error");
  await archive.fill(missing + "-corrected");
  await destination.fill(target + "-corrected");
  await expect(
    page.getByLabel(
      "Confirm the archive and clean separate restore destination",
    ),
  ).not.toBeChecked();
});
