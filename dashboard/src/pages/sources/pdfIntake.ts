import { ApiClient, ApiError } from "../../api/client";
import type {
  PdfIntakeDescriptor,
  PdfIntakeStatus,
  PdfIntakeSubmission,
} from "../../api/contracts";
import type { JobHandle } from "../../api/types";
import { encodedBody, REQUEST_LIMIT } from "./submission";
export const PDF_LIMIT = 104857600;
const path = (id: string) => `/v1/pdf-intakes/${encodeURIComponent(id)}`;
function json(value: unknown) {
  const encoded = encodedBody(value);
  if (encoded.size >= REQUEST_LIMIT)
    throw new ApiError(
      "INPUT_BUDGET_EXCEEDED",
      "PDF metadata exceeds the 1 MiB JSON request limit.",
    );
  return encoded.body;
}
export function preparePdf(
  client: ApiClient,
  descriptor: PdfIntakeDescriptor,
  signal: AbortSignal,
) {
  return client.request<PdfIntakeStatus>("/v1/pdf-intakes", {
    method: "POST",
    body: json(descriptor),
    signal,
  });
}
export function uploadPdf(
  client: ApiClient,
  intakeId: string,
  file: File,
  signal: AbortSignal,
): Promise<PdfIntakeStatus> {
  return client.request(`${path(intakeId)}/content`, {
    method: "PUT",
    body: file,
    headers: { "Content-Type": "application/pdf" },
    signal,
  });
}
export function analysePdf(client: ApiClient, id: string, signal: AbortSignal) {
  return client.request<PdfIntakeStatus>(`${path(id)}/analyse`, {
    method: "POST",
    signal,
  });
}
export function inspectPdf(client: ApiClient, id: string, signal: AbortSignal) {
  return client.request<PdfIntakeStatus>(path(id), { signal });
}
export function abortPdf(client: ApiClient, id: string, signal: AbortSignal) {
  return client.request<PdfIntakeStatus>(path(id), {
    method: "DELETE",
    signal,
  });
}
export function submitPdf(
  client: ApiClient,
  id: string,
  value: PdfIntakeSubmission,
  key: string,
  signal: AbortSignal,
) {
  return client.request<JobHandle>(`${path(id)}/submit`, {
    method: "POST",
    body: json(value),
    headers: { "Idempotency-Key": key },
    signal,
  });
}

/** Connection-owned ambiguous acceptance; never serialized to browser storage. */
export interface PendingPdfSubmission {
  file: File;
  owner: ApiClient;
  status: PdfIntakeStatus;
  submission: PdfIntakeSubmission;
  key: string;
  descriptor: PdfIntakeDescriptor;
}
