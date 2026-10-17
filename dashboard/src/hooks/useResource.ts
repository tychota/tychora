import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "../session";
import { ApiClient, publicError } from "../api/client";
import { pagePath } from "../api/operations";
/** The owner chooses selected knowledge or full-grant administration explicitly. */
export function useResource<T>(path: string | null, owner?: ApiClient | null) {
  const session = useSession();
  const client = owner === undefined ? session.client : owner;
  const [state, setState] = useState<{
    client: ApiClient | null;
    path: string | null;
    value: T | null;
    error: unknown;
    busy: boolean;
  }>({ client, path, value: null, error: null, busy: !!client && !!path });
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setState({
      client,
      path,
      value: null,
      error: null,
      busy: !!client && !!path,
    });
    if (!client || !path) return;
    try {
      const value = await client.request<T>(path, { signal: next.signal });
      if (!next.signal.aborted)
        setState({ client, path, value, error: null, busy: false });
    } catch (e) {
      if (!next.signal.aborted)
        setState({
          client,
          path,
          value: null,
          error: publicError(e),
          busy: false,
        });
    }
  }, [client, path]);
  useEffect(() => {
    void load();
    return () => controller.current?.abort();
  }, [load]);
  const current = state.client === client && state.path === path;
  const setValue = (value: T | null) =>
    setState((s) => ({ ...s, client, path, value }));
  const setError = (error: unknown) =>
    setState((s) => ({ ...s, client, path, error }));
  return {
    value: current ? state.value : null,
    error: current ? state.error : null,
    busy: current ? state.busy : !!client && !!path,
    setValue,
    setError,
    load,
  };
}
export function usePages<T extends { next_cursor: string | null }>(
  path: string | null,
  owner?: ApiClient | null,
) {
  const session = useSession();
  const client = owner === undefined ? session.client : owner;
  const resource = useResource<T>(path ? pagePath(path) : null, client);
  const [stored, setStored] = useState<{
    client: ApiClient | null;
    path: string | null;
    first: T | null;
    pages: T[];
  }>({ client, path, first: null, pages: [] });
  const [moreBusy, setMoreBusy] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    setMoreBusy(false);
    setStored({ client, path, first: null, pages: [] });
    return () => lifetime.current.abort();
  }, [client, path]);
  const pages =
    stored.client === client &&
    stored.path === path &&
    stored.first === resource.value
      ? stored.pages
      : resource.value
        ? [resource.value]
        : [];
  const more = async () => {
    const cursor = pages.at(-1)?.next_cursor;
    if (!client || !path || !cursor || moreBusy) return;
    setMoreBusy(true);
    const signal = lifetime.current.signal;
    try {
      const result = await client.request<T>(pagePath(path, cursor), {
        signal,
      });
      if (!signal.aborted)
        setStored({
          client,
          path,
          first: resource.value,
          pages: [...pages, result],
        });
    } catch (e) {
      if (!signal.aborted) resource.setError(publicError(e));
    } finally {
      if (!signal.aborted) setMoreBusy(false);
    }
  };
  return {
    ...resource,
    pages,
    more,
    busy: resource.busy || moreBusy,
    next: pages.at(-1)?.next_cursor,
  };
}
