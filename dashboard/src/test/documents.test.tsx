import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import handshakeFixture from "../../contracts/collections-handshake.json";
import collectionFixture from "../../contracts/collections-page.json";
import collection from "../../contracts/collections-detail.json";
import archived from "../../contracts/collections-archived.json";
import consent from "../../contracts/collections-consent.json";
import documentFixture from "../../contracts/collections-documents.json";
import units from "../../contracts/extraction-units.json";
import windows from "../../contracts/extraction-windows.json";
import documentSchema from "../../contracts/metadata-document-schema.json";
import manualSchema from "../../contracts/metadata-manual-schema.json";
import models from "../../contracts/models-list.json";
import readiness from "../../contracts/models-readiness.json";
import evidence from "../../contracts/evidence.json";
import receipt from "../../contracts/source-add.json";
import assetManifest from "../../../src/tychora/resources/model-assets.json";
import type { ModelCatalog, ModelReadiness, Handshake } from "../api/contracts";
import { App } from "../App";
import { ingestionPrerequisites } from "../pages/sources/readiness";
const second = "00000000-0000-4000-8000-000000000099";
const handshake = {
  ...handshakeFixture,
  managed_workspace_owner: true,
  scope: {
    ...handshakeFixture.scope,
    collection_ids: [collection.collection_id, second],
  },
  default_collection_id: collection.collection_id,
};
const document = documentFixture.documents[0];
const collections = {
  ...collectionFixture,
  collections: [
    collection,
    {
      ...collection,
      collection_id: second,
      title: "Working notes",
      slug: "working-notes",
    },
  ],
};
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
        : path.endsWith("/consent")
          ? consent
          : path.startsWith("/v1/collections?")
            ? collections
            : path.startsWith("/v1/collections/")
              ? collection
              : path.endsWith("metadata-schema")
                ? path.includes("manual")
                  ? manualSchema
                  : documentSchema
                : path.startsWith("/v1/discovery")
                  ? { definitions: [], next_cursor: null }
                  : path === "/v1/settings"
                    ? { profile: "local", values: { providers: {} } }
                    : path === "/v1/models"
                      ? models
                      : path.startsWith("/v1/models/readiness")
                        ? readiness
                        : path.includes("/units")
                          ? units
                          : path.includes("/windows")
                            ? windows
                            : path.includes("/extractions")
                              ? { extractions: [], next_cursor: null }
                              : path.startsWith("/v1/evidence/")
                                ? evidence
                                : path.startsWith("/v1/jobs")
                                  ? { jobs: [], next_cursor: null }
                                  : path.startsWith("/v1/queries")
                                    ? { definitions: [], next_cursor: null }
                                    : documentFixture,
    );
  vi.stubGlobal("fetch", (path: string, init: RequestInit = {}) => {
    requests.push({ path, init });
    return respond(path, init);
  });
});
async function open(path = "/dashboard/documents") {
  history.replaceState({}, "", path);
  render(<App />);
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "synthetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
}
it("uses actual unknown observations and opens exact server content/chunks deliberately", async () => {
  await open(`/dashboard/documents/${document.source_id}`);
  await screen.findByRole("heading", { name: document.title });
  expect(screen.getByText("Unknown / Unknown")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Content" }));
  await screen.findByText(units.units[0].preview);
  expect(requests.some((r) => r.path.startsWith("/v1/evidence/"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Full unit" }));
  await screen.findByRole("heading", { name: "Exact evidence" });
  expect(
    requests.some((r) => r.path === `/v1/evidence/${units.units[0].id}`),
  ).toBe(true);
  fireEvent.click(screen.getByRole("tab", { name: "Chunks" }));
  expect(await screen.findByText(windows.windows[0].text)).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Exact evidence" })).toBeNull();
  expect(
    screen.getByText(/Search may use a different extraction/),
  ).toBeInTheDocument();
});
it("keeps bounded empty pages continuable and restarts expired content cursors", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path.includes("/units")
      ? path.includes("cursor=")
        ? json({ code: "INVALID_REQUEST", message: "Cursor expired" }, 400)
        : json({
            ...units,
            units: [],
            scope_limited: true,
            next_cursor: "continue",
          })
      : fallback(path, init);
  await open(`/dashboard/documents/${document.source_id}`);
  fireEvent.click(await screen.findByRole("tab", { name: "Content" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Load more units" }),
  );
  await screen.findByText(/Cursor expired/);
  fireEvent.click(
    screen.getByRole("button", { name: "Restart from first page" }),
  );
  await screen.findByText(
    "No readable items on this page. Continue to the next page.",
  );
  expect(
    requests.filter(
      (r) => r.path.includes("/units") && !r.path.includes("cursor="),
    ).length,
  ).toBe(2);
});
it("requires one explicit destination and sends qualified title-only document metadata even with advanced optional schema", async () => {
  const fallback = respond;
  respond = (p, i) =>
    p === "/v1/sources" ? json(receipt, 202) : fallback(p, i);
  await open("/dashboard/documents/add");
  fireEvent.change(screen.getByLabelText("Content"), {
    target: {
      value: "# Cedar notes\nExact original text 😀 <strong>inert</strong>",
    },
  });
  await screen.findByRole("option", { name: collection.title });
  expect(screen.getByRole("button", { name: "Index text" })).toBeDisabled();
  expect(documentSchema.advanced).toBe(true);
  fireEvent.change(screen.getByLabelText("Destination"), {
    target: { value: collection.collection_id },
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Index text" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Index text" }));
  await screen.findByRole("link", { name: "Follow job" });
  const posted = requests.find((r) => r.path === "/v1/sources")!;
  expect(JSON.parse(posted.init.body as string)).toMatchObject({
    kind: "document@1",
    metadata: { title: "Cedar notes" },
    collection_id: collection.collection_id,
  });
  expect(
    JSON.parse(new Headers(posted.init.headers).get("X-Tychora-Scope")!)
      .collection_ids,
  ).toEqual([collection.collection_id]);
  expect(localStorage.length + sessionStorage.length).toBe(0);
});
it("preserves the text draft through Models and collection setup, clears it on disconnect", async () => {
  await open("/dashboard/documents/add");
  fireEvent.change(screen.getByLabelText("Content"), {
    target: { value: "Private in-memory draft" },
  });
  fireEvent.click(screen.getByRole("link", { name: "Models" }));
  fireEvent.click(screen.getByRole("link", { name: "Documents" }));
  fireEvent.click(screen.getByRole("link", { name: "Add documents" }));
  expect(screen.getByLabelText("Content")).toHaveValue(
    "Private in-memory draft",
  );
  fireEvent.click(screen.getByRole("link", { name: "Collections" }));
  fireEvent.click(screen.getByRole("link", { name: "Documents" }));
  fireEvent.click(screen.getByRole("link", { name: "Add documents" }));
  expect(screen.getByLabelText("Content")).toHaveValue(
    "Private in-memory draft",
  );
  fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "synthetic" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
  expect(screen.getByLabelText("Content")).toHaveValue("");
});
it("renders specialized required schema fields and never manufactures their values", async () => {
  await open("/dashboard/documents/add");
  fireEvent.click(screen.getByText("Advanced source options"));
  fireEvent.change(screen.getByLabelText("Kind"), {
    target: { value: "manual@1" },
  });
  expect(await screen.findByLabelText("Publisher")).toHaveValue("");
  expect(screen.getByLabelText("Edition")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Submit source" })).toBeDisabled();
});
it("knowledge scope changes clear inventory before delayed replacement data can arrive", async () => {
  let release!: (r: Response) => void;
  const fallback = respond;
  respond = (p, i) =>
    p.startsWith("/v1/documents") &&
    new Headers(i.headers).get("X-Tychora-Scope")?.includes(second)
      ? new Promise((r) => {
          release = r;
        })
      : fallback(p, i);
  await open();
  await screen.findByRole("button", { name: new RegExp(document.title) });
  fireEvent.click(screen.getByLabelText("Working notes"));
  expect(
    screen.queryByRole("button", { name: new RegExp(document.title) }),
  ).toBeNull();
  await act(async () => release(json({ ...documentFixture, documents: [] })));
  expect(
    screen.queryByRole("button", { name: new RegExp(document.title) }),
  ).toBeNull();
});
it("archive confirmation preserves data wording and refreshes lifecycle; reactivate uses backend", async () => {
  const fallback = respond;
  let isArchived = false;
  respond = (p, i) =>
    p.endsWith("/archive")
      ? ((isArchived = true), json(archived))
      : p.endsWith("/reactivate")
        ? ((isArchived = false), json(collection))
        : p === `/v1/collections/${collection.collection_id}`
          ? json(isArchived ? archived : collection)
          : fallback(p, i);
  await open(`/dashboard/collections/${collection.collection_id}`);
  fireEvent.click(
    await screen.findByRole("button", { name: "Archive collection" }),
  );
  expect(requests.some((r) => r.path.endsWith("/archive"))).toBe(false);
  expect(
    screen.getByText(/Archiving retains documents and evidence/),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Confirm archive" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "Reactivate collection" }),
  );
  await screen.findByRole("button", { name: "Archive collection" });
  expect(requests.filter((r) => r.path.endsWith("/reactivate"))).toHaveLength(
    1,
  );
});
it("administration keeps full-grant requests while selected query history uses exact scope", async () => {
  await open("/dashboard/jobs");
  expect(
    new Headers(
      requests.find((r) => r.path.startsWith("/v1/jobs"))!.init.headers,
    ).has("X-Tychora-Scope"),
  ).toBe(false);
  fireEvent.click(screen.getAllByRole("link", { name: "Activity" })[0]);
  await waitFor(() =>
    expect(requests.some((r) => r.path.startsWith("/v1/queries"))).toBe(true),
  );
  const queryScope = JSON.parse(
    new Headers(
      requests.find((r) => r.path.startsWith("/v1/queries"))!.init.headers,
    ).get("X-Tychora-Scope")!,
  );
  expect(queryScope.collection_ids).toEqual([collection.collection_id]);
  expect(
    screen.getByText("History for this exact selected query scope"),
  ).toBeVisible();
});
it("reports actual missing assets separately from unchecked credentials", () => {
  const catalog = {
    ...models,
    providers: models.providers.map((p) =>
      p.slot === "text"
        ? {
            ...p,
            configured_provider: "voyage",
            configured_model: "voyage-context-4",
            model_qualification: "qualified" as const,
            upload_allowed_collection_ids: [],
          }
        : p,
    ),
  };
  const result = ingestionPrerequisites(
    catalog as never,
    readiness as never,
    handshake as never,
    collection.collection_id,
    ["output-tokenizer"],
  );
  expect(result.some((p) => p.blocking && p.message.includes("missing"))).toBe(
    true,
  );
  expect(
    result.some((p) => !p.blocking && p.message.includes("credentials")),
  ).toBe(true);
  expect(result.find((p) => p.message.includes("consent"))!.route).toBe(
    `/dashboard/collections/${collection.collection_id}`,
  );
});

it("allows a writer-only simple text path without manager model inventory calls", async () => {
  const fallback = respond;
  respond = (p, i) =>
    p === "/v1/handshake"
      ? json({
          ...handshake,
          scope: { ...handshake.scope, capabilities: ["read", "write"] },
        })
      : p === "/v1/sources"
        ? json(receipt, 202)
        : fallback(p, i);
  await open("/dashboard/documents/add");
  fireEvent.change(screen.getByLabelText("Content"), {
    target: { value: "Writer-only original notes" },
  });
  await screen.findByRole("option", { name: collection.title });
  fireEvent.change(screen.getByLabelText("Destination"), {
    target: { value: collection.collection_id },
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Index text" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Index text" }));
  await screen.findByRole("link", { name: "Follow job" });
  expect(requests.some((r) => r.path.startsWith("/v1/models"))).toBe(false);
});

it("create uses server safe defaults and refreshes the managed grant before selecting its identity", async () => {
  const newId = "11111111-1111-4111-8111-111111111111";
  const created = {
    ...collection,
    collection_id: newId,
    title: "New collection",
    slug: "new-collection",
    document_count: 0,
  };
  const fallback = respond;
  let hasCreated = false;
  respond = (p, i) =>
    p === "/v1/collections" && i.method === "POST"
      ? ((hasCreated = true), json(created, 201))
      : p === "/v1/handshake" && hasCreated
        ? json({
            ...handshake,
            scope: {
              ...handshake.scope,
              collection_ids: [...handshake.scope.collection_ids, newId],
            },
          })
        : p === `/v1/collections/${newId}`
          ? json(created)
          : p.startsWith("/v1/collections?") && hasCreated
            ? json({
                ...collections,
                collections: [...collections.collections, created],
              })
            : fallback(p, i);
  await open("/dashboard/collections");
  fireEvent.click(screen.getByRole("button", { name: "Create collection" }));
  fireEvent.change(screen.getByLabelText("Collection name"), {
    target: { value: "New collection" },
  });
  fireEvent.change(screen.getByLabelText("Slug"), {
    target: { value: "new-collection" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  await screen.findByRole("heading", { name: "New collection" });
  expect(
    JSON.parse(
      requests.find((r) => r.path === "/v1/collections")!.init.body as string,
    ),
  ).toEqual({ title: "New collection", slug: "new-collection" });
  expect(requests.filter((r) => r.path === "/v1/handshake")).toHaveLength(2);
});

it("no selected active collection disables Search and issues no empty-scope knowledge request", async () => {
  const fallback = respond;
  respond = (p, i) =>
    p.startsWith("/v1/collections?")
      ? json({ ...collections, collections: [] })
      : p.startsWith("/v1/collections/")
        ? json(archived)
        : fallback(p, i);
  await open("/dashboard/search");
  expect(
    await screen.findByRole("heading", {
      name: "No active collection selected",
    }),
  ).toBeVisible();
  expect(screen.queryByLabelText("Question")).toBeNull();
  expect(requests.some((r) => r.path.startsWith("/v1/queries"))).toBe(false);
});

it("retained revision links remain authorized inspection when every ordinary collection is archived", async () => {
  const fallback = respond;
  respond = (p, i) =>
    p.startsWith("/v1/collections?")
      ? json({ ...collections, collections: [] })
      : p.startsWith("/v1/collections/")
        ? json(archived)
        : p.includes("/revisions")
          ? p.includes("/extractions")
            ? json({ extractions: [], next_cursor: null })
            : json({
                source_id: document.source_id,
                revisions: [],
                next_cursor: null,
                scope_limited: false,
              })
          : fallback(p, i);
  await open(
    `/dashboard/documents/${document.source_id}/revisions/${document.revision_id}`,
  );
  expect(
    await screen.findByRole("heading", { name: "Selected revision" }),
  ).toBeInTheDocument();
  await screen.findByText("No retained extractions reported.");
  const inspected = requests.find((r) => r.path.includes("/extractions"))!;
  expect(new Headers(inspected.init.headers).has("X-Tychora-Scope")).toBe(
    false,
  );
  expect(
    requests.some(
      (r) =>
        r.path.startsWith("/v1/documents") || r.path.startsWith("/v1/queries"),
    ),
  ).toBe(false);
});

it("saving a collection refreshes its heading and named scope from the server", async () => {
  const fallback = respond;
  let edited = false;
  const renamed = { ...collection, title: "Renamed field notes" };
  respond = (p, i) =>
    p === `/v1/collections/${collection.collection_id}` && i.method === "PATCH"
      ? ((edited = true), json(renamed))
      : p === `/v1/collections/${collection.collection_id}` && edited
        ? json(renamed)
        : p.startsWith("/v1/collections?") && edited
          ? json({
              ...collections,
              collections: [renamed, collections.collections[1]],
            })
          : fallback(p, i);
  await open(`/dashboard/collections/${collection.collection_id}`);
  fireEvent.change(await screen.findByLabelText("Collection name"), {
    target: { value: renamed.title },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save collection" }));
  await screen.findByRole("heading", { name: renamed.title });
  expect(
    screen.getByText(`Collection scope · ${renamed.title}`),
  ).toBeInTheDocument();
});

it("keeps one sync key after an ambiguous failure and retries the accepted intent", async () => {
  const fallback = respond;
  const accepted = { ...receipt, operation: "sync" };
  let attempts = 0;
  respond = (p, i) =>
    p.endsWith("/sync")
      ? ++attempts === 1
        ? Promise.reject(new Error("accepted response lost"))
        : json(accepted, 202)
      : fallback(p, i);
  await open(`/dashboard/documents/${document.source_id}`);
  fireEvent.click(await screen.findByText("Processing and lifecycle"));
  fireEvent.click(screen.getByRole("button", { name: "Sync source" }));
  await screen.findByText("CONNECTION_FAILED");
  fireEvent.click(screen.getByRole("button", { name: "Sync source" }));
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "Follow job" })).toHaveAttribute(
      "href",
      `/dashboard/jobs/${accepted.id}`,
    ),
  );
  const posts = requests.filter((r) => r.path.endsWith("/sync"));
  expect(posts).toHaveLength(2);
  expect(
    new Headers(posts[0].init.headers).get("Idempotency-Key"),
  ).toBeTruthy();
  expect(new Headers(posts[1].init.headers).get("Idempotency-Key")).toBe(
    new Headers(posts[0].init.headers).get("Idempotency-Key"),
  );
});

it("starts a fresh sync intent and follows a fresh job after successful acceptance", async () => {
  const fallback = respond;
  const jobs = new Map<string, typeof receipt>();
  const nextId = "11111111-1111-4111-8111-111111111111";
  respond = (p, i) => {
    if (!p.endsWith("/sync")) return fallback(p, i);
    const key = new Headers(i.headers).get("Idempotency-Key")!;
    if (!jobs.has(key))
      jobs.set(key, {
        ...receipt,
        id: jobs.size ? nextId : receipt.id,
        operation: "sync",
      });
    return json(jobs.get(key), 202);
  };
  await open(`/dashboard/documents/${document.source_id}`);
  fireEvent.click(await screen.findByText("Processing and lifecycle"));
  fireEvent.click(screen.getByRole("button", { name: "Sync source" }));
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "Follow job" })).toHaveAttribute(
      "href",
      `/dashboard/jobs/${receipt.id}`,
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Sync source" }));
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "Follow job" })).toHaveAttribute(
      "href",
      `/dashboard/jobs/${nextId}`,
    ),
  );
  const posts = requests.filter((r) => r.path.endsWith("/sync"));
  expect(posts).toHaveLength(2);
  expect(new Headers(posts[0].init.headers).get("Idempotency-Key")).not.toBe(
    new Headers(posts[1].init.headers).get("Idempotency-Key"),
  );
  expect(jobs.size).toBe(2);
});

