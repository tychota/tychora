/** Browser DTOs mirror tychora.contracts and the captured HTTP contracts. */
export type Json =
  | null
  | boolean
  | number
  | string
  | Json[]
  | { [key: string]: Json };
export interface Scope {
  principal_id: string;
  collection_ids: string[];
  access_ceiling: number;
  uses: string[];
  capabilities: string[];
  upload_providers: string[];
}
export interface Handshake {
  managed_workspace_owner: boolean;
  policy_epoch: number;
  schema_version: string;
  protocol_version: number;
  workspace_id: string;
  instance_id: string;
  scope: Scope;
  default_collection_id: string | null;
}
export interface Definition {
  id: string;
  version: string;
  label: string;
  definition: Record<string, Json>;
  schema_digest: string;
}
export interface Discovery {
  subject: string;
  definitions: Definition[];
  counts: { name: string; count: number }[];
  capabilities: string[];
  capability_status: {
    name: string;
    installed: boolean;
    required: boolean;
    version: string | null;
  }[];
  next_cursor: string | null;
}
export interface Settings {
  profile: string;
  values: {
    providers: Record<
      string,
      {
        provider: string;
        model?: string | null;
        enabled?: boolean;
        [key: string]: Json | undefined;
      }
    >;
    [key: string]: Json | Record<string, { [key: string]: Json | undefined }>;
  };
  origins?: Record<string, Json>;
}
export interface Geometry {
  domain: "pixels" | "points" | "normalized";
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}
interface LocatorBase {
  artifact_ref: string;
  native_ref: string;
}
export type Locator = LocatorBase &
  (
    | { type: "text"; char_start: number; char_end: number }
    | {
        type: "document";
        char_start: number;
        char_end: number;
        char_domain?: "unit";
        physical_page: number | null;
        printed_page: string | null;
        geometry: Geometry | null;
      }
    | {
        type: "code";
        char_start: number;
        char_end: number;
        relative_path: string;
        byte_start: number;
        byte_end: number;
        line_start: number;
        line_end: number;
      }
    | {
        type: "image";
        physical_page: number | null;
        printed_page: string | null;
        geometry: Geometry | null;
      }
  );
