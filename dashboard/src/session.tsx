import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiClient, ApiError } from "./api/client";
import type {
  Handshake,
  CollectionInput,
  CollectionSummary,
} from "./api/contracts";

interface Session {
  client: ApiClient | null;
  adminClient: ApiClient | null;
  handshake: Handshake | null;
  selectedCollections: string[];
  sessionKey: number;
  connect: (token: string) => Promise<void>;
  refreshGrant: (selectCreatedId?: string) => Promise<void>;
  createCollection: (value: CollectionInput) => Promise<CollectionSummary>;
  selectCollections: (ids: string[]) => void;
  disconnect: () => void;
}
const Context = createContext<Session | null>(null);
export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{
    client: ApiClient | null;
    adminClient: ApiClient | null;
    handshake: Handshake | null;
    selectedCollections: string[];
    sessionKey: number;
  }>({
    client: null,
    adminClient: null,
    handshake: null,
    selectedCollections: [],
    sessionKey: 0,
  });
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
      adminClient: null,
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
      grant.current = new ApiClient(token);
      active.current = grant.current
        .withGrant(handshake.scope)
        .narrow(collections);
      setState({
        client: active.current,
        adminClient: grant.current,
        handshake,
        selectedCollections: collections,
        sessionKey: epoch.current,
      });
    } finally {
      candidate.dispose();
      if (pending.current === candidate) pending.current = null;
    }
  };
  const refreshGrant = async (selectCreatedId?: string) => {
    const full = grant.current;
    if (!full) return;
    const generation = epoch.current;
    // This credential-only client deliberately carries no stale scope header.
    const handshake = await full.handshake();
    const page = await full.collections();
    const candidates = (
      selectCreatedId ? [selectCreatedId] : state.selectedCollections
    ).filter((id) => handshake.scope.collection_ids.includes(id));
    const current = await Promise.all(
      candidates.map((id) => full.getCollection(id)),
    );
    if (generation !== epoch.current || grant.current !== full) return;
    let selected = current
      .filter((c) => !c.archived_at)
      .map((c) => c.collection_id);
    if (!selected.length) {
      const fallback =
        page.collections.find(
          (c) =>
            !c.archived_at &&
            c.collection_id === handshake.default_collection_id,
        ) ?? page.collections.find((c) => !c.archived_at);
      selected = fallback ? [fallback.collection_id] : [];
    }
    const freshFull = full.credentialOnly();
    const fresh = freshFull.withGrant(handshake.scope);
    const client = selected.length ? fresh.narrow(selected) : fresh;
    active.current?.dispose();
    pending.current?.dispose();
    full.dispose();
    grant.current = freshFull;
    active.current = client;
    epoch.current++;
    setState({
      client,
      adminClient: freshFull,
      handshake,
      selectedCollections: selected,
      sessionKey: epoch.current,
    });
  };
  const createCollection = async (value: CollectionInput) => {
    if (!grant.current)
      throw new ApiError("FORBIDDEN_SCOPE", "Connect to a workspace first.");
    const created = await grant.current.createCollection(value);
    await refreshGrant(created.collection_id);
    return created;
  };
  const selectCollections = (ids: string[]) => {
    if (!grant.current) return;
    if (!state.handshake) return;
    const client = grant.current.withGrant(state.handshake.scope).narrow(ids);
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
      value={{
        ...state,
        connect,
        refreshGrant,
        createCollection,
        selectCollections,
        disconnect,
      }}
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
