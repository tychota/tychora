import type { Scope } from "./contracts";

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly retryable = false,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function publicError(error: unknown): ApiError {
  return error instanceof ApiError
    ? error
    : new ApiError(
        "CONNECTION_FAILED",
        "Could not reach the local workspace. Check the server and reconnect.",
        true,
      );
}
export class ApiClient {
  private lifetime = new AbortController();
  constructor(
    private token: string,
    private readonly scope?: Scope,
  ) {}
  narrow(collections: string[]): ApiClient {
    if (
      !this.scope ||
      !collections.length ||
      collections.some((id) => !this.scope!.collection_ids.includes(id))
    )
      throw new ApiError(
        "FORBIDDEN_SCOPE",
        "Select at least one granted collection.",
      );
    return new ApiClient(this.token, {
      ...this.scope,
      collection_ids: [...collections],
    });
  }
  dispose() {
    this.lifetime.abort();
    this.token = "";
  }
  private async fetch<T>(
    path: string,
    init: RequestInit = {},
    consume: (response: Response) => Promise<T>,
  ): Promise<T> {
    if (!path.startsWith("/v1/") || path.includes("\\") || /[\r\n]/.test(path))
      throw new ApiError(
        "INVALID_REQUEST",
        "Only same-origin API paths are allowed.",
      );
    if (this.lifetime.signal.aborted)
      throw new DOMException("Session ended", "AbortError");
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Accept", "application/json");
    if (this.scope) headers.set("X-Tychora-Scope", JSON.stringify(this.scope));
    if (init.body && !headers.has("Content-Type"))
      headers.set("Content-Type", "application/json");
    const controller = new AbortController();
    const abort = () => controller.abort();
    this.lifetime.signal.addEventListener("abort", abort, { once: true });
    init.signal?.addEventListener("abort", abort, { once: true });
    if (init.signal?.aborted) abort();
    try {
      const response = await fetch(path, {
        ...init,
        headers,
        signal: controller.signal,
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
      });
      if (controller.signal.aborted)
        throw new DOMException("Request ended", "AbortError");
      if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new ApiError(
          typeof error?.code === "string"
            ? error.code
            : `HTTP_${response.status}`,
          typeof error?.message === "string"
            ? error.message
            : "The workspace rejected this request.",
          error?.retryable === true,
        );
      }
      const value = await consume(response);
      if (controller.signal.aborted)
        throw new DOMException("Request ended", "AbortError");
      return value;
    } catch (error) {
      if (controller.signal.aborted)
        throw new DOMException("Request ended", "AbortError");
      if (error instanceof ApiError) throw error;
      throw publicError(error);
    } finally {
      this.lifetime.signal.removeEventListener("abort", abort);
      init.signal?.removeEventListener("abort", abort);
    }
  }
  async request<T>(path: string, init?: RequestInit): Promise<T> {
    return this.fetch(path, init, (response) => response.json() as Promise<T>);
  }
  async blob(path: string, init?: RequestInit): Promise<Blob> {
    return this.fetch(path, init, (response) => response.blob());
  }
}
