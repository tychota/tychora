import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { SessionProvider, useSession } from "../session";
import handshake from "../../contracts/handshake.json";

it("refreshes the credential grant without a stale scope and disposes selected requests", async () => {
  const added = "11111111-1111-4111-8111-111111111111";
  let created = false;
  const headers: Headers[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      if (path === "/v1/handshake") headers.push(new Headers(init.headers));
      const value =
        path === "/v1/handshake"
          ? {
              ...handshake,
              managed_workspace_owner: true,
              policy_epoch: created ? 1 : 0,
              scope: {
                ...handshake.scope,
                collection_ids: [
                  ...handshake.scope.collection_ids,
                  ...(created ? [added] : []),
                ],
              },
            }
          : path === "/v1/collections" && init.method === "POST"
            ? ((created = true), { collection_id: added })
            : path.startsWith("/v1/collections/")
              ? { collection_id: path.split("/").at(-1), archived_at: null }
              : {
                  collections: [
                    {
                      collection_id: created
                        ? added
                        : handshake.default_collection_id,
                      archived_at: null,
                    },
                  ],
                  next_cursor: null,
                  policy_epoch: 1,
                };
      return new Response(JSON.stringify(value), { status: 200 });
    }),
  );
  const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
  await act(async () => {
    await hook.result.current.connect("synthetic");
  });
  const old = hook.result.current.client!;
  await act(async () => {
    await hook.result.current.createCollection({
      title: "Library",
      slug: "library",
    });
  });
  expect(headers).toHaveLength(2);
  expect(headers.every((h) => !h.has("X-Tychora-Scope"))).toBe(true);
  expect(hook.result.current.selectedCollections).toEqual([added]);
  expect(hook.result.current.handshake!.scope.collection_ids).toContain(added);
  await expect(old.request("/v1/handshake")).rejects.toThrow("Session ended");
  hook.unmount();
});

it("static refresh cannot gain ungranted collections", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path === "/v1/handshake"
              ? {
                  ...handshake,
                  managed_workspace_owner: false,
                  policy_epoch: 0,
                }
              : {
                  collections: [
                    {
                      collection_id: handshake.default_collection_id,
                      archived_at: null,
                    },
                  ],
                  next_cursor: null,
                  policy_epoch: 0,
                },
          ),
        ),
    ),
  );
  const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
  await act(async () => {
    await hook.result.current.connect("static");
  });
  await act(async () => {
    await hook.result.current.refreshGrant("ungranted");
  });
  expect(hook.result.current.handshake!.scope.collection_ids).toEqual(
    handshake.scope.collection_ids,
  );
  expect(hook.result.current.selectedCollections).not.toContain("ungranted");
});

it("refresh drops an archived selection even outside the first collection page", async () => {
  const fallback = "11111111-1111-4111-8111-111111111111";
  let archived = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path === "/v1/handshake"
              ? {
                  ...handshake,
                  scope: {
                    ...handshake.scope,
                    collection_ids: [
                      ...handshake.scope.collection_ids,
                      fallback,
                    ],
                  },
                }
              : path.startsWith("/v1/collections?")
                ? {
                    collections: [
                      { collection_id: fallback, archived_at: null },
                    ],
                    next_cursor: null,
                    policy_epoch: 1,
                  }
                : {
                    collection_id: handshake.default_collection_id,
                    archived_at: archived ? "2026-01-01T00:00:00Z" : null,
                  },
          ),
        ),
    ),
  );
  const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
  await act(async () => {
    await hook.result.current.connect("managed");
  });
  await act(async () => {
    archived = true;
    await hook.result.current.refreshGrant();
  });
  expect(hook.result.current.selectedCollections).toEqual([fallback]);
});

it.each([true, false])(
  "connect selects an active granted collection with archived default=%s",
  async (archived) => {
    const fallback = "11111111-1111-4111-8111-111111111111";
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (path: string) =>
          new Response(
            JSON.stringify(
              path === "/v1/handshake"
                ? {
                    ...handshake,
                    scope: {
                      ...handshake.scope,
                      collection_ids: [
                        ...handshake.scope.collection_ids,
                        fallback,
                      ],
                    },
                  }
                : path.startsWith("/v1/collections?")
                  ? {
                      collections: [
                        { collection_id: fallback, archived_at: null },
                      ],
                      next_cursor: null,
                    }
                  : {
                      collection_id: handshake.default_collection_id,
                      archived_at: archived ? "2026-01-01T00:00:00Z" : null,
                    },
            ),
          ),
      ),
    );
    const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
    await act(async () => {
      await hook.result.current.connect("managed");
    });
    expect(hook.result.current.selectedCollections).toEqual([
      archived ? fallback : handshake.default_collection_id,
    ]);
  },
);

it("connect keeps administration available when all granted collections are archived", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path === "/v1/handshake"
              ? handshake
              : path.startsWith("/v1/collections?")
                ? { collections: [], next_cursor: null }
                : {
                    collection_id: handshake.default_collection_id,
                    archived_at: "2026-01-01T00:00:00Z",
                  },
          ),
        ),
    ),
  );
  const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
  await act(async () => {
    await hook.result.current.connect("managed");
  });
  expect(hook.result.current.selectedCollections).toEqual([]);
  expect(hook.result.current.adminClient).not.toBeNull();
  expect(hook.result.current.handshake).toEqual(handshake);
});

it("disconnect cancels initial lifecycle reads without restoring the session", async () => {
  let release!: () => void;
  let started!: () => void;
  const reading = new Promise<void>((resolve) => {
    started = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      if (path === "/v1/handshake")
        return new Response(JSON.stringify(handshake));
      started();
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return new Response(
        JSON.stringify({ collections: [], next_cursor: null }),
      );
    }),
  );
  const hook = renderHook(() => useSession(), { wrapper: SessionProvider });
  let connecting!: Promise<void>;
  await act(async () => {
    connecting = hook.result.current.connect("managed");
    // Bound the regression before lifecycle reads exist in connect().
    await Promise.race([reading, connecting]);
  });
  expect(release).toBeDefined();
  act(() => {
    hook.result.current.disconnect();
  });
  await act(async () => {
    release();
    await expect(connecting).rejects.toMatchObject({ name: "AbortError" });
  });
  expect(hook.result.current.client).toBeNull();
  expect(hook.result.current.selectedCollections).toEqual([]);
});
