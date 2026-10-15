import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { SessionProvider, useSession } from "./session";
import { publicError } from "./api/client";
import { ErrorNotice } from "./components/Status";
import { ModelDraftProvider } from "./pages/models/draft";
import { Search } from "./pages/Search";
import { OperationsRouter } from "./pages/OperationsRouter";
import { useRoute, RouteLink } from "./routing";
import { WorkspaceScope } from "./components/WorkspaceScope";
import { AddDocumentDraftProvider } from "./pages/documents/draft";
export { useRoute } from "./routing";

export const destinations = [
  "Collections",
  "Documents",
  "Search",
  "Explore",
  "Jobs",
  "Models",
  "Evaluations",
  "Activity",
  "Settings",
] as const;
function Connection() {
  const { connect } = useSession();
  const [token, setToken] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const live = useRef(true);
  useEffect(
    () => () => {
      live.current = false;
    },
    [],
  );
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await connect(token);
      setToken("");
    } catch (error) {
      if (live.current) setError(publicError(error));
    } finally {
      if (live.current) setLoading(false);
    }
  };
  return (
    <main id="main" className="connection">
      <div className="wordmark">
        tychora<span>Evidence workbench</span>
      </div>
      <div className="connection-copy">
        <p className="eyebrow">Local workspace</p>
        <h1>Connect to your evidence.</h1>
        <p>
          Use the bearer token configured for this local server. Your granted
          workspace and collections will appear after connection.
        </p>
      </div>
      <form onSubmit={(event) => void submit(event)}>
        <label htmlFor="bearer">Bearer token</label>
        <input
          id="bearer"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          required
          disabled={loading}
        />
        <button
          type="submit"
          className="primary"
          disabled={loading || !token.trim()}
        >
          {loading ? "Connecting…" : "Connect"}
        </button>
      </form>
      <p className="muted">
        The token stays in memory for this connection. Disconnecting clears
        workspace data and cancels outstanding requests.
      </p>
      {error !== null && <ErrorNotice error={error} />}
    </main>
  );
}
/** Optional route override for embedding the workbench. */
export interface AppProps {
  renderRoute?: (path: string) => ReactNode;
}
function Shell({ renderRoute }: AppProps) {
  const session = useSession();
  const { path } = useRoute();
  const rawPage =
    path.replace(/^\/dashboard\/?/, "").split("/")[0] || "collections";
  const page =
    rawPage === "sources"
      ? "documents"
      : rawPage === "runs"
        ? "activity"
        : rawPage;
  const [navigationOpen, setNavigationOpen] = useState(false);
  useEffect(() => setNavigationOpen(false), [path]);
  if (!session.adminClient || !session.handshake) return <Connection />;
  const administration = ["jobs", "models", "evaluations", "settings"].includes(
    page,
  );
  const routeKey =
    page === "documents" && path !== "/dashboard/documents/add"
      ? "documents"
      : path;
  const noScope =
    !session.client &&
    ["documents", "search", "explore", "activity"].includes(page) &&
    path !== "/dashboard/documents/add" &&
    !/^\/dashboard\/(?:documents|sources)\/[^/]+\/revisions\/[^/]+$/.test(path);
  return (
    <AddDocumentDraftProvider key={session.connectionKey}>
      <ModelDraftProvider>
        <div className="app-shell">
          <aside className="sidebar">
            <RouteLink className="wordmark" to="/dashboard/collections">
              tychora<span>Evidence workbench</span>
            </RouteLink>
            <button
              className="navigation-toggle"
              type="button"
              aria-expanded={navigationOpen}
              aria-controls="workbench-navigation"
              onClick={() => setNavigationOpen((v) => !v)}
            >
              Navigation
            </button>
            <nav
              id="workbench-navigation"
              className={navigationOpen ? "navigation-open" : ""}
              aria-label="Workbench"
            >
              {[
                { label: "Knowledge", pages: destinations.slice(0, 4) },
                { label: "Administration", pages: destinations.slice(4) },
              ].map((group) => (
                <div className="navigation-group" key={group.label}>
                  <p className="eyebrow">{group.label}</p>
                  {group.pages.map((destination) => (
                    <RouteLink
                      key={destination}
                      to={`/dashboard/${destination.toLowerCase()}`}
                      aria-current={
                        page === destination.toLowerCase() ? "page" : undefined
                      }
                    >
                      {destination}
                    </RouteLink>
                  ))}
                </div>
              ))}
            </nav>
            <div className="workspace-context">
              <p className="eyebrow">Connected workspace</p>
              <details>
                <summary>Workspace identity</summary>
                <p className="workspace-id">{session.handshake.workspace_id}</p>
              </details>
              <button type="button" onClick={session.disconnect}>
                Disconnect
              </button>
            </div>
            <p className="sidebar-footer">Evidence before conclusions.</p>
          </aside>
          <div className="workspace-body">
            <header className="workspace-bar">
              <span>Local workspace</span>
              <WorkspaceScope key={session.sessionKey} />
              {administration && (
                <p className="authority-context">
                  {page === "jobs"
                    ? "All jobs within your full granted scope"
                    : "Entire workspace · server authorization applies"}
                </p>
              )}
              {page === "activity" && (
                <p className="authority-context">
                  History for this exact selected query scope
                </p>
              )}
            </header>
            <main id="main" className="main-content" tabIndex={-1}>
              <div
                key={`${session.sessionKey}-${administration ? "admin" : session.selectionKey}-${routeKey}`}
              >
                {noScope ? (
                  <section className="empty-state">
                    <h1>No active collection selected</h1>
                    <p>
                      Select a granted active collection, or create or
                      reactivate one in Collections.
                    </p>
                    <RouteLink to="/dashboard/collections">
                      Open Collections
                    </RouteLink>
                  </section>
                ) : page === "search" ? (
                  <Search />
                ) : renderRoute ? (
                  renderRoute(path)
                ) : (
                  <OperationsRouter path={path} />
                )}
              </div>
            </main>
          </div>
        </div>
      </ModelDraftProvider>
    </AddDocumentDraftProvider>
  );
}
export function App(props: AppProps) {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <SessionProvider>
        <Shell {...props} />
      </SessionProvider>
    </>
  );
}
