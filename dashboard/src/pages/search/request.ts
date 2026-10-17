import { ApiError } from "../../api/client";
import type { Json } from "../../api/contracts";

function isJson(value: unknown): value is Json {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value)) ||
    (Array.isArray(value) ? value.every(isJson) : isObject(value))
  );
}
function isObject(value: unknown): value is Record<string, Json> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every(isJson)
  );
}

/** Parse an editable body without claiming server validation. The backend owns
 * defaults, field types, registered modes, authorization and request semantics. */
export function parseQueryDraft(draft: string): Record<string, Json> {
  if (new TextEncoder().encode(draft).byteLength > 1048576)
    throw new ApiError(
      "INVALID_REQUEST",
      "Query requests must fit within the ordinary 1 MiB body limit.",
    );
  let value: unknown;
  try {
    value = JSON.parse(draft);
  } catch {
    throw new ApiError(
      "INVALID_REQUEST",
      "Enter valid JSON for the QueryRequest. Your draft is preserved.",
    );
  }
  if (!isObject(value))
    throw new ApiError(
      "INVALID_REQUEST",
      "QueryRequest must be a JSON object.",
    );
  return value;
}

export function requestMode(body: Record<string, Json>, defaultMode: string) {
  return typeof body.mode === "string" ? body.mode : defaultMode;
}