export interface Citation {
  unit_id: string | null;
  asset_id: string | null;
  source_id: string | null;
  revision_id: string | null;
  source_ref: string | null;
  physical_page: number | null;
  printed_page: string | null;
  locators: Locator[];
}
export interface Excerpt {
  unit_id: string;
  char_start: number;
  char_end: number;
  text: string;
}
export interface EvidenceUnit {
  unit_id: string;
  extraction_id: string;
  parent_id: string | null;
  ordinal: number;
  unit_type: string;
  native_ref: string;
  boundary_key: string;
  heading_path: string[];
  text: string;
  text_sha: string;
  text_origin: "source" | "ocr" | "derived";
  citable: boolean;
  review_state: string;
  locators: Locator[];
  attributes: Record<string, Json>;
}
export interface StructuredRecord {
  record_id: string;
  unit_id: string;
  record_type: string;
  schema_id: string;
  value: Record<string, Json>;
  dependencies: string[];
}
export interface Segment {
  segment_id: string;
  source_id: string;
  revision_id: string;
  extraction_id: string;
  kind: string;
  role: string;
  lane: string;
  text: string;
  excerpts: Excerpt[];
  context_excerpts: Excerpt[];
  matched_unit_ids: string[];
  context_only_unit_ids: string[];
  anchors: {
    unit_id: string;
    origin: string;
    indexed_window_id: string | null;
    char_start: number;
    char_end: number;
  }[];
  signals: string[];
  citations: Citation[];
  complete: boolean;
  truncated: boolean;
  omissions: { reason: string; unit_ids: string[]; count: number }[];
  structured_records: StructuredRecord[];
}
export interface RelationPath {
  unit_ids: string[];
  assertion_ids: string[];
  predicates: string[];
  support_unit_ids: string[];
  provenance: Record<string, Json>;
}
export interface RelationResult {
  paths: RelationPath[];
  truncated: boolean;
  next_cursor: string | null;
}
export interface VisualEvidence {
  asset_id: string;
  source_id: string;
  revision_id: string;
  asset_ref: string;
  locator: Locator;
  caption: string | null;
  proxies: {
    proxy_id: string;
    label: string;
    text: string;
    origin: string;
    dependencies: string[];
    review_state: string;
  }[];
  selection_reasons: string[];
  citations: Citation[];
}
export interface QueryResult {
  schema_version: string;
  run_id: string;
  snapshot_id: string;
  applied_scope: Scope;
  segments: Segment[];
  citations: Citation[];
  visuals: VisualEvidence[];
  relations: RelationPath[];
  coverage: {
    status: "complete" | "partial" | "unavailable";
    completed_channels: string[];
    unavailable_channels: string[];
    warnings: string[];
  };
  coverage_gaps: { code: string; message: string; channel: string | null }[];
  decision_requirements: {
    requirement_id: string;
    reason: string;
    unit_ids: string[];
    assertion_ids: string[];
  }[];
  checks: {
    status: string;
    predicates: string[];
    assertion_ids: string[];
    assertions: RelationPath[];
    decisions: {
      requirement_id: string;
      reason: string;
      unit_ids: string[];
      assertion_ids: string[];
    }[];
    truncated: boolean;
  };
  warnings: string[];
  budget_contract: {
    max_tokens: number;
    tokenizer_id: string;
    max_response_bytes: number;
    render_targets: {
      renderer: string;
      profile_digest: string;
      schema_digest: string;
      serialization: string;
    }[];
  };
  trace: Record<string, Json> | null;
}
export interface Inspection {
  subject: string;
  id: string;
  evidence: EvidenceUnit | null;
  partial_evidence: {
    unit_id: string;
    extraction_id: string;
    unit_type: string;
    canonical_text_sha: string | null;
    canonical_text_length: number | null;
  } | null;
  excerpts: Excerpt[];
  records: {
    id: string;
    record_type: string;
    schema_id: string;
    value: Record<string, Json>;
  }[];
  original_ref: string | null;
  payload: string | null;
  media_type: string | null;
  truncated: boolean;
}
export type Filter =
  | { op: "eq"; field: string; value: string | number | boolean | null }
  | { op: "in"; field: string; values: (string | number | boolean | null)[] }
  | {
      op: "range";
      field: string;
      lower?: string | number | null;
      upper?: string | number | null;
      include_lower?: boolean;
      include_upper?: boolean;
    }
  | { op: "and" | "or"; children: Filter[] };
export interface QueryRequest {
  input:
    | { type: "question" | "passage"; text: string }
    | { type: "image"; asset_ref: string };
  profile?: string | null;
  mode?: string | null;
  filters?: Filter | null;
  hints?: string[];
  context?: Record<string, Json>;
  revision_selector?: "current" | "all";
  revision_ids?: string[];
  as_of?: string | null;
  cutoff?: string | null;
  unknown_dates?: "strict" | "permissive";
  include_global?: boolean;
  modalities?: string[];
  previous_unit_ids?: string[];
  exact_identifiers?: string[];
  exact_only?: boolean;
  limit?: number;
  trace?: boolean;
  render_targets?: RenderTarget[];
  budgets?: {
    max_tokens?: number;
    tokenizer_id?: string;
    max_response_bytes?: number;
    render_targets?: RenderTarget[];
  };
}
export interface RenderTarget {
  renderer: string;
  profile_digest: string;
  schema_digest: string;
  serialization: string;
}

export interface PdfCoverage {
  page_count: number;
  native_pages: number[];
  ocr_pages: number[];
  blank_pages: number[];
  no_text_detected_pages: number[];
}

