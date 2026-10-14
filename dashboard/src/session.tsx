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

async function selectActiveCollections(
  client: ApiClient,
  handshake: Handshake,
  preferredIds: string[],
): Promise<string[]> {
  const granted = (id: string) => handshake.scope.collection_ids.includes(id);
  const page = await client.collections();
  const current = await Promise.all(
    preferredIds.filter(granted).map((id) => client.getCollection(id)),
  );
  const selected = current
    .filter((c) => !c.archived_at)
    .map((c) => c.collection_id);
  if (selected.length) return selected;
  const defaultId = handshake.default_collection_id;
  if (defaultId && granted(defaultId)) {
    const defaultCollection =
      current.find((c) => c.collection_id === defaultId) ??
      page.collections.find((c) => c.collection_id === defaultId) ??
      (await client.getCollection(defaultId));
    if (!defaultCollection.archived_at) return [defaultId];
  }
  const fallback = page.collections.find(
    (c) => !c.archived_at && granted(c.collection_id),
  );
  return fallback ? [fallback.collection_id] : [];
}

interface Session {
  client: ApiClient | null;
  adminClient: ApiClient | null;
  handshake: Handshake | null;
  selectedCollections: string[];
  sessionKey: number;
  selectionKey: number;
  connectionKey: number;
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
    selectionKey: number;
    connectionKey: number;
  }>({
    client: null,
    adminClient: null,
    handshake: null,
    selectedCollections: [],
    sessionKey: 0,
    selectionKey: 0,
    connectionKey: 0,
  });
  const active = useRef<ApiClient | null>(null);
  const pending = useRef<ApiClient | null>(null);
  const grant = useRef<ApiClient | null>(null);
  const epoch = useRef(0);
  const selectedRef = useRef<string[]>([]);
  const selectionEpoch = useRef(0);
  const disconnect = () => {
    epoch.current++;
    selectionEpoch.current++;
    selectedRef.current = [];
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
      selectionKey: epoch.current,
      connectionKey: epoch.current,
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
      const collections = await selectActiveCollections(
        candidate,
        handshake,
        [],
      );
      if (generation !== epoch.current) return;
      grant.current = new ApiClient(token);
      const scoped = grant.current.withGrant(handshake.scope);
      active.current = collections.length ? scoped.narrow(collections) : null;
      scoped.dispose();
      selectedRef.current = collections;
      setState({
        client: active.current,
        adminClient: grant.current,
        handshake,
        selectedCollections: collections,
        sessionKey: epoch.current,
        selectionKey: epoch.current,
        connectionKey: epoch.current,
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
    const selectionGeneration = selectionEpoch.current;
    // This credential-only client deliberately carries no stale scope header.
    const handshake = await full.handshake();
    const selected = await selectActiveCollections(
      full,
      handshake,
      selectCreatedId ? [selectCreatedId] : selectedRef.current,
    );
    if (generation !== epoch.current || grant.current !== full) return;
    if (selectionGeneration !== selectionEpoch.current) {
      await refreshGrant();
      return;
    }
    const freshFull = full.credentialOnly();
    const fresh = freshFull.withGrant(handshake.scope);
    const client = selected.length ? fresh.narrow(selected) : null;
    fresh.dispose();
    selectedRef.current = selected;
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
      selectionKey: state.selectionKey + 1,
      connectionKey: state.connectionKey,
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
    const unique = [...new Set(ids)];
    if (
      unique.some((id) => !state.handshake!.scope.collection_ids.includes(id))
    )
      throw new ApiError("FORBIDDEN_SCOPE", "Select only granted collections.");
    const scoped = grant.current.withGrant(state.handshake.scope);
    const client = unique.length ? scoped.narrow(unique) : null;
    scoped.dispose();
    active.current?.dispose();
    active.current = client;
    selectedRef.current = unique;
    selectionEpoch.current++;
    setState((previous) => ({
      ...previous,
      client,
      selectedCollections: unique,
      selectionKey: previous.selectionKey + 1,
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