it.each(["documents", "sources"])(
  "continues an unresolved /%s source-only link through an empty first inventory page in its detail pane",
  async (route) => {
    const fallback = respond;
    respond = (p, i) =>
      p.startsWith("/v1/documents")
        ? json(
            p.includes("cursor=")
              ? documentFixture
              : {
                  ...documentFixture,
                  documents: [],
                  next_cursor: "continue-documents",
                },
          )
        : fallback(p, i);
    const path = `/dashboard/${route}/${document.source_id}`;
    await open(path);
    const detail = within(
      screen.getByRole("region", { name: "Selected document" }),
    );
    fireEvent.click(
      await detail.findByRole("button", { name: "Load more documents" }),
    );
    await detail.findByRole("heading", { name: document.title });
    expect(window.location.pathname).toBe(path);
    const continued = requests.find((r) =>
      r.path.includes("cursor=continue-documents"),
    )!;
    expect(
      JSON.parse(new Headers(continued.init.headers).get("X-Tychora-Scope")!)
        .collection_ids,
    ).toEqual([collection.collection_id]);
  },
);

it("retries an unresolved document inventory load from the detail pane without changing the link", async () => {
  const fallback = respond;
  let loads = 0;
  respond = (p, i) =>
    p.startsWith("/v1/documents")
      ? ++loads === 1
        ? json(
            {
              code: "WORKSPACE_BUSY",
              message: "Inventory temporarily unavailable",
              retryable: true,
            },
            503,
          )
        : json(documentFixture)
      : fallback(p, i);
  const path = `/dashboard/documents/${document.source_id}`;
  await open(path);
  const detail = within(
    screen.getByRole("region", { name: "Selected document" }),
  );
  await detail.findByRole("alert");
  fireEvent.click(detail.getByRole("button", { name: "Retry" }));
  await detail.findByRole("heading", { name: document.title });
  expect(window.location.pathname).toBe(path);
});

