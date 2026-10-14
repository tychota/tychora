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
import { Search } from "./pages/Search";
import { OperationsRouter } from "./pages/OperationsRouter";

export const destinations = [
  "Search",
  "Sources",
  "Jobs",
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
export function useRoute() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const navigate = (next: string) => {
    history.pushState({}, "", next);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  return { path, navigate };
}
/** Optional route override for embedding the workbench. */
export interface AppProps {
  renderRoute?: (path: string) => ReactNode;
}
function Shell({ renderRoute }: AppProps) {
  const session = useSession();
  const { path, navigate } = useRoute();
  const page = path.replace(/^\/dashboard\/?/, "").split("/")[0] || "search";
  if (!session.client || !session.handshake) return <Connection />;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="wordmark"
          href="/dashboard/search"
          onClick={(event) => {
            event.preventDefault();
            navigate("/dashboard/search");
          }}
        >
          tychora<span>Evidence workbench</span>
        </a>
        <nav aria-label="Workbench">
          {destinations.map((destination) => (
            <a
              key={destination}
              href={`/dashboard/${destination.toLowerCase()}`}
              aria-current={
                page === destination.toLowerCase() ? "page" : undefined
              }
              onClick={(event) => {
                if (
                  event.metaKey ||
                  event.ctrlKey ||
                  event.shiftKey ||
                  event.altKey
                )
                  return;
                event.preventDefault();
                navigate(`/dashboard/${destination.toLowerCase()}`);
              }}
            >
              {destination}
            </a>
          ))}
        </nav>
        <div className="workspace-context">
          <p className="eyebrow">Workspace</p>
          <p className="workspace-id">{session.handshake.workspace_id}</p>
          <fieldset>
            <legend>Granted collections</legend>
            {session.handshake.scope.collection_ids.map((id) => (
              <label key={id} className="collection-option">
                <input
                  type="checkbox"
                  checked={session.selectedCollections.includes(id)}
                  disabled={
                    session.selectedCollections.length === 1 &&
                    session.selectedCollections.includes(id)
                  }
                  onChange={(e) =>
                    session.selectCollections(
                      e.target.checked
                        ? [...session.selectedCollections, id]
                        : session.selectedCollections.filter(
                            (value) => value !== id,
                          ),
                    )
                  }
                />
                <span>{id}</span>
              </label>
            ))}
          </fieldset>
          <button type="button" onClick={session.disconnect}>
            Disconnect
          </button>
        </div>
        <p className="sidebar-footer">Evidence before conclusions.</p>
      </aside>
      <main id="main" className="main-content" tabIndex={-1}>
        <div key={`${session.sessionKey}-${path}`}>
          {page === "search" ? (
            <Search />
          ) : renderRoute ? (
            renderRoute(path)
          ) : (
            <OperationsRouter path={path} />
          )}
        </div>
      </main>
    </div>
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
