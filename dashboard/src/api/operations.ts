import type { Json, QueryResult } from "./contracts";
export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export interface JobHandle {
  id: string;
  operation: string;
  state: JobState;
}
export interface Artifact {
  sha256: string;
  size_bytes: number;
  media_type: string;
  storage_key: string;
}
export interface JobStatus extends JobHandle {
  request_digest: string;
  effective_config: Artifact;
  captured_inputs: Artifact | null;
  base_snapshot_id: string | null;
  result_snapshot_id: string | null;
  result: Artifact | null;
  cancellation_requested: boolean;
  latest_sequence: number;
  retry_count: number;
  error_code: string | null;
  retention_limited: boolean;
}
export interface JobEvent {
  job_id: string;
  sequence: number;
  timestamp: string;
  stage: string;
  state: JobState;
  counters: { name: string; count: number }[];
  diagnostic_refs: string[];
}
export interface JobPage {
  jobs: JobStatus[];
  next_cursor: string | null;
}
export interface Extraction {
  extraction_id: string;
  stages: Record<string, Json>;
  code_resolution?: Record<string, Json> | null;
  channels_truncated: boolean;
}
export interface Revision {
  revision_id: string;
  kind_id: string;
  stages: Record<string, Json>;
  extractions: Extraction[];
  extractions_truncated: boolean;
}
export interface RevisionPage {
  source_id: string;
  revisions: Revision[];
  next_cursor: string | null;
  scope_limited: boolean;
}
export interface ExtractionPage {
  source_id: string;
  revision_id: string;
  extractions: Extraction[];
  next_cursor: string | null;
}
export interface ChannelPage {
  channels: { family_id: string; channel: string; state: string }[];
  next_cursor: string | null;
}
export interface Metric {
  name: string;
  value: number | null;
  sample_count: number;
  denominator: number | null;
  not_applicable_reason: string | null;
}
export interface EvaluationSummary {
  evaluation_id: string;
  dataset_id: string;
  snapshot_id: string;
  created_at: string;
  available: boolean;
}
export interface CaseRun {
  variant: string;
  repetition: number;
  seed: number;
  run_id: string;
  config_sha: string;
  metrics: { metrics: Metric[] };
  request: Json;
  result: QueryResult;
  execution_plan: Json;
  usage: Json;
}
export interface Evaluation extends Omit<
  EvaluationSummary,
  "created_at" | "available"
> {
  job_id: string;
  policy_epoch: number;
  lexical_statistics_epoch: number;
  config_sha: string;
  hardware: Record<string, Json>;
  report: {
    dataset_id: string;
    split: string;
    snapshot_id: string;
    created_at: string;
    aggregate: { metrics: Metric[] };
    cases: {
      case_id: string;
      variant: string;
      run_id: string;
      metrics: { metrics: Metric[] };
    }[];
  };
  cases: Record<
    string,
    { case_id: string; runs: CaseRun[]; schema_version: string }
  >;
}
export const terminal = (state: string) =>
  ["succeeded", "failed", "cancelled", "interrupted"].includes(state);
export const objectPath = (id: string) => encodeURIComponent(id);
export const pagePath = (path: string, cursor?: string | null) =>
  `${path}${path.includes("?") ? "&" : "?"}limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