function boundedDocumentReadiness(failure?: "missing" | "corrupt") {
  const assets = assetManifest.assets
    .filter((a) => a.selection === "documents")
    .map((a) => {
      const unchecked = a.id === "docling-table-weights";
      const bad = failure && a.id === "docling-layout-config";
      return {
        asset_id: a.id,
        selection: a.selection,
        sha256: a.sha256,
        size_bytes: a.size_bytes,
        state: bad
          ? failure
          : unchecked
            ? ("not_checked" as const)
            : ("installed" as const),
        checked_bytes: bad || unchecked ? 0 : a.size_bytes,
        verification:
          bad || unchecked ? ("none" as const) : ("current_full_hash" as const),
        reason: unchecked
          ? "needs_full_verification"
          : bad && failure === "corrupt"
            ? "invalid_file"
            : null,
      };
    });
  return {
    ...readiness,
    assets,
    selections: [
      {
        selection: "documents",
        state: "not_checked" as const,
        complete: false,
      },
    ],
    total_size_bytes: assets.reduce((total, a) => total + a.size_bytes, 0),
    checked_bytes: assets.reduce((total, a) => total + a.checked_bytes, 0),
    complete: false,
  };
}

it("keeps incomplete bounded document verification nonblocking without calling assets ready", () => {
  const result = ingestionPrerequisites(
    models as ModelCatalog,
    boundedDocumentReadiness() as ModelReadiness,
    handshake as Handshake,
    collection.collection_id,
    ["documents"],
  );
  expect(result.some((p) => p.blocking)).toBe(false);
  expect(
    result.some(
      (p) =>
        p.message.includes("not checked") &&
        p.message.includes("server will fully verify"),
    ),
  ).toBe(true);
});

