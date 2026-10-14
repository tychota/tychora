import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import handshakeFixture from "../../contracts/task2-handshake.json";
import collectionFixture from "../../contracts/task2-collections.json";
import collection from "../../contracts/task2-collection.json";
import archived from "../../contracts/task2-collection-archived.json";
import consent from "../../contracts/task2-collection-consent.json";
import documentFixture from "../../contracts/task2-documents.json";
import units from "../../contracts/extraction-units.json";
import windows from "../../contracts/extraction-windows.json";
import documentSchema from "../../contracts/task4-document-metadata-schema.json";
import manualSchema from "../../contracts/task4-manual-metadata-schema.json";
import models from "../../contracts/task3-models.json";
import readiness from "../../contracts/task3-model-readiness.json";
import evidence from "../../contracts/evidence.json";
import receipt from "../../contracts/source-add.json";
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
