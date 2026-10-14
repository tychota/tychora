import { useCallback, useEffect, useRef, useState } from "react";
import { useSession } from "../session";
import { publicError } from "../api/client";
import { pagePath } from "../api/operations";
export function useResource<T>(path: string | null) {
  const { client } = useSession();
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    if (!client || !path) return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setBusy(true);
    setError(null);
    try {
      const result = await client.request<T>(path, { signal: next.signal });
      if (!next.signal.aborted) setValue(result);
    } catch (e) {
      if (!next.signal.aborted) setError(publicError(e));
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }, [client, path]);
  useEffect(() => {
    setValue(null);
    void load();
    return () => controller.current?.abort();
  }, [load]);
  return { value, setValue, error, setError, busy, load };
}
export function usePages<T extends { next_cursor: string | null }>(
  path: string,
) {
  const resource = useResource<T>(pagePath(path));
  const { client } = useSession();
  const [pages, setPages] = useState<T[]>([]);
  const [moreBusy, setMoreBusy] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    if (resource.value) setPages([resource.value]);
  }, [resource.value]);
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, [client, path]);
  const more = async () => {
    const cursor = pages.at(-1)?.next_cursor;
    if (!client || !cursor || moreBusy) return;
    setMoreBusy(true);
    const signal = lifetime.current.signal;
    try {
      const result = await client.request<T>(pagePath(path, cursor), {
        signal,
      });
      if (!signal.aborted) setPages((old) => [...old, result]);
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