it.each(["missing", "corrupt"] as const)(
  "preserves known %s asset evidence masked by an incomplete aggregate",
  (state) => {
    const result = ingestionPrerequisites(
      models as ModelCatalog,
      boundedDocumentReadiness(state) as ModelReadiness,
      handshake as Handshake,
      collection.collection_id,
      ["documents"],
    );
    expect(
      result.some(
        (p) =>
          p.blocking &&
          p.message.includes("docling-layout-config") &&
          p.message.includes(state),
      ),
    ).toBe(true);
  },
);

it.each([undefined, "missing", "corrupt"] as const)(
  "document file intake with bounded asset failure=%s reaches the server only without known failure",
  async (state) => {
    const fallback = respond;
    respond = (p, i) =>
      p.startsWith("/v1/models/readiness")
        ? json(boundedDocumentReadiness(state))
        : p === "/v1/sources" && i.method === "POST"
          ? json(
              {
                code: "CAPABILITY_UNAVAILABLE",
                message: "Server asset verification rejected this input",
                retryable: false,
              },
              503,
            )
          : fallback(p, i);
    await open("/dashboard/documents/add");
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Synthetic document asset review" },
    });
    await screen.findByRole("option", { name: collection.title });
    fireEvent.change(screen.getByLabelText("Destination"), {
      target: { value: collection.collection_id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Files" }));
    const file = new File(["synthetic bytes"], "review.docx");
    Object.defineProperty(file, "arrayBuffer", {
      value: async () => new TextEncoder().encode("synthetic bytes").buffer,
    });
    fireEvent.change(screen.getByLabelText("Source file"), {
      target: { files: [file] },
    });
    await screen.findByText(/server will fully verify/);
    await waitFor(() =>
      expect(screen.queryByText("Reading selected file…")).toBeNull(),
    );
    fireEvent.click(
      screen.getByLabelText(
        "I confirm this source and processing destination.",
      ),
    );
    const submit = screen.getByRole("button", { name: "Submit source" });
    if (state) {
      expect(submit).toBeDisabled();
      fireEvent.submit(submit.closest("form")!);
      expect(
        requests.some(
          (r) => r.path === "/v1/sources" && r.init.method === "POST",
        ),
      ).toBe(false);
    } else {
      await waitFor(() => expect(submit).toBeEnabled());
      fireEvent.click(submit);
      await screen.findByText(/Server asset verification rejected this input/);
      const posted = requests.find(
        (r) => r.path === "/v1/sources" && r.init.method === "POST",
      )!;
      expect(JSON.parse(posted.init.body as string)).toMatchObject({
        type: "bytes",
        format: "docx",
        kind: "document@1",
        collection_id: collection.collection_id,
      });
      expect(screen.queryByText(/Indexing accepted/)).toBeNull();
    }
  },
);

it("keeps missing document packages blocking when bounded assets remain unknown", () => {
  const catalog = {
    ...models,
    selection_capabilities: models.selection_capabilities.map((c) =>
      c.selection === "documents" ? { ...c, packages_available: false } : c,
    ),
  };
  const result = ingestionPrerequisites(
    catalog as ModelCatalog,
    boundedDocumentReadiness() as ModelReadiness,
    handshake as Handshake,
    collection.collection_id,
    ["documents"],
  );
  expect(
    result.some((p) => p.blocking && p.message.includes("Required packages")),
  ).toBe(true);
});
it("keeps a selected PDF draft through Models, and text stays submit-ready after PDF upload failure", async () => {
  const fallback = respond;
  respond = (path, init) =>
    path === "/v1/handshake"
      ? json({
          ...handshake,
          scope: { ...handshake.scope, capabilities: ["read", "write"] },
        })
      : path === "/v1/pdf-intakes"
        ? json({
            intake_id: "a".repeat(64),
            state: "prepared",
            byte_ceiling: 2000000,
            received_bytes: 0,
            expires_at: new Date(Date.now() + 1800000).toISOString(),
            analysis: null,
            job: null,
            error: null,
          })
        : path.endsWith("/content")
          ? json(
              { code: "LIMIT_EXCEEDED", message: "Synthetic upload limit" },
              413,
            )
          : path === "/v1/sources"
            ? json(receipt, 202)
            : fallback(path, init);
  await open("/dashboard/documents/add");
  fireEvent.change(screen.getByLabelText("Content"), {
    target: { value: "Retained plain text" },
  });
  fireEvent.change(screen.getByLabelText("Destination"), {
    target: { value: collection.collection_id },
  });
  fireEvent.click(screen.getByRole("button", { name: "Files" }));
  const file = new File(["synthetic"], "synthetic.pdf");
  Object.defineProperty(file, "size", { value: 2000000 });
  Object.defineProperty(file, "arrayBuffer", {
    value: vi.fn(() => {
      throw new Error("forbidden whole PDF read");
    }),
  });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("link", { name: "Models" }));
  fireEvent.click(screen.getByRole("link", { name: "Documents" }));
  fireEvent.click(screen.getByRole("link", { name: "Add documents" }));
  expect(screen.getByText(/Selected file: synthetic.pdf/)).toBeVisible();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Analyse PDF" }));
  await screen.findByText(/Synthetic upload limit/);
  expect(screen.getByText(/Upload failed/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Pasted text" }));
  expect(screen.getByLabelText("Content")).toHaveValue("Retained plain text");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Index text" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Index text" }));
  await screen.findByRole("link", { name: "Follow job" });
  expect(file.arrayBuffer).not.toHaveBeenCalled();
  expect(localStorage.length + sessionStorage.length).toBe(0);
});
it("keeps pending PDF recovery available while independently submitting preserved text", async () => {
  const pdf = (await import("../../contracts/pdf-mixed-status.json")).default;
  const fallback = respond;
  let submits = 0;
  respond = (path, init) =>
    path === "/v1/handshake"
      ? json({
          ...handshake,
          scope: { ...handshake.scope, capabilities: ["read", "write"] },
        })
      : path === "/v1/pdf-intakes"
        ? json({ ...pdf, state: "prepared", analysis: null })
        : path.endsWith("/content")
          ? json({ ...pdf, state: "staged", analysis: null })
          : path.endsWith("/analyse")
            ? json(pdf)
            : path.endsWith("/submit")
              ? ++submits === 1
                ? Promise.reject(new Error("lost response"))
                : json(
                    { id: "recovered-pdf", operation: "add", state: "queued" },
                    202,
                  )
              : path.startsWith("/v1/pdf-intakes/")
                ? Promise.reject(new Error("unavailable"))
                : path === "/v1/sources"
                  ? json(receipt, 202)
                  : fallback(path, init);
  await open("/dashboard/documents/add");
  fireEvent.change(screen.getByLabelText("Content"), {
    target: { value: "Independent preserved text" },
  });
  fireEvent.change(screen.getByLabelText("Destination"), {
    target: { value: collection.collection_id },
  });
  fireEvent.click(screen.getByRole("button", { name: "Files" }));
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [new File(["synthetic"], "synthetic.pdf")] },
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Analyse PDF" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Analyse PDF" }));
  await screen.findByText(/3 physical pages/);
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Ingest PDF" }));
  await screen.findByText(/Submission was not confirmed/);
  fireEvent.click(screen.getByRole("button", { name: "Pasted text" }));
  expect(
    screen.getByRole("button", { name: "Check submission" }),
  ).toBeVisible();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Index text" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Index text" }));
  await screen.findByRole("link", { name: "Follow job" });
  fireEvent.click(
    screen.getByRole("checkbox", { name: /confirm this exact analysis/ }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Retry confirmed submission" }),
  );
  await waitFor(() =>
    expect(requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(2),
  );
  await waitFor(() =>
    expect(screen.getByRole("link", { name: "Follow job" })).toHaveAttribute(
      "href",
      "/dashboard/jobs/recovered-pdf",
    ),
  );
});
it("never base64 reads an identified PDF under a conflicting format override", async () => {
  await open("/dashboard/documents/add");
  fireEvent.click(screen.getByText("Advanced source options"));
  fireEvent.click(screen.getByRole("button", { name: "Files" }));
  fireEvent.change(screen.getByLabelText("Format"), {
    target: { value: "docx" },
  });
  const file = new File(["synthetic"], "scan.pdf", { type: "application/pdf" });
  const read = vi.fn(async () => new ArrayBuffer(1));
  Object.defineProperty(file, "arrayBuffer", { value: read });
  fireEvent.change(screen.getByLabelText("Source file"), {
    target: { files: [file] },
  });
  await screen.findByText(/PDF files require PDF format/);
  expect(read).not.toHaveBeenCalled();
});
