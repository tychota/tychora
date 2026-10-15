import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { SessionProvider, useSession } from "./session";
import { publicError } from "./api/client";
import {
  exchangeLaunchCode,
  isRejectedCredential,
  readRememberedToken,
  takeLaunchCode,
} from "./credentials";
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
interface Startup {
  /** A launch-link exchange or remembered connection is in progress. */
  starting: boolean;
  error: unknown;
  notice: string | null;
  retry: (() => void) | null;
}
/**
 * Runs once per page load: a launch link in the fragment wins over a
 * remembered credential. Disconnecting later never reconnects automatically.
 */
function useStartup(): Startup {
  const { connect, setRemembered } = useSession();
  // Taken synchronously during the first render, before any request.
  const [launchCode] = useState(takeLaunchCode);
  const [startup, setStartup] = useState<Startup>(() => ({
    starting: launchCode !== null || readRememberedToken() !== null,
    error: null,
    notice: null,
    retry: null,
  }));
  const live = useRef(true);
  const ran = useRef(false);
  const update = (next: Partial<Startup>) => {
    if (live.current) setStartup((previous) => ({ ...previous, ...next }));
  };
  const connectRemembered = async () => {
    const stored = readRememberedToken();
    if (!stored) return update({ starting: false });
    update({ starting: true, error: null, notice: null, retry: null });
    try {
      await connect(stored);
      update({ starting: false });
    } catch (error) {
      if (isRejectedCredential(error)) {
        setRemembered(false);
        update({
          starting: false,
          notice:
            "The credential remembered on this browser was rejected, so it was removed. The owner token may have been rotated; connect again below.",
        });
      } else
        update({
          starting: false,
          error: publicError(error),
          retry: () => void connectRemembered(),
        });
    }
  };
  useEffect(() => {
    live.current = true;
    if (!ran.current) {
      ran.current = true;
      if (launchCode !== null) {
        void (async () => {
          try {
            await connect(await exchangeLaunchCode(launchCode));
            update({ starting: false });
          } catch (error) {
            update({
              starting: false,
              error: publicError(error),
              notice:
                "Launch links work once and expire after two minutes. Run `tychora manage dashboard-link` for a new one.",
            });
          }
        })();
      } else void connectRemembered();
    }
    return () => {
      live.current = false;
    };
    // Startup runs exactly once per page load.
  }, []);
  return startup;
}
function Connection({ startup }: { startup: Startup }) {
  const { connect } = useSession();
  const [token, setToken] = useState("");
  const [remember, setRemember] = useState(false);
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
      await connect(token, { remember });
      if (live.current) setToken("");
    } catch (error) {
      if (live.current) setError(publicError(error));
    } finally {
      if (live.current) setLoading(false);
    }
  };
  const busy = loading || startup.starting;
  return (
    <main id="main" className="connection">
      <div className="wordmark">
        tychora<span>Evidence workbench</span>
      </div>
      <div className="connection-copy">
        <p className="eyebrow">Local workspace</p>
        <h1>Connect to your evidence.</h1>
        <p>
          Open the one-time link printed by the server, or run{" "}
          <code>tychora manage dashboard-link</code>. You can also paste the
          bearer token configured for this local server.
        </p>
      </div>
      {startup.starting && (
        <p className="muted" role="status">
          Connecting to the local workspace…
        </p>
      )}
      {startup.notice !== null && (
        <p className="connection-notice" role="status">
          {startup.notice}
        </p>
      )}
      {startup.error !== null && (
        <ErrorNotice error={startup.error} retry={startup.retry ?? undefined} />
      )}
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
          disabled={busy}
        />
        <button
          type="submit"
          className="primary"
          disabled={busy || !token.trim()}
        >
          {loading ? "Connecting…" : "Connect"}
        </button>
        <label className="remember-choice" htmlFor="remember">
          <input
            id="remember"
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            disabled={busy}
            aria-describedby="remember-help"
          />
          Remember on this browser
        </label>
      </form>
      <p className="muted" id="remember-help">
        Without Remember, the token stays in memory for this tab only. Remember
        stores it in this browser&apos;s local storage until you choose Forget
        this browser; avoid it on shared machines. Disconnecting clears
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
  const startup = useStartup();
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
  if (!session.adminClient || !session.handshake)
    return <Connection startup={startup} />;
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
              <label className="remember-choice" htmlFor="remember-session">
                <input
                  id="remember-session"
                  type="checkbox"
                  checked={session.remembered}
                  onChange={(e) => session.setRemembered(e.target.checked)}
                />
                Remember on this browser
              </label>
              <div className="session-actions">
                <button type="button" onClick={session.disconnect}>
                  Disconnect
                </button>
                {session.remembered && (
                  <button type="button" onClick={session.forget}>
                    Forget this browser
                  </button>
                )}
              </div>
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
