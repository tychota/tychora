import { ApiError, publicError } from "./api/client";

/** Opt-in, per-origin storage of the bearer token ("Remember on this browser"). */
export const REMEMBERED_TOKEN_KEY = "tychora.dashboard.v1.rememberedToken";

export function readRememberedToken(): string | null {
  try {
    const value = window.localStorage.getItem(REMEMBERED_TOKEN_KEY);
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

/** Returns whether the token is now stored. */
export function rememberToken(token: string): boolean {
  try {
    window.localStorage.setItem(REMEMBERED_TOKEN_KEY, token);
    return true;
  } catch {
    return false;
  }
}

export function forgetRememberedToken(): void {
  try {
    window.localStorage.removeItem(REMEMBERED_TOKEN_KEY);
  } catch {
    // Storage may be unavailable (private mode, blocked site data).
  }
}

/**
 * Take a one-time launch code from the URL fragment and remove the fragment
 * synchronously, before any network request, so it never lingers in history.
 */
export function takeLaunchCode(): string | null {
  const hash = window.location.hash;
  if (!hash.includes("launch=")) return null;
  try {
    history.replaceState(
      history.state,
      "",
      window.location.pathname + window.location.search,
    );
  } catch {
    window.location.hash = "";
  }
  const code = new URLSearchParams(hash.slice(1)).get("launch");
  return code && /^[A-Za-z0-9_-]{32,128}$/.test(code) ? code : null;
}

/** Exchange a launch code once for the owner's bearer token. */
export async function exchangeLaunchCode(code: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch("/v1/session/launch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({ code }),
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch (error) {
    throw publicError(error);
  }
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      typeof value?.code === "string" ? value.code : `HTTP_${response.status}`,
      typeof value?.message === "string"
        ? value.message
        : "The launch link was rejected.",
      value?.retryable === true,
    );
  }
  if (typeof value?.token !== "string" || !value.token)
    throw new ApiError("INVALID_RESPONSE", "The launch exchange failed.");
  return value.token;
}

/** Whether a failure means the credential itself was rejected. */
export function isRejectedCredential(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === "FORBIDDEN_SCOPE" ||
      error.code === "HTTP_401" ||
      error.code === "HTTP_403")
  );
}
