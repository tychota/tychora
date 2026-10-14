import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiClient, ApiError } from "./api/client";
import type { Handshake } from "./api/contracts";

interface Session {
  client: ApiClient | null;
  handshake: Handshake | null;
  selectedCollections: string[];
  sessionKey: number;
  connect: (token: string) => Promise<void>;
  selectCollections: (ids: string[]) => void;
  disconnect: () => void;
}
const Context = createContext<Session | null>(null);
export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{
    client: ApiClient | null;
    handshake: Handshake | null;
    selectedCollections: string[];
    sessionKey: number;
  }>({ client: null, handshake: null, selectedCollections: [], sessionKey: 0 });
  const active = useRef<ApiClient | null>(null);
  const pending = useRef<ApiClient | null>(null);
  const grant = useRef<ApiClient | null>(null);
  const epoch = useRef(0);
  const disconnect = () => {
    epoch.current++;
    active.current?.dispose();
    pending.current?.dispose();
    grant.current?.dispose();
    active.current = pending.current = grant.current = null;
    setState({
      client: null,
      handshake: null,
      selectedCollections: [],
      sessionKey: epoch.current,
    });
  };
  const connect = async (token: string) => {
    disconnect();
    const generation = epoch.current;
    const candidate = new ApiClient(token);
    pending.current = candidate;
    try {
      const handshake = await candidate.request<Handshake>("/v1/handshake");
      if (generation !== epoch.current) return;
      const collections =
        handshake.default_collection_id &&
        handshake.scope.collection_ids.includes(handshake.default_collection_id)
          ? [handshake.default_collection_id]
          : handshake.scope.collection_ids;
      if (!collections.length)
        throw new ApiError(
          "FORBIDDEN_SCOPE",
          "No readable collections were granted.",
        );
      grant.current = new ApiClient(token, handshake.scope);
      active.current = grant.current.narrow(collections);
      setState({
        client: active.current,
        handshake,
        selectedCollections: collections,
        sessionKey: epoch.current,
      });
    } finally {
      candidate.dispose();
      if (pending.current === candidate) pending.current = null;
    }
  };
  const selectCollections = (ids: string[]) => {
    if (!grant.current) return;
    const client = grant.current.narrow(ids);
    active.current?.dispose();
    active.current = client;
    epoch.current++;
    setState((previous) => ({
      ...previous,
      client,
      selectedCollections: ids,
      sessionKey: epoch.current,
    }));
  };
  useEffect(
    () => () => {
      active.current?.dispose();
      pending.current?.dispose();
      grant.current?.dispose();
    },
    [],
  );
  return (
    <Context.Provider
      value={{ ...state, connect, selectCollections, disconnect }}
    >
      {children}
    </Context.Provider>
  );
}
export function useSession(): Session {
  const session = useContext(Context);
  if (!session) throw new Error("useSession requires SessionProvider");
  return session;
}