export interface CollectionSummary {
  collection_id: string;
  workspace_id: string;
  slug: string;
  title: string;
  archived_at: string | null;
  default_kind_id: string | null;
  effective_default_kind_id: string | null;
  access_floor: number;
  policy_epoch: number;
  document_count: number | null;
  summary_complete: boolean;
}
export interface CollectionPage {
  collections: CollectionSummary[];
  next_cursor: string | null;
  policy_epoch: number;
}
export interface CollectionInput {
  title: string;
  slug: string;
  default_kind_id?: string | null;
}
export interface CollectionConsentView {
  collection_id: string;
  collection_upload_providers: string[];
  owner_upload_providers: string[];
  caller_upload_providers: string[];
  enabled_upload_providers: string[];
  policy_epoch: number;
  managed_grant_refresh_available: boolean;
}
export interface DocumentSummary {
  source_id: string;
  source_key: string;
  title: string;
  collection_id: string;
  collection_title: string;
  source_type: string;
  revision_id: string | null;
  extraction_id: string | null;
  original_media_type: string | null;
  original_size_bytes: number | null;
  created_at: string | null;
  captured_at: string | null;
  processing_state: "unknown";
  published_content_state: "published" | "not_observed";
  channel_readiness: string[];
  channel_observations: { family_id: string; channel: string; state: string }[];
  channel_observations_complete: boolean;
  latest_job: null;
  job_observations_complete: boolean;
  unit_count: number | null;
  window_count: number | null;
  counts_complete: boolean;
  counts_unavailable_reason: "bounded_exact_authorization_unavailable";
  jobs_unavailable_reason: "bounded_subject_lineage_unavailable";
}
export interface DocumentPage {
  documents: DocumentSummary[];
  next_cursor: string | null;
  policy_epoch: number;
  candidate_record_limit: 5000;
  channel_record_limit: 400;
  job_record_limit: 100;
  job_metadata_byte_limit: 1048576;
}

export type SettingsFieldChange =
  | { path: string; action: "set"; value: Exclude<Json, null> }
  | { path: string; action: "inherit" };
export interface SettingsPatch {
  base_workspace_sha256: string;
  changes: SettingsFieldChange[];
}
export interface SettingsFieldView {
  active_profile: string;
  base_workspace_sha256: string;
  fields: {
    path: string;
    effective_value: Json;
    override_value: Json;
    overridden: boolean;
    origin: Record<string, Json>;
    value_schema: Record<string, Json>;
    choices: Json[];
  }[];
  blockers: string[];
  credential_readiness: "not_checked";
}
export interface SettingsPreview {
  base_workspace_sha256: string;
  candidate_workspace_sha256: string;
  changes: { path: string; before: Json; after: Json }[];
  activation_allowed: boolean;
  blockers: string[];
  rebuild_queued: false;
}
export interface ModelCatalog {
  providers: {
    slot: "text" | "image" | "rerank" | "vision";
    configured_provider: string;
    configured_model: string | null;
    qualified_models: string[];
    model_qualification:
      | "qualified"
      | "unsupported"
      | "explicit_identifier"
      | "disabled";
    package_available: boolean;
    credential_readiness: "not_checked";
    upload_allowed_collection_ids: string[];
    asset_state: "not_checked";
  }[];
  selections: string[];
  selection_capabilities: {
    selection: string;
    required_modules: string[];
    packages_available: boolean;
    asset_state: "not_checked";
  }[];
  manifest_sha256: string;
  offline: boolean;
  pdf_ocr_policy: "per_intake";
}
export interface ModelAssetPlan {
  manifest_sha256: string;
  selection: string[];
  assets: {
    id: string;
    selection: string;
    path: string;
    sha256: string;
    size_bytes: number;
    url: string;
    license: string;
    owner: string;
    revision: string;
    notice_url: string;
  }[];
  notices: {
    selection: string;
    attribution: string;
    license: string;
    notice_url: string;
    model_card_url: string;
  }[];
  total_size_bytes: number;
}
export type ModelAssetState =
  | "installed"
  | "missing"
  | "corrupt"
  | "not_checked";
