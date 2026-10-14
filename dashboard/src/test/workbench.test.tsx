import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import handshake from "../../contracts/handshake.json";
import query from "../../contracts/query.json";
import evidence from "../../contracts/evidence.json";
import original from "../../contracts/original.json";
import kinds from "../../contracts/discovery-kinds.json";
import { ApiClient, ApiError } from "../api/client";
import type { Citation } from "../api/contracts";
import {
  citationText,
  decodeBase64url,
  OriginalInspection,
  EvidenceReader,
} from "../components/EvidenceReader";
import { Relations } from "../components/Relations";
import { App, useRoute } from "../App";

const collection = handshake.default_collection_id;
const second = "00000000-0000-4000-8000-000000000002";
let requests: { path: string; init: RequestInit }[];
let queryResponse: unknown;
let failConnect: boolean;
beforeEach(() => {
  requests = [];
  queryResponse = query;
  failConnect = false;
  history.replaceState({}, "", "/dashboard/search");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      requests.push({ path, init });
      const value =
        path === "/v1/handshake"
          ? {
              ...handshake,
              scope: {
                ...handshake.scope,
                collection_ids: [collection, second],
              },
            }
          : path.startsWith("/v1/collections?")
            ? {
                collections: [
                  {
                    collection_id: collection,
                    title: "Default collection",
                    archived_at: null,
                  },
                  {
                    collection_id: second,
                    title: "Second collection",
                    archived_at: null,
                  },
                ],
                next_cursor: null,
              }
            : path.startsWith("/v1/collections/")
              ? { collection_id: path.split("/").at(-1), archived_at: null }
              : path.startsWith("/v1/discovery")
                ? { ...kinds, next_cursor: null }
                : path === "/v1/settings"
                  ? {
                      profile: "local",
                      values: { providers: { text: { provider: "none" } } },
                    }
                  : path === "/v1/queries"
                    ? queryResponse
                    : path.startsWith("/v1/evidence/")
                      ? evidence
                      : path === "/v1/inspect"
                        ? original
                        : { paths: [], truncated: false, next_cursor: null };
      return new Response(
        JSON.stringify(
          failConnect && path === "/v1/handshake"
            ? {
                code: "FORBIDDEN_SCOPE",
                message: "Connection denied.",
                retryable: false,
              }
            : value,
        ),
        {
          status: failConnect && path === "/v1/handshake" ? 403 : 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }),
  );
});
async function connect() {
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "test-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
}
async function search() {
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "verification" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search evidence" }));
}
describe("authorization boundary", () => {
  it("rejects attempts to expand a grant", () => {
    expect(() =>
      new ApiClient("test-token", handshake.scope).narrow([second]),
    ).toThrow("granted collection");
  });
  it("keeps cancellation active while a response body is being consumed", async () => {
    let signal: AbortSignal | null | undefined;
    let complete!: (value: unknown) => void;
    vi.stubGlobal("fetch", async (_path: string, init: RequestInit) => {
      signal = init.signal;
      return {
        ok: true,
        json: () =>
          new Promise((resolve) => {
            complete = resolve;
          }),
      };
    });
    const client = new ApiClient("test-token", handshake.scope);
    const request = client.request("/v1/queries");
    await waitFor(() => expect(complete).toBeDefined());
    client.dispose();
    expect(signal?.aborted).toBe(true);
    complete(query);
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
  });
  it("sends full narrowed scope, bearer, no-store and no credentials in browser storage", async () => {
    const client = new ApiClient("test-token", {
      ...handshake.scope,
      collection_ids: [collection],
    });
    await client.request("/v1/settings");
    const headers = new Headers(requests[0].init.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-token");
    expect(JSON.parse(headers.get("X-Tychora-Scope")!)).toEqual({
      ...handshake.scope,
      collection_ids: [collection],
    });
    expect(requests[0].init.cache).toBe("no-store");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
  it("rejects off-origin and non-API credential destinations", async () => {
    const client = new ApiClient("test-token", handshake.scope);
    await expect(
      client.request("https://example.org/v1/settings"),
    ).rejects.toThrow();
    expect(requests).toHaveLength(0);
  });
  it("disconnect aborts outstanding requests", async () => {
    let signal: AbortSignal | null | undefined;
    vi.stubGlobal("fetch", (_path: string, init: RequestInit) => {
      signal = init.signal;
      return new Promise(() => {});
    });
    const client = new ApiClient("test-token", handshake.scope);
    void client.request("/v1/queries").catch(() => {});
    client.dispose();
    expect(signal?.aborted).toBe(true);
  });
  it("keeps typed API errors", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({
            code: "INDEX_INCOMPLETE",
            message: "Lexical index unavailable.",
            retryable: true,
          }),
          { status: 503 },
        ),
    );
    await expect(
      new ApiClient("test-token").request("/v1/queries"),
    ).rejects.toMatchObject({ code: "INDEX_INCOMPLETE", retryable: true });
  });
});
describe("connected workbench", () => {
  it("synchronizes child navigation with the shell and its keyed route subtree", async () => {
    function OtherPage() {
      const { path, navigate } = useRoute();
      return (
        <>
          <p>{path}</p>
          <button onClick={() => navigate("/dashboard/jobs/persisted-job")}>
            Open persisted job
          </button>
        </>
      );
    }
    render(<App renderRoute={() => <OtherPage />} />);
    await connect();
    fireEvent.click(screen.getByRole("link", { name: "Documents" }));
    fireEvent.click(screen.getByRole("button", { name: "Open persisted job" }));
    expect(screen.getByRole("link", { name: "Jobs" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      screen.getByText("/dashboard/jobs/persisted-job"),
    ).toBeInTheDocument();
  });
  it("keeps returned relation paths synchronized while inspecting a target unit", async () => {
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", (url: string, init: RequestInit) =>
      url === "/v1/relations"
        ? Promise.resolve(
            new Response(
              JSON.stringify({
                paths: [
                  {
                    unit_ids: [evidence.id, "target-unit"],
                    assertion_ids: ["assertion-1"],
                    predicates: ["supports@1"],
                    support_unit_ids: [evidence.id],
                    provenance: {},
                  },
                ],
                truncated: false,
                next_cursor: null,
              }),
            ),
          )
        : fetch(url, init),
    );
    render(<App />);
    await connect();
    await search();
    fireEvent.click(
      await screen.findByRole("button", { name: /Record synthetic/ }),
    );
    expect(await screen.findByText("Matched evidence")).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "Explore relations" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Inspect target-unit" }),
    );
    expect(
      screen.getByRole("button", { name: "Select graph unit target-unit" }),
    ).toHaveClass("selected");
    expect(screen.getByText("Related evidence")).toBeInTheDocument();
    expect(screen.queryByText("Matched evidence")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(requests.some((r) => r.path === "/v1/evidence/target-unit")).toBe(
        true,
      ),
    );
  });
  it("constructs facet filters using registered names, operators and value types", async () => {
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
      path.includes("subject=facets")
        ? Promise.resolve(
            new Response(
              JSON.stringify({
                subject: "facets",
                definitions: [
                  {
                    id: "measure.count",
                    version: "1",
                    label: "Measurement count",
                    schema_digest: "digest",
                    definition: {
                      key: "measure.count",
                      source_binding: "metadata.count",
                      value_type: "integer",
                      operators: ["eq", "in"],
                    },
                  },
                ],
                counts: [],
                capabilities: [],
                capability_status: [],
                next_cursor: null,
              }),
            ),
          )
        : fetch(path, init),
    );
    render(<App />);
    await connect();
    await screen.findByRole("option", {
      name: "Measurement count",
      hidden: true,
    });
    fireEvent.change(screen.getByLabelText("Metadata facet"), {
      target: { value: "measure.count" },
    });
    fireEvent.change(screen.getByLabelText("Exact facet value"), {
      target: { value: "3" },
    });
    await search();
    await screen.findByRole("button", { name: /Record synthetic/ });
    expect(
      JSON.parse(
        requests.find((r) => r.path === "/v1/queries")!.init.body as string,
      ).filters,
    ).toEqual({ op: "eq", field: "measure.count", value: 3 });
  });
  it("isolates an unresolved query when collection scope changes", async () => {
    render(<App />);
    await connect();
    const fetch = globalThis.fetch;
    let resolve!: (value: Response) => void;
    vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
      path === "/v1/queries"
        ? new Promise<Response>((r) => {
            resolve = r;
          })
        : fetch(path, init),
    );
    await search();
    fireEvent.click(await screen.findByLabelText("Second collection"));
    await act(async () => resolve(new Response(JSON.stringify(query))));
    expect(
      screen.queryByRole("button", { name: /Record synthetic/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveValue("");
  });
  it("does not offer retry for a nonretryable query error", async () => {
    render(<App />);
    await connect();
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
      path === "/v1/queries"
        ? Promise.resolve(
            new Response(
              JSON.stringify({
                code: "INVALID_REQUEST",
                message: "Invalid filter.",
                retryable: false,
              }),
              { status: 400 },
            ),
          )
        : fetch(path, init),
    );
    await search();
    await screen.findByRole("alert");
    expect(
      screen.queryByRole("button", { name: "Retry query" }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveValue("verification");
  });
  it("reports denied connection without revealing the token", async () => {
    failConnect = true;
    render(<App />);
    fireEvent.change(screen.getByLabelText("Bearer token"), {
      target: { value: "test-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Connection denied.",
    );
    expect(screen.queryByText(handshake.workspace_id)).not.toBeInTheDocument();
  });
  it("connects, clears token form, shows named granted collections and disconnects", async () => {
    render(<App />);
    await connect();
    expect(screen.queryByLabelText("Bearer token")).not.toBeInTheDocument();
    expect(
      await screen.findByLabelText("Second collection"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(screen.getByLabelText("Bearer token")).toHaveValue("");
    expect(screen.queryByText(handshake.workspace_id)).not.toBeInTheDocument();
  });
  it("clears prior results and scopes the next query when collections change", async () => {
    render(<App />);
    await connect();
    await search();
    await screen.findByRole("button", { name: /Record synthetic/ });
    fireEvent.click(await screen.findByLabelText("Second collection"));
    expect(
      screen.queryByRole("button", { name: /Record synthetic/ }),
    ).not.toBeInTheDocument();
    await search();
    await screen.findByRole("button", { name: /Record synthetic/ });
    const request = requests.filter((r) => r.path === "/v1/queries").at(-1)!;
    expect(
      JSON.parse(new Headers(request.init.headers).get("X-Tychora-Scope")!)
        .collection_ids,
    ).toEqual([collection, second]);
  });
  it("ignores an unresolved query after disconnect", async () => {
    render(<App />);
    await connect();
    let resolve!: (value: Response) => void;
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
      path === "/v1/queries"
        ? new Promise<Response>((r) => {
            resolve = r;
          })
        : fetch(path, init),
    );
    await search();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await act(async () => resolve(new Response(JSON.stringify(query))));
    expect(screen.queryByText(query.run_id)).not.toBeInTheDocument();
  });
  it("preserves question after typed errors and offers retry only when permitted", async () => {
    render(<App />);
    await connect();
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", (path: string, init: RequestInit) =>
      path === "/v1/queries"
        ? Promise.resolve(
            new Response(
              JSON.stringify({
                code: "INDEX_INCOMPLETE",
                message: "Index incomplete.",
                retryable: true,
              }),
              { status: 503 },
            ),
          )
        : fetch(path, init),
    );
    await search();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "INDEX_INCOMPLETE",
    );
    expect(screen.getByLabelText("Question")).toHaveValue("verification");
    expect(
      screen.getByRole("button", { name: "Retry query" }),
    ).toBeInTheDocument();
  });
  it("distinguishes no matches from incomplete coverage and unavailable trace", async () => {
    queryResponse = {
      ...query,
      segments: [],
      coverage: {
        status: "partial",
        completed_channels: ["exact"],
        unavailable_channels: ["lexical"],
        warnings: [],
      },
    };
    render(<App />);
    await connect();
    await search();
    expect(await screen.findByText("No matching evidence")).toBeInTheDocument();
    expect(screen.getByText("Coverage: partial")).toBeInTheDocument();
    expect(
      screen.getByText(/Trace details are unavailable/),
    ).toBeInTheDocument();
  });
  it("uses exact evidence identity and displays imported markup as inert text", async () => {
    queryResponse = {
      ...query,
      segments: [
        { ...query.segments[0], text: "<img src=x onerror=alert(1)>" },
      ],
    };
    render(<App />);
    await connect();
    await search();
    const row = await screen.findByRole("button", { name: /<img src=x/ });
    fireEvent.click(row);
    await waitFor(() =>
      expect(
        requests.some((r) => r.path === `/v1/evidence/${evidence.id}`),
      ).toBe(true),
    );
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(await screen.findByText(/Original source text/)).toBeInTheDocument();
  });
});
describe("exact provenance", () => {
  it("labels returned context-only evidence without calling it a query match", async () => {
    queryResponse = {
      ...query,
      segments: [
        { ...query.segments[0], context_only_unit_ids: ["context-unit"] },
      ],
    };
    render(<App />);
    await connect();
    await search();
    fireEvent.click(
      await screen.findByRole("button", { name: /Record synthetic/ }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Context · context-unit" }),
    );
    expect(screen.getByText("Context only")).toBeInTheDocument();
    expect(screen.queryByText("Matched evidence")).not.toBeInTheDocument();
  });
  it("defaults direct evidence inspection to a neutral provenance label", async () => {
    render(
      <EvidenceReader
        client={new ApiClient("test-token", handshake.scope)}
        unitId={evidence.id}
      />,
    );
    expect(await screen.findByText(/Original source text/)).toBeInTheDocument();
    expect(screen.getByText("Direct inspection")).toBeInTheDocument();
    expect(screen.queryByText("Matched evidence")).not.toBeInTheDocument();
    expect(screen.queryByText("Context only")).not.toBeInTheDocument();
  });
  it("offers SVG as download without rendering imported markup", async () => {
    const svg = '<svg onload="alert(1)"></svg>';
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({
            ...original,
            payload: btoa(svg),
            media_type: "image/svg+xml",
          }),
        ),
    );
    render(
      <OriginalInspection
        client={new ApiClient("test-token", handshake.scope)}
        revisionId={original.id}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect original" }));
    await screen.findByRole("button", { name: "Download original bytes" });
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(document.querySelector("svg")).toBeNull();
  });
  it("revokes retained original object URLs when the reader unmounts", async () => {
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const create = vi.spyOn(URL, "createObjectURL");
    const { unmount } = render(
      <OriginalInspection
        client={new ApiClient("test-token", handshake.scope)}
        revisionId={original.id}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect original" }));
    await screen.findByRole("button", { name: "Download original bytes" });
    const url = create.mock.results[0].value;
    unmount();
    expect(revoke).toHaveBeenCalledWith(url);
  });
  it("shows a missing retained payload as unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      async () => new Response(JSON.stringify({ ...original, payload: null })),
    );
    render(
      <OriginalInspection
        client={new ApiClient("test-token", handshake.scope)}
        revisionId={original.id}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect original" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Retained original is unavailable.",
    );
  });
  it("loads the retained revision original and renders HTML as inert text", async () => {
    const markup = "<script>window.bad=true</script>";
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({
            ...original,
            payload: btoa(markup),
            media_type: "text/html",
          }),
        ),
    );
    render(
      <OriginalInspection
        client={new ApiClient("test-token", handshake.scope)}
        revisionId={original.id}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect original" }));
    expect(await screen.findByText(markup)).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });
  it("shows removed originals explicitly", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          JSON.stringify({
            code: "ORIGINAL_REMOVED",
            message: "Retained original was removed.",
            retryable: false,
          }),
          { status: 410 },
        ),
    );
    render(
      <OriginalInspection
        client={new ApiClient("test-token", handshake.scope)}
        revisionId={original.id}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Inspect original" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "ORIGINAL_REMOVED",
    );
  });
  it("sends bounded relation requests with exact unit IDs and exposes returned paths", async () => {
    const path = {
      unit_ids: [evidence.id, "target-unit"],
      assertion_ids: ["assertion-1"],
      predicates: ["supports@1"],
      support_unit_ids: [evidence.id],
      provenance: { method: "native" },
    };
    const fetch = globalThis.fetch;
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) =>
      url === "/v1/relations"
        ? (requests.push({ path: url, init }),
          new Response(
            JSON.stringify({
              paths: [path],
              truncated: true,
              next_cursor: "next-page",
            }),
          ))
        : fetch(url, init),
    );
    render(
      <Relations
        client={new ApiClient("test-token", handshake.scope)}
        unitIds={[evidence.id]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Explore relations" }));
    expect(
      await screen.findByRole("button", { name: "Inspect target-unit" }),
    ).toBeInTheDocument();
    expect(JSON.parse(requests[0].init.body as string)).toMatchObject({
      unit_ids: [evidence.id],
      max_hops: 1,
      max_edges: 30,
      direction: "outgoing",
    });
    expect(screen.getByText(/Relation view is truncated/)).toBeInTheDocument();
  });
  it("copies the complete identity and exact locator rather than a snippet citation", () => {
    const text = citationText(query.citations[0] as Citation);
    for (const id of [
      query.citations[0].source_id,
      query.citations[0].revision_id,
      evidence.id,
      query.citations[0].locators[0].artifact_ref,
    ])
      expect(text).toContain(id);
    expect(text).toContain("char_start");
    expect(text).toContain("65");
  });
  it("decodes URL-safe unpadded original bytes without UTF-8 corruption", () => {
    expect([...decodeBase64url("-_8A")]).toEqual([251, 255, 0]);
    expect(new TextDecoder().decode(decodeBase64url(original.payload))).toBe(
      evidence.evidence.text,
    );
  });
});
