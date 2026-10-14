import { ApiError } from "../../api/client";
export const REQUEST_LIMIT = 1048576;
export function encodedBody(value: unknown): { body: string; size: number } {
  const body = JSON.stringify(value);
  return { body, size: new TextEncoder().encode(body).length };
}
/** One encoded intent, one retry key. A changed intent cannot reuse an earlier key. */
export class SubmissionIntent {
  private body: string | null = null;
  private key: string | null = null;
  keyFor(body: string): string {
    if (body !== this.body) {
      this.body = body;
      this.key = crypto.randomUUID();
    }
    return this.key!;
  }
}
export function parseMetadata(
  text: string,
  common: Record<string, string>,
): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new ApiError("INVALID_REQUEST", "Metadata must be valid JSON.");
  }
  if (!value || Array.isArray(value) || typeof value !== "object")
    throw new ApiError("INVALID_REQUEST", "Metadata must be a JSON object.");
  return {
    ...value,
    ...Object.fromEntries(Object.entries(common).filter(([, v]) => v.trim())),
  };
}
export async function fileBase64(file: File): Promise<string> {
  if (file.size > (REQUEST_LIMIT * 3) / 4)
    throw new ApiError(
      "INPUT_BUDGET_EXCEEDED",
      "The encoded request exceeds the 1 MiB limit. Choose a smaller file.",
    );
  const buffer = await file.arrayBuffer();
  let binary = "";
  for (const byte of new Uint8Array(buffer))
    binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_");
}

/** Mirrors the parser formats and filename aliases in SourceService registration. */
export function fileFormat(name: string): string {
  const suffix = name.split(".").at(-1)?.toLowerCase();
  if (!name.includes(".")) return "";
  const formats: Record<string, string> = {
    md: "markdown",
    markdown: "markdown",
    txt: "text",
    text: "text",
    json: "json",
    pdf: "pdf",
    docx: "docx",
    png: "png",
    jpg: "jpeg",
    jpeg: "jpeg",
  };
  return suffix && Object.hasOwn(formats, suffix) ? formats[suffix] : "";
}