export interface ModelReadiness {
  manifest_sha256: string;
  assets: {
    asset_id: string;
    selection: string;
    sha256: string;
    size_bytes: number;
    state: ModelAssetState;
    checked_bytes: number;
    verification: "current_full_hash" | "none";
    reason: string | null;
  }[];
  selections: {
    selection: string;
    state: ModelAssetState;
    complete: boolean;
  }[];
  total_size_bytes: number;
  checked_bytes: number;
  complete: boolean;
  receipts: {
    job_id: string;
    manifest_sha256: string;
    verified_at: string;
    asset_ids: string[];
    verification: "durable_setup_result";
  }[];
  receipt_search_complete: boolean;
  receipt_checked_bytes: number;
}

export interface ExtractionUnitSummary {
  id: string;
  ordinal: number;
  unit_type: string;
  heading_path: string[];
  text_origin: string;
  review_state: string;
  citable: boolean;
  locators: Locator[];
  preview: string;
}
export interface ExtractionWindow {
  id: string;
  unit_id: string;
  extraction_id: string;
  char_start: number;
  char_end: number;
  text: string;
  token_count: number;
  chunker_fingerprint: string;
  context_group_key: string;
  locators: Locator[];
}
export interface ExtractionUnitsPage {
  source_id: string;
  revision_id: string;
  extraction_id: string;
  units: ExtractionUnitSummary[];
  next_cursor: string | null;
  scope_limited: boolean;
}
export interface ExtractionWindowsPage {
  source_id: string;
  revision_id: string;
  extraction_id: string;
  windows: ExtractionWindow[];
  next_cursor: string | null;
  scope_limited: boolean;
}
export interface KindMetadataForm {
  kind_id: string;
  schema_id: string;
  schema_digests: Record<string, Json>;
  fields: {
    name: string;
    required: boolean;
    value_types: string[];
    description: string;
    constraints: Record<string, Json> | boolean;
    advanced: boolean;
  }[];
  schemas: Record<string, Json>;
  schema_reference_bindings: Record<string, Json>;
  advanced: boolean;
}

export interface PdfIntakeDescriptor {
  source_key: string;
  collection_id: string;
  kind: string;
  metadata: Record<string, Json>;
  expected_size_bytes: number;
  expected_sha256?: string;
  format: "pdf";
}
export interface PdfProcessingPolicy {
  origin_proof?: "ordered-ocr-cells@1" | null;
  version: "pdf-processing@1";
  mode: "native" | "local_ocr";
  native_pages: number[];
  ocr_pages: number[];
  blank_pages: number[];
  backend_identity: Record<string, Json>;
  runtime_inventory: Record<string, Json>;
  model_inventory_sha256: string;
  options: Record<string, Json>;
  limits: Record<string, Json>;
}
export interface PdfAnalysis {
  original_sha256: string;
  size_bytes: number;
  page_count: number;
  native_text_pages: number[];
  image_only_pages: number[];
  blank_pages: number[];
  unknown_pages: number[];
  requires_ocr: boolean;
  blockers: string[];
  proposed_policy: PdfProcessingPolicy | null;
  analysis_sha256: string;
  policy_sha256?: string | null;
}
export interface PdfIntakeSubmission {
  analysis_sha256: string;
  policy_sha256: string;
}
export interface PdfIntakeStatus {
  intake_id: string;
  state:
    | "prepared"
    | "receiving"
    | "staged"
    | "analysing"
    | "analysed"
    | "submitted"
    | "aborted"
    | "expired"
    | "failed";
  byte_ceiling: number;
  received_bytes: number;
  expires_at: string;
  analysis: PdfAnalysis | null;
  job: import("./operations").JobHandle | null;
  error: { code: string; message: string; retryable: boolean } | null;
}
