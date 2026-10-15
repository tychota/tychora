import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import handshake from "../../contracts/handshake.json";
import { App } from "../App";
import { REMEMBERED_TOKEN_KEY } from "../credentials";

const CODE = "A".repeat(43);
let requests: { path: string; init: RequestInit; href: string }[];
let rejectToken: string | null;
let launchStatus: number;

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
function bearer(init: RequestInit) {
  return new Headers(init.headers).get("Authorization");
}

beforeEach(() => {
  requests = [];
  rejectToken = null;
  launchStatus = 200;
  localStorage.clear();
  history.replaceState({}, "", "/dashboard/collections");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit = {}) => {
      requests.push({ path, init, href: window.location.href });
      if (path === "/v1/session/launch")
        return launchStatus === 200
          ? json({ schema_version: "1", token_type: "Bearer", token: "owner" })
          : json(
              {
                code: "FORBIDDEN_SCOPE",
                message: "Launch link is invalid or expired.",
                retryable: false,
              },
              launchStatus,
            );
      if (rejectToken && bearer(init) === `Bearer ${rejectToken}`)
        return json(
          {
            code: "FORBIDDEN_SCOPE",
            message: "Local credential is invalid.",
            retryable: false,
          },
          403,
        );
      if (path === "/v1/handshake") return json(handshake);
      if (path.startsWith("/v1/collections?"))
        return json({
          collections: [
            {
              collection_id: handshake.default_collection_id,
              title: "Default collection",
              archived_at: null,
            },
          ],
          next_cursor: null,
        });
      if (path.startsWith("/v1/collections/"))
        return json({
          collection_id: path.split("/").at(-1),
          archived_at: null,
        });
      return json({ collections: [], next_cursor: null });
    }),
  );
});
afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

function renderApp() {
  return render(<App renderRoute={() => <p>Workspace page</p>} />);
}

it("strips the launch fragment before exchanging the code, then connects", async () => {
  history.replaceState({}, "", `/dashboard/collections#launch=${CODE}`);
  renderApp();
  // Removed synchronously during the first render, before any request.
  expect(window.location.hash).toBe("");
  await screen.findByText(handshake.workspace_id);
  const exchange = requests.find((r) => r.path === "/v1/session/launch")!;
  expect(exchange.href).not.toContain("launch=");
  expect(exchange.href).not.toContain(CODE);
  expect(exchange.init.method).toBe("POST");
  expect(exchange.init.credentials).toBe("omit");
  expect(JSON.parse(exchange.init.body as string)).toEqual({ code: CODE });
  expect(requests.indexOf(exchange)).toBe(0);
  const handshakeRequest = requests.find((r) => r.path === "/v1/handshake")!;
  expect(bearer(handshakeRequest.init)).toBe("Bearer owner");
  expect(window.location.pathname).toBe("/dashboard/collections");
  // A launch alone never stores anything.
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBeNull();
  expect(sessionStorage.length).toBe(0);
  expect(document.cookie).toBe("");
});

it("explains an expired or reused launch link and shows the form", async () => {
  launchStatus = 403;
  history.replaceState({}, "", `/dashboard/#launch=${CODE}`);
  renderApp();
  expect(window.location.hash).toBe("");
  expect(
    await screen.findByText(/Launch link is invalid or expired/),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Launch links work once and expire/),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("Bearer token")).toBeEnabled();
});

it("keeps the token in memory only unless Remember is checked", async () => {
  renderApp();
  const remember = screen.getByLabelText("Remember on this browser");
  expect(remember).not.toBeChecked();
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "memory-only" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBeNull();
  expect(screen.queryByRole("button", { name: "Forget this browser" })).toBe(
    null,
  );
});

it("remembers when opted in and reconnects on the next load", async () => {
  const first = renderApp();
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "kept" },
  });
  fireEvent.click(screen.getByLabelText("Remember on this browser"));
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBe("kept");
  first.unmount();
  requests = [];
  renderApp();
  await screen.findByText(handshake.workspace_id);
  expect(bearer(requests[0].init)).toBe("Bearer kept");
});

it("can remember a launch-link session from the sidebar", async () => {
  history.replaceState({}, "", `/dashboard/#launch=${CODE}`);
  renderApp();
  await screen.findByText(handshake.workspace_id);
  fireEvent.click(screen.getByLabelText("Remember on this browser"));
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBe("owner");
  fireEvent.click(screen.getByLabelText("Remember on this browser"));
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBeNull();
});

it("clears a stale remembered token rejected by the server", async () => {
  localStorage.setItem(REMEMBERED_TOKEN_KEY, "rotated");
  rejectToken = "rotated";
  renderApp();
  expect(
    await screen.findByText(/remembered on this browser was rejected/),
  ).toBeInTheDocument();
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBeNull();
  expect(screen.getByLabelText("Bearer token")).toBeEnabled();
});

it("keeps a remembered token when the server is merely unreachable", async () => {
  localStorage.setItem(REMEMBERED_TOKEN_KEY, "kept");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }),
  );
  renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "CONNECTION_FAILED",
  );
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBe("kept");
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
});

it("Disconnect keeps the remembered token; Forget this browser clears it", async () => {
  localStorage.setItem(REMEMBERED_TOKEN_KEY, "kept");
  renderApp();
  await screen.findByText(handshake.workspace_id);
  fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
  await screen.findByLabelText("Bearer token");
  // An explicit disconnect never reconnects automatically.
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(screen.getByLabelText("Bearer token")).toBeInTheDocument();
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBe("kept");
  fireEvent.change(screen.getByLabelText("Bearer token"), {
    target: { value: "kept" },
  });
  fireEvent.click(screen.getByLabelText("Remember on this browser"));
  fireEvent.click(screen.getByRole("button", { name: "Connect" }));
  await screen.findByText(handshake.workspace_id);
  fireEvent.click(screen.getByRole("button", { name: "Forget this browser" }));
  await screen.findByLabelText("Bearer token");
  expect(localStorage.getItem(REMEMBERED_TOKEN_KEY)).toBeNull();
});

it("survives unavailable storage", async () => {
  const failing = {
    getItem: () => {
      throw new DOMException("blocked", "SecurityError");
    },
    setItem: () => {
      throw new DOMException("blocked", "SecurityError");
    },
    removeItem: () => {
      throw new DOMException("blocked", "SecurityError");
    },
  };
  const original = Object.getOwnPropertyDescriptor(window, "localStorage");
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get: () => failing,
  });
  try {
    renderApp();
    fireEvent.change(screen.getByLabelText("Bearer token"), {
      target: { value: "kept" },
    });
    fireEvent.click(screen.getByLabelText("Remember on this browser"));
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await screen.findByText(handshake.workspace_id);
    await waitFor(() =>
      expect(
        screen.getByLabelText("Remember on this browser"),
      ).not.toBeChecked(),
    );
  } finally {
    if (original) Object.defineProperty(window, "localStorage", original);
    else delete (window as { localStorage?: Storage }).localStorage;
  }
});
