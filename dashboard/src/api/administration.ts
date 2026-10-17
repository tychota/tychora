import type {
  Json,
  SettingsPatch,
  SettingsPreview,
  SettingsFieldView,
  ModelAssetPlan,
} from "./contracts";
import type { JobHandle } from "./types";
import { ApiClient, ApiError } from "./client";
export interface MaintenanceResult {
  plan: {
    operation: string;
    target_ids: string[];
    actions: string[];
    estimated_bytes?: number | null;
    original_bindings?: Json[];
    reference_cut?: Json[];
    retained_derived_artifact_shas?: string[];
  };
  job?: JobHandle | null;
  receipt?: {
    operation: string;
    receipt_id: string;
    target_ids: string[];
    completed_at: string;
    manifest_sha: string | null;
  } | null;
  recovery?: Record<string, Json> | null;
  deleted_payloads?: number;
  shared_payloads?: number;
  pending_payloads?: number;
}
export function boundedBody(value: unknown) {
  const body = JSON.stringify(value);
  if (new TextEncoder().encode(body).byteLength >= 1048576)
    throw new ApiError(
      "INPUT_BUDGET_EXCEEDED",
      "Request exceeds the 1 MiB JSON limit.",
    );
  return body;
}
export const previewSettings = (
  client: ApiClient,
  patch: SettingsPatch,
  signal: AbortSignal,
) =>
  client.request<SettingsPreview>("/v1/settings/preview", {
    method: "POST",
    body: boundedBody(patch),
    signal,
  });
export const applySettings = (
  client: ApiClient,
  patch: SettingsPatch,
  signal: AbortSignal,
) =>
  client.request<SettingsFieldView>("/v1/settings", {
    method: "PATCH",
    body: boundedBody(patch),
    signal,
  });
export const setupPlan = (
  client: ApiClient,
  selections: string[],
  signal: AbortSignal,
) =>
  client.request<ModelAssetPlan>("/v1/models/setup-plan", {
    method: "POST",
    body: boundedBody({ selections }),
    signal,
  });
export const maintain = (
  client: ApiClient,
  value: unknown,
  signal: AbortSignal,
) =>
  client.request<MaintenanceResult>("/v1/jobs", {
    method: "POST",
    body: boundedBody(value),
    signal,
  });
